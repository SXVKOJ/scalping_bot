import asyncio
import json
import time
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from bot.utils.mexc import get_user_client
from bot.utils.websocket_manager import websocket_manager
from bot.logger import logger

router = Router()


@router.message(Command("ws_debug"))
async def debug_websocket_connection(message: Message):
    """
    Debug command to test WebSocket connections and diagnose issues.
    """
    try:
        # Get user's trading client and pair
        client, pair = get_user_client(message.from_user.id)

        if not pair:
            await message.answer(
                "❌ Валютная пара не указана. Установите пару в /parameters"
            )
            return

        debug_info = []
        debug_info.append("🔍 *WebSocket Debug Information*\n")

        # Check market connection status
        if websocket_manager.market_connection:
            debug_info.append("✅ *Market WebSocket:* Подключен")
            ws = websocket_manager.market_connection.get("ws")
            if ws:
                debug_info.append(
                    f"   • Состояние: {'Открыт' if not ws.closed else 'Закрыт'}"
                )
                debug_info.append(f"   • Close Code: {ws.close_code}")
        else:
            debug_info.append("❌ *Market WebSocket:* Не подключен")

        # Check subscriptions
        debug_info.append(f"\n📈 *Подписки:*")
        debug_info.append(
            f"   • Market Data: {len(websocket_manager.market_subscriptions)} символов"
        )
        debug_info.append(
            f"   • BookTicker: {len(websocket_manager.bookticker_subscriptions)} символов"
        )

        if websocket_manager.market_subscriptions:
            debug_info.append(
                f"   • Market Symbols: {', '.join(websocket_manager.market_subscriptions)}"
            )
        if websocket_manager.bookticker_subscriptions:
            debug_info.append(
                f"   • BookTicker Symbols: {', '.join(websocket_manager.bookticker_subscriptions)}"
            )

        # Check cached data
        debug_info.append(f"\n💾 *Кэшированные данные:*")
        debug_info.append(
            f"   • BookTicker entries: {len(websocket_manager.current_bookticker)}"
        )

        if websocket_manager.current_bookticker:
            for symbol, data in websocket_manager.current_bookticker.items():
                debug_info.append(
                    f"   • {symbol}: bid={data.get('bid_price', 'N/A')}, ask={data.get('ask_price', 'N/A')}"
                )

        # Check callbacks
        debug_info.append(f"\n🔄 *Callbacks:*")
        debug_info.append(
            f"   • Price callbacks: {len(websocket_manager.price_callbacks)} symbols"
        )
        debug_info.append(
            f"   • BookTicker callbacks: {len(websocket_manager.bookticker_callbacks)} symbols"
        )

        # Test REST API for comparison
        try:
            ticker = await client.book_ticker(pair)
            rest_bid = ticker.get("bidPrice", "N/A")
            rest_ask = ticker.get("askPrice", "N/A")
            debug_info.append(f"\n📡 *REST API Test:*")
            debug_info.append(f"   • {pair}: bid={rest_bid}, ask={rest_ask}")
        except Exception as e:
            debug_info.append(f"\n❌ *REST API Error:* {str(e)}")

        # Test WebSocket subscription
        debug_info.append(f"\n🧪 *WebSocket Test:*")
        if (
            websocket_manager.market_connection
            and not websocket_manager.market_connection["ws"].closed
        ):
            try:
                # Try to subscribe to bookTicker for this pair
                await websocket_manager.subscribe_bookticker_data([pair])
                debug_info.append(f"   • Подписка на {pair} отправлена")

                # Wait a moment and check if we got data
                await asyncio.sleep(2)
                current_data = websocket_manager.get_current_bookticker(pair)
                if current_data:
                    debug_info.append(
                        f"   • ✅ Данные получены: bid={current_data['bid_price']}, ask={current_data['ask_price']}"
                    )
                else:
                    debug_info.append(
                        f"   • ⏳ Данные пока не получены (попробуйте еще раз)"
                    )
            except Exception as e:
                debug_info.append(f"   • ❌ Ошибка подписки: {str(e)}")
        else:
            debug_info.append(f"   • ❌ WebSocket не подключен")

        await message.answer("\n".join(debug_info), parse_mode="Markdown")

    except Exception as e:
        logger.exception(f"Error in WebSocket debug command: {e}")
        await message.answer(f"❌ Ошибка отладки: {str(e)}")


@router.message(Command("ws_test"))
async def test_websocket_subscription(message: Message):
    """
    Test WebSocket subscription and force reconnect if needed.
    """
    try:
        # Get user's trading client and pair
        client, pair = get_user_client(message.from_user.id)

        if not pair:
            await message.answer(
                "❌ Валютная пара не указана. Установите пару в /parameters"
            )
            return

        test_info = []
        test_info.append("🧪 *WebSocket Connection Test*\n")

        # Force reconnect to market data
        test_info.append("1️⃣ *Переподключение к WebSocket...*")

        # Disconnect if connected
        if websocket_manager.market_connection:
            await websocket_manager.disconnect_market()
            test_info.append("   • Существующее соединение закрыто")

        # Connect fresh
        success = await websocket_manager.connect_market_data()
        if success:
            test_info.append("   • ✅ Новое соединение установлено")
        else:
            test_info.append("   • ❌ Ошибка подключения")
            await message.answer("\n".join(test_info), parse_mode="Markdown")
            return

        # Subscribe to both deals and bookTicker
        test_info.append("\n2️⃣ *Подписка на данные...*")

        try:
            # Subscribe to deals
            await websocket_manager.subscribe_market_data([pair])
            test_info.append(f"   • ✅ Подписка на deals для {pair}")

            # Subscribe to bookTicker
            await websocket_manager.subscribe_bookticker_data([pair])
            test_info.append(f"   • ✅ Подписка на bookTicker для {pair}")

        except Exception as e:
            test_info.append(f"   • ❌ Ошибка подписки: {str(e)}")

        # Wait for data
        test_info.append("\n3️⃣ *Ожидание данных...*")
        await asyncio.sleep(5)  # Wait 5 seconds for data

        # Check what we received
        deals_count = len(
            [s for s in websocket_manager.market_subscriptions if s == pair]
        )
        bookticker_count = len(
            [s for s in websocket_manager.bookticker_subscriptions if s == pair]
        )
        cached_data = websocket_manager.get_current_bookticker(pair)

        test_info.append(f"   • Подписки deals: {deals_count}")
        test_info.append(f"   • Подписки bookTicker: {bookticker_count}")

        if cached_data:
            test_info.append(
                f"   • ✅ Кэшированные данные: bid={cached_data['bid_price']}, ask={cached_data['ask_price']}"
            )
        else:
            test_info.append(f"   • ⏳ Кэшированных данных нет")

        # Test callback registration
        test_info.append("\n4️⃣ *Тест callback системы...*")

        callback_called = False

        async def test_callback(symbol, bid_price, ask_price, bid_qty, ask_qty):
            nonlocal callback_called
            callback_called = True
            logger.info(
                f"Test callback called for {symbol}: bid={bid_price}, ask={ask_price}"
            )

        try:
            await websocket_manager.register_bookticker_callback(pair, test_callback)
            test_info.append(f"   • ✅ Callback зарегистрирован")

            # Wait a bit more for potential data
            await asyncio.sleep(3)

            if callback_called:
                test_info.append(f"   • ✅ Callback вызван с данными")
            else:
                test_info.append(f"   • ⏳ Callback пока не вызван (данные не пришли)")

        except Exception as e:
            test_info.append(f"   • ❌ Ошибка callback: {str(e)}")

        await message.answer("\n".join(test_info), parse_mode="Markdown")

    except Exception as e:
        logger.exception(f"Error in WebSocket test command: {e}")
        await message.answer(f"❌ Ошибка теста: {str(e)}")


@router.message(Command("ws_stats"))
async def ws_stats(message: Message):
    """
    Show WebSocket stats:
    - Market WS status and age
    - Total sessions and subscriptions
    - Per-user WS status, age, and time since last message (first 20 users)
    """
    try:
        lines = []
        lines.append("📊 *WebSocket Stats*\n")

        # Market connection
        market = websocket_manager.market_connection
        if market:
            ws = market.get("ws")
            created = market.get("created_at", time.time())
            age = int(time.time() - created)
            state = "ОК" if (ws and not ws.closed) else "Закрыт"
            lines.append(f"🌐 *Market:* {state} (age: {age}s)")
        else:
            lines.append("🌐 *Market:* Не подключен")

        # Aggregated stats
        try:
            stats = await websocket_manager.get_connection_stats()
        except Exception:
            stats = {}

        lines.append(
            f"👥 Пользовательских соединений: {stats.get('user_connections', 0)}"
        )
        lines.append(
            f"🔌 Активных сессий: {stats.get('active_sessions', 0)} | Закрытых: {stats.get('closed_sessions', 0)}"
        )
        lines.append(
            f"📈 Подписки: market={stats.get('total_market_subscriptions', 0)} | bookTicker={stats.get('total_bookticker_subscriptions', 0)}"
        )

        # Per-user connections (limit for readability)
        user_conns = getattr(websocket_manager, "user_connections", {}) or {}
        if user_conns:
            lines.append("\n— Пользователи:")
            shown = 0
            for uid in sorted(user_conns.keys()):
                conn = user_conns.get(uid, {})
                ws = conn.get("ws")
                created = conn.get("created_at", time.time())
                age = int(time.time() - created)
                last_msg = conn.get("last_message_at", 0) or 0
                last_ago = int(time.time() - last_msg) if last_msg else None
                state = "ОК" if (ws and not ws.closed) else "Закрыт"
                part = f"• {uid}: {state}, age={age}s"
                if last_ago is not None:
                    part += f", last={last_ago}s"
                lines.append(part)
                shown += 1
                if shown >= 20:
                    break
            remain = len(user_conns) - shown
            if remain > 0:
                lines.append(f"… и ещё {remain}")

        await message.answer("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.exception(f"Error in WebSocket stats command: {e}")
        await message.answer(f"❌ Ошибка статистики: {str(e)}")
