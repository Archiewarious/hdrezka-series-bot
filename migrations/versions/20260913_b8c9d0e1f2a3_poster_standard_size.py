"""Сброс poster_file_id: постеры теперь приводятся к одному размеру.

12.09.2026 в ленте уведомлений картинки шли разной высоты — отношение сторон у постеров HDREZKA
гуляет от 0.63 до 0.75. Загруженные ранее file_id указывают на картинки прежнего размера, поэтому
сбрасываем: при следующем посте постер загрузится заново, уже приведённый к стандарту.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-13 01:00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE pages SET poster_file_id = NULL WHERE poster_file_id IS NOT NULL")


def downgrade() -> None:
    pass
