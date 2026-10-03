#!/bin/bash
# Бэкап базы бота: pg_dump (custom-формат, сжатый) → локально + копия в Oracle Object Storage.
# Запускается таймером rezka-backup.timer от пользователя из юнита (docker — через sudo -n).
# Восстановление проверяется deploy/restore-check.sh; из офсайт-копии — README, «Обслуживание».
set -euo pipefail

PROJECT=$(cd "$(dirname "$0")/.." && pwd)
# Ссылка на запись в бакет — в deploy/backup.env (не в git; образец — backup.env.example).
# shellcheck source=backup.env.example
source "$PROJECT/deploy/backup.env"
: "${OFFSITE_URL:?OFFSITE_URL в deploy/backup.env}"
LOCAL_DIR=${LOCAL_DIR:-/var/backups/rezka}
KEEP_LOCAL=${KEEP_LOCAL:-7}

stamp=$(date -u +%Y%m%d-%H%M)
file="$LOCAL_DIR/rezka-$stamp.dump"

mkdir -p "$LOCAL_DIR"
cd "$PROJECT"
sudo -n docker compose exec -T postgres pg_dump -U rezka -Fc --no-owner rezka > "$file.tmp"
mv "$file.tmp" "$file"
size=$(du -h "$file" | cut -f1)

# Офсайт — по ссылке «только запись» (pre-authenticated request): прочитать или удалить копии по ней нельзя.
# Хранение — правило бакета: daily/ удаляется через 30 дней. MD5 из ответа сверяем: копия дошла целой.
object="daily/$(basename "$file")"
want=$(openssl dgst -md5 -binary "$file" | base64)
got=$(curl -sS --fail --retry 3 --max-time 300 -D - -o /dev/null -T "$file" "$OFFSITE_URL$object" \
      | tr -d '\r' | awk -F': ' 'tolower($1) == "opc-content-md5" {print $2}')
if [ "$got" != "$want" ]; then
    echo "офсайт-копия $object не подтверждена: MD5 файла $want, в ответе ${got:-нет}" >&2
    exit 1
fi
ls -1t "$LOCAL_DIR"/rezka-*.dump | tail -n +$((KEEP_LOCAL + 1)) | xargs -r rm -f

echo "backup ok: $file ($size), offsite: $object"
