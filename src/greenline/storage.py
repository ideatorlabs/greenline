"""Database models and the ``Storage`` handle.

Greenline keeps its own tables (``<prefix>numbers``, ``<prefix>contacts``, ``<prefix>messages``,
``<prefix>templates``, ``<prefix>webhook_events``). They reference your application's records only
by plain string ids (``tenant_id``, ``external_id``), never by foreign key, so the tables can live
in your database or in a separate one and be moved later without a migration of your own schema.

Works with SQLite and PostgreSQL (anything SQLAlchemy 2.0 supports should be fine). Timestamps are
stored as naive UTC.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cache
from typing import Any

from sqlalchemy import Boolean, DateTime, Engine, Integer, MetaData, String, Text, UniqueConstraint
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, declared_attr, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

DEFAULT_PREFIX = "greenline_"


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (what the tables store)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NumberMixin:
    """A sending phone number. ``tenant_id`` empty means a shared number serving many tenants."""

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    phone_number_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    waba_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    display_phone: Mapped[str] = mapped_column(String(32), default="")
    display_name: Mapped[str] = mapped_column(String(256), default="")
    token_enc: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="live")
    quality: Mapped[str] = mapped_column(String(16), default="unknown")
    tier: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ContactMixin:
    """Consent and service-window state for one phone on one sending number."""

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    phone_number_id: Mapped[str] = mapped_column(String(64), index=True)
    phone: Mapped[str] = mapped_column(String(32), index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    opted_in: Mapped[bool] = mapped_column(Boolean, default=False)
    opt_in_source: Mapped[str] = mapped_column(String(64), default="")
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    window_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MessageMixin:
    """Every message in or out, for audit, status tracking, idempotency and metering."""

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    phone_number_id: Mapped[str] = mapped_column(String(64), index=True)
    contact_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(8), default="out")
    kind: Mapped[str] = mapped_column(String(16), default="template")
    category: Mapped[str] = mapped_column(String(16), default="utility")
    template_name: Mapped[str] = mapped_column(String(512), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    provider_message_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    error_code: Mapped[str] = mapped_column(String(32), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    cost_minor: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TemplateMixin:
    """What we know about one template on one business account (WABA)."""

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    waba_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(512), index=True)
    language: Mapped[str] = mapped_column(String(16), default="en")
    category: Mapped[str] = mapped_column(String(16), default="utility")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    provider_template_id: Mapped[str] = mapped_column(String(64), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    param_count: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WebhookEventMixin:
    """Ids of webhook items already processed, so provider retries are handled once."""

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


@dataclass(frozen=True)
class Models:
    """The mapped classes for one table prefix."""

    base: type[DeclarativeBase]
    Number: Any
    Contact: Any
    Message: Any
    Template: Any
    WebhookEvent: Any

    @property
    def metadata(self) -> MetaData:
        return self.base.metadata


@cache
def build_models(prefix: str = DEFAULT_PREFIX) -> Models:
    """Mapped classes whose table names start with ``prefix``. Cached per prefix."""

    class Base(DeclarativeBase):
        pass

    class Number(NumberMixin, Base):
        __tablename__ = f"{prefix}numbers"

    class Contact(ContactMixin, Base):
        __tablename__ = f"{prefix}contacts"

        @declared_attr.directive
        def __table_args__(cls) -> tuple[Any, ...]:
            return (UniqueConstraint("phone_number_id", "phone", name=f"uq_{prefix}contact"),)

    class Message(MessageMixin, Base):
        __tablename__ = f"{prefix}messages"

    class Template(TemplateMixin, Base):
        __tablename__ = f"{prefix}templates"

        @declared_attr.directive
        def __table_args__(cls) -> tuple[Any, ...]:
            return (UniqueConstraint("waba_id", "name", "language", name=f"uq_{prefix}template"),)

    class WebhookEvent(WebhookEventMixin, Base):
        __tablename__ = f"{prefix}webhook_events"

    return Models(
        base=Base,
        Number=Number,
        Contact=Contact,
        Message=Message,
        Template=Template,
        WebhookEvent=WebhookEvent,
    )


def _make_engine(url: str, **kwargs: Any) -> Engine:
    if url.startswith("sqlite"):
        kwargs.setdefault("connect_args", {"check_same_thread": False})
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs.setdefault("poolclass", StaticPool)
    return sa_create_engine(url, **kwargs)


class Storage:
    """A database connection plus Greenline's mapped classes.

    ``Storage("sqlite:///greenline.db")`` or ``Storage(existing_sqlalchemy_engine)``. An in-memory
    SQLite URL gets a single shared connection so every session sees the same data.
    """

    def __init__(
        self, url_or_engine: str | Engine, *, table_prefix: str = DEFAULT_PREFIX, **engine_kwargs: Any
    ):
        self.engine = (
            _make_engine(url_or_engine, **engine_kwargs) if isinstance(url_or_engine, str) else url_or_engine
        )
        self.models = build_models(table_prefix)
        self.table_prefix = table_prefix
        self._sessions = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)

    @property
    def metadata(self) -> MetaData:
        """Greenline's table metadata, e.g. for Alembic's ``target_metadata``."""
        return self.models.metadata

    def create_all(self) -> None:
        """Create Greenline's tables if they do not exist. Safe to call repeatedly."""
        self.models.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        self.models.metadata.drop_all(self.engine)

    def session(self) -> Session:
        return self._sessions()


def create_all(storage: Storage) -> None:
    """Create Greenline's tables on ``storage``."""
    storage.create_all()
