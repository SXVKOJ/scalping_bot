"""BingX gzip WebSockets with independent readers and ordered dispatchers."""
import asyncio
import gzip
import json
import time
import uuid

import aiohttp
from asgiref.sync import sync_to_async
from django.conf import settings

from bot.logger import logger
from bot.utils.bingx_rest import BingXRestClient, bingx_symbol
from bot.utils.mexc_rest import get_shared_session, to_mexc_symbol
from bot.utils.ws.dispatcher import StreamDispatcher
from bot.utils.ws.price_direction import PriceDirectionTracker


class BingXWebSocketManager:
    BASE_URL = "wss://open-api-ws.bingx.com/market"

    def __init__(self):
        self.market_connection = None
        self.user_connections = {}
        self.market_subscriptions = []
        self.bookticker_subscriptions = []
        self.price_callbacks = {}
        self.bookticker_callbacks = {}
        self.current_bookticker = {}
        self.direction_tracker = PriceDirectionTracker()
        self.is_shutting_down = False
        self.market_listener_active = False
        self._lock = asyncio.Lock()
        self._users_lock = asyncio.Lock()
        self._tasks = set()

    def _start(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _subscribe(self, ws, channel):
        await ws.send_json({"id": uuid.uuid4().hex, "reqType": "sub", "dataType": channel})

    async def connect_market_data(self, symbols=None):
        async with self._lock:
            if not self.market_connection or self.market_connection["ws"].closed:
                session = await get_shared_session()
                ws = await session.ws_connect(self.BASE_URL, heartbeat=None, receive_timeout=45)
                conn = {"ws": ws, "created_at": time.time()}
                try:
                    for symbol in self.bookticker_subscriptions:
                        await self._subscribe(ws, bingx_symbol(symbol) + "@bookTicker")
                    for symbol in self.market_subscriptions:
                        await self._subscribe(ws, bingx_symbol(symbol) + "@lastPrice")
                except BaseException:
                    await ws.close()
                    raise
                self.market_connection = conn
                self.market_listener_active = True
                conn["task"] = self._start(self._read(conn, self._market_event))
        if symbols:
            await self.subscribe_market_data(symbols)
        return True

    async def _read(self, conn, handler, private=False):
        ws = conn["ws"]
        dispatcher = StreamDispatcher("BingXPrivate" if private else "BingXMarket", handler)
        dispatcher.start()
        try:
            async for message in ws:
                if message.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    continue
                payload = message.data
                if isinstance(payload, bytes):
                    payload = gzip.decompress(payload).decode() if payload[:2] == b"\x1f\x8b" else payload.decode()
                conn["last_message_at"] = time.time()
                if payload.lower() == "ping":
                    await ws.send_str("Pong")
                    continue
                data = json.loads(payload)
                if not dispatcher.submit(data):
                    raise RuntimeError("BingX dispatcher unavailable")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"BingX stream stopped ({type(exc).__name__})")
        finally:
            await ws.close()
            await dispatcher.stop(drain=private)

    async def _market_event(self, message):
        channel, data = message.get("dataType", ""), message.get("data", {})
        if message.get("code", 0) != 0:
            logger.error(f"BingX subscription error: {message.get('code')}")
            return
        if not isinstance(data, dict) or "@" not in channel:
            return
        symbol = to_mexc_symbol(data.get("s", channel.split("@")[0]))
        if channel.endswith("@bookTicker"):
            bid, ask = data.get("b"), data.get("a")
            if not bid or not ask or float(bid) <= 0 or float(ask) <= 0:
                return
            self.current_bookticker[symbol] = {
                "bid_price": bid, "ask_price": ask, "bid_qty": data.get("B", 0),
                "ask_qty": data.get("A", 0), "timestamp": data.get("T", int(time.time()*1000)),
            }
            await self.direction_tracker.update(symbol, float(bid), float(ask))
            for callback in list(self.bookticker_callbacks.get(symbol, [])):
                try:
                    await callback(symbol, bid, ask, data.get("B", 0), data.get("A", 0))
                except Exception:
                    logger.exception("BingX bookTicker callback failed")
        elif channel.endswith("@lastPrice"):
            for callback in list(self.price_callbacks.get(symbol, [])):
                try:
                    await callback(symbol, data["c"])
                except Exception:
                    logger.exception("BingX price callback failed")

    async def subscribe_bookticker_data(self, symbols):
        await self.connect_market_data()
        for symbol in map(to_mexc_symbol, symbols):
            if symbol not in self.bookticker_subscriptions:
                await self._subscribe(self.market_connection["ws"], bingx_symbol(symbol) + "@bookTicker")
                self.bookticker_subscriptions.append(symbol)
        return True

    async def subscribe_market_data(self, symbols):
        await self.connect_market_data()
        for symbol in map(to_mexc_symbol, symbols):
            if symbol not in self.market_subscriptions:
                await self._subscribe(self.market_connection["ws"], bingx_symbol(symbol) + "@lastPrice")
                self.market_subscriptions.append(symbol)
        return True

    async def register_bookticker_callback(self, symbol, callback):
        self.bookticker_callbacks.setdefault(to_mexc_symbol(symbol), []).append(callback)

    async def unregister_bookticker_callback(self, symbol, callback):
        callbacks = self.bookticker_callbacks.get(to_mexc_symbol(symbol), [])
        if callback in callbacks:
            callbacks.remove(callback)

    async def register_price_callback(self, symbol, callback):
        self.price_callbacks.setdefault(to_mexc_symbol(symbol), []).append(callback)

    def get_current_bookticker(self, symbol):
        return self.current_bookticker.get(to_mexc_symbol(symbol))

    def get_price_direction(self, symbol):
        return self.direction_tracker.get(to_mexc_symbol(symbol))

    async def connect_user_data_stream(self, user_id):
        from users.models import User
        async with self._users_lock:
            if user_id in self.user_connections:
                if not self.user_connections[user_id]['ws'].closed:
                    return True
                await self.disconnect_user(user_id)
            user = await sync_to_async(User.objects.get)(telegram_id=user_id)
            if user.api_exchange != "bingx" or not user.api_key or not user.api_secret:
                return False
            client = BingXRestClient(user.api_key, user.api_secret)
            key = (await client.listen_key())["listenKey"]
            session = await get_shared_session()
            ws = await session.ws_connect(self.BASE_URL, params={"listenKey": key}, heartbeat=None, receive_timeout=45)
            conn = {"ws": ws, "listen_key": key, "client": client, "created_at": time.time()}
            await self._subscribe(ws, "spot.executionReport")

            async def event(message):
                data = message.get("data", {})
                if message.get("dataType") == "spot.executionReport" and data.get("i"):
                    from bot.utils.websocket_handlers import update_order_status
                    await update_order_status(str(data["i"]), to_mexc_symbol(data["s"]),
                                              "NEW" if data["X"] == "PENDING" else data["X"], user_id=user_id)

            conn["task"] = self._start(self._read(conn, event, private=True))
            conn["keepalive"] = self._start(self._keepalive(conn))
            self.user_connections[user_id] = conn
        return True

    async def _keepalive(self, conn):
        while not conn["ws"].closed:
            await asyncio.sleep(1800)
            try:
                await conn["client"].listen_key("PUT", conn["listen_key"])
            except Exception:
                await conn["ws"].close()
                return

    async def subscribe_user_orders(self, user_id, symbol=None):
        return await self.connect_user_data_stream(user_id)

    async def disconnect_user(self, user_id):
        conn = self.user_connections.pop(user_id, None)
        if conn:
            conn["keepalive"].cancel()
            await conn["ws"].close()
            await asyncio.gather(conn["task"], conn["keepalive"], return_exceptions=True)
            try:
                await conn["client"].listen_key("DELETE", conn["listen_key"])
            except Exception:
                pass

    async def disconnect_market(self):
        conn, self.market_connection = self.market_connection, None
        self.market_listener_active = False
        self.current_bookticker.clear()
        if conn:
            await conn["ws"].close()
            await asyncio.gather(conn["task"], return_exceptions=True)

    async def disconnect_all(self):
        self.is_shutting_down = True
        for user_id in list(self.user_connections):
            await self.disconnect_user(user_id)
        await self.disconnect_market()

    async def connect_valid_users(self):
        from users.models import User
        ids = await sync_to_async(list)(User.objects.filter(api_exchange="bingx").exclude(api_key="").exclude(api_key=None).values_list("telegram_id", flat=True))
        for user_id in ids:
            try:
                await self.connect_user_data_stream(user_id)
            except Exception:
                logger.error(f"BingX private stream unavailable for {user_id}")

    async def monitor_connections(self):
        while not self.is_shutting_down:
            try:
                if self.market_connection and self.market_connection["ws"].closed:
                    await self.disconnect_market()
                    await self.connect_market_data()
                for user_id, conn in list(self.user_connections.items()):
                    if conn["ws"].closed:
                        await self.disconnect_user(user_id)
                        await self.connect_user_data_stream(user_id)
            except Exception:
                logger.exception("BingX reconnect failed")
            await asyncio.sleep(3)

    async def get_connection_stats(self):
        return {"user_connections": len(self.user_connections), "market_connection": bool(self.market_connection),
                "total_market_subscriptions": len(self.market_subscriptions),
                "total_bookticker_subscriptions": len(self.bookticker_subscriptions)}
