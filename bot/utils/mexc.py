import hmac
import hashlib
import time
import requests
import aiohttp
from users.models import User
from bot.logger import logger
from bot.utils.api_errors import parse_mexc_error
from bot.utils.mexc_rest import MexcRestClient


async def get_actual_order_status_async(user: User, symbol: str, order_id: str) -> str:
    """Async function to get order status using MexcRestClient."""
    try:
        client = client_for_user(user, allow_hold=True)
        response = await client.query_order(symbol, {"orderId": order_id})
        if not isinstance(response, dict):
            logger.error(
                f"⚠️ Ответ не является dict: {type(response)} — {response}",
                extra={"user_id": getattr(user, "telegram_id", None)},
            )
            return None
        return response.get("status", "UNKNOWN")
    except Exception as e:
        logger.exception(
            f"Ошибка при получении статуса ордера {order_id}: {e}",
            extra={"user_id": getattr(user, "telegram_id", None)},
        )
        return None


# Legacy module path retained for existing command imports.
from bot.utils.exchange import client_for_user, ExchangeClient


async def check_mexc_keys_async(api_key, api_secret):
    try:
        await ExchangeClient(api_key, api_secret).account_info()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def get_user_client(telegram_id):
    user = User.objects.get(telegram_id=telegram_id)
    return client_for_user(user, allow_hold=True), user.pair


def handle_mexc_response(response, context=""):
    if isinstance(response, dict) and response.get("code") not in (None, 0, 200):
        raise RuntimeError(f"{context}: {response.get('code')} {response.get('msg')}")
    return response
