"""Оффлайн-проверка исполнения ордеров на фейковой бирже.

Проверяет bot/utils/orders.py без Django, aiohttp и сети: рыночный вход,
IOC-вход, откаты при сбоях, повтор продажи при нехватке баланса и поведение
при недоступном exchangeInfo. Запуск:

    python scripts/check_order_flow.py
"""

import asyncio
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


_stub(
    "bot.logger",
    logger=types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    ),
)
_stub(
    "aiohttp",
    ClientSession=object,
    ClientTimeout=object,
    TCPConnector=object,
    ClientConnectorError=Exception,
    ClientOSError=Exception,
    ServerDisconnectedError=Exception,
)


def _handle_mexc_response(response, context=""):
    if isinstance(response, dict) and response.get("code") and response.get("code") != 200:
        raise Exception(f"[MEXC ERROR] {context}: {response}")
    return response


_stub("bot.utils.mexc", handle_mexc_response=_handle_mexc_response)
_stub(
    "bot.constants",
    ENTRY_ORDER_TYPE="market",
    ENTRY_MAX_SLIPPAGE_PCT=0.3,
    PROFIT_FEE_BUFFER_PCT=0,
)

import bot.utils.orders as orders  # noqa: E402
import bot.utils.symbol_rules as symbol_rules  # noqa: E402

BTC_INFO = {
    "symbols": [
        {
            "baseAssetPrecision": 8,
            "quoteAssetPrecision": 2,
            "baseSizePrecision": "0.000001",
            "quoteAmountPrecision": "1",
            "makerCommission": "0",
            "takerCommission": "0.0005",
            "maxQuoteAmountMarket": "4000000",
        }
    ]
}

FAILURES = []


def check(name, got, expected):
    ok = got == expected
    print(f"  {'OK ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {expected!r}"))
    if not ok:
        FAILURES.append(name)


class FakeExchange:
    """Минимальная имитация MEXC: фиксирует все отправленные ордера."""

    BASE_URL = "https://api.mexc.com"

    def __init__(self, fill_ratio=1.0, sell_errors=None, post_raises=None, info=BTC_INFO):
        self.orders = []
        self.fill_ratio = fill_ratio
        self.sell_errors = list(sell_errors or [])
        self.post_raises = post_raises
        self.info = info
        self.accepted_client_ids = set()

    @staticmethod
    def make_client_order_id(prefix="sb"):
        return f"{prefix}-test-{len(prefix)}"

    async def exchange_info(self, symbol):
        if self.info is None:
            raise RuntimeError("exchangeInfo недоступен")
        return self.info

    async def new_order(self, symbol, side, order_type, options):
        record = {"symbol": symbol, "side": side, "type": order_type, **options}
        if side == "SELL" and self.sell_errors:
            self.orders.append({**record, "rejected": True})
            raise Exception(self.sell_errors.pop(0))
        self.orders.append(record)
        if self.post_raises and side == "BUY":
            # Биржа ордер приняла, но ответ не дошёл до клиента.
            self.accepted_client_ids.add(options.get("newClientOrderId"))
            raise TimeoutError("ответ не дошёл")
        return {"orderId": f"id{len(self.orders)}"}

    async def query_order(self, symbol, options):
        if "origClientOrderId" in options:
            cid = options["origClientOrderId"]
            if cid not in self.accepted_client_ids:
                raise Exception("order not found")
            return {"orderId": "recovered", "executedQty": "0.001", "cummulativeQuoteQty": "100"}
        qty = 0.001 * self.fill_ratio
        return {
            "orderId": options.get("orderId"),
            "executedQty": str(qty),
            "cummulativeQuoteQty": str(qty * 100000),
        }

    async def find_order_by_client_id(self, symbol, client_order_id):
        try:
            return await self.query_order(symbol, {"origClientOrderId": client_order_id})
        except Exception:
            return None

    def buys(self):
        return [o for o in self.orders if o["side"] == "BUY"]

    def sells(self):
        return [o for o in self.orders if o["side"] == "SELL" and not o.get("rejected")]


def reset_cache():
    symbol_rules._cache.clear()
    symbol_rules._cache_time.clear()


async def main():
    print("Рыночный вход (по умолчанию):")
    reset_cache()
    orders.ENTRY_ORDER_TYPE = "market"
    ex = FakeExchange()
    entry = await orders.execute_entry(ex, "BTCUSDT", 100.0, ask_price=100000.0)
    check("тип ордера", ex.buys()[0]["type"], "MARKET")
    check("сумма в котируемой", ex.buys()[0]["quoteOrderQty"], 100.0)
    check("исполнен", entry.filled, True)
    check("средняя цена", entry.avg_price, 100000.0)

    print("\nIOC-вход:")
    reset_cache()
    orders.ENTRY_ORDER_TYPE = "ioc"
    ex = FakeExchange()
    entry = await orders.execute_entry(ex, "BTCUSDT", 100.0, ask_price=100000.0)
    buy = ex.buys()[0]
    check("тип ордера", buy["type"], "LIMIT")
    check("time in force", buy["timeInForce"], "IOC")
    check("лимит = ask + 0.3%", buy["price"], "100300.00")
    check("количество вниз по точности", buy["quantity"], "0.00099700")
    check("исполнен", entry.filled, True)

    print("\nIOC не налился — это не ошибка:")
    reset_cache()
    ex = FakeExchange(fill_ratio=0.0)
    entry = await orders.execute_entry(ex, "BTCUSDT", 100.0, ask_price=100000.0)
    check("не исполнен", entry.filled, False)
    check("тип для вызывающего кода", entry.order_type, "ioc")
    check("повторных ордеров нет", len(ex.buys()), 1)

    print("\nIOC отвергнут биржей — откат на рыночный:")
    reset_cache()

    class RejectIoc(FakeExchange):
        async def new_order(self, symbol, side, order_type, options):
            if order_type == "LIMIT" and options.get("timeInForce") == "IOC":
                self.orders.append({**options, "side": side, "type": order_type, "rejected": True})
                raise Exception('{"code":30010,"msg":"invalid price"}')
            return await FakeExchange.new_order(self, symbol, side, order_type, options)

    ex = RejectIoc()
    entry = await orders.execute_entry(ex, "BTCUSDT", 100.0, ask_price=100000.0)
    check("вход всё равно состоялся", entry.filled, True)
    check("исполнен рыночным", entry.order_type, "market")

    print("\nОбрыв сети после отправки ордера:")
    reset_cache()
    orders.ENTRY_ORDER_TYPE = "market"
    ex = FakeExchange(post_raises=True)
    entry = await orders.execute_entry(ex, "BTCUSDT", 100.0)
    check("ордер найден по клиентскому ID", entry.filled, True)
    check("дубликат НЕ отправлен", len(ex.buys()), 1)

    print("\nПродажа: комиссия списана в базовой монете:")
    reset_cache()
    ex = FakeExchange(sell_errors=['{"code":30005,"msg":"Oversold"}'])
    result = await orders.place_take_profit(ex, "BTCUSDT", 0.001, 100000.0, 1.0)
    check("лимит выставлен со второй попытки", len(ex.sells()), 1)
    check("первая попытка — всё количество", ex.orders[-2]["quantity"], "0.00100000")
    check("вторая — минус комиссия", ex.sells()[0]["quantity"], "0.00099950")
    check("цена тейк-профита", result["price"], 101000.0)

    print("\nexchangeInfo недоступен — прежнее поведение:")
    reset_cache()
    ex = FakeExchange(info=None)
    result = await orders.place_take_profit(ex, "BTCUSDT", 0.001, 100000.0, 1.0)
    check("цена в старом формате 6 знаков", ex.sells()[0]["price"], "101000.000000")
    check("сделка всё равно защищена лимитом", result["order_id"] is not None, True)

    if FAILURES:
        print(f"\nПРОВАЛЕНО: {len(FAILURES)} — {', '.join(FAILURES)}")
        sys.exit(1)
    print("\nOK: все проверки пройдены")


asyncio.run(main())
