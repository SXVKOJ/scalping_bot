"""BingX Spot adapter. Internal symbols stay BASEQUOTE for the existing strategy."""
import asyncio
import hashlib
import hmac
import os
import time
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_UP

import aiohttp

from bot.utils.mexc_rest import get_shared_session, split_pair, to_mexc_symbol


class BingXError(RuntimeError):
    def __init__(self, code, message):
        self.code = int(code)
        super().__init__(f"BingX {code}: {message}")


class OrderOutcomeUnknown(RuntimeError):
    """A write may have reached the exchange. Never submit it again blindly."""


def bingx_symbol(pair):
    return "-".join(split_pair(pair))


def decimal_text(value):
    return format(Decimal(str(value)), "f")


class BingXRestClient:
    BASE_URL = "https://open-api.bingx.com"
    # Per API key across client instances, with conservative write pacing.
    _write_locks = {}
    _last_write = {}
    _rules_cache = {}

    def __init__(self, api_key="", api_secret=""):
        self.api_key, self.api_secret = api_key or "", api_secret or ""
        self._time_offset_ms = 0
        self._last_time_sync = 0
        self.order_interval = max(0.1, float(os.getenv('BINGX_ORDER_INTERVAL', '0.25')))
        self._buy_fee_rate = None

    async def close(self):
        pass  # Transport belongs to the process, not to one account.

    @staticmethod
    def sign(params, secret):
        canonical = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()

    async def _ensure_time_offset(self):
        if time.monotonic() - self._last_time_sync < 300:
            return
        start = time.time() * 1000
        data = await self._request("GET", "/openApi/spot/v1/server/time")
        end = time.time() * 1000
        server_time = int(data["serverTime"])
        # The live Spot endpoint returns seconds on some deployments.
        if server_time < 100_000_000_000:
            server_time *= 1000
        self._time_offset_ms = server_time - (start + end) / 2
        self._last_time_sync = time.monotonic()

    async def _request(self, method, path, params=None, signed=False, key_only=False):
        values = {k: str(v) for k, v in (params or {}).items() if v is not None}
        headers = {}
        if signed:
            await self._ensure_time_offset()
            values.update(timestamp=str(int(time.time() * 1000 + self._time_offset_ms)), recvWindow="5000")
            values["signature"] = self.sign(values, self.api_secret)
        if signed or key_only:
            headers["X-BX-APIKEY"] = self.api_key
        session = await get_shared_session()
        # No automatic retry of POST: a timeout does not mean the order failed.
        try:
            async with session.request(
                method, self.BASE_URL + path, params=values, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15, connect=5),
            ) as response:
                body = await response.json(content_type=None)
                if not isinstance(body, dict):
                    raise RuntimeError("BingX: invalid response")
                if response.status >= 500:
                    raise RuntimeError(f"BingX HTTP {response.status}")
                if int(body.get("code", 0)) != 0:
                    raise BingXError(body["code"], body.get("msg", "API error"))
                if response.status >= 400:
                    raise BingXError(response.status, body.get("msg", "HTTP error"))
                return body.get("data", body)
        except BingXError:
            raise
        except Exception as exc:
            # Avoid leaking a signed URL or API key from aiohttp exceptions.
            raise RuntimeError(f"BingX transport error ({type(exc).__name__})") from None

    async def symbol_rules(self, symbol):
        key = bingx_symbol(symbol)
        cached = self._rules_cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        data = await self._request("GET", "/openApi/spot/v1/common/symbols", {"symbol": key})
        rules = next((r for r in data["symbols"] if r["symbol"] == key), None)
        if not rules or int(rules["status"]) != 1:
            raise ValueError(f"BingX: spot pair {key} unavailable")
        self._rules_cache[key] = (time.monotonic(), rules)
        return rules

    async def prepare_limit_sell(self, symbol, quantity, price):
        rules = await self.symbol_rules(symbol)
        if rules.get("apiStateSell") is False:
            raise ValueError("BingX: API SELL disabled for this pair")
        step, tick = Decimal(str(rules["stepSize"])), Decimal(str(rules["tickSize"]))
        qty = (Decimal(str(quantity)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        price = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_UP) * tick
        if not qty.is_finite() or not price.is_finite() or qty <= 0 or price <= 0:
            raise ValueError("Invalid price or quantity")
        for field, value in (("Qty", qty), ("Notional", qty * price)):
            minimum, maximum = Decimal(str(rules.get("min" + field, 0))), Decimal(str(rules.get("max" + field, 0)))
            if value < minimum or (maximum > 0 and value > maximum):
                raise ValueError(f"BingX: {field} {value} outside [{minimum}, {maximum}]")
        return qty, price

    @staticmethod
    def normalize_order(data):
        order = dict(data.get("order", data))
        if "orderId" in order:
            order["orderId"] = str(order["orderId"])
        if "symbol" in order:
            order["symbol"] = to_mexc_symbol(order["symbol"])
        if order.get("status") == "PENDING":
            order["status"] = "NEW"
        return order

    async def ticker_price(self, symbol):
        data = await self._request("GET", "/openApi/spot/v2/quote/price", {"symbol": bingx_symbol(symbol)})
        if isinstance(data, list):
            data = data[0]
        return {"symbol": to_mexc_symbol(symbol), "price": data["price"]}

    async def book_ticker(self, symbol):
        data = await self._request("GET", "/openApi/spot/v2/quote/bookTicker", {"symbol": bingx_symbol(symbol)})
        return data[0] if isinstance(data, list) else data

    async def account_info(self):
        return await self._request("GET", "/openApi/spot/v1/account/balance", signed=True)

    async def open_orders(self, symbol):
        data = await self._request("GET", "/openApi/spot/v1/trade/openOrders", {"symbol": bingx_symbol(symbol)}, signed=True)
        return [self.normalize_order(o) for o in data["orders"]]

    async def query_order(self, symbol, options):
        data = await self._request("GET", "/openApi/spot/v1/trade/query", {"symbol": bingx_symbol(symbol), **options}, signed=True)
        return self.normalize_order(data)

    async def new_order(self, symbol, side, order_type, options):
        options = dict(options or {})
        if order_type == "LIMIT" and side == "SELL":
            qty, price = await self.prepare_limit_sell(symbol, options["quantity"], options["price"])
            options.update(quantity=decimal_text(qty), price=decimal_text(price))
        rules = await self.symbol_rules(symbol)
        if side == "BUY" and rules.get("apiStateBuy") is False:
            raise ValueError("BingX: API BUY disabled for this pair")
        if side == "BUY" and order_type == "MARKET":
            amount = Decimal(str(options["quoteOrderQty"]))
            if amount < Decimal(str(rules["minNotional"])) or amount > Decimal(str(rules.get("maxMarketNotional", rules["maxNotional"]))):
                raise ValueError("BingX: buy amount outside pair limits")
        client_id = options.setdefault("newClientOrderId", "skb_" + uuid.uuid4().hex)
        params = {"symbol": bingx_symbol(symbol), "side": side, "type": order_type, **options}
        key = hashlib.sha256(self.api_key.encode()).hexdigest()
        lock = self._write_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await asyncio.sleep(max(0, self.order_interval - (time.monotonic() - self._last_write.get(key, 0))))
            self._last_write[key] = time.monotonic()
            try:
                data = await self._request("POST", "/openApi/spot/v1/trade/order", params, signed=True)
                order = self.normalize_order(data)
                if not order.get("orderId"):
                    raise RuntimeError("Missing order ID")
                return order
            except Exception as exc:
                if isinstance(exc, BingXError) and exc.code != 100500:
                    raise
                # Recover by client ID. Never repeat the POST after an ambiguous outcome.
                try:
                    return await self.query_order(symbol, {"clientOrderID": client_id})
                except Exception:
                    raise OrderOutcomeUnknown(f"BingX order outcome unknown; clientOrderID={client_id}. Reconcile before restarting.") from None

    async def commission_rate(self, symbol):
        rates = await self._request("GET", "/openApi/spot/v1/user/commissionRate", {"symbol": bingx_symbol(symbol)}, signed=True)
        self._buy_fee_rate = max(Decimal(str(rates['makerCommissionRate'])), Decimal(str(rates['takerCommissionRate'])))
        return rates

    async def validate_buy_budget(self, symbol, amount, ask):
        """Reject a known-too-small BUY before acquiring an unsellable position."""
        rules = await self.symbol_rules(symbol)
        if self._buy_fee_rate is None:
            rates = await self.commission_rate(symbol)
            self._buy_fee_rate = max(Decimal(str(rates['makerCommissionRate'])), Decimal(str(rates['takerCommissionRate'])))
        price, budget = Decimal(str(ask)), Decimal(str(amount))
        if price <= 0 or budget <= 0 or not 0 <= self._buy_fee_rate < 1:
            raise ValueError('Invalid BUY budget, ask or commission')
        step = Decimal(str(rules['stepSize']))
        net_qty = ((budget / price * (1 - self._buy_fee_rate)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        if net_qty < Decimal(str(rules['minQty'])) or net_qty * price < Decimal(str(rules['minNotional'])):
            raise ValueError(f"BingX: buy amount {budget} is too small for the subsequent SELL (minQty={rules['minQty']}). Increase /parameters buy amount.")

    async def listen_key(self, method="POST", key=None):
        return await self._request(method, "/openApi/user/auth/userDataStream", {"listenKey": key}, key_only=True)

    async def sell_terms(self, symbol, order, profit_percent):
        if order.get('status') not in ('FILLED', 'CANCELED'):
            raise ValueError('BUY fill is not final; reconcile before SELL')
        qty = Decimal(str(order["executedQty"]))
        spent = Decimal(str(order["cummulativeQuoteQty"]))
        base, quote = split_pair(symbol)
        if 'fee' not in order:
            raise ValueError('BUY commission is missing; reconcile before SELL')
        fee = abs(Decimal(str(order['fee'])))
        asset = order.get("feeAsset", "")
        if fee and asset not in (base, quote):
            raise ValueError("BingX charged fee in a third asset; review fill before SELL")
        if asset == base:
            qty -= fee
        if asset == quote:
            spent += fee
        # Actual buy fee is included above; reserve the larger maker/taker SELL fee.
        rates = await self.commission_rate(symbol)
        rate = max(Decimal(str(rates["makerCommissionRate"])), Decimal(str(rates["takerCommissionRate"])))
        if not 0 <= rate < 1 or qty <= 0:
            raise ValueError("Invalid BingX fee or net quantity")
        rules = await self.symbol_rules(symbol)
        step = Decimal(str(rules['stepSize']))
        qty = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
        if qty <= 0:
            raise ValueError('Net quantity below BingX step')
        target = spent * (1 + Decimal(str(profit_percent)) / 100) / (qty * (1 - rate))
        return await self.prepare_limit_sell(symbol, qty, target)
