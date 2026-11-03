"""Exchange boundary for the bot; legacy keys must never cross exchanges."""
from django.conf import settings
from asgiref.sync import sync_to_async
from django.db import transaction
import asyncio
import uuid
from bot.utils.mexc_rest import MexcRestClient, split_pair, to_mexc_symbol, close_shared_session
from bot.utils.bingx_rest import BingXRestClient


def require_user_exchange(user, allow_hold=False):
    if user.api_exchange != settings.EXCHANGE:
        raise ValueError(f"Введите API-ключи {settings.EXCHANGE.upper()} через /set_keys перед запуском.")
    if user.execution_hold and not allow_hold:
        raise ValueError(f"Торговля приостановлена: {user.execution_hold}. Сверьте сделки с биржей; затем снимите execution_hold в админке.")


def client_for_user(user, allow_hold=False):
    require_user_exchange(user, allow_hold=allow_hold)
    return ExchangeClient(user.api_key, user.api_secret)


@sync_to_async
def begin_trade(user_id, symbol, autobuy=False):
    """Persist the intent before BUY, including IDs for crash reconciliation."""
    if settings.EXCHANGE != 'bingx':
        return None
    from users.models import User
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        require_user_exchange(user)
        if autobuy and not user.autobuy:
            raise ValueError('Автобай остановлен')
        nonce = uuid.uuid4().hex
        intent = {'buy_id': 'skb_b_' + nonce, 'sell_id': 'skb_s_' + nonce}
        intent['hold'] = f"Trade pending {symbol}; BUY={intent['buy_id']}; SELL={intent['sell_id']}"
        user.execution_hold = intent['hold']
        user.save(update_fields=['execution_hold'])
        return intent


@sync_to_async
def end_trade(user_id, intent, error=None):
    if intent is None:
        return
    from users.models import User
    values = {'execution_hold': ''}
    if error is not None:
        values = {'autobuy': False, 'execution_hold': f"{intent['hold']}; {error}"}
    User.objects.filter(pk=user_id, execution_hold=intent['hold']).update(**values)


def ExchangeClient(api_key="", api_secret=""):
    cls = BingXRestClient if settings.EXCHANGE == "bingx" else MexcRestClient
    return cls(api_key, api_secret)


class EntryNotFilled(RuntimeError):
    """The exchange confirmed a terminal BUY with zero execution."""


async def place_entry(client, symbol, amount, options, ask=None):
    from decimal import Decimal
    from bot.constants import ENTRY_ORDER_TYPE, ENTRY_MAX_SLIPPAGE_PCT
    if ENTRY_ORDER_TYPE not in ('market', 'ioc'):
        raise ValueError('ENTRY_ORDER_TYPE must be market or ioc')
    if ENTRY_ORDER_TYPE == 'ioc':
        if ask is None:
            ask = (await client.book_ticker(symbol))['askPrice']
        limit = Decimal(str(ask)) * (1 + Decimal(str(ENTRY_MAX_SLIPPAGE_PCT)) / 100)
        rules = await client.symbol_rules(symbol)
        tick = Decimal(str(rules['tickSize']))
        from decimal import ROUND_UP
        limit = (limit / tick).to_integral_value(rounding=ROUND_UP) * tick
        qty, limit = await client.prepare_limit_sell(symbol, Decimal(str(amount)) / limit, limit)
        await client.validate_buy_budget(symbol, qty * limit, limit)
        return await client.new_order(symbol, 'BUY', 'LIMIT', {
            'newClientOrderId': options['newClientOrderId'], 'quantity': str(qty),
            'price': str(limit), 'timeInForce': 'IOC',
        })
    return await client.new_order(symbol, 'BUY', 'MARKET', options)


async def buy_fill(client, symbol, order_id):
    """Only repeat reads while an accepted market BUY is settling."""
    delays = (0, 0.15, 0.3, 0.6, 1.0) if isinstance(client, BingXRestClient) else (0,)
    for delay in delays:
        if delay:
            await asyncio.sleep(delay)
        order = await client.query_order(symbol, {'orderId': order_id})
        if not isinstance(client, BingXRestClient) or order.get('status') in ('FILLED', 'CANCELED', 'FAILED'):
            return order
    raise ValueError(f'BUY {order_id} fill not final; reconcile before restarting')


async def sell_terms(client, symbol, order, profit):
    if isinstance(client, BingXRestClient):
        return await client.sell_terms(symbol, order, profit)
    from decimal import Decimal
    qty = Decimal(str(order["executedQty"]))
    price = Decimal(str(order["cummulativeQuoteQty"])) / qty
    return qty, (price * (1 + Decimal(str(profit)) / 100)).quantize(Decimal("0.000001"))
