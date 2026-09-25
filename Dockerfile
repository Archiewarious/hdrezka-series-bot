# Три цели (24.09.2026): base — зависимости строго по хешам; test — плюс pytest, ruff, pip-audit и тесты;
# боевой образ (последний, по умолчанию) — без тестов и без pytest.
#   docker compose build                         — боевой образ для bot, poller, sender, migrate
#   docker compose --profile test run --rm --no-deps tests   — тесты (README, «Тесты»)
FROM python:3.12-slim AS base

# HOME в /tmp: корень контейнера только на чтение (docker-compose.yml), писать можно лишь в tmpfs.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp

WORKDIR /srv
COPY requirements.txt .
# --require-hashes: ставится ровно то, что закреплено (uv pip compile --generate-hashes), подмена пакета не пройдёт.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
# Процессы бота — не root: уязвимость в зависимости не даёт root даже внутри контейнера.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app


FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.txt
COPY pytest.ini .
COPY tests ./tests
USER app
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


FROM base AS prod
USER app
CMD ["python", "-m", "app.bot.main"]
