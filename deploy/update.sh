#!/bin/bash
# Обновление прода (24.09.2026): сначала свежий бэкап, потом сборка и перезапуск. Миграции применяет сервис
# migrate до старта bot/poller/sender; если миграция упадёт, сервисы не стартуют — восстановление из этого бэкапа.
#   deploy/update.sh              — из каталога репозитория
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== бэкап"
sudo systemctl start rezka-backup.service
sudo systemctl is-failed --quiet rezka-backup.service && { echo "бэкап не удался — не обновляю"; exit 1; }

echo "== сборка и перезапуск"
docker compose up -d --build

echo "== состояние"
docker compose ps
docker compose exec -T postgres psql -U rezka -d rezka -tAc "SELECT version_num FROM alembic_version"
