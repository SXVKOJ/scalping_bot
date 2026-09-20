"""Торговые правила символа: точность цены/количества, минимумы, комиссии.

Данные берутся из `GET /api/v3/exchangeInfo` и кешируются в памяти процесса.
До появления этого модуля цена округлялась жёстко до 6 знаков, а количество
уходило как float. Для BTCUSDT (quoteAssetPrecision=2) это лишние знаки, а
для монет вроде PEPEUSDT (quoteAssetPrecision=9, baseAssetPrecision=0) —
прямой убыток: округление цены до 6 знаков схлопывает профит в ноль, а
дробное количество биржа не принимает.

Модуль намеренно устроен так, чтобы не ломать торговлю при недоступности
exchangeInfo: `get_symbol_rules` возвращает None, а вызывающий код
откатывается на прежнее поведение.
"""

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Dict, Optional

from bot.logger import logger
from bot.utils.mexc_rest import to_mexc_symbol

# Сколько держать правила символа в кеше (они меняются крайне редко).
_CACHE_TTL_SEC = 12 * 60 * 60

# Значения, на которые откатываемся, если биржа не ответила.
LEGACY_PRICE_DECIMALS = 6
LEGACY_QTY_DECIMALS = 6

_cache: Dict[str, "SymbolRules"] = {}
_cache_time: Dict[str, float] = {}
_locks: Dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class SymbolRules:
    """Ограничения биржи по одному символу."""

    symbol: str
    price_decimals: int  # quoteAssetPrecision — знаков в цене
    qty_decimals: int  # baseAssetPrecision — знаков в количестве
    min_qty: Decimal  # baseSizePrecision — минимальный объём в базовой валюте
    min_notional: Decimal  # quoteAmountPrecision — минимальная сумма ордера
    max_quote_market: Optional[Decimal]  # потолок рыночного ордера в котируемой
    taker_fee: Decimal
    maker_fee: Decimal

    @property
    def round_trip_fee_pct(self) -> Decimal:
        """Суммарная комиссия входа и выхода в процентах."""
        return (self.taker_fee + self.maker_fee) * Decimal(100)


def _dec(value: Any, default: Optional[Decimal] = None) -> Optional[Decimal]:
    try:
        if value is None or value == "":
            return default
        return Decimal(str(value))
    except Exception:
        return default


def _int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_symbol_rules(symbol: str, payload: Dict[str, Any]) -> Optional[SymbolRules]:
    """Разбирает элемент symbols[] из exchangeInfo. None — данные непригодны."""
    try:
        price_decimals = _int(
            payload.get("quoteAssetPrecision", payload.get("quotePrecision")), -1
        )
        qty_decimals = _int(payload.get("baseAssetPrecision"), -1)

        # Санити-проверка: биржа не выдаёт ни отрицательных, ни огромных значений.
        if not (0 <= price_decimals <= 18) or not (0 <= qty_decimals <= 18):
            logger.warning(
                f"[SymbolRules] {symbol}: неправдоподобная точность "
                f"(price={price_decimals}, qty={qty_decimals}), используем запасные значения"
            )
            return None

        return SymbolRules(
            symbol=symbol,
            price_decimals=price_decimals,
            qty_decimals=qty_decimals,
            min_qty=_dec(payload.get("baseSizePrecision"), Decimal(0)) or Decimal(0),
            min_notional=_dec(payload.get("quoteAmountPrecision"), Decimal(0))
            or Decimal(0),
            max_quote_market=_dec(
                payload.get("maxQuoteAmountMarket") or payload.get("maxQuoteAmount")
            ),
            taker_fee=_dec(payload.get("takerCommission"), Decimal(0)) or Decimal(0),
            maker_fee=_dec(payload.get("makerCommission"), Decimal(0)) or Decimal(0),
        )
    except Exception as e:
        logger.error(f"[SymbolRules] Не удалось разобрать правила для {symbol}: {e}")
        return None


async def get_symbol_rules(rest_client, symbol: str) -> Optional[SymbolRules]:
    """Правила символа из кеша или с биржи. None — работать по-старому."""
    symbol = to_mexc_symbol(symbol)
    now = time.time()

    cached = _cache.get(symbol)
    if cached is not None and (now - _cache_time.get(symbol, 0)) < _CACHE_TTL_SEC:
        return cached

    lock = _locks.setdefault(symbol, asyncio.Lock())
    async with lock:
        # Пока ждали блокировку, кеш мог обновить другой вызов.
        cached = _cache.get(symbol)
        if cached is not None and (now - _cache_time.get(symbol, 0)) < _CACHE_TTL_SEC:
            return cached

        try:
            data = await rest_client.exchange_info(symbol)
            symbols = (data or {}).get("symbols") or []
            if not symbols:
                raise ValueError("пустой список symbols")
            rules = parse_symbol_rules(symbol, symbols[0])
        except Exception as e:
            logger.warning(
                f"[SymbolRules] Не удалось получить exchangeInfo для {symbol}: {e}. "
                f"Откат на прежнее поведение."
            )
            rules = None

        if rules is not None:
            _cache[symbol] = rules
            _cache_time[symbol] = time.time()
            logger.info(
                f"[SymbolRules] {symbol}: цена {rules.price_decimals} зн., "
                f"кол-во {rules.qty_decimals} зн., мин. объём {rules.min_qty}, "
                f"мин. сумма {rules.min_notional}, комиссии "
                f"maker {rules.maker_fee} / taker {rules.taker_fee}"
            )
        return rules


# ===== Чистые функции форматирования (покрыты scripts/check_order_math.py) =====


def _plain(value: Decimal) -> str:
    """Decimal → строка без экспоненты (биржа не принимает '1e-05').

    Нули после запятой намеренно сохраняются: значение должно приходить ровно
    в той точности, которую объявила биржа ('100300.00', а не '100300').
    """
    return format(value, "f")


def format_price(value, decimals: int, round_up: bool = False) -> str:
    """Цена с точностью биржи.

    round_up=True для цены тейк-профита: округление вниз срезало бы
    заложенный профит, а лимит на продажу выше — всегда допустим.
    """
    quant = Decimal(1).scaleb(-decimals)
    rounding = ROUND_CEILING if round_up else ROUND_HALF_UP
    return _plain(Decimal(str(value)).quantize(quant, rounding=rounding))


def format_qty(value, decimals: int) -> str:
    """Количество с точностью биржи, всегда вниз — чтобы хватило баланса."""
    quant = Decimal(1).scaleb(-decimals)
    return _plain(Decimal(str(value)).quantize(quant, rounding=ROUND_DOWN))


def floor_qty(value, decimals: int) -> Decimal:
    quant = Decimal(1).scaleb(-decimals)
    return Decimal(str(value)).quantize(quant, rounding=ROUND_DOWN)


def take_profit_price(buy_price, profit_pct, fee_buffer_pct=0) -> Decimal:
    """Цена лимита на продажу с учётом надбавки на комиссии.

    fee_buffer_pct=0 сохраняет прежнее поведение: профит считается «грязным».
    """
    price = Decimal(str(buy_price))
    total_pct = Decimal(str(profit_pct)) + Decimal(str(fee_buffer_pct))
    return price * (Decimal(1) + total_pct / Decimal(100))
