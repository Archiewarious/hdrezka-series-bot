"""Обратная связь: обращения к автору и сообщения Telegram, которые к ним относятся.

13.09.2026: человек пишет автору через бота, автор отвечает «Ответить» на карточку. Текст обращений
не храним — он у автора в Telegram; здесь кто, тема, и по какому сообщению искать адресата ответа.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-09-13 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('feedback_topic', sa.String(16), nullable=True))
    op.add_column('users', sa.Column('feedback_until', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'feedback',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('topic', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("topic IN ('bug', 'idea', 'collab', 'other')", name='ck_feedback_topic'),
    )
    op.create_index('feedback_user', 'feedback', ['user_id'])
    op.create_table(
        'feedback_links',
        sa.Column('chat_id', sa.BigInteger(), primary_key=True),
        sa.Column('message_id', sa.BigInteger(), primary_key=True),
        sa.Column('feedback_id', sa.BigInteger(), sa.ForeignKey('feedback.id', ondelete='CASCADE'), nullable=False),
        sa.Column('side', sa.String(8), nullable=False),
        sa.CheckConstraint("side IN ('admin', 'user')", name='ck_feedback_link_side'),
    )


def downgrade() -> None:
    op.drop_table('feedback_links')
    op.drop_table('feedback')
    op.drop_column('users', 'feedback_until')
    op.drop_column('users', 'feedback_topic')
