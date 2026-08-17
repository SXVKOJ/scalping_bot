import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode


def get_telegram_proxy() -> str:
    try:
        from django.conf import settings

        proxy = getattr(settings, "TELEGRAM_PROXY", "") or ""
    except Exception:
        proxy = ""
    if not proxy:
        proxy = (
            os.getenv("TELEGRAM_PROXY")
            or os.getenv("HTTPS_PROXY")
            or os.getenv("HTTP_PROXY")
            or ""
        )
    return proxy.strip()


def create_bot(token: str, parse_html: bool = True) -> Bot:
    """Create aiogram Bot, optionally via TELEGRAM_PROXY (http/socks)."""
    kwargs = {"token": token}
    if parse_html:
        kwargs["default"] = DefaultBotProperties(parse_mode=ParseMode.HTML)
    proxy = get_telegram_proxy()
    if proxy:
        from aiogram.client.session.aiohttp import AiohttpSession

        kwargs["session"] = AiohttpSession(proxy=proxy)
    return Bot(**kwargs)
