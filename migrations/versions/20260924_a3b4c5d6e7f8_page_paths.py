"""Адреса страниц — путями, без домена зеркала.

24.09.2026, аудит: pages.url хранился с доменом зеркала, где страницу нашли. Клиент абсолютный адрес
не меняет, поэтому переключение на запасное зеркало на страницы тайтлов не действовало, а кнопки
«Смотреть» вели на тот же домен. Теперь в базе путь, домен подставляет клиент (текущее зеркало)
или HDREZKA_PUBLIC_URL (кнопки).

Данные: отрезает схему и домен у pages.url. Downgrade дописывает первое зеркало из HDREZKA_BASE_URLS.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-09-24 14:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE pages SET url = regexp_replace(url, '^https?://[^/]+', '') WHERE url ~ '^https?://'")


def downgrade() -> None:
    from app.config import cfg
    op.execute(sa.text("UPDATE pages SET url = :base || url WHERE url LIKE '/%'")
               .bindparams(base=cfg.base_urls[0].rstrip("/")))
