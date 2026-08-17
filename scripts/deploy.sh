#!/usr/bin/env bash
# Мастер установки скальпинг-бота на Ubuntu.
# Заказчику достаточно:  sudo bash install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log()  { printf '\n\033[1;36m%s\033[0m\n' "$*" >/dev/tty; }
ok()   { printf '\033[1;32m%s\033[0m\n' "$*" >/dev/tty; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*" >/dev/tty; }
err()  { printf '\033[1;31m%s\033[0m\n' "$*" >/dev/tty; }
die()  { err "ОШИБКА: $*"; exit 1; }

tty_in() {
  if [[ -t 0 ]]; then
    cat
  else
    cat </dev/tty
  fi
}

ask() {
  local prompt="$1" default="${2:-}" reply
  if [[ -n "$default" ]]; then
    printf '%s [%s]: ' "$prompt" "$default" >/dev/tty
  else
    printf '%s: ' "$prompt" >/dev/tty
  fi
  IFS= read -r reply </dev/tty || true
  if [[ -z "$reply" ]]; then
    printf '%s' "$default"
  else
    printf '%s' "$reply"
  fi
}

ask_required() {
  local prompt="$1" value=""
  while [[ -z "$value" ]]; do
    value="$(ask "$prompt" "")"
    value="$(printf '%s' "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -n "$value" ]] || warn "Это поле обязательно, попробуйте ещё раз."
  done
  printf '%s' "$value"
}

ask_secret() {
  local prompt="$1" a="" b=""
  while true; do
    printf '%s: ' "$prompt" >/dev/tty
    stty -echo </dev/tty
    IFS= read -r a </dev/tty || true
    stty echo </dev/tty
    printf '\n' >/dev/tty
    printf 'Повторите пароль: ' >/dev/tty
    stty -echo </dev/tty
    IFS= read -r b </dev/tty || true
    stty echo </dev/tty
    printf '\n' >/dev/tty
    if [[ -z "$a" ]]; then
      warn "Пароль не может быть пустым."
      continue
    fi
    if [[ "$a" != "$b" ]]; then
      warn "Пароли не совпали. Ещё раз."
      continue
    fi
    if printf '%s' "$a" | grep -q '[$="'\''[:space:]]'; then
      warn "Не используйте пробелы, кавычки, знак = и знак \$ в пароле."
      continue
    fi
    printf '%s' "$a"
    return
  done
}

ask_yes_no() {
  local prompt="$1" default="${2:-y}" reply
  local hint="y/n"
  [[ "$default" == "y" ]] && hint="Y/n"
  [[ "$default" == "n" ]] && hint="y/N"
  while true; do
    printf '%s [%s]: ' "$prompt" "$hint" >/dev/tty
    IFS= read -r reply </dev/tty || true
    reply="$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')"
    [[ -z "$reply" ]] && reply="$default"
    case "$reply" in
      y|yes|д|да) printf 'y'; return ;;
      n|no|н|нет) printf 'n'; return ;;
      *) warn "Введите да или нет (y/n)." ;;
    esac
  done
}

random_secret() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
}

random_password() {
  python3 - <<'PY'
import secrets, string
alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(20)))
PY
}

need_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    warn "Нужны права администратора. Перезапускаю через sudo..."
    exec sudo -E bash "$0" "$@"
  fi
  die "Запустите скрипт так: sudo bash install.sh"
}

install_packages() {
  log "Шаг 1/5. Проверяю систему и программы"
  command -v apt-get >/dev/null 2>&1 || die "Этот скрипт рассчитан на Ubuntu / Debian."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg python3
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok "Docker уже установлен."
    return
  fi
  log "Ставлю Docker (это займёт несколько минут, можно отойти)"
  install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
  ok "Docker установлен."
}

# На многих VPS AAAA-запись api.telegram.org есть, а IPv6 не маршрутизируется.
# Старый curl без -4 зависал/отдавал пустоту — выглядело как «нет подключения».
check_token() {
  local token="$1" proxy="$2"
  python3 - "$token" "$proxy" <<'PY'
import json, socket, subprocess, sys, urllib.error, urllib.request

token = sys.argv[1]
proxy = (sys.argv[2] if len(sys.argv) > 2 else "").strip()
url = f"https://api.telegram.org/bot{token}/getMe"
errors = []

_old_getaddrinfo = socket.getaddrinfo

def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    infos = _old_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    if infos:
        return infos
    return _old_getaddrinfo(host, port, family, type, proto, flags)


def parse_ok(raw: str) -> str:
    data = json.loads(raw)
    if not data.get("ok"):
        desc = data.get("description") or data.get("error_code") or raw[:200]
        return f"FAIL|||Токен не принят: {desc}"
    r = data.get("result") or {}
    username = r.get("username") or ""
    name = r.get("first_name") or ""
    tid = r.get("id") or ""
    uname = f"@{username}" if username else "(без username)"
    return f"OK|||{uname}|||{name}|||{tid}"


def try_urllib() -> str:
    socket.getaddrinfo = ipv4_getaddrinfo
    handlers = []
    if proxy:
        if proxy.startswith("socks"):
            raise RuntimeError("SOCKS через urllib пропускаем, будет curl")
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "scalping-bot-setup"})
    with opener.open(req, timeout=20) as resp:
        return parse_ok(resp.read().decode("utf-8", "replace"))


def try_curl(force_ipv4: bool) -> str:
    cmd = ["curl", "-sS", "--connect-timeout", "12", "--max-time", "25", "-A", "scalping-bot-setup"]
    if force_ipv4:
        cmd.append("-4")
    if proxy:
        cmd.extend(["-x", proxy])
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    raw = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if raw:
        try:
            return parse_ok(raw)
        except Exception as e:
            return f"FAIL|||Непонятный ответ Telegram: {e}; {raw[:200]}"
    extra = err or f"код curl {proc.returncode}"
    raise RuntimeError(extra)


for label, fn in (
    ("python/IPv4", try_urllib),
    ("curl/IPv4", lambda: try_curl(True)),
    ("curl", lambda: try_curl(False)),
):
    try:
        result = fn()
        print(result)
        raise SystemExit(0)
    except Exception as e:
        errors.append(f"{label}: {e}")

print("FAIL|||Нет связи с api.telegram.org. " + " | ".join(errors))
PY
}

ask_token() {
  local label="$1" proxy="$2" other="${3:-}"
  local token parsed status uname name tid
  while true; do
    echo >/dev/tty
    echo "Токен берётся у @BotFather в Telegram: /newbot → он пришлёт длинную строку вида 123456:AA...." >/dev/tty
    token="$(ask_required "Вставьте токен для $label")"
    token="$(printf '%s' "$token" | tr -d '[:space:]')"
    if [[ "$token" != *:* ]]; then
      warn "Это не похоже на токен. Должно быть число, двоеточие и латинские буквы."
      continue
    fi
    if [[ -n "$other" && "$token" == "$other" ]]; then
      warn "У второго бота должен быть ДРУГОЙ токен (другой бот в BotFather)."
      continue
    fi
    echo "Проверяю токен в Telegram..." >/dev/tty
    parsed="$(check_token "$token" "$proxy")"
    status="${parsed%%|||*}"
    if [[ "$status" != "OK" ]]; then
      warn "${parsed#FAIL|||}"
      if [[ -z "$proxy" ]]; then
        warn "Если сервер в России, Telegram часто не открывается без прокси. Можно вернуться и указать прокси."
      fi
      continue
    fi
    uname="$(printf '%s' "$parsed" | awk -F'\\|\\|\\|' '{print $2}')"
    name="$(printf '%s' "$parsed" | awk -F'\\|\\|\\|' '{print $3}')"
    tid="$(printf '%s' "$parsed" | awk -F'\\|\\|\\|' '{print $4}')"
    ok "Токен верный. Это бот: $uname  ($name, id $tid)"
    printf '%s|||%s|||%s|||%s' "$token" "$uname" "$name" "$tid"
    return
  done
}

write_env() {
  python3 - "$@" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
# key=value pairs after path
items = sys.argv[2:]
lines = [
    "# Сгенерировано scripts/deploy.sh. Не публикуйте этот файл.",
    "",
]
for item in items:
    if "=" not in item:
        continue
    key, value = item.split("=", 1)
    value = value.replace("\r", "").replace("\n", "")
    lines.append(f"{key}={value}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

public_ip() {
  curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || echo "UNKNOWN"
}

wait_http() {
  local port="$1" tries=40
  local i
  for i in $(seq 1 "$tries"); do
    if curl -fsS --max-time 2 -o /dev/null -w '' "http://127.0.0.1:${port}/admin/" 2>/dev/null; then
      return 0
    fi
    # 302 is also ok
    code="$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/admin/" 2>/dev/null || true)"
    if [[ "$code" == "200" || "$code" == "301" || "$code" == "302" ]]; then
      return 0
    fi
    sleep 3
  done
  return 1
}

[[ -f "$ROOT/docker-compose.yml" ]] || die "Запустите скрипт из папки проекта (не найден docker-compose.yml)."

clear 2>/dev/null || true
cat >/dev/tty <<'EOF'
============================================================
  Установка торговых Telegram-ботов (MEXC)
============================================================

Скрипт сам:
  • поставит Docker
  • спросит, сколько ботов нужно (1 или 2)
  • проверит токены Telegram
  • создаст пароли и конфиги
  • запустит ботов и админ-панель

Ничего руками в файлах прописывать не нужно.
На вопросы можно жать Enter — тогда используется значение по умолчанию.
============================================================
EOF

need_root "$@"
install_packages
install_docker
command -v docker >/dev/null 2>&1 || die "Docker не установился."
docker compose version >/dev/null 2>&1 || die "Не найден docker compose. Установите пакет docker-compose-plugin."

log "Шаг 2/5. Сколько ботов запустить?"
echo "Один бот = одна торговая пара (например BTC/USDT)." >/dev/tty
echo "Два бота = две пары, два разных Telegram-бота от BotFather." >/dev/tty
BOTS_COUNT=""
while [[ "$BOTS_COUNT" != "1" && "$BOTS_COUNT" != "2" ]]; do
  BOTS_COUNT="$(ask "Сколько ботов поставить?" "2")"
done

log "Шаг 3/5. Связь с Telegram"
echo "Если этот сервер в России, Telegram почти всегда нужен прокси (SOCKS5 или HTTP в Европе)." >/dev/tty
echo "Биржа MEXC при этом продолжит работать с IP этого сервера." >/dev/tty
echo "Пример: socks5://login:password@10.0.0.1:1080" >/dev/tty
PROXY=""
if [[ "$(ask_yes_no "Нужен прокси для Telegram?" "n")" == "y" ]]; then
  while true; do
    PROXY="$(ask_required "Вставьте строку прокси")"
    PROXY="$(printf '%s' "$PROXY" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    echo "Проверяю прокси на api.telegram.org ..." >/dev/tty
    if curl -4 -sS --connect-timeout 12 --max-time 20 -x "$PROXY" "https://api.telegram.org/bot123:AAA/getMe" 2>/dev/null | grep -q 'Unauthorized\|ok'; then
      ok "Прокси отвечает, Telegram доступен."
      break
    fi
    code="$(curl -4 -sS --connect-timeout 12 --max-time 20 -x "$PROXY" -o /tmp/tg_proxy_check.json -w '%{http_code}' "https://api.telegram.org/bot123:AAA/getMe" || true)"
    if [[ "$code" == "401" || "$code" == "200" ]]; then
      ok "Прокси отвечает, Telegram доступен."
      break
    fi
    warn "Через этот прокси Telegram не открылся (код: ${code:-нет ответа}). Проверьте строку."
    [[ "$(ask_yes_no "Попробовать другой прокси?" "y")" == "y" ]] || die "Без рабочего прокси бот из РФ не запустится."
  done
fi

log "Шаг 4/5. Админка и база данных"
echo "Это логин и пароль для страницы управления в браузере." >/dev/tty
ADMIN_USER="$(ask "Логин администратора" "admin")"
[[ -n "$ADMIN_USER" ]] || ADMIN_USER="admin"
if [[ "$(ask_yes_no "Придумать пароль админки самостоятельно?" "y")" == "y" ]]; then
  ADMIN_PASS="$(ask_secret "Пароль админки")"
else
  ADMIN_PASS="$(random_password)"
  ok "Пароль админки сгенерирован автоматически."
fi

echo >/dev/tty
echo "Пароль базы данных нужен только системе. Вам его запоминать не обязательно." >/dev/tty
if [[ "$(ask_yes_no "Задать пароль базы данных самостоятельно?" "n")" == "y" ]]; then
  PG_PASS="$(ask_secret "Пароль базы данных")"
else
  PG_PASS="$(random_password)"
  ok "Пароль базы данных сгенерирован автоматически."
fi

PAIR1="BTC/USDT"
PAIR2="ETH/USDT"
TOKEN1="" TOKEN2=""
UNAME1="" UNAME2=""
NAME1="" NAME2=""

log "Настройка бота №1"
PAIR1="$(ask "Торговая пара первого бота (как на MEXC)" "BTC/USDT")"
info1="$(ask_token "бота №1" "$PROXY" "")"
TOKEN1="${info1%%|||*}"
rest="${info1#*|||}"
UNAME1="${rest%%|||*}"
rest="${rest#*|||}"
NAME1="${rest%%|||*}"

if [[ "$BOTS_COUNT" == "2" ]]; then
  log "Настройка бота №2"
  PAIR2="$(ask "Торговая пара второго бота" "ETH/USDT")"
  if [[ "$PAIR2" == "$PAIR1" ]]; then
    warn "Пары одинаковые. Обычно для двух ботов берут разные пары."
    [[ "$(ask_yes_no "Оставить как есть?" "n")" == "y" ]] || PAIR2="$(ask_required "Другая торговая пара для бота №2")"
  fi
  info2="$(ask_token "бота №2" "$PROXY" "$TOKEN1")"
  TOKEN2="${info2%%|||*}"
  rest="${info2#*|||}"
  UNAME2="${rest%%|||*}"
  rest="${rest#*|||}"
  NAME2="${rest%%|||*}"
fi

IP="$(public_ip)"
SECRET1="$(random_secret)"
SECRET2="$(random_secret)"

log "Сохраняю настройки"
write_env "$ROOT/.env" \
  "PAIR=${PAIR1}" \
  "TELEGRAM_TOKEN=${TOKEN1}" \
  "TELEGRAM_PROXY=${PROXY}" \
  "NOTIFICATION_CHAT_ID=" \
  "DJANGO_SECRET_KEY=${SECRET1}" \
  "DEBUG=True" \
  "DJANGO_SUPERUSER_USERNAME=${ADMIN_USER}" \
  "DJANGO_SUPERUSER_PASSWORD=${ADMIN_PASS}" \
  "DJANGO_SUPERUSER_EMAIL=admin@localhost" \
  "WEB_PORT=8000" \
  "CSRF_TRUSTED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000,http://${IP}:8000" \
  "POSTGRES_DB=scalping" \
  "POSTGRES_USER=scalping" \
  "POSTGRES_PASSWORD=${PG_PASS}" \
  "POSTGRES_HOST=scalpingdb" \
  "POSTGRES_PORT=5432" \
  "POSTGRES_PUBLISH_PORT=5432" \
  "POSTGRES_PUBLISH_BIND=127.0.0.1"

if [[ "$BOTS_COUNT" == "2" ]]; then
  write_env "$ROOT/.env.pair2" \
    "PAIR=${PAIR2}" \
    "TELEGRAM_TOKEN=${TOKEN2}" \
    "TELEGRAM_PROXY=${PROXY}" \
    "NOTIFICATION_CHAT_ID=" \
    "DJANGO_SECRET_KEY=${SECRET2}" \
    "DEBUG=True" \
    "DJANGO_SUPERUSER_USERNAME=${ADMIN_USER}" \
    "DJANGO_SUPERUSER_PASSWORD=${ADMIN_PASS}" \
    "DJANGO_SUPERUSER_EMAIL=admin@localhost" \
    "WEB_PORT=8001" \
    "CSRF_TRUSTED_ORIGINS=http://localhost:8001,http://127.0.0.1:8001,http://${IP}:8001" \
    "POSTGRES_DB=scalping_pair2" \
    "POSTGRES_USER=scalping" \
    "POSTGRES_PASSWORD=${PG_PASS}" \
    "POSTGRES_HOST=scalpingdb" \
    "POSTGRES_PORT=5432" \
    "POSTGRES_PUBLISH_PORT=5433" \
    "POSTGRES_PUBLISH_BIND=127.0.0.1" \
    "BOT_ENV_FILE=.env.pair2"
fi

chmod 600 "$ROOT/.env" 2>/dev/null || true
[[ -f "$ROOT/.env.pair2" ]] && chmod 600 "$ROOT/.env.pair2" 2>/dev/null || true

log "Шаг 5/5. Запускаю сервисы (сборка образа может занять 5–15 минут)"
echo "Первый запуск долгий: качается Python, Node.js и собирается бот. Это нормально." >/dev/tty

docker compose -p scalping --env-file "$ROOT/.env" up -d --build
if [[ "$BOTS_COUNT" == "2" ]]; then
  docker compose -p scalping-pair2 --env-file "$ROOT/.env.pair2" up -d --build
fi

echo >/dev/tty
echo "Жду, пока админка начнёт отвечать..." >/dev/tty
wait_http 8000 && ok "Админка бота №1 открылась." || warn "Админка :8000 пока не ответила — смотрите логи, сборка могла ещё идти."
if [[ "$BOTS_COUNT" == "2" ]]; then
  wait_http 8001 && ok "Админка бота №2 открылась." || warn "Админка :8001 пока не ответила — смотрите логи."
fi

{
  echo "========================================"
  echo "  Данные для входа (сохраните файл)"
  echo "========================================"
  echo "Дата: $(date)"
  echo "IP сервера: $IP"
  echo
  echo "Админка бота №1:  http://${IP}:8000/admin/"
  echo "Telegram:         $UNAME1  ($NAME1)"
  echo "Пара:             $PAIR1"
  echo
  if [[ "$BOTS_COUNT" == "2" ]]; then
    echo "Админка бота №2:  http://${IP}:8001/admin/"
    echo "Telegram:         $UNAME2  ($NAME2)"
    echo "Пара:             $PAIR2"
    echo
  fi
  echo "Логин админки:    $ADMIN_USER"
  echo "Пароль админки:   $ADMIN_PASS"
  echo "Пароль базы:      $PG_PASS"
  echo
  echo "Что сделать дальше:"
  echo "1) Откройте бота в Telegram → /start → /set_keys (ключи MEXC)."
  echo "2) В админке: Subscriptions → добавьте подписку этому пользователю."
  echo "3) В API-ключе MEXC в белый список IP добавьте: $IP"
  echo "4) /parameters и /autobuy"
  echo "========================================"
} | tee "$ROOT/CREDENTIALS.txt"
chmod 600 "$ROOT/CREDENTIALS.txt" 2>/dev/null || true

echo >/dev/tty
ok "Готово. Копия этих данных сохранена в файл CREDENTIALS.txt в папке проекта."
echo "Логи бота №1:  docker compose -p scalping --env-file .env logs -f scalpingbot" >/dev/tty
if [[ "$BOTS_COUNT" == "2" ]]; then
  echo "Логи бота №2:  docker compose -p scalping-pair2 --env-file .env.pair2 logs -f scalpingbot" >/dev/tty
fi
echo >/dev/tty
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' >/dev/tty || true
