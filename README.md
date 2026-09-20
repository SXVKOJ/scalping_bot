# Scalping Bot (MEXC + Telegram)

Telegram-бот для скальпинга на споте MEXC и Django-админка. Один запущенный экземпляр торгует **одну пару**. Две пары = два экземпляра.

## Быстрый старт

1. Скопируйте `.env.example` в `.env` и заполните `TELEGRAM_TOKEN` и `PAIR`.
2. Запуск:

```bash
docker compose up -d --build
```

3. Админка: http://localhost:8000/admin/  
   Логин/пароль задаются в `.env` (`DJANGO_SUPERUSER_USERNAME` / `DJANGO_SUPERUSER_PASSWORD`).
4. В Telegram откройте бота → `/start`, `/help`, `/set_keys`.
5. В админке выдайте пользователю подписку (`Subscriptions`). Без неё торговые команды закрыты.

Миграции и создание админа выполняются автоматически при старте контейнеров.

Сборка образа тянет Python и Node.js с Docker Hub. Если из РФ это зависает, поднимите только базу и запустите бота локально (см. ниже).

## Локально (если Docker-образ не собирается)

Postgres всё равно из Docker, бот и админка — с хоста:

```bash
docker compose up -d scalpingdb
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
set POSTGRES_HOST=localhost
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py ensure_defaults
.venv\Scripts\python manage.py runserver 0.0.0.0:8000
.venv\Scripts\python bot\tg_bot.py
```

Пакет `mexc-sdk` на Windows/Python 3.12 может не встать — торговля идёт через свой REST-клиент, для демо это не мешает.

## Две пары = два бота на Ubuntu

Один сервер, два стека Docker. Код один, меняются только `.env` / `.env.pair2` (`PAIR` и `TELEGRAM_TOKEN`).

### 1. Сервер

Ubuntu 22.04/24.04, 2–4 GB RAM, 2 vCPU. Если сервер в РФ — заранее SOCKS5/HTTP прокси в ЕС для Telegram (MEXC идёт напрямую с IP сервера).

### 2. Залить проект и запустить мастер

```bash
sudo apt-get update
sudo apt-get install -y unzip
sudo mkdir -p /opt/scalping_bot
sudo unzip -o scalping_bot.zip -d /opt/scalping_bot
cd /opt/scalping_bot
sudo bash install.sh
```

Скрипт сам спросит: сколько ботов, токены Telegram (проверит через getMe и покажет @username), нужен ли прокси, логин/пароль админки и пароль базы. Секретные ключи Django сгенерирует сам. В конце выведет адреса и сохранит копию в `CREDENTIALS.txt`.

Файлы `.env` руками заполнять не нужно.

Если установка уже была и нужно только пересобрать:

```bash
cd /opt/scalping_bot
sudo bash install.sh
```

(мастер перезапишет конфиг — токены спросят заново).

### 3. Проверка

- Админка 1: `http://IP:8000/admin/`
- Админка 2: `http://IP:8001/admin/`
- Логин и пароль — в конце установки и в файле `CREDENTIALS.txt`
- В Telegram у каждого бота `/start`, `/help`
- В админке: **Subscriptions** → подписка пользователю
- IP сервера добавить в whitelist API-ключа MEXC (скрипт печатает IP)

```bash
docker compose -p scalping --env-file .env logs -f scalpingbot
docker compose -p scalping-pair2 --env-file .env.pair2 logs -f scalpingbot
```

В логе при прокси: `Telegram proxy enabled`. При старте: `Starting polling`.

### 4. После деплоя

1. `/set_keys` — ключи MEXC (спот + IP сервера).
2. `/parameters` — профит, падение, пауза, сумма.
3. `/autobuy`.

Чтобы поставить заново (токены спросят снова):

```bash
cd /opt/scalping_bot
sudo bash install.sh
```

## Если Telegram не открывается из РФ

В `.env` укажите прокси:

```env
TELEGRAM_PROXY=http://127.0.0.1:1080
```

Поддерживаются HTTP и SOCKS5 (`socks5://...`). Либо запускайте сервер в ЕС / через VPN.

## Переменные `.env`

| Переменная | Смысл |
|---|---|
| `PAIR` | Пара этого бота, например `BTC/USDT`. Новые пользователи получают её автоматически. |
| `TELEGRAM_TOKEN` | Токен BotFather |
| `TELEGRAM_PROXY` | Прокси до Telegram API (необязательно) |
| `WEB_PORT` | Порт админки на хосте |
| `POSTGRES_*` | База. В Docker `POSTGRES_HOST=scalpingdb` |
| `DJANGO_SUPERUSER_*` | Админ, создаётся при первом старте |
| `NOTIFICATION_CHAT_ID` | Куда слать ошибки |

API-ключи MEXC пользователь вводит командой `/set_keys`, не в `.env`. На ключе нужны спот-торговля и IP сервера в whitelist.

## Тонкая настройка автобая (необязательно)

Логика покупок настраивается в `/parameters` (профит, падение, пауза, сумма).
Поведение покупок **на росте** дополнительно регулируется переменными окружения —
значения по умолчанию подходят для большинства пар, менять их не обязательно.

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `RISE_TREND_TOLERANCE_PCT` | `0.05` | Допустимый откат mid-цены внутри окна анализа роста, %. `0` = ни одного тика вниз (на живом рынке покупка почти никогда не срабатывает). |
| `RISE_MIN_PCT` | `0` | Минимальный чистый рост к уровню триггера на момент покупки, %. |
| `RISE_MAX_BUYS_PER_CYCLE` | `3` | Сколько покупок на росте допускается в одном цикле (до закрытия всех сделок). |
| `RISE_BUY_COOLDOWN_SEC` | `3` | Минимальный интервал между покупками на росте, сек. |
| `DROP_BUY_COOLDOWN_SEC` | `10` | Антидребезг покупок на падении, сек. |
| `BUY_RETRY_DELAY_SEC` | `30` | Через сколько повторить покупку после ошибки. |

Окно анализа роста = значение `пауза` из `/parameters`. Бот открывает окно, когда
цена доходит до уровня триггера, и покупает, если к концу окна цена выше уровня, а
просадка внутри окна не превысила `RISE_TREND_TOLERANCE_PCT`.

## Команды бота

`/start` `/help` `/ping` `/set_keys` — без подписки.  
`/parameters` `/autobuy` `/stop` `/buy` `/price` `/balance` `/status` `/stats` `/subscription` `/faq` — с подпиской.

## Логи

```bash
docker compose logs -f scalpingbot
```
