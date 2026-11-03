import csv
import json
from decimal import Decimal
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from bot.utils.mexc_rest import to_mexc_symbol
from users.models import Deal


def apply_history(rows, history, columns, canceled_statuses, filled_statuses):
    """Join a user-supplied exchange export by exact order ID, never by price."""
    indexed = {}
    for entry in history:
        if any(name not in entry for name in columns.values()):
            raise ValueError('History CSV columns do not match --*-column options')
        order_id = entry[columns['id']].strip()
        if not order_id or order_id in indexed:
            raise ValueError('Missing or duplicate order ID in history CSV')
        indexed[order_id] = entry
    canceled = {s.strip().upper() for s in canceled_statuses}
    filled = {s.strip().upper() for s in filled_statuses}
    if canceled & filled:
        raise ValueError('Canceled and filled status values overlap')
    verified = []
    for row in rows:
        entry = indexed.get(row['old_order_id'])
        if entry is None:
            raise ValueError(f"Order {row['old_order_id']} missing from history CSV")
        if to_mexc_symbol(entry[columns['symbol']]) != to_mexc_symbol(row['symbol']):
            raise ValueError('History symbol does not match database')
        # Require exact dot-decimal values; never guess comma/thousands separators.
        original = Decimal(entry[columns['quantity']].strip())
        executed = Decimal(entry[columns['filled']].strip())
        if (not original.is_finite() or not executed.is_finite() or original <= 0
                or executed < 0 or executed > original or original != Decimal(row['original_quantity'])):
            raise ValueError('History quantities do not match database or are invalid')
        status = entry[columns['status']].strip().upper()
        if status in filled:
            if executed != original:
                raise ValueError('FILLED history row has incomplete quantity')
            continue
        if status not in canceled:
            raise ValueError(f"Order {row['old_order_id']}: cancellation not confirmed in CSV")
        if original == executed:
            continue
        verified.append({**row, 'remaining_quantity': str(original - executed),
                         'confirmed_canceled': True, 'verification': 'history_csv',
                         'source_executed_quantity': str(executed)})
    return verified


class Command(BaseCommand):
    help = 'Export MEXC sell-order candidates without API keys. Review canceled orders and remaining quantities.'

    def add_arguments(self, parser):
        parser.add_argument('--telegram-id', type=int, required=True)
        parser.add_argument('--output', required=True)
        parser.add_argument('--history-csv', help='Optional MEXC order-history export; IDs must be exact text')
        parser.add_argument('--csv-delimiter', default=',')
        parser.add_argument('--id-column', default='orderId')
        parser.add_argument('--symbol-column', default='symbol')
        parser.add_argument('--quantity-column', default='origQty')
        parser.add_argument('--filled-column', default='executedQty')
        parser.add_argument('--status-column', default='status')
        parser.add_argument('--canceled-status', action='append', help='Exact CSV status for canceled orders; repeat if needed')
        parser.add_argument('--filled-status', action='append', help='Exact CSV status for filled orders')

    def handle(self, *args, **opts):
        orders = Deal.objects.filter(
            user__telegram_id=opts['telegram_id'], exchange='mexc',
            status__in=['NEW', 'PARTIALLY_FILLED', 'CANCELED', 'PARTIALLY_CANCELED', 'SKIPPED'],
            migrated_deal__isnull=True,
        ).order_by('id')
        rows = [{
            'deal_id': d.pk, 'old_order_id': d.order_id, 'symbol': d.symbol,
            'buy_price': str(d.buy_price), 'sell_price': str(d.sell_price),
            'original_quantity': str(d.quantity), 'old_status': d.status,
            'remaining_quantity': None, 'confirmed_canceled': False,
        } for d in orders]
        if opts['history_csv']:
            try:
                with Path(opts['history_csv']).open(encoding='utf-8-sig', newline='') as stream:
                    rows = apply_history(rows, csv.DictReader(stream, delimiter=opts['csv_delimiter']), {
                        name: opts[name + '_column'] for name in ('id', 'symbol', 'quantity', 'filled', 'status')
                    }, opts['canceled_status'] or ['CANCELED', 'PARTIALLY_CANCELED'], opts['filled_status'] or ['FILLED'])
            except Exception as exc:
                raise CommandError(str(exc)) from None
        # Exclusive creation protects the reviewed file from accidental replacement.
        with Path(opts['output']).open('x', encoding='utf-8') as stream:
            json.dump({'version': 1, 'telegram_id': opts['telegram_id'], 'orders': rows}, stream, ensure_ascii=False, indent=2)
        self.stdout.write(f'Exported {len(rows)} candidates. Review quantities and unwanted historical cancellations. Without --history-csv, fill remaining_quantity and confirmed_canceled manually.')
