import asyncio
import aiohttp
import hmac
import hashlib
import time
import uuid
from typing import Dict, Any, Optional
from urllib.parse import urlencode, quote

# PAIR in .env / DB is stored as BTC/USDT; MEXC REST/WS need BTCUSDT.
_QUOTE_ASSETS = ("USDT", "USDC", "BUSD", "USD")

# Одна общая keep-alive сессия на весь процесс. Соединение к MEXC остаётся
# открытым между запросами, поэтому market BUY не платит за новый TCP+TLS
# handshake на каждом ордере (критично для скорости на скальпинге).
# Авторизация задаётся заголовками per-request, поэтому сессию безопасно
# делить между пользователями и ключами — общий пул только на транспорт.
_shared_session: Optional["aiohttp.ClientSession"] = None
_session_lock = asyncio.Lock()


async def get_shared_session() -> aiohttp.ClientSession:
    """Лениво создаёт и переиспользует одну keep-alive сессию на процесс."""
    global _shared_session
    if _shared_session is None or _shared_session.closed:
        async with _session_lock:
            if _shared_session is None or _shared_session.closed:
                connector = aiohttp.TCPConnector(
                    limit=100,
                    limit_per_host=20,
                    ttl_dns_cache=300,
                    keepalive_timeout=60,
                    enable_cleanup_closed=True,
                )
                _shared_session = aiohttp.ClientSession(connector=connector)
    return _shared_session


async def close_shared_session() -> None:
    """Закрывает общую сессию (вызывать только при остановке процесса)."""
    global _shared_session
    if _shared_session is not None and not _shared_session.closed:
        try:
            await _shared_session.close()
        except Exception:
            pass
    _shared_session = None



async def rest_keepalive_loop(interval_sec: int = 20) -> None:
    """Держит TCP/TLS-соединение к MEXC горячим.

    keepalive_timeout у пула — 60с, но MEXC/Cloudflare закрывают простаивающие
    соединения раньше. Без прогрева первый ордер после паузы платит полный
    TCP+TLS handshake — десятки миллисекунд прямо в момент входа в сделку.
    """
    from bot.logger import logger

    while True:
        try:
            session = await get_shared_session()
            async with session.get(
                f"{MexcRestClient.BASE_URL}/api/v3/ping",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                await resp.read()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[REST] keep-alive ping не прошёл: {e}")
        await asyncio.sleep(interval_sec)


def to_mexc_symbol(pair: str) -> str:
    return (pair or "").replace("/", "").replace("-", "").replace("_", "").upper().strip()


def split_pair(pair: str) -> tuple:
    raw = (pair or "").strip().upper()
    if "/" in raw:
        base, quote = raw.split("/", 1)
        return base.strip(), quote.strip()
    if "-" in raw:
        base, quote = raw.split("-", 1)
        return base.strip(), quote.strip()
    symbol = to_mexc_symbol(raw)
    for quote in _QUOTE_ASSETS:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol[:-4], symbol[-4:]


class MexcRestClient:
    """Minimal MEXC Spot v3 REST client (async), signed endpoints included."""

    BASE_URL = "https://api.mexc.com"

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self._time_offset_ms: Optional[int] = None
        self._last_time_sync: float = 0.0
        self._time_sync_interval_sec: int = 300
        self._time_sync_task: Optional[asyncio.Task] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Возвращает общую keep-alive сессию процесса."""
        return await get_shared_session()

    async def close(self) -> None:
        """No-op: транспортная сессия общая для процесса и не закрывается
        при остановке автобая одного пользователя. Метод оставлен для
        совместимости с местами, которые вызывают client.close()."""
        return None

    async def _server_time(self) -> int:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.BASE_URL}/api/v3/time",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
                return int(data.get("serverTime", int(time.time() * 1000)))
        except Exception:
            # Fallback to local timestamp on timeout/network errors
            return int(time.time() * 1000)

    async def _sync_time(self) -> None:
        now = time.time()
        server_ms = await self._server_time()
        self._time_offset_ms = server_ms - int(now * 1000)
        self._last_time_sync = now

    async def _ensure_time_offset(self) -> None:
        """Держит смещение часов свежим, НЕ задерживая текущий запрос.

        Раньше устаревший офсет означал лишний HTTP-круг к /api/v3/time прямо
        перед отправкой ордера — десятки миллисекунд проскальзывания раз в
        5 минут. Теперь первый запрос синхронизируется честно, а дальше
        обновление уходит в фон: recvWindow с запасом перекрывает дрейф часов.
        """
        now = time.time()
        if self._time_offset_ms is None:
            # Офсета нет вообще — без него подпись не пройдёт.
            await self._sync_time()
            return

        if (now - self._last_time_sync) > self._time_sync_interval_sec:
            if self._time_sync_task is None or self._time_sync_task.done():
                # Помечаем сразу, чтобы фоновых задач не наплодилось.
                self._last_time_sync = now
                self._time_sync_task = asyncio.create_task(self._sync_time())

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        timeout_sec: int = 20,
        recv_window_ms: int = 59000,
        retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        params = params.copy() if params else {}
        headers = {}

        if signed:
            # Keep client clock aligned and honor recvWindow
            await self._ensure_time_offset()
            server_ts = int(time.time() * 1000 + (self._time_offset_ms or 0))
            sign_params = params.copy()
            # stringify values to be safe
            for k, v in list(sign_params.items()):
                if isinstance(v, (float, int)):
                    sign_params[k] = str(v)
            if "recvWindow" not in sign_params and recv_window_ms:
                # Clamp to allowed maximum (< 60000)
                if int(recv_window_ms) >= 60000:
                    recv_window_ms = 59000
                sign_params["recvWindow"] = str(recv_window_ms)
            sign_base = urlencode(sign_params, quote_via=quote)
            to_sign = (
                f"{sign_base}&timestamp={server_ts}"
                if sign_base
                else f"timestamp={server_ts}"
            )
            signature = hmac.new(
                self.api_secret.encode(), to_sign.encode(), hashlib.sha256
            ).hexdigest()

            # final query params include original params + timestamp + signature
            params = sign_params
            params["timestamp"] = server_ts
            params["signature"] = signature
            headers["x-mexc-apikey"] = self.api_key
            headers["Content-Type"] = "application/json"

        url = f"{self.BASE_URL}{path}"
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        session = await self._get_session()
        # POST /api/v3/order неидемпотентен: повтор после таймаута мог создать
        # вторую рыночную покупку, если биржа приняла первую, а ответ не дошёл.
        # По умолчанию повторяем только GET.
        max_retries = retries if retries is not None else (3 if method == "GET" else 1)
        backoff = 0.5
        last_err = None
        for _ in range(max_retries):
            try:
                if method == "GET":
                    async with session.get(
                        url, params=params, headers=headers, timeout=timeout
                    ) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status != 200:
                            # If timestamp window error, resync time and retry
                            if isinstance(data, dict) and data.get("code") == 700003:
                                self._last_time_sync = 0.0
                                await self._ensure_time_offset()
                                raise aiohttp.ServerDisconnectedError()
                            raise RuntimeError(data)
                        return data
                elif method == "POST":
                    if signed:
                        # Send with params in query (no body) per official examples
                        async with session.post(
                            url, params=params, headers=headers, timeout=timeout
                        ) as resp:
                            data = await resp.json(content_type=None)
                            if resp.status != 200:
                                if (
                                    isinstance(data, dict)
                                    and data.get("code") == 700003
                                ):
                                    self._last_time_sync = 0.0
                                    await self._ensure_time_offset()
                                    raise aiohttp.ServerDisconnectedError()
                                raise RuntimeError(data)
                            return data
                    # Unsigned POST (rare): send JSON
                    async with session.post(
                        url, json=params, headers=headers, timeout=timeout
                    ) as resp:
                        data = await resp.json(content_type=None)
                        if resp.status != 200:
                            if isinstance(data, dict) and data.get("code") == 700003:
                                self._last_time_sync = 0.0
                                await self._ensure_time_offset()
                                raise aiohttp.ServerDisconnectedError()
                            raise RuntimeError(data)
                        return data
                else:
                    raise ValueError("Unsupported method")
            except (
                aiohttp.ClientConnectorError,
                aiohttp.ClientOSError,
                aiohttp.ServerDisconnectedError,
                asyncio.TimeoutError,
            ) as e:
                last_err = e
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
        if last_err:
            raise last_err

    # Public
    async def ticker_price(self, symbol: str) -> Dict[str, Any]:
        symbol = to_mexc_symbol(symbol)
        return await self._request(
            "GET",
            "/api/v3/ticker/price",
            {"symbol": symbol},
            signed=False,
            timeout_sec=10,
        )

    async def exchange_info(self, symbol: str) -> Dict[str, Any]:
        """Торговые правила символа (точность, минимумы, комиссии)."""
        return await self._request(
            "GET",
            "/api/v3/exchangeInfo",
            {"symbol": to_mexc_symbol(symbol)},
            signed=False,
            timeout_sec=15,
        )

    async def ping(self) -> Dict[str, Any]:
        """Лёгкий запрос, держит TCP/TLS-соединение горячим."""
        return await self._request(
            "GET", "/api/v3/ping", {}, signed=False, timeout_sec=5, retries=1
        )

    # Signed
    async def account_info(self) -> Dict[str, Any]:
        return await self._request(
            "GET", "/api/v3/account", {}, signed=True, timeout_sec=20
        )

    async def open_orders(self, symbol: str) -> Any:
        symbol = to_mexc_symbol(symbol)
        return await self._request(
            "GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True, timeout_sec=20
        )

    # Ордер, подписанный больше чем на несколько секунд назад, для скальпинга
    # бесполезен: исполнится по цене, к сигналу уже не относящейся.
    ORDER_RECV_WINDOW_MS = 5000
    ORDER_TIMEOUT_SEC = 10

    @staticmethod
    def make_client_order_id(prefix: str = "sb") -> str:
        """Идемпотентный ключ ордера: повтор с тем же ключом биржа отклонит."""
        return f"{prefix}{uuid.uuid4().hex[:24]}"

    async def new_order(
        self, symbol: str, side: str, order_type: str, options: Dict[str, Any]
    ) -> Dict[str, Any]:
        params = {"symbol": to_mexc_symbol(symbol), "side": side, "type": order_type}
        params.update(options or {})
        # Клиентский ID позволяет после сетевой ошибки выяснить, был ли ордер
        # на самом деле создан, вместо слепого повтора.
        params.setdefault("newClientOrderId", self.make_client_order_id())
        return await self._request(
            "POST",
            "/api/v3/order",
            params,
            signed=True,
            timeout_sec=self.ORDER_TIMEOUT_SEC,
            recv_window_ms=self.ORDER_RECV_WINDOW_MS,
        )

    async def find_order_by_client_id(
        self, symbol: str, client_order_id: str
    ) -> Optional[Dict[str, Any]]:
        """Ищет ордер по клиентскому ID. None — биржа его не создавала.

        Нужно после сетевого сбоя на POST /order: повторять ордер вслепую
        нельзя, но и бросать позицию без лимита на продажу тоже.
        """
        try:
            return await self.query_order(
                symbol, {"origClientOrderId": client_order_id}
            )
        except Exception:
            return None

    async def query_order(self, symbol: str, options: Dict[str, Any]) -> Dict[str, Any]:
        params = {"symbol": to_mexc_symbol(symbol)}
        params.update(options or {})
        return await self._request(
            "GET", "/api/v3/order", params, signed=True, timeout_sec=20
        )
