"""SQLite ledger.

The ledger is the whole reason this service is safe to re-run. Every
(reservation, lock) pair gets exactly one row holding the *desired* state and
the *synced* state. All the sync logic does is drive one towards the other, so
a duplicate webhook, a retry, or a restart mid-flight converges instead of
stacking a second passcode on the door.
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import get_settings


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class PasscodeRecord(Base):
    """One passcode on one door for one reservation."""

    __tablename__ = "passcodes"
    __table_args__ = (UniqueConstraint("reservation_id", "lock_id", name="uq_res_lock"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reservation_id: Mapped[str] = mapped_column(String(64), index=True)
    lock_id: Mapped[int] = mapped_column(Integer, index=True)
    listing_map_id: Mapped[int] = mapped_column(Integer, index=True)

    # What we want the lock to look like.
    desired_present: Mapped[bool] = mapped_column(Boolean, default=True)
    desired_start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    desired_end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    desired_hash: Mapped[str] = mapped_column(String(64), default="")

    # What TTLock has actually confirmed.
    synced_hash: Mapped[str] = mapped_column(String(64), default="")
    keyboard_pwd_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    strategy: Mapped[str] = mapped_column(String(16), default="auto")

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    @property
    def in_sync(self) -> bool:
        return bool(self.desired_hash) and self.desired_hash == self.synced_hash

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Passcode res={self.reservation_id} lock={self.lock_id} "
            f"pwdId={self.keyboard_pwd_id} synced={self.in_sync}>"
        )


class WebhookEvent(Base):
    """Audit trail. Also the dedupe key for repeated deliveries."""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    event: Mapped[str] = mapped_column(String(64), default="")
    reservation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payload: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(32), default="queued")


class TokenCache(Base):
    """Persisted OAuth tokens so a restart does not re-authenticate needlessly."""

    __tablename__ = "tokens"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    access_token: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):  # pragma: no cover
    """WAL keeps the reconciler's reads from blocking the webhook's writes."""
    try:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()
    except Exception:
        pass


def init_db(url: str | None = None) -> Engine:
    global _engine, _SessionFactory
    url = url or get_settings().database_url
    if url.startswith("sqlite:///"):
        target = url.replace("sqlite:///", "", 1)
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(url, future=True, connect_args=_connect_args(url))
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(_engine)
    return _engine


def _connect_args(url: str) -> dict:
    return {"check_same_thread": False} if url.startswith("sqlite") else {}


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionFactory is None:
        init_db()
    assert _SessionFactory is not None
    s = _SessionFactory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
