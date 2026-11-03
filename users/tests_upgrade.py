from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from core.bootstrap import MIGRATION_LOCK_ID, bootstrap_database
from users.models import Deal, User


class StartupLockTests(SimpleTestCase):
    def test_postgres_lock_released_when_migration_fails(self):
        database = MagicMock(vendor='postgresql')
        with patch('core.bootstrap.connections', {'default': database}), patch('core.bootstrap.MigrationExecutor'), patch('core.bootstrap.call_command', side_effect=RuntimeError('migration failed')):
            with self.assertRaisesRegex(RuntimeError, 'migration failed'):
                bootstrap_database()
        cursor = database.cursor.return_value.__enter__.return_value
        self.assertEqual(cursor.execute.call_args_list[0].args, ('SELECT pg_advisory_lock(%s)', [MIGRATION_LOCK_ID]))
        self.assertEqual(cursor.execute.call_args_list[-1].args, ('SELECT pg_advisory_unlock(%s)', [MIGRATION_LOCK_ID]))


class PersistedDatabaseUpgradeTests(TransactionTestCase):
    @override_settings(PAIR='KAS/USDC')
    def test_old_database_upgrades_without_losing_history_or_settings(self):
        old_target = [('users', '0011_deal_indexes')]
        latest_targets = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(old_target)
            old_apps = executor.loader.project_state(old_target).apps
            OldUser = old_apps.get_model('users', 'User')
            OldDeal = old_apps.get_model('users', 'Deal')
            user = OldUser.objects.create(telegram_id=456, pair='KAS/USDC', api_key='old-key',
                api_secret='old-secret', profit=0.8, loss=0.7, pause=19, buy_amount='12.34', autobuy=True)
            deal_ids = []
            for order_id, status in [('old-open', 'NEW'), ('old-filled', 'FILLED')]:
                deal = OldDeal.objects.create(user=user, order_id=order_id, symbol='KASUSDC',
                    quantity='400.25', buy_price='0.03', sell_price='0.031', status=status, is_autobuy=True)
                deal_ids.append(deal.pk)

            bootstrap_database(verbosity=0)
            upgraded = User.objects.get(pk=user.pk)
            self.assertEqual((upgraded.profit, upgraded.loss, upgraded.pause, upgraded.buy_amount), (0.8, 0.7, 19, Decimal('12.34')))
            self.assertEqual((upgraded.api_key, upgraded.api_secret, upgraded.api_exchange), ('old-key', 'old-secret', 'mexc'))
            self.assertFalse(upgraded.autobuy)  # Exchange transition pauses new entries once.
            self.assertEqual(list(Deal.objects.order_by('pk').values_list('pk', flat=True)), deal_ids)
            self.assertEqual(list(Deal.objects.order_by('pk').values_list('status', flat=True)), ['NEW', 'FILLED'])
            self.assertEqual(Deal.objects.get(pk=deal_ids[0]).quantity, Decimal('400.25'))
            self.assertEqual(Deal.objects.get(pk=deal_ids[0]).sell_price, Decimal('0.031'))
            self.assertFalse(Deal.objects.exclude(exchange='mexc').exists())

            # Ordinary restarts must not replay the one-time pause or reset parameters.
            upgraded.api_exchange, upgraded.autobuy = 'bingx', True
            upgraded.save(update_fields=['api_exchange', 'autobuy'])
            bootstrap_database(verbosity=0)
            upgraded.refresh_from_db()
            self.assertTrue(upgraded.autobuy)
            self.assertEqual(upgraded.api_exchange, 'bingx')
            self.assertEqual(Deal.objects.count(), 2)
        finally:
            MigrationExecutor(connection).migrate(latest_targets)

    @override_settings(PAIR='KAS/USDT')
    def test_startup_syncs_instance_pair_but_only_fills_missing_parameters(self):
        user = User.objects.create(telegram_id=789, pair='KAS/USDC', profit=1.25,
                                  loss=None, pause=0, buy_amount='17.50')
        bootstrap_database(verbosity=0)
        user.refresh_from_db()
        self.assertEqual(user.pair, 'KAS/USDT')
        self.assertEqual(user.profit, 1.25)
        self.assertEqual(user.pause, 0)
        self.assertEqual(user.buy_amount, Decimal('17.50'))
        self.assertIsNotNone(user.loss)
