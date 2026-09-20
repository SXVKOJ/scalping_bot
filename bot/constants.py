import os

from django.conf import settings


PAYMENT_TIME = 30
PAYMENT_AMOUNT = 100
PAYMENT_WALLET = "TY43ubA82J5mrViFwAsNpNLkNLaj2rvx1Z"
PAYMENT_NETWORK = "TRC20"

DEFAULT_PAYMENT_MESSAGE = (
    f"🔒 Для получения доступа к боту на {PAYMENT_TIME} дней:\n\n"
    f"1️⃣ Оплатите {PAYMENT_AMOUNT} USDT в сети {PAYMENT_NETWORK} на кошелёк:\n"
    f"<code>{PAYMENT_WALLET}</code>\n\n"
    f"2️⃣ После оплаты отправьте скриншот и TXID в ЛС 👉 @TestScalpingBotSupport\n\n"
    f"Перед оплатой рекомендуем нажать /start для актуализации информации."
)

MONTHS_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
}

PAIR = settings.PAIR

MAX_FAILS = 5 # Максимальное количество неудачных попыток до остановки мониторинга

# ===== Настройки автобая (можно переопределить через переменные окружения) =====

# Допустимая просадка mid-цены внутри окна анализа роста, в процентах.
# 0 = прежнее поведение (любой тик вниз сбрасывает окно), что на реальном
# рынке практически никогда не даёт покупку на росте.
RISE_TREND_TOLERANCE_PCT = float(os.getenv("RISE_TREND_TOLERANCE_PCT", "0.05"))

# Минимальный чистый рост к уровню триггера на момент покупки, в процентах.
# 0 = достаточно любого превышения уровня (поведение по умолчанию).
RISE_MIN_PCT = float(os.getenv("RISE_MIN_PCT", "0"))

# Максимум покупок на росте в рамках одного цикла (до полного закрытия позиций).
RISE_MAX_BUYS_PER_CYCLE = int(os.getenv("RISE_MAX_BUYS_PER_CYCLE", "3"))

# Минимальный интервал между покупками на росте, сек.
RISE_BUY_COOLDOWN_SEC = float(os.getenv("RISE_BUY_COOLDOWN_SEC", "3"))

# Антидребезг для покупок на падении, сек.
DROP_BUY_COOLDOWN_SEC = float(os.getenv("DROP_BUY_COOLDOWN_SEC", "10"))

# Через сколько секунд повторить попытку покупки после ошибки.
BUY_RETRY_DELAY_SEC = float(os.getenv("BUY_RETRY_DELAY_SEC", "30"))

# ===== Исполнение ордеров =====

# Тип ордера на вход: "market" (как было) или "ioc" — лимит по ask с запасом
# ENTRY_MAX_SLIPPAGE_PCT, остаток отменяется. IOC ограничивает худшую цену
# исполнения, но часть сигналов может остаться без входа.
ENTRY_ORDER_TYPE = (os.getenv("ENTRY_ORDER_TYPE", "market") or "market").strip().lower()

# Максимальное проскальзывание для IOC-входа, %.
ENTRY_MAX_SLIPPAGE_PCT = float(os.getenv("ENTRY_MAX_SLIPPAGE_PCT", "0.3"))

# Надбавка к профиту на комиссии, %. 0 = профит считается «грязным», как было.
# У MEXC спот taker 0.05%, maker 0% — для «чистого» профита ставьте 0.05.
PROFIT_FEE_BUFFER_PCT = float(os.getenv("PROFIT_FEE_BUFFER_PCT", "0"))
