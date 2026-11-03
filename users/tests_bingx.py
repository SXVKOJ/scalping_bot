import asyncio
import gzip
import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TransactionTestCase

from bot.utils.bingx_rest import BingXRestClient, OrderOutcomeUnknown, bingx_symbol
from bot.utils.bingx_ws import BingXWebSocketManager
from bot.utils.exchange import client_for_user, begin_trade, end_trade, buy_fill, place_entry
from bot.utils.order_migration import build_plan, execute_plan, preflight
from users.models import Deal, OrderMigration, User
from users.management.commands.export_orders import apply_history


RULES = {'symbol': 'KAS-USDT', 'status': 1, 'stepSize': '0.1', 'tickSize': '0.00001',
         'minQty': '1', 'maxQty': '1000000', 'minNotional': '1', 'maxNotional': '100000'}


class AdapterTests(SimpleTestCase):
    async def test_ioc_uses_a_bounded_limit_without_market_fallback(self):
        client = BingXRestClient()
        client.symbol_rules = AsyncMock(return_value=RULES)
        client._buy_fee_rate = Decimal('0.001')
        client.new_order = AsyncMock(side_effect=OrderOutcomeUnknown('timeout'))
        with patch('bot.constants.ENTRY_ORDER_TYPE', 'ioc'), patch('bot.constants.ENTRY_MAX_SLIPPAGE_PCT', 0.3):
            with self.assertRaises(OrderOutcomeUnknown):
                await place_entry(client, 'KASUSDT', '6', {'newClientOrderId': 'stable'}, '0.03')
        client.new_order.assert_awaited_once()
        args = client.new_order.call_args.args
        self.assertEqual(args[2], 'LIMIT')
        self.assertEqual(args[3]['timeInForce'], 'IOC')
        self.assertLessEqual(Decimal(args[3]['quantity']) * Decimal(args[3]['price']), Decimal('6'))

    def test_history_csv_joins_ids_and_subtracts_partial_fills(self):
        rows = [{'old_order_id': '1234567890123456789', 'symbol': 'KASUSDC', 'original_quantity': '200'}]
        columns = {'id': 'ID', 'symbol': 'Pair', 'quantity': 'Quantity', 'filled': 'Filled', 'status': 'Status'}
        history = [{'ID': '1234567890123456789', 'Pair': 'KAS/USDC', 'Quantity': '200', 'Filled': '50.05', 'Status': 'Partially canceled'}]
        verified = apply_history(rows, history, columns, ['Partially canceled'], ['Filled'])
        self.assertEqual(verified[0]['remaining_quantity'], '149.95')
        self.assertTrue(verified[0]['confirmed_canceled'])
        history[0]['Status'], history[0]['Filled'] = 'Filled', '200'
        self.assertEqual(apply_history(rows, history, columns, ['Canceled'], ['Filled']), [])
        history[0]['ID'] = '1.2345678901234568e18'
        with self.assertRaises(ValueError):
            apply_history(rows, history, columns, ['Canceled'], ['Filled'])

    def test_history_csv_rejects_active_or_mismatched_orders(self):
        rows = [{'old_order_id': '1', 'symbol': 'KASUSDC', 'original_quantity': '200'}]
        columns = {name: name for name in ('id', 'symbol', 'quantity', 'filled', 'status')}
        history = [{'id': '1', 'symbol': 'KASUSDC', 'quantity': '200', 'filled': '0', 'status': 'NEW'}]
        for change in ({}, {'status': 'CANCELED', 'symbol': 'BTCUSDC'}, {'status': 'CANCELED', 'quantity': '201'}):
            history = [{'id': '1', 'symbol': 'KASUSDC', 'quantity': '200', 'filled': '0', 'status': 'NEW', **change}]
            with self.assertRaises(ValueError):
                apply_history(rows, history, columns, ['CANCELED'], ['FILLED'])

    def test_signing_and_symbol(self):
        params = {'timestamp': '123', 'symbol': 'KAS-USDT', 'side': 'BUY'}
        expected = hmac.new(b'secret', b'side=BUY&symbol=KAS-USDT&timestamp=123', hashlib.sha256).hexdigest()
        self.assertEqual(BingXRestClient.sign(params, 'secret'), expected)
        for pair in ('kas/usdt', 'KASUSDT', 'KAS-USDT', 'KAS_USDT'):
            self.assertEqual(bingx_symbol(pair), 'KAS-USDT')

    async def test_precision_and_fee_in_base(self):
        client = BingXRestClient()
        client.symbol_rules = AsyncMock(return_value=RULES)
        client.commission_rate = AsyncMock(return_value={'makerCommissionRate': '0.001', 'takerCommissionRate': '0.001'})
        qty, price = await client.sell_terms('KASUSDT', {
            'executedQty': '200', 'cummulativeQuoteQty': '6', 'fee': '0.2', 'feeAsset': 'KAS', 'status': 'FILLED',
        }, '0.5')
        self.assertEqual(qty, Decimal('199.8'))
        self.assertGreaterEqual(qty * price * Decimal('0.999'), Decimal('6.03'))
        qty, price = await client.prepare_limit_sell('KASUSDT', '199.87', '0.031001')
        self.assertEqual((qty, price), (Decimal('199.8'), Decimal('0.03101')))
        qty, price = await client.sell_terms('KASUSDT', {
            'executedQty': '200', 'cummulativeQuoteQty': '6', 'fee': '0.13', 'feeAsset': 'KAS', 'status': 'FILLED',
        }, '0.5')
        self.assertGreaterEqual(qty * price * Decimal('0.999'), Decimal('6.03'))

    async def test_server_time_seconds_and_milliseconds(self):
        for server_time in (1700000000, 1700000000000):
            client = BingXRestClient()
            client._request = AsyncMock(return_value={'serverTime': server_time})
            with patch('bot.utils.bingx_rest.time.time', return_value=1700000000):
                await client._ensure_time_offset()
            self.assertEqual(client._time_offset_ms, 0)

    async def test_missing_fee_and_unfinished_fill_fail_closed(self):
        client = BingXRestClient()
        for order in ({'status': 'PARTIALLY_FILLED'}, {'status': 'FILLED', 'executedQty': '200', 'cummulativeQuoteQty': '6'}):
            with self.assertRaises(ValueError):
                await client.sell_terms('KASUSDT', order, '0.5')

    async def test_buy_budget_below_minimum_sell_quantity_rejected(self):
        client = BingXRestClient()
        client.symbol_rules = AsyncMock(return_value={**RULES, 'minQty': '260.2'})
        client.commission_rate = AsyncMock(return_value={'makerCommissionRate': '0.001', 'takerCommissionRate': '0.001'})
        with self.assertRaises(ValueError):
            await client.validate_buy_budget('KASUSDT', '5', '0.03')
        await client.validate_buy_budget('KASUSDT', '10', '0.03')
        self.assertEqual(client.commission_rate.await_count, 1)

    async def test_pending_buy_only_repeats_reads(self):
        client = BingXRestClient()
        client.query_order = AsyncMock(side_effect=[{'status': 'PENDING'}, {'status': 'PARTIALLY_FILLED'}, {'status': 'FILLED'}])
        with patch('bot.utils.exchange.asyncio.sleep', new=AsyncMock()):
            self.assertEqual((await buy_fill(client, 'KASUSDT', '7'))['status'], 'FILLED')
        self.assertEqual(client.query_order.await_count, 3)

    async def test_timeout_queries_client_id_without_second_post(self):
        client = BingXRestClient('test_timeout', 'secret')
        client.symbol_rules = AsyncMock(return_value=RULES)
        client._request = AsyncMock(side_effect=RuntimeError('timeout'))
        client.query_order = AsyncMock(return_value={'orderId': '7'})
        result = await client.new_order('KASUSDT', 'BUY', 'MARKET', {'quoteOrderQty': '5', 'newClientOrderId': 'stable_id'})
        self.assertEqual(result['orderId'], '7')
        self.assertEqual(client._request.await_count, 1)
        client.query_order.assert_awaited_once_with('KASUSDT', {'clientOrderID': 'stable_id'})

    async def test_unknown_outcome_is_explicit(self):
        client = BingXRestClient('test_unknown', 'secret')
        client.symbol_rules = AsyncMock(return_value=RULES)
        client._request = AsyncMock(side_effect=RuntimeError('timeout'))
        client.query_order = AsyncMock(side_effect=RuntimeError('not found yet'))
        with self.assertRaises(OrderOutcomeUnknown):
            await client.new_order('KASUSDT', 'BUY', 'MARKET', {'quoteOrderQty': '5'})
        self.assertEqual(client._request.await_count, 1)

    def test_old_keys_never_used_on_bingx(self):
        user = User(api_exchange='mexc')
        with self.assertRaises(ValueError):
            client_for_user(user)

    async def test_bookticker_reaches_strategy(self):
        manager = BingXWebSocketManager()
        callback = AsyncMock()
        await manager.register_bookticker_callback('KASUSDT', callback)
        await manager._market_event({'dataType': 'KAS-USDT@bookTicker', 'data': {
            's': 'KAS-USDT', 'b': '0.03', 'a': '0.03001', 'B': '100', 'A': '200', 'T': 123,
        }})
        callback.assert_awaited_once_with('KASUSDT', '0.03', '0.03001', '100', '200')
        self.assertEqual(manager.get_current_bookticker('KAS/USDT')['timestamp'], 123)


class MigrationTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create(telegram_id=123, api_exchange='bingx', api_key='key', api_secret='secret')
        self.source = Deal.objects.create(user=self.user, exchange='mexc', order_id='old', symbol='KASUSDC',
                                          buy_price='0.03', sell_price='0.031', quantity='200', status='CANCELED')
        self.document = {'version': 1, 'telegram_id': 123, 'orders': [{
            'deal_id': self.source.pk, 'remaining_quantity': '150.05', 'confirmed_canceled': True,
        }]}
        self.client = BingXRestClient()
        self.client.symbol_rules = AsyncMock(return_value=RULES)
        self.client.commission_rate = AsyncMock(return_value={'makerCommissionRate': '0.001', 'takerCommissionRate': '0.001'})
        self.client.account_info = AsyncMock(return_value={'balances': [{'asset': 'KAS', 'free': '1000'}]})
        self.client.book_ticker = AsyncMock(return_value={'bidPrice': '0.02', 'askPrice': '0.02001'})
        self.client.new_order = AsyncMock(return_value={
            'orderId': 'new', 'symbol': 'KASUSDT', 'side': 'SELL', 'type': 'LIMIT',
            'origQty': '150', 'price': '0.031', 'status': 'NEW',
        })

    def plan(self):
        return async_to_sync(build_plan)(self.document, self.user, self.client, 'KAS/USDT', '1')

    def test_preview_no_writes_and_no_automatic_conversion(self):
        with self.assertRaises(ValueError):
            async_to_sync(build_plan)(self.document, self.user, self.client, 'KAS/USDT')
        rows = self.plan()
        async_to_sync(preflight)(rows, self.client)
        self.assertEqual(rows[0]['quantity'], Decimal('150.0'))
        self.assertFalse(OrderMigration.objects.exists())
        self.client.new_order.assert_not_awaited()

    def test_restoration_never_lowers_old_target_and_covers_sell_fee(self):
        self.source.sell_price = Decimal('0.03001')
        self.source.save(update_fields=['sell_price'])
        self.source.refresh_from_db()
        row = self.plan()[0]
        self.assertGreaterEqual(row['price'] * Decimal('0.999'), self.source.buy_price)
        self.assertGreaterEqual(row['price'], self.source.sell_price)

    def test_partial_quantity_and_restart_do_not_duplicate(self):
        rows = self.plan()
        async_to_sync(execute_plan)(rows, self.client)
        async_to_sync(execute_plan)(self.plan(), self.client)
        self.assertEqual(Deal.objects.filter(exchange='bingx').count(), 1)
        self.assertEqual(OrderMigration.objects.get().state, 'COMPLETE')
        self.client.new_order.assert_awaited_once()
        restored = Deal.objects.get(exchange='bingx')
        self.assertEqual(restored.quantity, Decimal('150'))
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, 'CANCELED')

    def test_crash_after_acceptance_recovers_without_second_post(self):
        self.client.new_order.side_effect = OrderOutcomeUnknown('timeout')
        with self.assertRaises(OrderOutcomeUnknown):
            async_to_sync(execute_plan)(self.plan(), self.client)
        self.assertEqual(OrderMigration.objects.get().state, 'UNKNOWN')
        self.client.query_order = AsyncMock(return_value=self.client.new_order.return_value)
        async_to_sync(execute_plan)(self.plan(), self.client)
        self.assertEqual(OrderMigration.objects.get().state, 'COMPLETE')
        self.assertEqual(self.client.new_order.await_count, 1)

    def test_unknown_not_found_never_resubmits(self):
        OrderMigration.objects.create(source=self.source, client_order_id='stable', symbol='KASUSDT', quantity='150', price='0.031', state='SUBMITTING')
        self.client.query_order = AsyncMock(side_effect=RuntimeError('not found'))
        with self.assertRaises(RuntimeError):
            self.plan()
        self.client.new_order.assert_not_awaited()

    def test_balance_and_crossing_checked_before_submit(self):
        self.client.account_info.return_value = {'balances': [{'asset': 'KAS', 'free': '100'}]}
        with self.assertRaises(ValueError):
            async_to_sync(execute_plan)(self.plan(), self.client)
        self.client.account_info.return_value = {'balances': [{'asset': 'KAS', 'free': '1000'}]}
        self.client.book_ticker.return_value = {'bidPrice': '0.04'}
        with self.assertRaises(ValueError):
            async_to_sync(execute_plan)(self.plan(), self.client)
        self.client.new_order.assert_not_awaited()
        self.assertFalse(OrderMigration.objects.exists())

    def test_unconfirmed_cancellation_and_wrong_user_rejected(self):
        self.document['orders'][0]['confirmed_canceled'] = False
        with self.assertRaises(ValueError):
            self.plan()
        self.document['orders'][0]['confirmed_canceled'] = True
        self.document['telegram_id'] = 999
        with self.assertRaises(ValueError):
            self.plan()

    def test_persisted_intent_blocks_another_buy_but_allows_status_reads(self):
        intent = async_to_sync(begin_trade)(self.user.pk, 'KASUSDT')
        self.user.refresh_from_db()
        self.assertIn(intent['buy_id'], self.user.execution_hold)
        self.assertIn(intent['sell_id'], self.user.execution_hold)
        with self.assertRaises(ValueError):
            async_to_sync(begin_trade)(self.user.pk, 'KASUSDT')
        with self.assertRaises(ValueError):
            client_for_user(self.user)
        self.assertIsInstance(client_for_user(self.user, allow_hold=True), BingXRestClient)
        async_to_sync(end_trade)(self.user.pk, intent)
        self.user.refresh_from_db()
        self.assertEqual(self.user.execution_hold, '')

    def test_cancellation_during_buy_keeps_ids_and_stops_autobuy(self):
        from bot.commands.autobuy import process_buy, autobuy_states
        self.user.autobuy = True
        self.user.pair, self.user.buy_amount = 'KAS/USDT', Decimal('6')
        self.user.profit, self.user.loss, self.user.pause = 0.5, 0.5, 0
        self.user.save()
        self.client.api_key, self.client.api_secret = self.user.api_key, self.user.api_secret
        self.client.new_order = AsyncMock(side_effect=asyncio.CancelledError())
        autobuy_states[123] = {'rest_client': self.client, 'active_orders': [], 'buy_in_progress': False}
        try:
            with self.assertRaises(asyncio.CancelledError):
                async_to_sync(process_buy)(123, 'price_drop', AsyncMock(), self.user)
            self.user.refresh_from_db()
            self.assertFalse(self.user.autobuy)
            buy_id = self.client.new_order.call_args.args[3]['newClientOrderId']
            self.assertIn(buy_id, self.user.execution_hold)
            self.assertFalse(autobuy_states[123]['buy_in_progress'])
        finally:
            autobuy_states.pop(123, None)

    def test_manual_buy_zero_fill_stops_trading(self):
        from bot.commands.trading import buy_handler
        self.user.pair, self.user.buy_amount = 'KAS/USDT', Decimal('6')
        self.user.save()
        self.client.new_order = AsyncMock(return_value={'orderId': 'accepted_buy'})
        self.client.query_order = AsyncMock(return_value={'status': 'FILLED', 'executedQty': '0'})
        message = AsyncMock()
        message.from_user.id, message.from_user.username, message.chat.id = 123, 'test', 123
        with patch('bot.commands.trading.client_for_user', return_value=self.client), patch('bot.commands.trading.log_command', new=AsyncMock()):
            async_to_sync(buy_handler)(message)
        self.user.refresh_from_db()
        self.assertIn('BUY=', self.user.execution_hold)
        self.assertFalse(self.user.autobuy)

    def test_canceled_ioc_without_fill_does_not_count_as_an_error(self):
        from bot.commands.autobuy import process_buy, autobuy_states
        self.user.autobuy = True
        self.user.pair, self.user.buy_amount = 'KAS/USDT', Decimal('6')
        self.user.profit, self.user.loss, self.user.pause = 0.5, 0.5, 0
        self.user.save()
        self.client.api_key, self.client.api_secret = self.user.api_key, self.user.api_secret
        self.client.new_order = AsyncMock(return_value={'orderId': 'unfilled'})
        self.client.query_order = AsyncMock(return_value={'status': 'CANCELED', 'executedQty': '0'})
        autobuy_states[123] = {'rest_client': self.client, 'active_orders': [], 'buy_in_progress': False, 'consecutive_errors': 2}
        try:
            async_to_sync(process_buy)(123, 'price_drop', AsyncMock(), self.user)
            self.user.refresh_from_db()
            self.assertTrue(self.user.autobuy)
            self.assertEqual(self.user.execution_hold, '')
            self.assertEqual(autobuy_states[123]['consecutive_errors'], 2)
            self.client.new_order.assert_awaited_once()
        finally:
            autobuy_states.pop(123, None)

    def test_autobuy_creates_bingx_deal_and_sell_with_net_quantity(self):
        from bot.commands.autobuy import process_buy, autobuy_states
        from bot import config
        self.user.autobuy = True
        self.user.pair, self.user.buy_amount = 'KAS/USDT', Decimal('6')
        self.user.profit, self.user.loss, self.user.pause = 0.5, 0.5, 0
        self.user.save()
        self.client.api_key, self.client.api_secret = self.user.api_key, self.user.api_secret
        self.client.new_order = AsyncMock(side_effect=[{'orderId': 'buy_new'}, {'orderId': 'sell_new'}])
        self.client.query_order = AsyncMock(side_effect=[{
            'status': 'FILLED', 'executedQty': '200', 'cummulativeQuoteQty': '6', 'fee': '0.2', 'feeAsset': 'KAS',
        }, {'status': 'NEW'}])
        self.client.commission_rate = AsyncMock(return_value={'makerCommissionRate': '0.001', 'takerCommissionRate': '0.001'})
        autobuy_states[123] = {'rest_client': self.client, 'active_orders': [], 'buy_in_progress': False}
        bot = AsyncMock()
        try:
            with patch.object(config, 'bot_instance', bot):
                async_to_sync(process_buy)(123, 'price_drop', AsyncMock(), self.user)
            deal = Deal.objects.get(order_id='sell_new')
            self.assertEqual(deal.exchange, 'bingx')
            self.assertEqual(deal.quantity, Decimal('199.8'))
            self.user.refresh_from_db()
            self.assertEqual(self.user.execution_hold, '')
            self.assertFalse(autobuy_states[123]['buy_in_progress'])
        finally:
            autobuy_states.pop(123, None)

    def test_accepted_buy_failed_sell_persists_hold(self):
        from bot.commands.autobuy import process_buy, autobuy_states
        self.user.autobuy = True
        self.user.pair, self.user.buy_amount = 'KAS/USDT', Decimal('6')
        self.user.profit, self.user.loss, self.user.pause = 0.5, 0.5, 0
        self.user.save()
        self.client.api_key, self.client.api_secret = self.user.api_key, self.user.api_secret
        self.client.new_order = AsyncMock(return_value={'orderId': 'accepted_buy'})
        self.client.query_order = AsyncMock(side_effect=RuntimeError('response lost'))
        autobuy_states[123] = {'rest_client': self.client, 'active_orders': [], 'buy_in_progress': False}
        try:
            with patch('bot.commands.autobuy.notify_user_autobuy_error', new=AsyncMock()):
                async_to_sync(process_buy)(123, 'price_drop', AsyncMock(), self.user)
            self.user.refresh_from_db()
            self.assertFalse(self.user.autobuy)
            self.assertTrue(self.user.execution_hold)
            with self.assertRaises(ValueError):
                client_for_user(self.user)
        finally:
            autobuy_states.pop(123, None)
