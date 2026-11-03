import asyncio
import json
from pathlib import Path

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bot.utils.bingx_rest import BingXRestClient
from bot.utils.mexc_rest import close_shared_session
from bot.utils.order_migration import build_plan, preflight, execute_plan
from users.models import User


class Command(BaseCommand):
    help = 'Preview or restore reviewed MEXC sells on BingX. Defaults to read-only preview.'

    def add_arguments(self, parser):
        parser.add_argument('--telegram-id', type=int, required=True)
        parser.add_argument('--file', required=True)
        parser.add_argument('--target-pair', required=True)
        parser.add_argument('--quote-conversion')
        parser.add_argument('--execute', action='store_true')
        parser.add_argument('--allow-marketable', action='store_true')

    def handle(self, *args, **opts):
        if settings.EXCHANGE != 'bingx':
            raise CommandError('Set EXCHANGE=bingx')
        try:
            asyncio.run(self.run(opts))
        except Exception as exc:
            raise CommandError(str(exc)) from None

    async def run(self, opts):
        try:
            user = await sync_to_async(User.objects.get)(telegram_id=opts['telegram_id'])
            document = json.loads(Path(opts['file']).read_text(encoding='utf-8-sig'))
            client = BingXRestClient(user.api_key, user.api_secret)
            rows = await build_plan(document, user, client, opts['target_pair'], opts['quote_conversion'])
            await preflight(rows, client, opts['allow_marketable'])
            for row in rows:
                self.stdout.write(f"Deal {row['source'].pk}: {row['quantity']} {row['symbol']} @ {row['price']}; old target={row['old_target']}; dust={row['dust']}; already restored={bool(row['done'])}")
            if opts['execute']:
                await execute_plan(rows, client, opts['allow_marketable'])
                self.stdout.write(self.style.SUCCESS('Restoration complete. Autobuy remains stopped.'))
            else:
                self.stdout.write('PREVIEW ONLY. No orders submitted. Add --execute after review.')
        finally:
            await close_shared_session()
