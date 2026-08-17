#!/usr/bin/env bash
# Запуск:  sudo bash install.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$ROOT/scripts/deploy.sh" "$@"
