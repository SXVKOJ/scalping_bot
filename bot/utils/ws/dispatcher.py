"""Развязка цикла чтения вебсокета и обработки сообщений.

Зачем: раньше `ws.receive()` и вся обработка сообщения (включая торговую
логику и запросы к бирже) жили в одном цикле. Пока обрабатывался тик,
сокет не читался: ответ на PING от MEXC задерживался, а входящие тики
копились в буфере TCP. На сквизах это означало, что бот принимал решение
по устаревшей цене.

Как решено: читатель делает только `receive()` + разбор протокола и
кладёт сообщение в очередь (неблокирующе). Воркер разбирает очередь
строго в порядке поступления (FIFO) и вызывает тот же обработчик, что и
раньше.

Важно: очередь именно FIFO без «схлопывания» устаревших сообщений.
Торговая логика построена на сравнении соседних тиков (пересечение
триггера, сброс при малейшем падении mid-цены), поэтому пропуск даже
одного тика изменил бы поведение бота. Здесь обрабатываются все тики,
в исходном порядке — меняется только то, что это происходит вне цикла
чтения.
"""

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from bot.logger import logger


class StreamDispatcher:
    """Очередь сообщений + один воркер, сохраняющий порядок обработки.

    Args:
        name: имя для логов, например "MarketWS" или "UserWS:12345".
        handler: корутина-обработчик одного сообщения.
        maxsize: размер очереди. Аварийный предел, а не рабочий режим:
            воркер выполняет только вычисления в памяти, поэтому в норме
            очередь около нуля.
        lag_warn_ms: порог задержки «попал в очередь → начали обрабатывать»,
            выше которого пишем предупреждение.
    """

    def __init__(
        self,
        name: str,
        handler: Callable[[Any], Awaitable[None]],
        maxsize: int = 5000,
        lag_warn_ms: float = 250.0,
        warn_interval_sec: float = 10.0,
    ) -> None:
        self.name = name
        self._handler = handler
        self._maxsize = maxsize
        self._lag_warn_sec = lag_warn_ms / 1000.0
        self._warn_interval_sec = warn_interval_sec

        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None

        # Метрики
        self.enqueued = 0
        self.processed = 0
        self.dropped = 0
        self.handler_errors = 0
        self.max_depth_seen = 0
        self.max_lag_sec = 0.0
        self.last_lag_sec = 0.0
        self._last_lag_warn_at = 0.0
        self._last_full_warn_at = 0.0

    # ---------- жизненный цикл ----------

    def start(self) -> None:
        """Создаёт очередь и запускает воркер. Повторный вызов безопасен."""
        if self._worker is not None and not self._worker.done():
            return
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._queue = queue
        # Очередь передаём воркеру явным аргументом: если stop() успеет
        # обнулить self._queue до первого запуска воркера, тот всё равно
        # должен работать со своей очередью, а не выйти сразу.
        self._worker = asyncio.create_task(self._run(queue))
        logger.info(f"[{self.name}] Dispatcher started (maxsize={self._maxsize})")

    async def stop(self, drain: bool = False, timeout: float = 2.0) -> None:
        """Останавливает воркер и дожидается его завершения.

        drain=True — сначала пытаемся доработать очередь (нужно для событий
        по ордерам: их нельзя терять). drain=False — обрываем сразу
        (рыночные тики после обрыва соединения уже неактуальны).

        Устойчив к отмене: воркер снимается в любом случае, иначе он остался
        бы висеть после закрытия соединения.
        """
        worker = self._worker
        queue = self._queue
        self._worker = None
        self._queue = None

        if worker is None:
            return

        try:
            if drain and queue is not None and not queue.empty():
                try:
                    await asyncio.wait_for(asyncio.shield(queue.join()), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[{self.name}] Dispatcher drain timed out, "
                        f"{queue.qsize()} сообщений не обработано"
                    )
                except asyncio.CancelledError:
                    # Нас отменили во время дообработки — не мешаем снятию воркера ниже.
                    logger.warning(f"[{self.name}] Dispatcher drain cancelled")
                except Exception as e:
                    logger.error(f"[{self.name}] Dispatcher drain error: {e}")
        finally:
            if not worker.done():
                worker.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                pass

            logger.info(
                f"[{self.name}] Dispatcher stopped "
                f"(обработано={self.processed}, отброшено={self.dropped}, "
                f"макс. глубина={self.max_depth_seen}, "
                f"макс. задержка={self.max_lag_sec * 1000:.0f} мс)"
            )

    def shutdown(self, drain: bool = False, timeout: float = 5.0) -> None:
        """Неблокирующая остановка — безопасно вызывать из `finally`.

        В `finally` уже отменённой задачи любой `await` может тут же снова
        получить CancelledError и оставить воркер висящим. Поэтому остановку
        выполняет отдельная задача, не связанная с отменой читателя.
        """
        if self._worker is None:
            return
        try:
            asyncio.get_running_loop().create_task(self.stop(drain=drain, timeout=timeout))
        except RuntimeError:
            # Нет живого loop — снимаем воркер напрямую, без дообработки.
            worker, self._worker, self._queue = self._worker, None, None
            if worker is not None and not worker.done():
                worker.cancel()

    # ---------- сторона читателя (неблокирующая) ----------

    def submit(self, payload: Any) -> bool:
        """Кладёт сообщение в очередь. Никогда не блокирует читателя.

        Returns:
            True, если сообщение принято в обработку.
        """
        queue = self._queue
        if queue is None:
            return False

        item = (time.perf_counter(), payload)
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            # Аварийный путь: воркер критически не успевает. Освобождаем
            # место, выбрасывая самое старое сообщение, — свежая цена для
            # торговли важнее устаревшей. Громко логируем: при исправной
            # работе этого не бывает.
            self._on_queue_full()
            try:
                queue.get_nowait()
                queue.task_done()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                self.dropped += 1
                return False

        self.enqueued += 1
        depth = queue.qsize()
        if depth > self.max_depth_seen:
            self.max_depth_seen = depth
        return True

    def _on_queue_full(self) -> None:
        now = time.time()
        if now - self._last_full_warn_at < self._warn_interval_sec:
            return
        self._last_full_warn_at = now
        logger.critical(
            f"[{self.name}] Очередь сообщений переполнена ({self._maxsize}). "
            f"Обработчик не успевает, старые сообщения отбрасываются "
            f"(всего отброшено={self.dropped}). Возможен разрыв в анализе тренда."
        )

    # ---------- сторона воркера ----------

    async def _run(self, queue: asyncio.Queue) -> None:
        if queue is None:
            return
        try:
            while True:
                enqueued_at, payload = await queue.get()
                try:
                    lag = time.perf_counter() - enqueued_at
                    self.last_lag_sec = lag
                    if lag > self.max_lag_sec:
                        self.max_lag_sec = lag
                    if lag > self._lag_warn_sec:
                        self._warn_lag(lag, queue.qsize())

                    await self._handler(payload)
                    self.processed += 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # Одно плохое сообщение не должно убивать воркер.
                    self.handler_errors += 1
                    logger.error(
                        f"[{self.name}] Ошибка обработки сообщения из очереди: {e}",
                        exc_info=True,
                    )
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.name}] Dispatcher worker упал: {e}", exc_info=True)

    def _warn_lag(self, lag: float, depth: int) -> None:
        now = time.time()
        if now - self._last_lag_warn_at < self._warn_interval_sec:
            return
        self._last_lag_warn_at = now
        logger.warning(
            f"[{self.name}] Задержка обработки {lag * 1000:.0f} мс "
            f"(в очереди {depth}). Обработчик не успевает за потоком тиков."
        )

    # ---------- наблюдаемость ----------

    def stats(self) -> Dict[str, Any]:
        queue = self._queue
        return {
            "name": self.name,
            "running": self._worker is not None and not self._worker.done(),
            "depth": queue.qsize() if queue is not None else 0,
            "max_depth_seen": self.max_depth_seen,
            "enqueued": self.enqueued,
            "processed": self.processed,
            "dropped": self.dropped,
            "handler_errors": self.handler_errors,
            "last_lag_ms": round(self.last_lag_sec * 1000, 1),
            "max_lag_ms": round(self.max_lag_sec * 1000, 1),
        }
