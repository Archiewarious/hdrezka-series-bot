FROM python:3.12-slim

# HOME в /tmp: корень контейнера только на чтение (docker-compose.yml), писать можно лишь в tmpfs.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Процессы бота — не root: уязвимость в зависимости не даёт root даже внутри контейнера.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app
COPY pytest.ini .
COPY tests ./tests

USER app
CMD ["python", "-m", "app.bot.main"]
