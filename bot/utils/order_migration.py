"""Restartable sell restoration; no withdrawals, cancellations or market orders."""
import asyncio
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.db import transaction

from bot.utils.bingx_rest import BingXError
from bot.utils.mexc_rest import split_pair, to_mexc_symbol
from users.models import Deal, OrderMigration, User


def positive(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError('Expected a positive finite decimal')
    return number


async def build_plan(document, user, client, target_pair, conversion=None):
    if document.get('version') != 1 or document.get('telegram_id') != user.telegram_id:
        raise ValueError('Wrong export version or Telegram user')
    if user.autobuy:
        raise ValueError('Stop autobuy before restoration')
    if user.api_exchange != 'bingx':
        raise ValueError('Set and verify BingX API keys first')
    if user.execution_hold:
        raise ValueError('Resolve the pending BUY/SELL before restoration')
    base, quote = split_pair(target_pair)
    rows, seen = [], set()
    sell_fee_rate = None
    for entry in document['orders']:
        pk = int(entry['deal_id'])
        if pk in seen:
            raise ValueError('Duplicate deal_id in file')
        seen.add(pk)
        source = await sync_to_async(Deal.objects.get)(pk=pk, user=user, exchange='mexc')
        if source.status == 'FILLED':
            raise ValueError(f'Deal {pk} already sold')
        if entry.get('confirmed_canceled') is not True:
            raise ValueError(f'Deal {pk}: confirm old order canceled')
        remaining = positive(entry.get('remaining_quantity'))
        if remaining > source.quantity:
            raise ValueError(f'Deal {pk}: remaining quantity exceeds original')
        source_base, source_quote = split_pair(source.symbol)
        if source_base != base:
            raise ValueError('Base asset cannot change during restoration')
        if source_quote != quote and conversion is None:
            raise ValueError('Quote asset changed: explicit --quote-conversion required (target quote per source quote)')
        factor = positive(conversion) if source_quote != quote else Decimal(1)
        if sell_fee_rate is None:
            rates = await client.commission_rate(target_pair)
            sell_fee_rate = max(Decimal(str(rates['makerCommissionRate'])), Decimal(str(rates['takerCommissionRate'])))
            if not 0 <= sell_fee_rate < 1:
                raise ValueError('Invalid BingX commission rate')
        old_target = positive(source.sell_price) * factor
        buy_price = positive(source.buy_price) * factor
        # Never lower an old target; cover the recorded cost and the new SELL fee.
        target = max(old_target, buy_price / (1 - sell_fee_rate))
        quantity, price = await client.prepare_limit_sell(target_pair, remaining, target)
        # Keep all journal values exact in the existing Decimal(20,8) schema.
        if quantity != quantity.quantize(Decimal('0.00000001')) or price != price.quantize(Decimal('0.00000001')):
            raise ValueError('Pair precision exceeds the database precision (8 decimal places)')
        done = await sync_to_async(Deal.objects.filter(migrated_from=source).first)()
        journal = await sync_to_async(OrderMigration.objects.filter(source=source).first)()
        symbol = to_mexc_symbol(target_pair)
        if journal and (journal.symbol != symbol or journal.quantity != quantity or journal.price != price):
            raise ValueError(f'Deal {pk}: plan changed after preparation')
        existing = None
        if journal and journal.state in ('SUBMITTING', 'UNKNOWN'):
            # A timeout/kill may have happened after acceptance, even if no ID was saved.
            existing = await client.query_order(symbol, {'clientOrderID': journal.client_order_id})
            validate_existing(existing, journal)
        rows.append({'source': source, 'symbol': symbol, 'quantity': quantity, 'price': price,
                     'buy_price': buy_price, 'done': done, 'existing': existing,
                     'old_target': old_target, 'dust': remaining - quantity})
    return rows


def validate_existing(order, journal):
    if (not order.get('orderId') or order.get('side') != 'SELL' or order.get('type') != 'LIMIT'
            or order.get('status') not in ('NEW', 'PARTIALLY_FILLED', 'FILLED')
            or to_mexc_symbol(order.get('symbol')) != journal.symbol
            or Decimal(str(order['origQty'])) != journal.quantity
            or Decimal(str(order['price'])) != journal.price):
        raise ValueError('Recovered order does not match migration journal')


async def preflight(rows, client, allow_marketable=False):
    pending = [r for r in rows if not r['done'] and not r['existing']]
    if not pending:
        return
    account = await client.account_info()
    free = {b['asset']: Decimal(str(b['free'])) for b in account['balances']}
    needed = {}
    for row in pending:
        base = split_pair(row['symbol'])[0]
        needed[base] = needed.get(base, Decimal(0)) + row['quantity']
    for base, qty in needed.items():
        if free.get(base, Decimal(0)) < qty:
            raise ValueError(f'Insufficient {base}: need {qty}, free {free.get(base, 0)}. No orders submitted.')
    if not allow_marketable:
        books = {}
        for row in pending:
            if row['symbol'] not in books:
                books[row['symbol']] = await client.book_ticker(row['symbol'])
            if row['price'] <= positive(books[row['symbol']]['bidPrice']):
                raise ValueError('A restored SELL could execute immediately. Review price or explicitly use --allow-marketable.')


@sync_to_async
def claim(row):
    with transaction.atomic():
        # Source row serializes concurrent restores even before the journal exists.
        source = Deal.objects.select_for_update().get(pk=row['source'].pk)
        user = User.objects.select_for_update().get(pk=source.user_id)
        if user.autobuy or user.execution_hold or user.api_exchange != 'bingx':
            raise ValueError('Autobuy became active; stop restoration')
        journal, _ = OrderMigration.objects.get_or_create(source=source, defaults={
            'client_order_id': f'skb_migrate_{source.user_id}_{source.pk}', 'symbol': row['symbol'],
            'quantity': row['quantity'], 'price': row['price'],
        })
        if (journal.symbol, journal.quantity, journal.price) != (row['symbol'], row['quantity'], row['price']):
            raise ValueError('Migration plan changed')
        send = journal.state == 'PREPARED'
        if send:
            journal.state = 'SUBMITTING'
            journal.save(update_fields=['state', 'updated_at'])
        return journal, send


@sync_to_async
def complete(row, journal, order):
    validate_existing(order, journal)
    with transaction.atomic():
        journal = OrderMigration.objects.select_for_update().get(pk=journal.pk)
        source = row['source']
        Deal.objects.get_or_create(migrated_from=source, defaults={
            'user_id': source.user_id, 'exchange': 'bingx', 'symbol': row['symbol'],
            'order_id': str(order['orderId']), 'quantity': row['quantity'],
            'buy_price': row['buy_price'], 'sell_price': row['price'],
            'user_order_number': source.user_order_number, 'is_autobuy': source.is_autobuy,
            'status': order.get('status', 'NEW'),
        })
        journal.state, journal.order_id = 'COMPLETE', str(order['orderId'])
        journal.save(update_fields=['state', 'order_id', 'updated_at'])


async def execute_plan(rows, client, allow_marketable=False):
    await preflight(rows, client, allow_marketable)
    for row in rows:
        if row['done']:
            continue
        if not allow_marketable and not row['existing']:
            book = await client.book_ticker(row['symbol'])
            if row['price'] <= positive(book['bidPrice']):
                raise ValueError('Market crossed restoration price; stopped')
        journal, send = await claim(row)
        if not send:
            order = row['existing'] or await client.query_order(row['symbol'], {'clientOrderID': journal.client_order_id})
        else:
            try:
                order = await client.new_order(row['symbol'], 'SELL', 'LIMIT', {
                    'quantity': str(row['quantity']), 'price': str(row['price']),
                    'timeInForce': 'GTC', 'newClientOrderId': journal.client_order_id,
                })
            except Exception as exc:
                state = 'PREPARED' if isinstance(exc, BingXError) else 'UNKNOWN'
                await sync_to_async(OrderMigration.objects.filter(pk=journal.pk).update)(state=state)
                raise
        await complete(row, journal, order)
