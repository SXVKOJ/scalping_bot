"""Оффлайн-проверка логики покупок на росте.

Прогоняет синтетические серии цен через check_rise_triggers без Django, aiohttp
и сети — нужен только Python. Запуск:

    python scripts/check_rise_logic.py
"""

import os
import sys
import types
import asyncio
import time
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

def mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

# --- stubs ---
mod("aiohttp", ClientSession=object, ClientTimeout=object, TCPConnector=object)
django = mod("django")
mod("django.conf", settings=types.SimpleNamespace(PAIR="BTC/USDT", TELEGRAM_BOT_TOKEN="x"))
mod("django.utils", timezone=types.SimpleNamespace(now=lambda: 0))
mod("django.db", models=types.SimpleNamespace())
mod("aiogram", types=types.SimpleNamespace(Message=object))
mod("aiogram.types", Message=object)
mod("asgiref", sync=None)
mod("asgiref.sync", sync_to_async=lambda f: f)


class _Obj:  # заглушка модели
    objects = types.SimpleNamespace()


mod("users", models=None)
mod("users.models", Deal=_Obj, User=_Obj)
mod("subscriptions", models=None)
mod("subscriptions.models", Subscription=_Obj)
mod("bot.utils.user_autobuy_tasks", user_autobuy_tasks={})
mod("bot.utils.mexc", handle_mexc_response=lambda *a, **k: None)
mod("bot.utils.api_errors", parse_mexc_error=lambda e: str(e))
mod("bot.utils.error_notifier", notify_user_autobuy_error=lambda *a, **k: None)


class _Logger:
    def __getattr__(self, name):
        def log(msg, *a, **k):
            if VERBOSE:
                print(f"  [{name}] {msg}")
        return log


VERBOSE = False
mod("bot.logger", logger=_Logger())
mod("bot.config", bot_instance=None)
mod("bot.utils.autobuy_restart", FakeMessage=lambda *a, **k: None)

import bot.commands.autobuy as ab

BUYS = []


async def fake_process_buy(telegram_id, reason, message, user, trigger_ts=None):
    BUYS.append((reason, time.time()))


ab.process_buy = fake_process_buy
ab._safe_send = lambda *a, **k: asyncio.sleep(0)


async def feed(prices, pause, tick=0.1, tolerance=None):
    """Прогоняет серию mid-цен через check_rise_triggers."""
    BUYS.clear()
    if tolerance is not None:
        ab.RISE_TREND_TOLERANCE_PCT = tolerance
    uid = 1
    ab.autobuy_states[uid] = {
        "active_orders": [], "rise_buy_count": 0, "last_rise_buy_time": 0,
        "buy_in_progress": False,
    }
    st = ab.autobuy_states[uid]
    now = 1000.0
    ab.arm_rise_trigger(st, prices[0] * 1.0001, now)  # триггер по ask, как в проде
    for p in prices:
        now += tick
        spread = p * 0.0001
        await ab.check_rise_triggers(uid, "BTCUSDT", p - spread, p + spread, True, now, pause)
        await asyncio.sleep(0)  # даём отработать create_task
    return len(BUYS)


def noisy_rise(n, start=100.0, drift=0.02, noise=0.01, seed=1):
    random.seed(seed)
    out, p = [], start
    for _ in range(n):
        p += drift + random.uniform(-noise, noise)
        out.append(p)
    return out


def noisy_fall(n, **kw):
    return [200.0 - (p - 100.0) for p in noisy_rise(n, **kw)]


async def main():
    pause = 3  # сек, при тике 100мс окно = 30 тиков

    n = await feed(noisy_rise(200), pause)
    print(f"Шумный рост (+0.02/тик, шум ±0.01): покупок = {n}")
    assert n >= 1, "на росте бот обязан покупать"

    n = await feed(noisy_fall(200), pause)
    print(f"Шумное падение:                     покупок = {n}")
    assert n == 0, "на падении покупок на росте быть не должно"

    n = await feed([100.0] * 200, pause)
    print(f"Плоский рынок:                      покупок = {n}")
    assert n == 0

    # Реалистичный рост: тренд вверх, но с откатами внутри окна
    wobbly = noisy_rise(600, drift=0.03, noise=0.06, seed=7)
    downticks = sum(1 for a, b in zip(wobbly, wobbly[1:]) if b < a)
    print(f"\nРост с откатами ({downticks} тиков вниз из {len(wobbly)}):")
    n_old = await feed(wobbly, pause, tolerance=0.0)
    print(f"  допуск 0%   (старая логика): покупок = {n_old}")
    n_new = await feed(wobbly, pause, tolerance=0.05)
    print(f"  допуск 0.05% (новая логика): покупок = {n_new}")
    assert n_old == 0 and n_new > 0, (n_old, n_new)

    n = await feed(noisy_rise(600), 3, tolerance=0.05)
    print(f"Длинный рост, лимит {ab.RISE_MAX_BUYS_PER_CYCLE}/цикл:            покупок = {n}")
    assert n <= ab.RISE_MAX_BUYS_PER_CYCLE

    print("\nOK: все проверки пройдены")


asyncio.run(main())
