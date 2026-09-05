"""Схема БД. Обоснование каждой таблицы — docs/ARCHITECTURE.md, §3.

Две единицы подписки: страница (один сезон/шоу) и франшиза (все части, как их
группирует сам сайт). Запросы к сайту делаются на ленту и на страницы, а не на
подписки, поэтому нагрузка не зависит от числа пользователей.
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index,
    Integer, SmallInteger, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram user id
    username: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Настройки (⚙️). Тихие часы и дайджест применяются при постановке в очередь: notify_at() в SQL.
    photos: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    tz_offset: Mapped[int] = mapped_column(SmallInteger, default=3, server_default=text("3"))       # часы от UTC, МСК
    quiet_from: Mapped[int | None] = mapped_column(SmallInteger, default=23, server_default=text("23"))
    quiet_to: Mapped[int | None] = mapped_column(SmallInteger, default=8, server_default=text("8"))  # NULL/NULL = выкл
    digest_hour: Mapped[int | None] = mapped_column(SmallInteger)                                    # NULL = сразу
    default_voice_filter: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))                   # для новых подписок


class Franchise(Base):
    """Группа частей, как её отдаёт сайт. Ключ — минимальный hdrezka_id среди частей:
    не меняется при добавлении новых. Имя — только для показа."""
    __tablename__ = "franchises"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_hdrezka_id: Mapped[int] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(Text)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Page(Base):
    """Страница сайта: у сериала — всё шоу, у аниме часто — один сезон, у фильма — фильм."""
    __tablename__ = "pages"
    __table_args__ = (
        Index("pages_ongoing", "page_refreshed_at", postgresql_where=text("NOT is_finished")),
        Index("pages_franchise", "franchise_id"),
        Index("pages_unread", "created_at", postgresql_where=text("page_refreshed_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hdrezka_id: Mapped[int] = mapped_column(Integer, unique=True)
    title: Mapped[str] = mapped_column(Text)
    orig_title: Mapped[str | None] = mapped_column(Text)         # со страницы; для поиска «one piece»
    meta_line: Mapped[str | None] = mapped_column(Text)          # «2026, Япония, Фэнтези» из карточки
    url: Mapped[str] = mapped_column(Text)
    year: Mapped[str | None] = mapped_column(String(16))
    section: Mapped[str | None] = mapped_column(String(32))        # series / animation / cartoons / films
    content_type: Mapped[str | None] = mapped_column(String(16))   # 'series' | 'film' | NULL — страницу не читали
    is_finished: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_season: Mapped[int | None] = mapped_column(Integer)
    last_episode: Mapped[int | None] = mapped_column(Integer)
    default_translator: Mapped[int | None] = mapped_column(Integer)
    poster_url: Mapped[str | None] = mapped_column(Text)        # og:image страницы, до её чтения — обложка из ленты
    poster_file_id: Mapped[str | None] = mapped_column(Text)    # file_id в Telegram после первой загрузки байтами
    franchise_id: Mapped[int | None] = mapped_column(
        ForeignKey("franchises.id", ondelete="SET NULL")
    )
    page_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Voice(Base):
    __tablename__ = "voices"

    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), primary_key=True)
    translator_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)


class Episode(Base):
    """Серия доступна хотя бы в одной озвучке (событие из ленты)."""
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("page_id", "season", "episode", name="uq_episode"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    season: Mapped[int] = mapped_column(Integer)
    episode: Mapped[int] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EpisodeVoice(Base):
    """Серия доступна в конкретной озвучке (подтверждено через ajax)."""
    __tablename__ = "episode_voices"

    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True)
    translator_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Schedule(Base):
    """Расписание выхода серий со страницы тайтла (даты оригинального эфира)."""
    __tablename__ = "schedule"

    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), primary_key=True)
    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    episode: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    air_date: Mapped[date | None] = mapped_column(Date)
    aired: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'page' AND page_id IS NOT NULL AND franchise_id IS NULL) OR "
            "(scope = 'franchise' AND franchise_id IS NOT NULL AND page_id IS NULL)",
            name="ck_subscription_scope",
        ),
        Index("subs_user_page", "user_id", "page_id", unique=True,
              postgresql_where=text("page_id IS NOT NULL")),
        Index("subs_user_franchise", "user_id", "franchise_id", unique=True,
              postgresql_where=text("franchise_id IS NOT NULL")),
        Index("subs_by_page", "page_id"),
        Index("subs_by_franchise", "franchise_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(16))                 # 'page' | 'franchise'
    page_id: Mapped[int | None] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"))
    franchise_id: Mapped[int | None] = mapped_column(ForeignKey("franchises.id", ondelete="CASCADE"))
    voice_filter: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))  # NULL = любая озвучка
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VoiceCheck(Base):
    """Отложенная проверка «озвучка X для серии уже появилась?» — с растущим интервалом."""
    __tablename__ = "voice_checks"
    __table_args__ = (Index("voice_checks_due", "next_check_at"),)

    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True)
    translator_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class Notification(Base):
    """Outbox. Транзитная таблица: отправленное удаляется через 7 дней.
    Несколько sender'ов работают через FOR UPDATE SKIP LOCKED."""
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "ref_id", name="uq_notification"),
        Index("notifications_queue", "next_attempt_at", "id", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))                  # episode | voice | new_part
    ref_id: Mapped[int] = mapped_column(BigInteger)                # episodes.id / episodes.id / pages.id
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Meta(Base):
    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
