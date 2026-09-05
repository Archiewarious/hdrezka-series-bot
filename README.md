# HDREZKA → Telegram

Бот, который следит за выходом новых серий на HDREZKA и присылает уведомления.
Подписаться можно на **один сезон** или на **всю франшизу** — тогда бот сообщит
и о новых сезонах, фильмах и спин-оффах.

Проектные решения и факты, на которых они основаны — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Как это устроено

```
 лента /series, /animation, /cartoons  ──►  poller  ──►  PostgreSQL  ──►  sender  ──►  Telegram
 (3 запроса раз в 3 минуты)                   │              ▲
 страницы тайтлов (озвучки, франшиза,         │              │
 расписание) — по событию / раз в сутки ──────┘             bot (поиск, подписки)
```

- **Нагрузка на сайт не зависит от числа пользователей**: опрашивается лента
  обновлений, а не подписки. ~70–100 запросов в час при 1 и при 100 000 подписчиков.
- **Франшизы берутся с сайта** — из блока «другие части», который стоит на каждой
  странице, включая фильмы. Никаких эвристик по названиям.
- **Озвучки**: по умолчанию уведомление при первом появлении серии; можно выбрать
  конкретные — тогда бот проверит их через тот же ajax, что использует плеер сайта.
- **Только Postgres**: хранение, очередь рассылки (`FOR UPDATE SKIP LOCKED`),
  singleton-лок поллера (advisory lock). Redis и брокеры не нужны.

## Доступ к сайту

| Проблема | Решение |
|---|---|
| Дата-центровые IP (Oracle, Cloudflare WARP…) забанены — 403 «Ошибка доступа 105» | SOCKS5 через SSH-туннель на сервер с чистым IP (`deploy/rezka-tunnel.service`) |
| Anubis proof-of-work | решается в `app/rezka/client.py`, браузер не нужен; cookie живёт 30 дней |

Вся эта логика изолирована в `RezkaClient`; остальной код видит только HTML.

## Установка

```bash
cp .env.example .env      # BOT_TOKEN, ADMIN_IDS, POSTGRES_PASSWORD
```

Туннель (без него сайт недоступен). В юните — адрес, порт и пользователь
вашего сервера-выхода; замените плейсхолдеры:

```bash
sudo cp deploy/rezka-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now rezka-tunnel
```

Проверка доступа и запуск:

```bash
docker compose run --rm --no-deps poller python -m app.check_access
docker compose up -d --build       # migrate → bot, poller, sender
```

## Тесты

Парсер, вывод «завершён по расписанию» и рендер уведомлений проверяются на живом HTML,
сохранённом в `tests/fixtures/` (см. там README). Запуск — внутри образа, секунды:

```bash
docker compose run --rm --no-deps bot python -m pytest
```

## Обслуживание

```bash
docker compose logs -f poller                       # что происходит
docker compose run --rm --no-deps poller python -m app.check_access   # доступ после смены IP/зеркала
docker compose exec -T postgres pg_dump -U rezka rezka | gzip > backup-$(date +%F).sql.gz
```

Схема БД — Alembic (`migrations/`). Изменили `app/models.py` →
`docker compose run --rm -v $PWD/migrations:/srv/migrations migrate alembic revision --autogenerate -m "…"`,
применится при следующем `docker compose up`.

Команда `/stats` в боте — для `ADMIN_IDS`; там же видно, если поллер молчит дольше
`STALE_ALERT_MINUTES`. Технические уведомления в Telegram не отправляются никогда —
состояние смотрите в логах и в `/stats`.

## Структура

```
app/rezka/client.py   доступ: прокси, Anubis, зеркала, паузы
app/rezka/parser.py   лента, страница тайтла (озвучки, серии, франшиза, расписание), ajax
app/service.py        доменные операции над БД (общие для бота и поллера); локальный поиск
app/poller.py         лента → события; франшизы; очередь чтения каталога; проверки озвучек
app/sender.py         очередь → Telegram (фото/дайджест, кнопки); watchdog; очистка
app/posters.py        постеры: скачивание с CDN, загрузка в Telegram, кэш file_id
app/bot/main.py       поиск, подписки, озвучки, Новое, Настройки, поделиться
app/bot/search.py     группировка локальной выдачи по франшизам
tests/                pytest на сохранённом HTML сайта
docs/ARCHITECTURE.md  решения и факты; docs/PRODUCT_AND_SCALE.md — продукт и масштаб
```
