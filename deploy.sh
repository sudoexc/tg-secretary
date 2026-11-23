#!/usr/bin/env bash
# Выкатка на Netcup: rsync исходников, сборка и перезапуск контейнера, хвост логов.
# .env на сервере свой (chmod 600), отсюда не копируется. Выкатываем только закоммиченное.
# Запуск: ./deploy.sh. Если файл подключить через source, он только объявит функцию deploy и ничего не сделает.

HOST="root@62.83.87.250"
DIR="/opt/tg-secretary"

deploy() (
  # Подоболочка: set -e и cd не утекают в shell, который вызвал функцию.
  set -euo pipefail
  local ssh=(ssh -o IdentitiesOnly=yes -i "$HOME/.ssh/id_ed25519")
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
  cd "$here"

  if [ -n "$(git status --porcelain)" ]; then
    echo "Есть незакоммиченные изменения: сначала commit, потом deploy." >&2
    git status --short >&2
    exit 1
  fi
  if [ ! -f kb/knowledge.md ] || [ ! -f kb/style.md ]; then
    echo "Нет kb/knowledge.md или kb/style.md: образ не соберётся." >&2
    exit 1
  fi

  echo "==> $(git rev-parse --short HEAD) -> $HOST:$DIR"
  "${ssh[@]}" "$HOST" "mkdir -p $DIR"
  rsync -az --delete \
    --exclude=/.venv \
    --exclude=/data \
    --exclude=/.env \
    --exclude=/.git \
    --exclude=/tests \
    --exclude=/.pytest_cache \
    --exclude=__pycache__ \
    --exclude='*.pyc' \
    --exclude=.DS_Store \
    -e "${ssh[*]}" ./ "$HOST:$DIR/"

  # Команды строкой, а не через stdin: так их не съест ни одна команда на сервере.
  "${ssh[@]}" "$HOST" "set -euo pipefail
cd $DIR
if [ ! -f .env ]; then echo 'На сервере нет $DIR/.env: создай его по .env.example (chmod 600).' >&2; exit 1; fi
chmod 600 .env
# Архив личных переписок: только uid контейнера (и root). Внутри файлы создаются с umask 077.
mkdir -p data && chown 10001:10001 data && chmod 700 data && chmod -R go-rwx data
docker compose up -d --build </dev/null
sleep 5
docker compose ps </dev/null
docker compose logs --tail=40 tg-secretary </dev/null"
)

# Только при прямом запуске, не при source.
if [ "${BASH_SOURCE[0]:-}" = "$0" ]; then
  deploy "$@"
fi
