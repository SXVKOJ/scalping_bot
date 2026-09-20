import asyncio
from aiogram.types import Message
from asgiref.sync import sync_to_async
from django.utils import timezone
from users.models import Deal, User
from subscriptions.models import Subscription
from bot.utils.user_autobuy_tasks import user_autobuy_tasks
from bot.utils.mexc import handle_mexc_response
from bot.utils.orders import execute_entry, place_take_profit
from bot.utils.api_errors import parse_mexc_error
from bot.utils.mexc_rest import MexcRestClient, to_mexc_symbol, split_pair
from bot.logger import logger
from bot.utils.error_notifier import notify_user_autobuy_error
from decimal import Decimal
from bot.constants import (
    MAX_FAILS,
    RISE_TREND_TOLERANCE_PCT,
    RISE_MIN_PCT,
    RISE_MAX_BUYS_PER_CYCLE,
    RISE_BUY_COOLDOWN_SEC,
    DROP_BUY_COOLDOWN_SEC,
    BUY_RETRY_DELAY_SEC,
)
import json
import time
import weakref

# Словарь для хранения состояния autobuy для каждого пользователя
autobuy_states = {}  # {user_id: {'last_buy_price': float, 'active_orders': [], etc.}}

# Глобальные переменные для отслеживания триггеров
trigger_states = {}  # {user_id: {'trigger_price': float, 'trigger_time': float, 'is_rise_trigger': bool}}


def get_rest_client(telegram_id: int, user: User) -> MexcRestClient:
    """Возвращает переиспользуемый REST-клиент с keep-alive сессией.

    Клиент создаётся один раз на пользователя и хранится в autobuy_states,
    чтобы каждый ордер не открывал новое TCP/TLS-соединение к MEXC.
    Пересоздаётся только если сменился API-ключ.
    """
    state = autobuy_states.get(telegram_id)
    if state is None:
        return MexcRestClient(api_key=user.api_key, api_secret=user.api_secret)
    client = state.get("rest_client")
    if client is None or getattr(client, "api_key", None) != user.api_key:
        client = MexcRestClient(api_key=user.api_key, api_secret=user.api_secret)
        state["rest_client"] = client
    return client


def _num(value, default: float = 0.0) -> float:
    """Безопасно приводит настройку к float (поля в БД nullable)."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default: int = 0) -> int:
    """Безопасно приводит настройку к int (pause в БД может быть NULL)."""
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def arm_rise_trigger(state: dict, ask_price: float, current_time: float) -> None:
    """Ставит (или переставляет) триггер роста на указанную ask-цену.

    В отличие от reset_rise_trigger триггер остаётся ВКЛЮЧЁННЫМ: после неудачного
    окна анализа бот продолжает ловить рост, а не замолкает до следующей сделки.
    """
    state["trigger_price"] = float(ask_price)
    state["trigger_time"] = current_time
    state["is_rise_trigger"] = True
    state["is_trigger_activated"] = False
    state["trigger_activated_time"] = 0
    state["pause_trend_prices"] = []
    state["pause_peak_mid"] = None
    state["pause_start_mid"] = None
    state["trend_only_rise"] = True
    state["last_pause_price"] = None


def _set_autobuy_inactive(telegram_id: int) -> None:
    """Гасит флаг автобая в памяти одновременно с записью в БД.

    Обработчик тиков и process_buy сверяются с кешем, а не с БД, поэтому
    флаг нужно сбрасывать сразу, иначе между остановкой и синхронизацией
    кеша мог уйти лишний ордер.
    """
    state = autobuy_states.get(telegram_id)
    if state is not None:
        state["autobuy_active"] = False


def schedule_buy_retry(telegram_id: int, why: str) -> None:
    """Возвращает пользователя в режим ожидания новой возможности.

    process_buy в начале сбрасывает waiting_for_opportunity/restart_after. Если
    покупка падала с ошибкой, эти флаги так и оставались сброшенными, а
    last_buy_price — None: основной цикл больше никогда не заходил в ветку
    повторной покупки, и бот молча простаивал при живом автобае.
    """
    state = autobuy_states.get(telegram_id)
    if not state:
        return
    if state.get("active_orders"):
        # Есть открытые сделки — цикл продолжится по их закрытию.
        return
    state["waiting_for_opportunity"] = True
    state["restart_after"] = time.time() + BUY_RETRY_DELAY_SEC
    state["waiting_reported"] = False
    logger.info(
        f"Покупка для {telegram_id} не состоялась ({why}); "
        f"повтор через {BUY_RETRY_DELAY_SEC:.0f}с"
    )


async def _safe_send(telegram_id: int, text: str):
    """Отправка уведомления, не влияющая на скорость покупки (fire-and-forget)."""
    try:
        from bot.config import bot_instance

        await bot_instance.send_message(telegram_id, text)
    except Exception as e:
        logger.error(f"Failed to send notification to {telegram_id}: {e}")


async def autobuy_loop(message: Message, telegram_id: int):
    startup_fail_count = 0

    # Используем lock для предотвращения одновременных закупок
    buy_lock = asyncio.Lock()

    # Глобальная сессия для всех запросов пользователя
    session = None

    while startup_fail_count < MAX_FAILS:
        try:
            # Импортируем websocket_manager внутри функции
            from bot.utils.websocket_manager import websocket_manager

            user = await sync_to_async(User.objects.get)(telegram_id=telegram_id)
            rest = MexcRestClient(api_key=user.api_key, api_secret=user.api_secret)
            symbol = to_mexc_symbol(user.pair)
            base_asset, quote_asset = split_pair(user.pair)

            # Инициализируем состояние для пользователя, если его еще нет
            if telegram_id not in autobuy_states:
                autobuy_states[telegram_id] = {
                    "active_orders": [],
                    "last_buy_price": None,
                    "current_price": None,
                    "price_callbacks": [],
                    "bookticker_callbacks": [],
                    "last_trade_time": 0,
                    "is_ready": False,
                    "waiting_for_opportunity": False,  # Флаг ожидания новой возможности
                    "restart_after": 0,  # Временная метка для возобновления покупок
                    "waiting_reported": False,  # Флаг для отслеживания, сообщили ли мы о том, что ожидаем
                    "consecutive_errors": 0,  # Счетчик последовательных ошибок
                    "last_drop_notification": 0,  # Время последнего уведомления о падении
                    "last_rise_notification": 0,  # Время последнего уведомления о росте
                    "last_buy_success_time": 0,  # Время последней успешной покупки
                    "last_order_filled_time": 0,  # Время последнего завершения сделки
                    "trigger_price": None,  # Цена триггера для покупок на росте
                    "trigger_time": 0,  # Время установки триггера
                    "trigger_activated_time": 0,  # Время активации триггера (когда цена пересекла триггер)
                    "is_rise_trigger": False,  # Флаг триггера на росте
                    "is_trigger_activated": False,  # Флаг активации триггера
                    "pause_trend_prices": [],  # Список цен во время паузы для анализа тренда
                    "trend_only_rise": True,  # Флаг исключительного роста во время паузы
                    "last_pause_price": None,  # Последняя цена во время паузы
                    "rise_buy_count": 0,  # Счетчик покупок на росте в текущем цикле
                    "last_ask_price": None,  # Последняя ask цена для анализа триггеров
                    "last_mid_price": None,  # Последняя mid цена для анализа тренда
                    "buy_in_progress": False,  # Глобальный флаг покупки
                    "buy_lock": asyncio.Lock(),  # Глобальная блокировка покупки на пользователя
                    "cached_loss": None,  # Кеш настройки "падение" (%), обновляется в фоне
                    "cached_profit": None,  # Кеш настройки "профит" (%)
                    "cached_pause": 0,  # Кеш настройки "пауза" (сек)
                    "autobuy_active": True,  # Кеш флага автобая (без запроса в БД на тике)
                    "cached_user": None,  # Кеш объекта пользователя (без БД на ордере)
                    "rest_client": None,  # Переиспользуемый REST-клиент (keep-alive)
                    "resource_task": None,  # Задача periodic_resource_check
                    "pause_peak_mid": None,  # Максимум mid внутри окна анализа роста
                    "pause_start_mid": None,  # mid на старте окна анализа роста
                    "last_rise_buy_time": 0,  # Время последней покупки на росте
                }

            # Кешируем настройки в память, чтобы hot-path (обработчик тиков)
            # не ходил в БД на каждом обновлении цены. Кеш обновляется в
            # основном цикле (10с) и в periodic_resource_check (60с).
            autobuy_states[telegram_id]["cached_loss"] = _num(user.loss)
            autobuy_states[telegram_id]["cached_profit"] = _num(user.profit)
            autobuy_states[telegram_id]["cached_pause"] = _int(user.pause)
            autobuy_states[telegram_id]["autobuy_active"] = True
            autobuy_states[telegram_id]["cached_user"] = user
            autobuy_states[telegram_id]["rest_client"] = rest

            # Восстанавливаем активные ордера из БД
            deals_qs = Deal.objects.filter(
                user=user, status__in=["NEW", "PARTIALLY_FILLED"], is_autobuy=True
            ).order_by("-created_at")

            active_deals = await sync_to_async(list)(deals_qs)

            # Заполняем активные ордера
            active_orders = []
            for deal in active_deals:
                active_orders.append(
                    {
                        "order_id": deal.order_id,
                        "buy_price": float(deal.buy_price),
                        "notified": False,
                        "user_order_number": deal.user_order_number,
                    }
                )

            autobuy_states[telegram_id]["active_orders"] = active_orders

            # Если есть активные ордера, устанавливаем last_buy_price на основе последнего
            if active_orders:
                most_recent_order = max(
                    active_orders, key=lambda x: x.get("user_order_number", 0)
                )
                autobuy_states[telegram_id]["last_buy_price"] = most_recent_order[
                    "buy_price"
                ]
                logger.info(
                    f"Установлена цена последней покупки: {most_recent_order['buy_price']} для пользователя {telegram_id}"
                )

            # Проверяем, есть ли соединение с WebSocket для рыночных данных
            if not websocket_manager.market_connection:
                await websocket_manager.connect_market_data()
                logger.info(f"Установлено соединение с WebSocket для рыночных данных")

            # Подписываемся на bookTicker данные (включает и цены, и bid/ask)
            if symbol not in websocket_manager.bookticker_subscriptions:
                await websocket_manager.subscribe_bookticker_data([symbol])
                logger.info(f"Подписались на bookTicker данные для {symbol}")

            # Регистрируем колбэк для bookTicker данных (заменяет старый колбэк для цен)
            async def update_bookticker_for_autobuy(
                symbol_name, bid_price, ask_price, bid_qty, ask_qty
            ):
                try:
                    # Быстрая проверка по кешу в памяти — без запроса в БД на каждый тик
                    state = autobuy_states.get(telegram_id)
                    if not state or not state.get("autobuy_active"):
                        return

                    # Получаем информацию о направлении цены
                    direction_info = websocket_manager.get_price_direction(symbol_name)
                    is_rise = direction_info.get("is_rise", False)
                    current_time = time.time()
                    mid_price = (float(bid_price) + float(ask_price)) / 2

                    # Настройки берём из кеша в памяти (обновляются в фоновых циклах),
                    # чтобы не ходить в БД на каждом тике bookTicker
                    loss_threshold = state.get("cached_loss")
                    pause_seconds = _int(state.get("cached_pause"), 0)
                    if loss_threshold is None:
                        return
                    loss_threshold = _num(loss_threshold)
                    if loss_threshold <= 0:
                        # Падение не настроено — покупки на падении отключены,
                        # но триггеры роста должны продолжать работать.
                        loss_threshold = None

                    # Обновляем текущую цену
                    autobuy_states[telegram_id]["current_price"] = mid_price

                    # Проверяем триггеры для покупок на росте
                    await check_rise_triggers(
                        telegram_id,
                        symbol_name,
                        float(bid_price),
                        float(ask_price),
                        is_rise,
                        current_time,
                        pause_seconds,
                    )

                    # Проверяем условия для покупок на падении (используем ask цену)
                    last_buy_price = autobuy_states[telegram_id]["last_buy_price"]
                    if last_buy_price is not None and loss_threshold is not None:
                        ask_price = float(ask_price)
                        price_drop_percent = (
                            ((last_buy_price - ask_price) / last_buy_price * 100)
                            if last_buy_price > 0
                            else 0
                        )
                        last_drop_notification = autobuy_states[telegram_id].get(
                            "last_drop_notification", 0
                        )

                        if (
                            price_drop_percent >= loss_threshold
                            and (current_time - last_drop_notification)
                            > DROP_BUY_COOLDOWN_SEC
                        ):
                            # Защита от параллельных покупок — проверяем ДО запуска.
                            # Антидребезг взводим только если покупка реально
                            # стартует, иначе один пропуск глушил сигнал на 10с.
                            if state.get("buy_in_progress"):
                                logger.info(
                                    f"Skip price_drop buy: buy_in_progress for {telegram_id}"
                                )
                                return

                            autobuy_states[telegram_id]["last_drop_notification"] = (
                                current_time
                            )

                            # СНАЧАЛА запускаем покупку (критично по времени).
                            # Уведомление в Telegram отправляем отдельной задачей,
                            # чтобы задержка API Telegram не тормозила ордер.
                            trigger_ts = time.perf_counter()
                            from bot.utils.autobuy_restart import FakeMessage
                            from bot.config import bot_instance

                            fake_message = FakeMessage(telegram_id, bot_instance)
                            logger.info(
                                f"Price drop condition met for {telegram_id}: ask={ask_price:.6f}, "
                                f"last_buy={last_buy_price:.6f}, drop={price_drop_percent:.2f}% "
                                f">= {loss_threshold:.2f}%. Starting process_buy."
                            )
                            asyncio.create_task(
                                process_buy(
                                    telegram_id,
                                    "price_drop",
                                    fake_message,
                                    None,
                                    trigger_ts,
                                )
                            )

                            # Уведомление о падении — не блокирует ордер
                            drop_text = (
                                f"🔻 Обнаружено падение цены для {symbol_name}\n\n"
                                f"🔻 Цена ({ask_price:.6f} {quote_asset}) снизилась на {price_drop_percent:.2f}% "
                                f"от покупки по {last_buy_price:.6f} {quote_asset}. \n"
                                f"Покупаем по условию падения ({loss_threshold:.2f}%)."
                            )
                            asyncio.create_task(_safe_send(telegram_id, drop_text))

                except Exception as e:
                    logger.error(
                        f"Ошибка в обработчике bookTicker autobuy для {telegram_id} ({symbol_name}): {e}",
                        exc_info=True,
                    )

            # Снимаем колбэки предыдущей итерации цикла: без этого после
            # каждого перезапуска autobuy_loop на один тик приходило несколько
            # обработчиков, что давало дублирующие покупки.
            for stale_callback in list(
                autobuy_states[telegram_id]["bookticker_callbacks"]
            ):
                try:
                    await websocket_manager.unregister_bookticker_callback(
                        symbol, stale_callback
                    )
                except Exception as e:
                    logger.error(
                        f"Не удалось снять старый bookTicker колбэк для {telegram_id}: {e}"
                    )
            autobuy_states[telegram_id]["bookticker_callbacks"] = []

            # Регистрируем колбэк с WebSocket менеджером
            await websocket_manager.register_bookticker_callback(
                symbol, update_bookticker_for_autobuy
            )
            autobuy_states[telegram_id]["bookticker_callbacks"].append(
                update_bookticker_for_autobuy
            )
            logger.info(f"Registered bookTicker callback for {telegram_id} on {symbol}")

            # Получаем текущую цену через REST API для начала
            ticker_data = await rest.ticker_price(symbol)
            handle_mexc_response(ticker_data, "Получение цены")
            current_price = float(ticker_data["price"])
            autobuy_states[telegram_id]["current_price"] = current_price
            logger.info(f"Получена начальная цена для {telegram_id}: {current_price}")

            # Отмечаем, что система готова обрабатывать обновления
            autobuy_states[telegram_id]["is_ready"] = True

            # Если нет активных ордеров и есть начальная цена, делаем первую покупку
            if not active_orders and current_price > 0:
                logger.info(
                    f"Запускаем первую покупку для {telegram_id} по цене {current_price}"
                )
                await process_buy(telegram_id, "initial_purchase", message, user)

            # Планируем задачу проверки ресурсов (одну на пользователя)
            resource_task = autobuy_states[telegram_id].get("resource_task")
            if resource_task is None or resource_task.done():
                autobuy_states[telegram_id]["resource_task"] = asyncio.create_task(
                    periodic_resource_check(telegram_id)
                )

            # Сообщаем пользователю, что автобай активирован
            await message.answer(
                f"✅ *Автобай активирован*\n\n"
                f"📊 Текущая цена: `{current_price:.6f}` {quote_asset}\n"
                f"💰 Сумма закупки: `{user.buy_amount}` {quote_asset}\n"
                f"📈 Профит: `{user.profit}%`\n"
                f"📉 Падение: `{user.loss}%`\n"
                f"⏱️ Пауза: `{user.pause}` сек\n",
                parse_mode="Markdown",
            )

            # Ждем завершения автобая или отмены задачи
            while True:
                # Обновляем кеш настроек из БД (~раз в 10с), чтобы hot-path
                # (обработчик тиков) не делал запросов в БД на каждом тике.
                try:
                    fresh = await sync_to_async(User.objects.get)(
                        telegram_id=telegram_id
                    )
                    st = autobuy_states.get(telegram_id)
                    if st is not None:
                        st["cached_loss"] = _num(fresh.loss)
                        st["cached_profit"] = _num(fresh.profit)
                        st["cached_pause"] = _int(fresh.pause)
                        st["autobuy_active"] = bool(fresh.autobuy)
                        st["cached_user"] = fresh
                except Exception as e:
                    logger.error(
                        f"Не удалось обновить кеш настроек для {telegram_id}: {e}"
                    )

                # Проверка подписки
                subscription = await sync_to_async(
                    Subscription.objects.filter(user=user).order_by("-expires_at").first
                )()
                if not subscription or subscription.expires_at < timezone.now():
                    user.autobuy = False
                    _set_autobuy_inactive(telegram_id)
                    await sync_to_async(user.save)(update_fields=["autobuy"])
                    task = user_autobuy_tasks.get(telegram_id)
                    if task:
                        task.cancel()
                        del user_autobuy_tasks[telegram_id]
                    await message.answer(
                        "⛔ Ваша подписка закончилась. Автобай остановлен."
                    )
                    break

                # Проверяем, не нужно ли начать новую покупку после периода ожидания
                current_time = time.time()
                restart_after = autobuy_states[telegram_id].get("restart_after", 0)
                waiting_for_opportunity = autobuy_states[telegram_id].get(
                    "waiting_for_opportunity", False
                )

                if (
                    waiting_for_opportunity
                    and restart_after > 0
                    and current_time >= restart_after
                ):
                    # Время ожидания истекло, запускаем новую покупку
                    autobuy_states[telegram_id]["restart_after"] = 0
                    autobuy_states[telegram_id]["waiting_for_opportunity"] = False
                    autobuy_states[telegram_id]["waiting_reported"] = False
                    logger.info(
                        f"Период ожидания после закрытия сделки истек для {telegram_id} (проверка в основном цикле)"
                    )

                    # Запускаем новую покупку, если нет активных ордеров
                    if not autobuy_states[telegram_id]["active_orders"]:
                        # Дополнительная проверка в БД на активные сделки autobuy
                        has_active = await sync_to_async(
                            Deal.objects.filter(
                                user=user,
                                is_autobuy=True,
                                status__in=["NEW", "PARTIALLY_FILLED"],
                            ).exists
                        )()
                        if has_active:
                            logger.info(
                                f"DB guard: активные сделки обнаружены для {telegram_id}, покупка не запускается"
                            )
                        else:
                            # await message.answer(f"🔄 Возобновляем автобай после паузы (основной цикл). Текущая цена: {autobuy_states[telegram_id]['current_price']}")
                            await process_buy(
                                telegram_id,
                                "after_waiting_period_main_loop",
                                message,
                                user,
                            )

                # Просто ждем, реальная работа происходит в колбэках
                await asyncio.sleep(
                    10
                )  # Проверка подписки и состояния каждые 10 секунд

            break  # Выход из внешнего цикла

        except asyncio.CancelledError:
            logger.info(f"Задача автобая для {telegram_id} была отменена")
            # Очищаем ресурсы
            if telegram_id in autobuy_states:
                # Импортируем websocket_manager внутри блока
                from bot.utils.websocket_manager import websocket_manager

                # Clean up price callbacks
                for callback in autobuy_states[telegram_id]["price_callbacks"]:
                    try:
                        user = await sync_to_async(User.objects.get)(
                            telegram_id=telegram_id
                        )
                        symbol_to_unregister = to_mexc_symbol(user.pair)
                        if symbol_to_unregister in websocket_manager.price_callbacks:
                            if (
                                callback
                                in websocket_manager.price_callbacks[
                                    symbol_to_unregister
                                ]
                            ):
                                websocket_manager.price_callbacks[
                                    symbol_to_unregister
                                ].remove(callback)
                    except Exception as e:
                        logger.error(
                            f"Ошибка при очистке price колбэков для {telegram_id}: {e}"
                        )

                # Clean up bookTicker callbacks
                for callback in autobuy_states[telegram_id]["bookticker_callbacks"]:
                    try:
                        user = await sync_to_async(User.objects.get)(
                            telegram_id=telegram_id
                        )
                        symbol_to_unregister = to_mexc_symbol(user.pair)
                        await websocket_manager.unregister_bookticker_callback(
                            symbol_to_unregister, callback
                        )
                    except Exception as e:
                        logger.error(
                            f"Ошибка при очистке bookTicker колбэков для {telegram_id}: {e}"
                        )

            # Закрываем переиспользуемую REST-сессию (keep-alive)
            rest_client = autobuy_states.get(telegram_id, {}).get("rest_client")
            if rest_client:
                try:
                    await rest_client.close()
                except Exception as e:
                    logger.error(f"Ошибка при закрытии REST-сессии: {e}")

            # Если есть сессия, закрываем её
            if session:
                try:
                    await session.close()
                except Exception as e:
                    logger.error(f"Ошибка при закрытии сессии: {e}")

            raise

        except Exception as e:
            logger.error(
                f"Ошибка в autobuy_loop для {telegram_id}, пауза автобая 30 секунд: {e}"
            )
            startup_fail_count += 1
            if startup_fail_count >= MAX_FAILS:
                error_message = parse_mexc_error(e)
                await message.answer(f"⛔ {error_message}\n\n  Автобай остановлен.")
                user.autobuy = False
                _set_autobuy_inactive(telegram_id)
                await sync_to_async(user.save)(update_fields=["autobuy"])
                task = user_autobuy_tasks.get(telegram_id)
                if task:
                    task.cancel()
                    del user_autobuy_tasks[telegram_id]

                # Send additional notification about autobuy stop
                try:
                    from bot.config import bot_instance

                    await bot_instance.send_message(
                        telegram_id,
                        f"⛔ Автобай остановлен после {MAX_FAILS} последовательных ошибок.\n"
                        f"Проверьте настройки и баланс.",
                    )
                except Exception as notify_error:
                    logger.error(
                        f"Failed to send autobuy stop notification to {telegram_id}: {notify_error}"
                    )

                # Удаляем колбэки и состояние
                if telegram_id in autobuy_states:
                    # Импортируем websocket_manager внутри блока
                    from bot.utils.websocket_manager import websocket_manager

                    # Clean up price callbacks
                    for callback in autobuy_states[telegram_id]["price_callbacks"]:
                        try:
                            symbol_to_unregister = to_mexc_symbol(user.pair)
                            if (
                                symbol_to_unregister
                                in websocket_manager.price_callbacks
                            ):
                                if (
                                    callback
                                    in websocket_manager.price_callbacks[
                                        symbol_to_unregister
                                    ]
                                ):
                                    websocket_manager.price_callbacks[
                                        symbol_to_unregister
                                    ].remove(callback)
                        except Exception as cleanup_error:
                            logger.error(
                                f"Ошибка при очистке price колбэков: {cleanup_error}"
                            )

                    # Clean up bookTicker callbacks
                    for callback in autobuy_states[telegram_id]["bookticker_callbacks"]:
                        try:
                            symbol_to_unregister = to_mexc_symbol(user.pair)
                            await websocket_manager.unregister_bookticker_callback(
                                symbol_to_unregister, callback
                            )
                        except Exception as cleanup_error:
                            logger.error(
                                f"Ошибка при очистке bookTicker колбэков: {cleanup_error}"
                            )

                    # Закрываем переиспользуемую REST-сессию (keep-alive)
                    rest_client = autobuy_states[telegram_id].get("rest_client")
                    if rest_client:
                        try:
                            await rest_client.close()
                        except Exception as cleanup_error:
                            logger.error(
                                f"Ошибка при закрытии REST-сессии: {cleanup_error}"
                            )

                    del autobuy_states[telegram_id]

                # Если есть сессия, закрываем её
                if session:
                    try:
                        await session.close()
                    except Exception as se:
                        logger.error(f"Ошибка при закрытии сессии: {se}")

                break
            await asyncio.sleep(30)
    else:
        logger.error(
            f"Автобай не удалось запустить для {telegram_id} после {MAX_FAILS} попыток."
        )
        user = await sync_to_async(User.objects.get)(telegram_id=telegram_id)
        user.autobuy = False
        _set_autobuy_inactive(telegram_id)
        await sync_to_async(user.save)(update_fields=["autobuy"])
        task = user_autobuy_tasks.get(telegram_id)
        if task:
            task.cancel()
            del user_autobuy_tasks[telegram_id]

        # Send notification about autobuy failure
        try:
            from bot.config import bot_instance

            await bot_instance.send_message(
                telegram_id,
                f"⛔ Автобай не удалось запустить после {MAX_FAILS} попыток.\n"
                f"Проверьте настройки и попробуйте снова.",
            )
        except Exception as notify_error:
            logger.error(
                f"Failed to send autobuy failure notification to {telegram_id}: {notify_error}"
            )


async def process_buy(
    telegram_id: int,
    reason: str,
    message: Message,
    user: User,
    trigger_ts: float = None,
):
    """Обработка покупки с защитой от одновременных операций"""
    # Импортируем здесь для избежания циклических импортов
    from bot.utils.websocket_manager import websocket_manager

    logger.info(f"process_buy called for {telegram_id} with reason: {reason}")

    # Глобальная защита на пользователя
    state = autobuy_states.get(telegram_id)
    if not state:
        logger.warning(f"No state for user {telegram_id} in process_buy")
        return

    # Пользователя берём из кеша состояния: раньше здесь был запрос в БД
    # прямо перед отправкой ордера — лишний поход в Postgres через thread
    # pool в самом критичном по времени месте. Кеш обновляется основным
    # циклом раз в 10с и periodic_resource_check раз в 60с.
    cached_user = state.get("cached_user")
    if cached_user is not None:
        user = cached_user
    else:
        user = await sync_to_async(User.objects.get)(telegram_id=telegram_id)
        state["cached_user"] = user

    lock = state.get("buy_lock")
    if lock is None:
        lock = asyncio.Lock()
        state["buy_lock"] = lock

    if state.get("buy_in_progress") or lock.locked():
        logger.info(f"Skip process_buy: buy_in_progress for {telegram_id}")
        return

    await lock.acquire()
    # Повторная проверка ПОД блокировкой: между быстрой проверкой выше и
    # захватом lock другая задача могла уже начать покупку.
    if state.get("buy_in_progress"):
        logger.info(f"Skip process_buy (after lock): buy_in_progress for {telegram_id}")
        lock.release()
        return
    state["buy_in_progress"] = True

    try:
        # Проверяем по кешу, что автобай всё ещё включён. Запрос в БД здесь
        # стоял на критическом пути к ордеру; флаг синхронизируется с БД
        # в фоновых циклах, а команда /stop сбрасывает его сразу.
        if not state.get("autobuy_active", True):
            logger.info(
                f"Отмена покупки - пользователь {telegram_id} больше не в режиме автобай"
            )
            return

        # Помечаем время последней операции
        autobuy_states[telegram_id]["last_trade_time"] = time.time()

        # Сбрасываем флаги ожидания
        autobuy_states[telegram_id]["waiting_for_opportunity"] = False
        autobuy_states[telegram_id]["restart_after"] = 0
        autobuy_states[telegram_id]["waiting_reported"] = False

        # Получаем текущие данные - ВСЕГДА свежие из БД
        client_session = None

        # Счетчик последовательных ошибок
        consecutive_errors = autobuy_states[telegram_id].get("consecutive_errors", 0)

        try:
            # Отправляем сообщение о начале покупки для лучшей обратной связи
            if reason == "after_waiting_period":
                symbol = to_mexc_symbol(user.pair)
                current_price = autobuy_states[telegram_id].get("current_price", 0)
                # await message.answer(f"🔄 Возобновляем автобай для {symbol} после паузы. Текущая цена: {current_price:.6f} {quote_asset}")

            rest = get_rest_client(telegram_id, user)
            symbol = to_mexc_symbol(user.pair)
            base_asset, quote_asset = split_pair(user.pair)
            buy_amount = float(user.buy_amount)
            profit_percent = _num(user.profit)
            pause_seconds = _int(user.pause)  # Для использования после покупки

            # Логируем начало покупки
            logger.info(f"Начинаем покупку для {telegram_id}, причина: {reason}")

            # Выполняем покупку. Тип ордера и контроль проскальзывания —
            # в bot/utils/orders.py, там же точность цены и количества по
            # правилам биржи.
            bookticker_now = websocket_manager.get_current_bookticker(symbol)
            ask_now = None
            if bookticker_now:
                try:
                    ask_now = float(bookticker_now["ask_price"])
                except (TypeError, ValueError, KeyError):
                    ask_now = None

            entry = await execute_entry(rest, symbol, buy_amount, ask_price=ask_now)

            # Замер задержки: от срабатывания сигнала до исполнения BUY на бирже
            if trigger_ts is not None:
                latency_ms = (time.perf_counter() - trigger_ts) * 1000
                logger.info(
                    f"[Latency] {telegram_id} {reason}: сигнал → исполнение BUY "
                    f"{latency_ms:.0f} мс ({entry.order_type})"
                )

            if not entry.filled:
                if entry.order_type == "ioc":
                    # Не ошибка: цена ушла дальше допустимого проскальзывания.
                    # Счётчик ошибок не трогаем, просто ждём следующий сигнал.
                    logger.info(
                        f"IOC-вход для {telegram_id} не исполнился, сигнал пропущен"
                    )
                    schedule_buy_retry(telegram_id, "IOC не исполнился")
                    return

                await message.answer("❗ Ошибка при создании ордера (executedQty=0).")
                autobuy_states[telegram_id]["consecutive_errors"] = (
                    consecutive_errors + 1
                )
                if autobuy_states[telegram_id]["consecutive_errors"] >= 3:
                    user.autobuy = False
                    _set_autobuy_inactive(telegram_id)
                    await sync_to_async(user.save)(update_fields=["autobuy"])
                    await message.answer(
                        "⛔ Автобай остановлен после 3 последовательных ошибок при создании ордеров."
                    )
                else:
                    schedule_buy_retry(telegram_id, "ордер не исполнился")
                return

            executed_qty = entry.executed_qty
            spent = entry.spent
            real_price = entry.avg_price

            # Сбрасываем счетчик ошибок при успешной покупке
            autobuy_states[telegram_id]["consecutive_errors"] = 0

            # Сохраняем новую цену последней покупки сразу
            autobuy_states[telegram_id]["last_buy_price"] = real_price

            # Логируем причину покупки
            logger.info(
                f"Buy triggered for {telegram_id} because of {reason}. New last_buy_price: {real_price}"
            )

            # Профит берём из кеша настроек (обновляется раз в 10с),
            # чтобы не ходить в БД между покупкой и защитным лимитом.
            profit_percent = _num(
                autobuy_states[telegram_id].get("cached_profit"), _num(user.profit)
            )

            take_profit = await place_take_profit(
                rest, symbol, executed_qty, real_price, profit_percent
            )
            sell_order_id = take_profit["order_id"]
            sell_price = take_profit["price"]
            sell_qty = take_profit["quantity"]
            logger.info(
                f"SELL ордер {sell_order_id} выставлен на {sell_price} {quote_asset}"
            )

            # Сохраняем ордер в базу
            last_number = await sync_to_async(Deal.objects.filter(user=user).count)()
            user_order_number = last_number + 1

            await sync_to_async(Deal.objects.create)(
                user=user,
                order_id=sell_order_id,
                user_order_number=user_order_number,
                symbol=symbol,
                buy_price=real_price,
                quantity=sell_qty,
                sell_price=sell_price,
                status="NEW",
                is_autobuy=True,
            )

            # Уточняем статус сразу после создания SELL через REST
            try:
                order_check = await rest.query_order(symbol, {"orderId": sell_order_id})
                current_status = order_check.get("status")
                if current_status and current_status != "NEW":
                    deal_obj = await sync_to_async(Deal.objects.get)(
                        order_id=sell_order_id
                    )
                    deal_obj.status = current_status
                    await sync_to_async(deal_obj.save)()
            except Exception as e:
                logger.warning(
                    f"[Autobuy] Не удалось уточнить начальный статус ордера {sell_order_id}: {e}"
                )

            # Добавляем ордер в список активных
            order_info = {
                "order_id": sell_order_id,
                "buy_price": real_price,
                "notified": False,
                "user_order_number": user_order_number,
            }

            # Получаем актуальный список активных ордеров
            active_orders = autobuy_states[telegram_id]["active_orders"]
            active_orders.append(order_info)
            autobuy_states[telegram_id]["active_orders"] = active_orders

            # Отправляем сообщение об открытии сделки
            try:
                from bot.config import bot_instance

                await bot_instance.send_message(
                    telegram_id,
                    f"🟢 *СДЕЛКА {user_order_number} ОТКРЫТА*\n\n"
                    f"📉 Куплено по: `{real_price:.6f}` {quote_asset}\n"
                    f"📦 Кол-во: `{executed_qty:.6f}` {base_asset}\n"
                    f"💸 Потрачено: `{spent:.2f}` {quote_asset}\n\n"
                    f"📈 Лимит на продажу: `{sell_price:.6f}` {quote_asset}\n",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(f"Failed to send buy notification to {telegram_id}: {e}")
                # Fallback to message.answer if bot_instance fails
                try:
                    await message.answer(
                        f"🟢 *СДЕЛКА {user_order_number} ОТКРЫТА*\n\n"
                        f"📉 Куплено по: `{real_price:.6f}` {quote_asset}\n"
                        f"📦 Кол-во: `{executed_qty:.6f}` {base_asset}\n"
                        f"💸 Потрачено: `{spent:.2f}` {quote_asset}\n\n"
                        f"📈 Лимит на продажу: `{sell_price:.6f}` {quote_asset}\n",
                        parse_mode="Markdown",
                    )
                except Exception as fallback_error:
                    logger.error(
                        f"Failed to send buy notification via fallback to {telegram_id}: {fallback_error}"
                    )

            # Перевзводим триггер роста после ЛЮБОЙ покупки. Раньше здесь был
            # белый список причин, в который не попадал "after_waiting_period_main_loop",
            # и после обычного цикла покупки на росте больше не срабатывали.
            bookticker_data = websocket_manager.get_current_bookticker(symbol)
            if bookticker_data:
                trigger_ask = float(bookticker_data["ask_price"])
                arm_rise_trigger(autobuy_states[telegram_id], trigger_ask, time.time())
                logger.info(
                    f"Rise trigger set for {telegram_id} at ask price {trigger_ask:.6f} after {reason}"
                )
            else:
                # Без bookTicker берём цену последней покупки — лучше, чем
                # остаться вообще без триггера роста.
                arm_rise_trigger(autobuy_states[telegram_id], real_price, time.time())
                logger.warning(
                    f"No bookTicker data for {telegram_id}: rise trigger armed at fill price {real_price:.6f}"
                )

            # Если это была покупка на росте, устанавливаем паузу ПОСЛЕ покупки
            if reason in ("price_rise", "rise_trigger") and pause_seconds > 0:
                # Устанавливаем время возобновления после паузы
                autobuy_states[telegram_id]["waiting_for_opportunity"] = True
                autobuy_states[telegram_id]["restart_after"] = (
                    time.time() + pause_seconds
                )
                logger.info(
                    f"Установлена пауза {pause_seconds}с после покупки на росте для {telegram_id}"
                )

        except Exception as e:
            logger.error(
                f"Ошибка в процессе выполнения покупки для {telegram_id}: {e}",
                extra={"user_id": telegram_id},
            )
            error_message = parse_mexc_error(e)
            await message.answer(f"❌ Ошибка при покупке: {error_message}")
            try:
                await notify_user_autobuy_error(telegram_id, "при создании ордера", e)
            except Exception:
                pass

            # Увеличиваем счетчик последовательных ошибок
            autobuy_states[telegram_id]["consecutive_errors"] = consecutive_errors + 1

            # Если достигли 3 последовательных ошибки, останавливаем автобай
            if autobuy_states[telegram_id]["consecutive_errors"] >= 3:
                user.autobuy = False
                _set_autobuy_inactive(telegram_id)
                await sync_to_async(user.save)(update_fields=["autobuy"])
                await message.answer(
                    "⛔ Автобай остановлен после 3 последовательных ошибок. Проверьте настройки и баланс."
                )
                logger.warning(
                    f"Автобай остановлен для {telegram_id} после 3 последовательных ошибок"
                )
            else:
                schedule_buy_retry(telegram_id, f"ошибка покупки: {error_message}")

        finally:
            # Закрываем сессию, если она была создана
            if client_session:
                try:
                    await client_session.close()
                except Exception as e:
                    logger.error(f"Ошибка при закрытии сессии: {e}")
    except Exception as e:
        logger.error(f"Ошибка при выполнении покупки для {telegram_id}: {e}")
        error_message = parse_mexc_error(e)
        await message.answer(f"❌ Ошибка при покупке: {error_message}")
        try:
            await notify_user_autobuy_error(telegram_id, "при выполнении покупки", e)
        except Exception:
            pass
        schedule_buy_retry(telegram_id, f"исключение: {error_message}")
    finally:
        # Всегда освобождаем блокировку и сбрасываем флаг
        try:
            state["buy_in_progress"] = False
        finally:
            try:
                lock.release()
            except RuntimeError:
                pass


async def check_rise_triggers(
    telegram_id: int,
    symbol: str,
    bid_price: float,
    ask_price: float,
    is_rise: bool,
    current_time: float,
    pause_seconds: int,
):
    """
    Проверяет триггеры для покупок на росте цены.

    Логика:
    1. Триггер стоит на ask_price последней покупки/продажи.
    2. Активация — когда ask достигает уровня триггера или уходит выше.
    3. С момента активации открывается окно анализа длиной pause_seconds.
    4. Внутри окна mid-цена должна расти. Допускается откат не глубже
       RISE_TREND_TOLERANCE_PCT от максимума окна — иначе на реальном рынке
       (тики каждые 100 мс) условие "ни одного тика вниз" не выполняется
       практически никогда, и покупки на росте не происходили вообще.
    5. Откат глубже допуска — окно закрывается, а триггер ПЕРЕВЗВОДИТСЯ на
       текущую цену. Раньше здесь вызывался reset_rise_trigger, который
       полностью выключал механизм роста до следующей сделки.
    6. По истечении окна покупаем, если ask выше уровня триггера (минимум на
       RISE_MIN_PCT) и mid не ушла ниже точки старта окна.
    """
    try:
        if telegram_id not in autobuy_states:
            return

        state = autobuy_states[telegram_id]
        ask_price_float = float(ask_price)
        bid_price_float = float(bid_price)
        mid_price = (bid_price_float + ask_price_float) / 2
        pause_seconds = _int(pause_seconds, 0)

        # Инициализация и сохранение предыдущих цен
        prev_ask_price = state.get("last_ask_price")
        state["last_ask_price"] = ask_price_float
        state["last_mid_price"] = mid_price

        # Проверяем, что триггер установлен
        if not state.get("is_rise_trigger") or state.get("trigger_price") is None:
            return

        trigger_price = float(state["trigger_price"])
        is_activated = state.get("is_trigger_activated", False)

        # ЭТАП 1: Активация — ask дошёл до уровня триггера или выше.
        # Раньше требовалось строгое пересечение уровня между двумя соседними
        # тиками (prev_ask <= trigger < ask). Триггер ставится по ask в момент
        # сделки, и на быстром движении бот успевал "перепрыгнуть" уровень
        # между тиками: пересечения не было, окно анализа не открывалось и
        # покупка на росте не происходила вообще.
        if not is_activated:
            if ask_price_float < trigger_price:
                return

            state["is_trigger_activated"] = True
            state["trigger_activated_time"] = current_time
            state["pause_trend_prices"] = [mid_price]
            state["pause_start_mid"] = mid_price
            state["pause_peak_mid"] = mid_price
            state["trend_only_rise"] = True
            state["last_pause_price"] = mid_price

            logger.info(
                f"Rise trigger activated for {telegram_id}: "
                f"ask {prev_ask_price if prev_ask_price is not None else ask_price_float:.6f}"
                f" → {ask_price_float:.6f} (level {trigger_price:.6f}), "
                f"mid {mid_price:.6f}. Окно анализа {pause_seconds}с."
            )
            return

        # ЭТАП 2: Анализ тренда внутри окна
        triggered_time = state.get("trigger_activated_time", 0)
        start_mid = state.get("pause_start_mid")
        peak_mid = state.get("pause_peak_mid")
        if start_mid is None:
            start_mid = mid_price
            state["pause_start_mid"] = start_mid
        if peak_mid is None:
            peak_mid = mid_price

        if mid_price > peak_mid:
            peak_mid = mid_price
        state["pause_peak_mid"] = peak_mid

        # 2.1 Откат глубже допуска — окно закрывается, триггер перевзводим
        drawdown_pct = ((peak_mid - mid_price) / peak_mid * 100) if peak_mid > 0 else 0
        if drawdown_pct > RISE_TREND_TOLERANCE_PCT:
            logger.info(
                f"Rise window cancelled for {telegram_id}: drawdown {drawdown_pct:.3f}% "
                f"> {RISE_TREND_TOLERANCE_PCT:.3f}% (peak {peak_mid:.6f} → {mid_price:.6f}). "
                f"Re-arming trigger at {ask_price_float:.6f}."
            )
            arm_rise_trigger(state, ask_price_float, current_time)
            return

        # 2.2 Копим историю окна (с ограничением, чтобы не течь по памяти)
        pause_prices = state.get("pause_trend_prices", [])
        pause_prices.append(mid_price)
        if len(pause_prices) > 500:
            pause_prices = pause_prices[-500:]
        state["pause_trend_prices"] = pause_prices

        # 2.3 Окно ещё не закончилось
        if (current_time - triggered_time) < pause_seconds:
            return

        # 2.4 Итог окна
        required_price = trigger_price * (1 + RISE_MIN_PCT / 100)
        rise_confirmed = (
            ask_price_float > trigger_price
            and ask_price_float >= required_price
            and mid_price >= start_mid
        )
        if not rise_confirmed:
            logger.info(
                f"Rise conditions NOT met for {telegram_id}. ask={ask_price_float:.6f}, "
                f"trigger={trigger_price:.6f}, mid={mid_price:.6f}, start_mid={start_mid:.6f}. "
                f"Re-arming trigger."
            )
            arm_rise_trigger(state, ask_price_float, current_time)
            return

        # Ограничения: не более RISE_MAX_BUYS_PER_CYCLE покупок на росте
        # в рамках одного цикла и не чаще RISE_BUY_COOLDOWN_SEC.
        if state.get("rise_buy_count", 0) >= RISE_MAX_BUYS_PER_CYCLE:
            logger.info(
                f"Rise buy limit reached for {telegram_id} "
                f"({RISE_MAX_BUYS_PER_CYCLE} per cycle). Skipping."
            )
            arm_rise_trigger(state, ask_price_float, current_time)
            return

        if (current_time - state.get("last_rise_buy_time", 0)) < RISE_BUY_COOLDOWN_SEC:
            return

        if state.get("buy_in_progress"):
            logger.info(f"Skip rise buy: buy_in_progress for {telegram_id}")
            return

        logger.info(
            f"Rise conditions met for {telegram_id}: window {pause_seconds}s, "
            f"trigger {trigger_price:.6f} → ask {ask_price_float:.6f}, "
            f"max drawdown within window {drawdown_pct:.3f}%"
        )

        # Сначала ордер, уведомление — отдельной задачей
        trigger_ts = time.perf_counter()
        from bot.utils.autobuy_restart import FakeMessage
        from bot.config import bot_instance

        fake_message = FakeMessage(telegram_id, bot_instance)

        state["last_rise_buy_time"] = current_time
        state["rise_buy_count"] = state.get("rise_buy_count", 0) + 1

        asyncio.create_task(
            process_buy(telegram_id, "rise_trigger", fake_message, None, trigger_ts)
        )

        asyncio.create_task(
            _safe_send(
                telegram_id,
                f"⏫ Покупка по росту для {symbol}\n\n"
                f"📈 Рост удержан {pause_seconds}с\n"
                f"🎯 Цена: {trigger_price:.6f} → {ask_price_float:.6f}\n"
                f"💰 Совершаем покупку!",
            )
        )

        # Перевзводим триггер от текущей цены. Если покупка пройдёт успешно,
        # process_buy переставит его ещё раз по факту исполнения.
        arm_rise_trigger(state, ask_price_float, current_time)

    except Exception as e:
        logger.error(
            f"Error in check_rise_triggers for {telegram_id}: {e}", exc_info=True
        )


def reset_rise_trigger(state):
    """Полностью выключает триггер на росте (используется при остановке автобая).

    Для штатного перезапуска окна анализа используйте arm_rise_trigger: он
    оставляет механизм роста включённым.
    """
    state["is_rise_trigger"] = False
    state["trigger_price"] = None
    state["trigger_time"] = 0
    state["is_trigger_activated"] = False
    state["trigger_activated_time"] = 0
    state["pause_trend_prices"] = []
    state["pause_peak_mid"] = None
    state["pause_start_mid"] = None
    state["trend_only_rise"] = True
    state["last_pause_price"] = None
    state["last_ask_price"] = None
    state["last_mid_price"] = None


async def process_order_update_for_autobuy(order_id, symbol, status, user_id):
    """Обработка обновлений ордеров для автобая через WebSocket"""
    if user_id not in autobuy_states:
        logger.debug(
            f"[AutobuyOrderUpdate] User {user_id} not in autobuy_states. Skipping."
        )
        return

    # Отладочный лог с полным состоянием
    # logger.info(
    #     f"[AutobuyOrderUpdate] User {user_id}: Processing order_id={order_id}, symbol={symbol}, status={status}."
    # )

    active_orders = autobuy_states[user_id]["active_orders"]
    old_last_buy_price = autobuy_states[user_id].get("last_buy_price")

    # Ищем ордер среди активных
    order_index = next(
        (i for i, order in enumerate(active_orders) if order["order_id"] == order_id),
        None,
    )

    if order_index is not None:
        # logger.info(f"[AutobuyOrderUpdate] User {user_id}: Found order {order_id} in active_orders at index {order_index}. Current active_orders: {active_orders}")
        if status in ["FILLED", "CANCELED"]:
            # Получаем информацию о завершенном ордере
            order_info = active_orders[order_index]
            logger.info(
                f"[AutobuyOrderUpdate] User {user_id}: Order {order_id} (UserOrderNum: {order_info.get('user_order_number')}) has status {status}. Removing from active_orders."
            )

            # Если ордер исполнен или отменен, удаляем его из активных
            active_orders.pop(order_index)
            autobuy_states[user_id]["active_orders"] = active_orders
            logger.info(
                f"[AutobuyOrderUpdate] User {user_id}: active_orders after removal: {len(active_orders)}"
            )

            # Устанавливаем триггер для покупок на росте после КАЖДОЙ продажи
            try:
                from bot.utils.websocket_manager import websocket_manager

                bookticker_data = websocket_manager.get_current_bookticker(
                    to_mexc_symbol(symbol)
                )
                if bookticker_data:
                    ask_price = float(
                        bookticker_data["ask_price"]
                    )  # Используем ask цену
                    current_time = time.time()

                    arm_rise_trigger(
                        autobuy_states[user_id], ask_price, current_time
                    )
                    autobuy_states[user_id]["last_ask_price"] = None
                    autobuy_states[user_id]["last_mid_price"] = None

                    logger.info(
                        f"[AutobuyOrderUpdate] User {user_id}: Rise trigger set at ask price {ask_price:.6f} after order {order_id} filled"
                    )
                else:
                    logger.warning(
                        f"[AutobuyOrderUpdate] User {user_id}: Could not set rise trigger - no bookTicker data"
                    )
            except Exception as e:
                logger.error(
                    f"[AutobuyOrderUpdate] User {user_id}: Error setting rise trigger: {e}"
                )

            # Если не осталось активных ордеров, устанавливаем паузу перед следующей покупкой
            if not active_orders:
                logger.info(
                    f"[AutobuyOrderUpdate] User {user_id}: No active orders remaining."
                )
                # Получаем пользовательские настройки для определения паузы
                try:
                    user = await sync_to_async(User.objects.get)(telegram_id=user_id)
                    pause_seconds = _int(user.pause)

                    # Цикл закрыт — разрешаем покупки на росте в новом цикле
                    autobuy_states[user_id]["rise_buy_count"] = 0

                    # Устанавливаем время следующей возможной покупки
                    autobuy_states[user_id]["last_buy_price"] = None
                    autobuy_states[user_id]["waiting_for_opportunity"] = True
                    autobuy_states[user_id]["restart_after"] = (
                        time.time() + pause_seconds
                    )
                    autobuy_states[user_id]["waiting_reported"] = False

                    logger.info(
                        f"[AutobuyOrderUpdate] User {user_id}: Reset last_buy_price to None. waiting_for_opportunity=True. Next buy possible after {pause_seconds}s (at {autobuy_states[user_id]['restart_after']})."
                    )
                except Exception as e:
                    logger.error(
                        f"[AutobuyOrderUpdate] User {user_id}: Error getting user settings for pause: {e}"
                    )
                    # Если не удалось получить настройки паузы — всё равно
                    # переводим в режим ожидания, иначе основной цикл никогда
                    # не запустит следующую покупку.
                    autobuy_states[user_id]["last_buy_price"] = None
                    autobuy_states[user_id]["rise_buy_count"] = 0
                    autobuy_states[user_id]["waiting_for_opportunity"] = True
                    autobuy_states[user_id]["restart_after"] = time.time()
                    autobuy_states[user_id]["waiting_reported"] = False
                    logger.info(
                        f"[AutobuyOrderUpdate] User {user_id}: Reset last_buy_price to None (error case)."
                    )
            else:
                # Иначе устанавливаем last_buy_price по самому свежему ордеру
                most_recent_order = max(
                    active_orders, key=lambda x: x.get("user_order_number", 0)
                )
                autobuy_states[user_id]["last_buy_price"] = most_recent_order[
                    "buy_price"
                ]
                logger.info(
                    f"[AutobuyOrderUpdate] User {user_id}: Updated last_buy_price to {most_recent_order['buy_price']} from active order #{most_recent_order['user_order_number']}. Active orders count: {len(active_orders)}"
                )
        else:
            logger.debug(
                f"[AutobuyOrderUpdate] User {user_id}: Order {order_id} status is {status} (not FILLED/CANCELED). No state change."
            )
    else:
        logger.info(
            f"[AutobuyOrderUpdate] User {user_id}: Order {order_id} not found in active_orders."
        )

    # Лог изменений
    new_last_buy_price = autobuy_states[user_id].get("last_buy_price")
    if old_last_buy_price != new_last_buy_price:
        logger.info(
            f"[AutobuyOrderUpdate] User {user_id}: last_buy_price changed from {old_last_buy_price} to {new_last_buy_price}."
        )
    elif status in ["FILLED", "CANCELED"] and order_index is not None:
        logger.info(
            f"[AutobuyOrderUpdate] User {user_id}: last_buy_price remains {new_last_buy_price} after processing order {order_id} ({status})."
        )


async def periodic_resource_check(telegram_id: int):
    """Периодическая проверка и очистка ресурсов + ресинк состояния из БД"""
    while telegram_id in autobuy_states:
        try:
            # Раньше здесь каждые 60с на каждого пользователя вызывался
            # сборщик мусора и полный обход кучи (gc.get_objects) ради
            # подсчёта ClientSession. Обход кучи блокирует event loop на
            # сотни миллисекунд: тик, пришедший в этот момент, обрабатывался
            # с задержкой, а ордер уходил по худшей цене. Счётчик больше не
            # нужен — транспорт общий (одна keep-alive сессия на процесс), а
            # утечки соединений видны через get_connection_stats().

            # Проверяем состояние ожидания и обновляем его при необходимости
            current_time = time.time()
            restart_after = autobuy_states[telegram_id].get("restart_after", 0)
            waiting_for_opportunity = autobuy_states[telegram_id].get(
                "waiting_for_opportunity", False
            )

            if (
                waiting_for_opportunity
                and restart_after > 0
                and current_time >= restart_after
            ):
                logger.info(
                    f"Период ожидания истек для {telegram_id} (проверка ресурсов)"
                )

            # Обновляем параметры пользователя
            from bot.utils.websocket_manager import websocket_manager

            # Проверяем соединение с WebSocket и восстанавливаем при необходимости
            user = await sync_to_async(User.objects.get)(telegram_id=telegram_id)
            symbol = to_mexc_symbol(user.pair)

            # Дублируем обновление кеша настроек (страховка, если основной цикл занят)
            st = autobuy_states.get(telegram_id)
            if st is not None:
                st["cached_loss"] = _num(user.loss)
                st["cached_profit"] = _num(user.profit)
                st["cached_pause"] = _int(user.pause)
                st["autobuy_active"] = bool(user.autobuy)
                st["cached_user"] = user

            if not websocket_manager.market_connection:
                logger.warning(
                    f"Соединение с WebSocket для рынка потеряно, переподключаемся"
                )
                success = await websocket_manager.connect_market_data()
                if not success:
                    logger.error(
                        f"Не удалось переподключиться к market WebSocket для {telegram_id}"
                    )
                    return

            if symbol not in websocket_manager.bookticker_subscriptions:
                logger.warning(
                    f"Подписка на bookTicker {symbol} отсутствует, переподписываемся"
                )
                success = await websocket_manager.subscribe_bookticker_data([symbol])
                if not success:
                    logger.error(
                        f"Не удалось подписаться на bookTicker {symbol} для {telegram_id}"
                    )

            # Колбэк мог потеряться при переподключении — проверяем, что он на месте
            registered = websocket_manager.bookticker_callbacks.get(symbol, [])
            expected = autobuy_states[telegram_id].get("bookticker_callbacks", [])
            for cb in expected:
                if cb not in registered:
                    logger.warning(
                        f"bookTicker колбэк для {telegram_id} ({symbol}) пропал, регистрируем заново"
                    )
                    await websocket_manager.register_bookticker_callback(symbol, cb)

            # ===== DB → State ресинк активных ордеров раз в ~60с =====
            # Пересобираем список активных ордеров из БД и синхронизируем in-memory состояние
            deals_qs = Deal.objects.filter(
                user=user, status__in=["NEW", "PARTIALLY_FILLED"], is_autobuy=True
            ).order_by("-created_at")

            active_deals = await sync_to_async(list)(deals_qs)

            rebuilt_active_orders = []
            for deal in active_deals:
                rebuilt_active_orders.append(
                    {
                        "order_id": deal.order_id,
                        "buy_price": float(deal.buy_price),
                        "notified": False,
                        "user_order_number": deal.user_order_number,
                    }
                )

            state = autobuy_states.get(telegram_id, {})
            current_active_orders = state.get("active_orders", [])

            # Обновляем только если реально поменялось
            if rebuilt_active_orders != current_active_orders:
                autobuy_states[telegram_id]["active_orders"] = rebuilt_active_orders
                logger.info(
                    f"[Resync] Пересобраны active_orders для {telegram_id}: {rebuilt_active_orders}"
                )

                # Если активных ордеров больше нет — переводим в режим ожидания новой возможности
                if not rebuilt_active_orders:
                    pause_seconds = _int(getattr(user, "pause", 0))

                    autobuy_states[telegram_id]["last_buy_price"] = None
                    autobuy_states[telegram_id]["rise_buy_count"] = 0
                    autobuy_states[telegram_id]["waiting_for_opportunity"] = True
                    # pause = 0 раньше давало restart_after = 0, а основной цикл
                    # требует restart_after > 0 — бот замолкал навсегда.
                    autobuy_states[telegram_id]["restart_after"] = (
                        time.time() + pause_seconds
                    )
                    autobuy_states[telegram_id]["waiting_reported"] = False
                    logger.info(
                        f"[Resync] Установлен режим ожидания для {telegram_id}. Пауза: {pause_seconds}s"
                    )

        except Exception as e:
            logger.error(f"Ошибка в periodic_resource_check для {telegram_id}: {e}")

        # Проверка каждые 60 секунд (ресинк и здоровье)
        await asyncio.sleep(60)
