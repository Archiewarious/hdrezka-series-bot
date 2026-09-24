#!/bin/bash
# Проверка бэкапа: восстановить дамп в отдельную базу rezka_restore_check, показать число строк, удалить базу.
# Использование: deploy/restore-check.sh [/var/backups/rezka/rezka-YYYYMMDD-HHMM.dump]  (по умолчанию — свежайший)
set -euo pipefail
PROJECT=$(cd "$(dirname "$0")/.." && pwd)
LOCAL_DIR=/var/backups/rezka
# shellcheck source=backup.env.example
[ -f "$PROJECT/deploy/backup.env" ] && source "$PROJECT/deploy/backup.env"
file=${1:-$(ls -1t "${LOCAL_DIR:-/var/backups/rezka}"/rezka-*.dump | head -1)}
db=rezka_restore_check
cd "$PROJECT"
PSQL=(sudo -n docker compose exec -T postgres psql -U rezka -d postgres -qAt)
"${PSQL[@]}" -c "DROP DATABASE IF EXISTS $db;" -c "CREATE DATABASE $db;"
sudo -n docker compose exec -T postgres pg_restore -U rezka -d "$db" --no-owner --exit-on-error < "$file"
echo "restored $file into $db:"
sudo -n docker compose exec -T postgres psql -U rezka -d "$db" -At -c "
  SELECT 'users ' || count(*) FROM users UNION ALL
  SELECT 'pages ' || count(*) FROM pages UNION ALL
  SELECT 'subscriptions ' || count(*) FROM subscriptions UNION ALL
  SELECT 'notifications ' || count(*) FROM notifications UNION ALL
  SELECT 'alembic ' || version_num FROM alembic_version;"
"${PSQL[@]}" -c "DROP DATABASE $db;"
echo "restore check ok, $db dropped"
