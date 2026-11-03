import asyncio
import time
from typing import List

from asgiref.sync import sync_to_async
from bot.logger import logger
from users.models import User, Deal
from bot.utils.exchange import client_for_user
from django.conf import settings
from bot.utils.websocket_handlers import update_order_status


async def _reconcile_user_orders(user: User) -> None:
    """Reconcile one user's active orders against REST API and update DB."""
    try:
        # Collect active deals from DB
        active_deals: List[Deal] = await sync_to_async(list)(
            Deal.objects.filter(exchange=settings.EXCHANGE,
                user=user, status__in=["NEW", "PARTIALLY_FILLED"]
            ).order_by("-created_at")
        )
        if not active_deals:
            return

        client = client_for_user(user, allow_hold=True)
        # Restored orders may use a different pair from the current strategy.
        for symbol in {deal.symbol for deal in active_deals}:
            try:
                open_orders = await client.open_orders(symbol)
            except Exception as exc:
                logger.warning(f"[Reconciler] open_orders failed for {symbol}: {exc}")
                open_orders = []

            # Map and update
            deals_by_id = {d.order_id: d for d in active_deals if d.symbol == symbol}
            api_by_id = {str(o['orderId']): o for o in open_orders if o.get('orderId')}

            # First update orders present in open_orders.
            for order_id, order in api_by_id.items():
                if order_id in deals_by_id and order.get('status'):
                    await update_order_status(order_id, symbol, order['status'], user_id=user.telegram_id)

            # Confirm missing orders individually; an API error never changes DB status.
            for order_id in deals_by_id.keys() - api_by_id.keys():
                try:
                    resp = await client.query_order(symbol, {'orderId': order_id})
                    if resp.get('status'):
                        await update_order_status(order_id, symbol, resp['status'], user_id=user.telegram_id)
                except Exception as exc:
                    logger.warning(f"[Reconciler] query_order failed for {order_id}: {exc}")

    except Exception as e:
        logger.error(
            f"[Reconciler] Unexpected error for user {getattr(user, 'telegram_id', 'unknown')}: {e}"
        )


async def order_status_reconciler_loop(poll_interval_seconds: int = 60) -> None:
    """Continuously reconcile order statuses for all users with API keys.

    - Only updates active statuses (NEW, PARTIALLY_FILLED)
    - Uses open_orders to batch, then per-order query as fallback
    """
    logger.info("[Reconciler] Starting background order status reconciler loop")
    while True:
        start_ts = time.time()
        try:
            users = await sync_to_async(list)(
                User.objects.filter(api_exchange=settings.EXCHANGE).exclude(api_key__isnull=True)
                .exclude(api_key="")
                .exclude(api_secret__isnull=True)
                .exclude(api_secret="")
            )
            for user in users:
                await _reconcile_user_orders(user)
        except Exception as e:
            logger.error(f"[Reconciler] Top-level loop error: {e}")

        # Sleep remaining time of the interval if processing was fast
        elapsed = time.time() - start_ts
        sleep_for = max(5, poll_interval_seconds - int(elapsed))
        await asyncio.sleep(sleep_for)
