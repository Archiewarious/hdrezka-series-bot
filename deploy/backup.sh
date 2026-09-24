#!/bin/bash
# Бэкап базы бота: pg_dump (custom-формат, сжатый) → локально + зашифрованная копия на сервер-выход по SSH.
# Запускается таймером rezka-backup.timer от пользователя из юнита (docker — через sudo -n).
# Восстановление проверяется deploy/restore-check.sh; из копии на сервере-выходе — README, «Обслуживание».
set -euo pipefail

PROJECT=$(cd "$(dirname "$0")/.." && pwd)
# Адрес сервера-выхода, порт и ключ — в deploy/backup.env (не в git; образец — backup.env.example).
# shellcheck source=backup.env.example
source "$PROJECT/deploy/backup.env"
: "${REMOTE_HOST:?REMOTE_HOST в deploy/backup.env}" "${REMOTE_PORT:=22}" "${SSH_KEY:?SSH_KEY в deploy/backup.env}"
# Копия на сервер-выход — только зашифрованной (age, 24.09.2026): в дампе id и имена людей, а сервер-выход нужен
# лишь как сетевой выход. AGE_RECIPIENT — публичный ключ; секретный — только у владельца, не на серверах.
: "${AGE_RECIPIENT:?AGE_RECIPIENT в deploy/backup.env — без шифрования копию на сервер-выход не отправляю}"
command -v age >/dev/null || { echo "age не установлен (sudo apt install age) — копию на сервер-выход не отправляю" >&2; exit 1; }
LOCAL_DIR=${LOCAL_DIR:-/var/backups/rezka}
REMOTE_DIR=${REMOTE_DIR:-rezka-backups}
KEEP_LOCAL=${KEEP_LOCAL:-7}
KEEP_REMOTE=${KEEP_REMOTE:-30}

SSH_OPTS=(-p "$REMOTE_PORT" -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new)
stamp=$(date -u +%Y%m%d-%H%M)
file="$LOCAL_DIR/rezka-$stamp.dump"

mkdir -p "$LOCAL_DIR"
cd "$PROJECT"
sudo -n docker compose exec -T postgres pg_dump -U rezka -Fc --no-owner rezka > "$file.tmp"
mv "$file.tmp" "$file"
size=$(du -h "$file" | cut -f1)

age -r "$AGE_RECIPIENT" -o "$file.age" "$file"
scp -q -P "$REMOTE_PORT" -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new \
    "$file.age" "$REMOTE_HOST:$REMOTE_DIR/"
rm -f "$file.age"
# Ротация: на сервере-выходе — последние KEEP_REMOTE, локально — KEEP_LOCAL.
ssh "${SSH_OPTS[@]}" "$REMOTE_HOST" "cd $REMOTE_DIR && ls -1t rezka-*.dump* 2>/dev/null | tail -n +$((KEEP_REMOTE + 1)) | xargs -r rm -f"
ls -1t "$LOCAL_DIR"/rezka-*.dump | tail -n +$((KEEP_LOCAL + 1)) | xargs -r rm -f

echo "backup ok: $file ($size), encrypted copy on $REMOTE_HOST:$REMOTE_DIR/"
