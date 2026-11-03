"""Read-only connectivity probe; no keys, account access or order submissions."""
import asyncio
import sys

from bot.utils.bingx_rest import BingXRestClient
from bot.utils.bingx_ws import BingXWebSocketManager
from bot.utils.mexc_rest import close_shared_session


async def main(symbol):
    client = BingXRestClient()
    manager = BingXWebSocketManager()
    event = asyncio.Event()
    async def tick(*args):
        print('Live bookTicker:', args)
        event.set()
    try:
        print('Rules:', await client.symbol_rules(symbol))
        print('Price:', await client.ticker_price(symbol))
        print('Book:', await client.book_ticker(symbol))
        await client._ensure_time_offset()
        print('Server time synchronized')
        await manager.register_bookticker_callback(symbol, tick)
        await manager.subscribe_bookticker_data([symbol])
        await asyncio.wait_for(event.wait(), timeout=20)
    finally:
        await manager.disconnect_all()
        await close_shared_session()


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else 'KAS-USDT'))
