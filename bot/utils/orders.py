"""Исполнение входа и выхода по сделке — единое место для автобая и /buy.

До этого модуля логика ордеров была продублирована в bot/commands/autobuy.py и
bot/commands/trading.py почти дословно, вместе с двумя ошибками:

* цена округлялась до 6 знаков жёстко, хотя у BTCUSDT знаков 2, а у PEPEUSDT 9
  (для последнего округление до 6 знаков схлопывало профит в ноль или в минус);
* количество уходило как float, хотя у части пар оно обязано быть целым.

Плюс продавалось всё купленное количество, тогда как комиссия за покупку
списывается из той же базовой монеты — биржа отвечала «Oversold».
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional

from bot.constants import (
    ENTRY_MAX_SLIPPAGE_PCT,
    ENTRY_ORDER_TYPE,
    PROFIT_FEE_BUFFER_PCT,
)
from bot.logger import logger
from bot.utils.mexc import handle_mexc_response
from bot.utils.mexc_rest import to_mexc_symbol
from bot.utils.symbol_rules import (
    LEGACY_PRICE_DECIMALS,
    floor_qty,
    format_price,
    format_qty,
    get_symbol_rules,
    take_profit_price,
)

# Коды и тексты MEXC, означающие «нечего продавать» — лечится уменьшением
# количества на комиссию, а не повтором того же ордера.
_BALANCE_ERROR_CODES = (30004, 30005, 200004, 10097, 10102)
_BALANCE_ERROR_WORDS = ("oversold", "insufficient", "balance")


@dataclass
class EntryResult:
    """Итог входа в позицию."""

    order_id: Optional[str]
    executed_qty: float
    spent: float
    avg_price: float
    order_type: str

    @property
    def filled(self) -> bool:
        return self.executed_qty > 0 and self.spent > 0


def _is_balance_error(error: Exception) -> bool:
    text = str(error).lower()
    if any(word in text for word in _BALANCE_ERROR_WORDS):
        return True
    return any(f'"code":{code}' in text.replace(" ", "") for code in _BALANCE_ERROR_CODES) or any(
        f"'code':{code}" in text.replace(" ", "") for code in _BALANCE_ERROR_CODES
    )


async def _order_details(rest, symbol: str, order_id: str) -> Dict[str, Any]:
    """Фактические объёмы по ордеру: ответ POST их не содержит."""
    return await rest.query_order(symbol, {"orderId": order_id})


async def execute_entry(
    rest,
    symbol: str,
    quote_amount: float,
    ask_price: Optional[float] = None,
) -> EntryResult:
    """Покупает на quote_amount котируемой валюты.

    ENTRY_ORDER_TYPE=ioc выставляет лимит по ask с запасом
    ENTRY_MAX_SLIPPAGE_PCT и отменяет остаток — худшая цена исполнения
    ограничена. При любой проблеме откатывается на рыночный ордер, чтобы
    не остаться вообще без входа.
    """
    symbol = to_mexc_symbol(symbol)
    rules = await get_symbol_rules(rest, symbol)

    use_ioc = (
        ENTRY_ORDER_TYPE == "ioc"
        and rules is not None
        and ask_price is not None
        and ask_price > 0
    )

    if use_ioc:
        try:
            limit_raw = Decimal(str(ask_price)) * (
                Decimal(1) + Decimal(str(ENTRY_MAX_SLIPPAGE_PCT)) / Decimal(100)
            )
            limit_str = format_price(limit_raw, rules.price_decimals, round_up=True)
            limit_dec = Decimal(limit_str)
            qty_dec = floor_qty(
                Decimal(str(quote_amount)) / limit_dec, rules.qty_decimals
            )
            notional = qty_dec * limit_dec

            if qty_dec <= 0 or qty_dec < rules.min_qty or notional < rules.min_notional:
                logger.info(
                    f"[Entry] {symbol}: IOC не применим "
                    f"(qty={qty_dec}, мин. объём {rules.min_qty}, "
                    f"сумма {notional}, мин. сумма {rules.min_notional}). "
                    f"Используем рыночный ордер."
                )
            else:
                result = await _place_and_read(
                    rest,
                    symbol,
                    "BUY",
                    "LIMIT",
                    {
                        "quantity": format_qty(qty_dec, rules.qty_decimals),
                        "price": limit_str,
                        "timeInForce": "IOC",
                    },
                    "ioc",
                )
                if result.filled:
                    return result
                logger.info(
                    f"[Entry] {symbol}: IOC по {limit_str} не налился "
                    f"(проскальзывание больше {ENTRY_MAX_SLIPPAGE_PCT}%), сигнал пропущен"
                )
                return result
        except Exception as e:
            logger.warning(
                f"[Entry] {symbol}: IOC-вход не удался ({e}), откат на рыночный ордер"
            )

    return await _place_and_read(
        rest, symbol, "BUY", "MARKET", {"quoteOrderQty": quote_amount}, "market"
    )


async def _place_and_read(
    rest,
    symbol: str,
    side: str,
    order_type: str,
    options: Dict[str, Any],
    label: str,
) -> EntryResult:
    """Ставит ордер и подтягивает фактическое исполнение.

    При сетевом сбое ордер НЕ повторяется: вместо этого он ищется по
    клиентскому ID. Слепой повтор мог бы открыть вторую позицию.
    """
    client_id = rest.make_client_order_id()
    options = dict(options)
    options["newClientOrderId"] = client_id

    try:
        order = await rest.new_order(symbol, side, order_type, options)
        handle_mexc_response(order, f"Покупка ({label})")
        order_id = order["orderId"]
    except Exception as e:
        existing = await rest.find_order_by_client_id(symbol, client_id)
        if not existing or not existing.get("orderId"):
            raise
        order_id = existing["orderId"]
        logger.warning(
            f"[Entry] {symbol}: ответ по ордеру не дошёл ({e}), "
            f"но биржа его создала (clientOrderId={client_id}). Повтор не делаем."
        )

    info = await _order_details(rest, symbol, order_id)
    executed_qty = float(info.get("executedQty", 0) or 0)
    spent = float(info.get("cummulativeQuoteQty", 0) or 0)
    avg_price = spent / executed_qty if executed_qty > 0 else 0.0

    return EntryResult(
        order_id=order_id,
        executed_qty=executed_qty,
        spent=spent,
        avg_price=avg_price,
        order_type=label,
    )


async def place_take_profit(
    rest,
    symbol: str,
    quantity: float,
    buy_price: float,
    profit_pct: float,
) -> Dict[str, Any]:
    """Выставляет лимит на продажу. Возвращает {order_id, price, quantity}.

    Цена округляется вверх по точности биржи, количество — вниз. Если биржа
    отвечает нехваткой баланса, количество уменьшается на комиссию: при
    покупке она удерживается из той же базовой монеты.
    """
    symbol = to_mexc_symbol(symbol)
    rules = await get_symbol_rules(rest, symbol)

    sell_price = take_profit_price(buy_price, profit_pct, PROFIT_FEE_BUFFER_PCT)

    if rules is None:
        # Прежнее поведение, если exchangeInfo недоступен.
        price_str = f"{float(sell_price):.{LEGACY_PRICE_DECIMALS}f}"
        attempts = [str(quantity)]
    else:
        price_str = format_price(sell_price, rules.price_decimals, round_up=True)
        base_qty = floor_qty(quantity, rules.qty_decimals)
        fee_adjusted = floor_qty(
            Decimal(str(quantity)) * (Decimal(1) - rules.taker_fee), rules.qty_decimals
        )
        safety = floor_qty(Decimal(str(quantity)) * Decimal("0.998"), rules.qty_decimals)

        attempts = []
        for candidate in (base_qty, fee_adjusted, safety):
            if candidate > 0 and candidate >= rules.min_qty:
                value = format_qty(candidate, rules.qty_decimals)
                if value not in attempts:
                    attempts.append(value)

        if not attempts:
            raise RuntimeError(
                f"Нечего продавать: количество {quantity} меньше минимального "
                f"{rules.min_qty} для {symbol}"
            )

    last_error: Optional[Exception] = None
    for index, qty_str in enumerate(attempts):
        try:
            order = await rest.new_order(
                symbol,
                "SELL",
                "LIMIT",
                {"quantity": qty_str, "price": price_str, "timeInForce": "GTC"},
            )
            handle_mexc_response(order, "Продажа")
            if index > 0:
                logger.info(
                    f"[Exit] {symbol}: лимит выставлен со второй попытки, "
                    f"количество уменьшено до {qty_str} (комиссия в базовой монете)"
                )
            return {
                "order_id": order["orderId"],
                "price": float(price_str),
                "quantity": float(qty_str),
            }
        except Exception as e:
            last_error = e
            if index + 1 < len(attempts) and _is_balance_error(e):
                logger.info(
                    f"[Exit] {symbol}: биржа отклонила продажу {qty_str} "
                    f"по нехватке баланса, пробуем меньшее количество"
                )
                continue
            raise

    raise last_error if last_error else RuntimeError("Не удалось выставить продажу")
