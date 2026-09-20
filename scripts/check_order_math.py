"""Оффлайн-проверка округлений цены/количества и расчёта тейк-профита.

Без Django, aiohttp и сети — нужен только Python. Правила символов взяты из
реальных ответов MEXC exchangeInfo. Запуск:

    python scripts/check_order_math.py
"""

import os
import sys
import types
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Заглушки, чтобы импортировать модуль без Django и aiohttp
_logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
    debug=lambda *a, **k: None,
)
sys.modules["bot.logger"] = types.ModuleType("bot.logger")
sys.modules["bot.logger"].logger = _logger
_aiohttp = types.ModuleType("aiohttp")
_aiohttp.ClientSession = object
_aiohttp.ClientTimeout = object
_aiohttp.TCPConnector = object
_aiohttp.ClientConnectorError = Exception
_aiohttp.ClientOSError = Exception
_aiohttp.ServerDisconnectedError = Exception
sys.modules["aiohttp"] = _aiohttp

from bot.utils.symbol_rules import (  # noqa: E402
    format_price,
    format_qty,
    parse_symbol_rules,
    take_profit_price,
)

# Реальные ответы MEXC exchangeInfo (сокращённые)
BTC = {
    "baseAssetPrecision": 8,
    "quoteAssetPrecision": 2,
    "quotePrecision": 2,
    "baseSizePrecision": "0.000001",
    "quoteAmountPrecision": "1",
    "makerCommission": "0",
    "takerCommission": "0.0005",
    "maxQuoteAmountMarket": "4000000",
}
PEPE = {
    "baseAssetPrecision": 0,
    "quoteAssetPrecision": 9,
    "quotePrecision": 9,
    "baseSizePrecision": "0",
    "quoteAmountPrecision": "1",
    "makerCommission": "0",
    "takerCommission": "0.0005",
    "maxQuoteAmountMarket": "2000000",
}

FAILURES = []


def check(name, got, expected):
    ok = got == expected
    print(f"  {'OK ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {expected!r}"))
    if not ok:
        FAILURES.append(name)


def main():
    btc = parse_symbol_rules("BTCUSDT", BTC)
    pepe = parse_symbol_rules("PEPEUSDT", PEPE)
    assert btc and pepe

    print("Правила символов:")
    check("BTC цена, знаков", btc.price_decimals, 2)
    check("BTC кол-во, знаков", btc.qty_decimals, 8)
    check("PEPE цена, знаков", pepe.price_decimals, 9)
    check("PEPE кол-во, знаков", pepe.qty_decimals, 0)
    check("BTC комиссия круга, %", btc.round_trip_fee_pct, Decimal("0.05000"))

    print("\nГлавный баг: профит на дешёвой монете")
    buy = "0.00001234"
    old_style = round(float(buy) * 1.01, 6)  # как было в коде
    new_style = format_price(
        take_profit_price(buy, 1), pepe.price_decimals, round_up=True
    )
    print(f"  покупка по {buy}, профит 1%")
    print(f"  старое округление до 6 знаков -> {old_style:.8f}")
    check("старый расчёт терял профит", old_style <= float(buy), True)
    print(f"  по точности биржи (9 знаков)  -> {new_style}")
    check("новый расчёт даёт профит", float(new_style) > float(buy), True)

    print("\nФормат цены")
    check("BTC 2 знака", format_price("104321.456", btc.price_decimals), "104321.46")
    check(
        "BTC тейк-профит округляется вверх",
        format_price("104321.451", btc.price_decimals, round_up=True),
        "104321.46",
    )
    check("PEPE 9 знаков", format_price("0.0000123456789", pepe.price_decimals), "0.000012346")

    print("\nФормат количества")
    check("PEPE кол-во целое", format_qty("1428571.83", pepe.qty_decimals), "1428571")
    check("BTC кол-во вниз", format_qty("0.000123456789", btc.qty_decimals), "0.00012345")
    check("ноль", format_qty("0", pepe.qty_decimals), "0")

    # Биржа не принимает научную нотацию вида '1e-05' — её не должно быть
    # ни в одном представлении, включая крупные и мелкие значения.
    for raw, decimals in (
        ("0.00001", btc.qty_decimals),
        ("0.0000001", btc.qty_decimals),
        ("10000000", pepe.qty_decimals),
        ("12345678901234", pepe.qty_decimals),
    ):
        check(
            f"нет экспоненты в кол-ве {raw}",
            "e" in format_qty(raw, decimals).lower(),
            False,
        )
        check(
            f"нет экспоненты в цене {raw}",
            "e" in format_price(raw, decimals).lower(),
            False,
        )

    print("\nНадбавка на комиссию")
    без = format_price(take_profit_price("100", "0.3"), btc.price_decimals, round_up=True)
    с = format_price(
        take_profit_price("100", "0.3", "0.05"), btc.price_decimals, round_up=True
    )
    check("профит 0.3% без надбавки", без, "100.30")
    check("профит 0.3% + комиссия 0.05%", с, "100.35")

    print("\nЗащита от мусорных данных")
    check("битая точность -> None", parse_symbol_rules("X", {"quoteAssetPrecision": -1}), None)
    check("пустой ответ -> None", parse_symbol_rules("X", {}), None)

    if FAILURES:
        print(f"\nПРОВАЛЕНО: {len(FAILURES)} — {', '.join(FAILURES)}")
        sys.exit(1)
    print("\nOK: все проверки пройдены")


main()
