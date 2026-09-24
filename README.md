# HDREZKA → Telegram

[![tests](https://github.com/Archiewarious/hdrezka-series-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/Archiewarious/hdrezka-series-bot/actions/workflows/tests.yml)

**▶ Открыть бота / Try it: [@HDRezkaSeriesBot](https://t.me/HDRezkaSeriesBot)** — уведомления о новых сериях
сериалов и аниме с HDREZKA в Telegram: постер, номер серии, озвучка и кнопка «Смотреть».

**EN.** A Telegram bot that watches HDREZKA for new episodes and notifies subscribers — per season or per
franchise (new seasons, films, spin-offs). Python 3.12 · aiogram 3 · PostgreSQL 17 (delivery queue on
`FOR UPDATE SKIP LOCKED`, pg_trgm search, Alembic) · Docker Compose · three processes (bot / poller / sender) ·
pytest on saved HTML pages. Site load does not depend on the number of users: the poller reads the site's
update feed, not subscriptions. UI in Russian, Ukrainian and English. Docs are in Russian:
[ARCHITECTURE.md](docs/ARCHITECTURE.md), [PRODUCT_AND_SCALE.md](docs/PRODUCT_AND_SCALE.md).

Бот, который следит за выходом новых серий на HDREZKA и присылает уведомления.
Подписаться можно на **один сезон** или на **всю франшизу** — тогда бот сообщит
и о новых сезонах, фильмах и спин-оффах.

Проектные решения и факты, на которых они основаны — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Как это устроено

```
 главная: блок «Обновления»  ──►  poller  ──►  PostgreSQL  ──►  sender  ──►  Telegram
 (1 запрос раз в 3 минуты)          │               ▲
 страницы тайтлов: по событию,      │               │
 подписанные раз в 12 ч, очередь ───┘              bot (поиск, подписки; сайт — только с разрешения)
```

- **Нагрузка на сайт не зависит от числа пользователей**: опрашивается блок обновлений на главной, а не подписки.
  Бюджет — **30–60 запросов в час** на всё (рабочий IP один): блок 20 в час, чтение страниц ради событий, обновление
  подписанных, очередь каталога; бот — не больше 10 в минуту и 120 в час на всех (`BOT_SITE_PER_*`).
- **Каталог** наполняется из блока обновлений, поиска по сайту и блоков частей франшиз; ленты разделов больше
  не читаются (они не упорядочены по выходу серий, 10.09.2026). Непрочитанные страницы дочитывает очередь.
- **Франшизы берутся с сайта** — из блока «другие части», который стоит на каждой
  странице, включая фильмы. Никаких эвристик по названиям.
- **Озвучки**: по умолчанию уведомление при первом появлении серии, с названием озвучки; можно выбрать
  конкретные — тогда по уведомлению на каждую. Озвучку события сообщает блок обновлений на главной.
- **Только Postgres**: хранение, очередь рассылки (`FOR UPDATE SKIP LOCKED`),
  singleton-лок поллера (advisory lock). Redis и брокеры не нужны.
- **Три языка** интерфейса и уведомлений: русский, украинский, английский (`app/i18n.py`).
  Новому человеку — язык его Telegram, дальше — ⚙️ Настройки → 🌐 Язык.
- **Обратная связь**: ❓ Помощь → ✉️ Написать автору или `/feedback`. Тема (ошибка, идея, сотрудничество,
  другое), письмо уходит автору карточкой и копией — со скриншотами; автор отвечает «Ответить» на карточку,
  человек получает ответ от имени бота и может ответить так же. Тексты писем в базе не хранятся.

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

Юнит-тесты на сохранённом HTML сайта и интеграционные — путь события от блока обновлений до уведомления
на настоящем Postgres. Интеграционные запускаются только на отдельной базе с именем `*_test`:

```bash
docker compose --profile test build tests
docker compose --profile test run --rm --no-deps tests
```

Тесты — отдельная цель образа (`Dockerfile`, `target: test`): в боевом образе нет ни `tests/`, ни pytest.
`--no-deps` обязателен: без него compose запустит сервис `migrate` на рабочей базе. База `rezka_test` создаётся
один раз: `docker compose exec postgres psql -U rezka -c 'CREATE DATABASE rezka_test'`. В CI Postgres поднимается
сервисом GitHub Actions. Тесты не ходят в сеть: настоящий HTTP-запрос из теста — ошибка (`tests/conftest.py`).

Зависимости: верхний уровень — `requirements.in` и `requirements-dev.in`, закреплённые версии с хешами —
`requirements.txt` и `requirements-dev.txt`, ставятся с `--require-hashes`. Обновить:

```bash
uv pip compile requirements.in --universal --python-version 3.12 --generate-hashes -o requirements.txt
uv pip compile requirements-dev.in --universal --python-version 3.12 --generate-hashes -o requirements-dev.txt
```

## Обслуживание

```bash
docker compose logs -f poller                       # что происходит
docker compose run --rm --no-deps poller python -m app.check_access   # доступ после смены IP/зеркала
sudo systemctl start rezka-backup.service        # бэкап вручную (deploy/backup.sh)
deploy/restore-check.sh                             # проверить свежий дамп восстановлением в отдельную базу
```

Бэкапы: `deploy/rezka-backup.timer` раз в сутки (03:30 UTC) делает `pg_dump -Fc` в `/var/backups/rezka`
(7 последних) и копирует на сервер-выход по SSH (`~/rezka-backups`, 30 последних) — **только зашифрованными age**:
в дампе id и имена людей. Адрес сервера, SSH-ключ и публичный ключ age (`AGE_RECIPIENT`) — `deploy/backup.env`
(образец `backup.env.example`, в git не попадает). Установка юнита — `deploy/rezka-backup.service` (команда в шапке).

Восстановление:

```bash
# из локального дампа
docker compose exec -T postgres pg_restore -U rezka -d rezka --no-owner --clean --if-exists < /var/backups/rezka/rezka-….dump
# из копии на сервере-выходе: секретный ключ age — только у владельца
scp -P <порт> <пользователь>@<сервер>:rezka-backups/rezka-….dump.age .
age -d -i key.txt rezka-….dump.age > rezka.dump
docker compose exec -T postgres pg_restore -U rezka -d rezka --no-owner --clean --if-exists < rezka.dump
```

Обновление прода — `deploy/update.sh`: свежий бэкап, затем `docker compose up -d --build`.

**Правило для новых CHECK в миграциях (24.09.2026).** Ограничение сначала добавляется `NOT VALID` (сразу, без
чтения таблицы), затем данные проверяются в самой миграции — нарушители считаются и, если есть, миграция падает
с понятным сообщением или исправляет их явно, — и только потом `VALIDATE CONSTRAINT`. Иначе `ADD CONSTRAINT`
на живых данных падает посреди деплоя, и bot, poller, sender не стартуют. Индексы — `CREATE INDEX CONCURRENTLY`
в `op.get_context().autocommit_block()`. `alembic check` должен проходить: индексы, созданные сырым SQL,
перечислены в `migrations/env.py` (`SQL_ONLY_INDEXES`).

Схема БД — Alembic (`migrations/`). Изменили `app/models.py` →
`docker compose run --rm -v $PWD/migrations:/srv/migrations migrate alembic revision --autogenerate -m "…"`,
применится при следующем `docker compose up`.

Команда `/stats` в боте — для `ADMIN_IDS`; там же видно, если поллер молчит дольше
`STALE_ALERT_MINUTES`. Технические уведомления в Telegram не отправляются никогда —
состояние смотрите в логах и в `/stats`.

## Структура

```
app/rezka/client.py   доступ: прокси, Anubis, зеркала, паузы
app/rezka/parser.py   блок обновлений, страница тайтла (озвучки с языком, серии, франшиза, расписание), поиск
app/service.py        доменные операции над БД (общие для бота и поллера); локальный поиск
app/poller.py         блок обновлений → серии и озвучки → уведомления; франшизы; очередь чтения
app/sender.py         очередь → Telegram (фото/дайджест, кнопки); очистка
app/health.py         здоровье «события идут» — для /stats и healthcheck поллера
app/posters.py        постеры: скачивание с CDN, загрузка в Telegram, кэш file_id
app/bot/main.py       поиск, подписки, озвучки, Новое, Настройки, поделиться, обратная связь
app/feedback.py       письма автору и ответы через «Ответить»: карточка, копия, связь сообщений
app/lifecycle.py      остановка по SIGTERM, сторож зависаний, пинг мониторинга, advisory lock
app/i18n.py           тексты бота и уведомлений: ru / uk / en
app/bot/search.py     группировка локальной выдачи по франшизам
tests/                pytest: сохранённый HTML сайта и интеграционные тесты на Postgres
docs/ARCHITECTURE.md  решения и факты; docs/PRODUCT_AND_SCALE.md — продукт и масштаб
```
