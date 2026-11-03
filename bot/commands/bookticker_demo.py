import asyncio
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from bot.utils.mexc import get_user_client
from bot.utils.websocket_manager import websocket_manager
from bot.logger import logger

router = Router()


@router.message(Command("spread"))
async def get_spread_info(message: Message):
    """
    Command to show real-time bid/ask spread information for user's trading pair.
    Demonstrates bookTicker WebSocket functionality.
    """
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or str(user_id)
    
    try:
        # Get user's trading client and pair
        client, pair = get_user_client(message.from_user.id)
        
        if not pair:
            await message.answer("❌ Валютная пара не указана. Установите пару в /parameters")
            return

        # Get initial REST API data for comparison
        try:
            ticker = await client.book_ticker(pair)
            rest_bid = ticker.get('bidPrice', 'N/A')
            rest_ask = ticker.get('askPrice', 'N/A')
        except Exception as e:
            logger.warning(f"Could not get REST bookTicker data: {e}")
            rest_bid = rest_ask = 'N/A'

        # Check if we have real-time bookTicker data
        current_bookticker = websocket_manager.get_current_bookticker(pair)
        
        if current_bookticker:
            bid_price = current_bookticker['bid_price']
            ask_price = current_bookticker['ask_price']
            bid_qty = current_bookticker['bid_qty']
            ask_qty = current_bookticker['ask_qty']
            
            # Calculate spread
            try:
                bid = float(bid_price)
                ask = float(ask_price)
                spread = ask - bid
                spread_percentage = (spread / bid) * 100 if bid > 0 else 0
                mid_price = (bid + ask) / 2
                
                response_text = (
                    f"📊 *Спред для {pair}* (Real-time)\n\n"
                    f"🟢 *Лучший бид:* `{bid_price}` (кол-во: {bid_qty})\n"
                    f"🔴 *Лучший аск:* `{ask_price}` (кол-во: {ask_qty})\n"
                    f"📏 *Спред:* `{spread:.8f}` ({spread_percentage:.4f}%)\n"
                    f"⚖️ *Средняя цена:* `{mid_price:.6f}`\n\n"
                    f"📡 *REST API (для сравнения):*\n"
                    f"Бид: `{rest_bid}` | Аск: `{rest_ask}`"
                )
            except (ValueError, TypeError):
                response_text = (
                    f"📊 *Спред для {pair}*\n\n"
                    f"🟢 *Бид:* `{bid_price}`\n"
                    f"🔴 *Аск:* `{ask_price}`\n"
                    f"❌ Ошибка расчета спреда"
                )
        else:
            response_text = (
                f"📊 *Спред для {pair}*\n\n"
                f"📡 *REST API данные:*\n"
                f"🟢 *Бид:* `{rest_bid}`\n"
                f"🔴 *Аск:* `{rest_ask}`\n\n"
                f"⏳ WebSocket bookTicker данные пока недоступны"
            )

        sent_message = await message.answer(response_text, parse_mode='Markdown')
        
        # Subscribe to bookTicker if not already subscribed
        if not websocket_manager.market_connection:
            await websocket_manager.connect_market_data()
            
        if pair not in websocket_manager.bookticker_subscriptions:
            await websocket_manager.subscribe_bookticker_data([pair])

        # Create callback to update the message with real-time data
        async def update_spread_message(symbol, bid_price, ask_price, bid_qty, ask_qty):
            try:
                bid = float(bid_price)
                ask = float(ask_price)
                spread = ask - bid
                spread_percentage = (spread / bid) * 100 if bid > 0 else 0
                mid_price = (bid + ask) / 2
                
                updated_text = (
                    f"📊 *Спред для {symbol}* (Real-time ✅)\n\n"
                    f"🟢 *Лучший бид:* `{bid_price}` (кол-во: {bid_qty})\n"
                    f"🔴 *Лучший аск:* `{ask_price}` (кол-во: {ask_qty})\n"
                    f"📏 *Спред:* `{spread:.8f}` ({spread_percentage:.4f}%)\n"
                    f"⚖️ *Средняя цена:* `{mid_price:.6f}`\n\n"
                    f"🔄 *Обновлено в реальном времени*"
                )
                
                await sent_message.edit_text(updated_text, parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Error updating spread message: {e}")

        # Register callback for real-time updates
        await websocket_manager.register_bookticker_callback(pair, update_spread_message)
        
        # Remove callback after 30 seconds to prevent accumulation
        async def cleanup_callback():
            await asyncio.sleep(30)
            await websocket_manager.unregister_bookticker_callback(pair, update_spread_message)
            
            try:
                final_text = sent_message.text + f"\n\n⏰ *Автообновление завершено*"
                await sent_message.edit_text(final_text, parse_mode='Markdown')
            except:
                pass  # Message might be too old to edit
        
        asyncio.create_task(cleanup_callback())
        
    except Exception as e:
        logger.exception(f"Error in spread command for user {user_id}: {e}")
        await message.answer(f"❌ Ошибка получения спреда: {str(e)}")


@router.message(Command("bookticker"))
async def show_bookticker_status(message: Message):
    """
    Show current bookTicker subscription status and available data.
    """
    try:
        if not websocket_manager.market_connection:
            await message.answer("❌ WebSocket соединение с рынком не установлено")
            return
            
        bookticker_count = len(websocket_manager.bookticker_subscriptions)
        cached_count = len(websocket_manager.current_bookticker)
        
        status_text = (
            f"📊 *BookTicker Статус*\n\n"
            f"🔗 *WebSocket:* {'✅ Подключен' if websocket_manager.market_connection else '❌ Отключен'}\n"
            f"📈 *Подписки:* {bookticker_count} символов\n"
            f"💾 *Кэш данных:* {cached_count} символов\n\n"
        )
        
        if websocket_manager.bookticker_subscriptions:
            status_text += f"*Подписанные символы:*\n"
            for symbol in websocket_manager.bookticker_subscriptions:
                status_text += f"• {symbol}\n"
                
        if websocket_manager.current_bookticker:
            status_text += f"\n*Доступные данные:*\n"
            for symbol, data in list(websocket_manager.current_bookticker.items())[:5]:  # Show max 5
                bid = data.get('bid_price', 'N/A')
                ask = data.get('ask_price', 'N/A')
                status_text += f"• {symbol}: {bid} / {ask}\n"
                
            if len(websocket_manager.current_bookticker) > 5:
                status_text += f"... и ещё {len(websocket_manager.current_bookticker) - 5}"
        
        await message.answer(status_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.exception(f"Error in bookticker status command: {e}")
        await message.answer(f"❌ Ошибка получения статуса: {str(e)}")
