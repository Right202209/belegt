# Spec: 08 §6.5（自托管：单容器 + SQLite 或 PostgreSQL）、03 §3.10（portal_token_hash 只存哈希）
"""SQLAlchemy 2.x 表定义与引擎工厂。时间列一律存 naive UTC。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnaive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class Base(DeclarativeBase):
    pass


class RequestRow(Base):
    __tablename__ = "supplier_requests"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(40), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), index=True)
    data: Mapped[dict] = mapped_column(JSON)            # SupplierRequest（03 字段 + 本地扩展）
    manifest: Mapped[dict] = mapped_column(JSON)        # RequestManifest
    manifest_hash: Mapped[str] = mapped_column(String(80))
    chase: Mapped[dict] = mapped_column(JSON, default=dict)  # {tier, done_steps[], paused_until, sender{}}
    status_changed_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class RequestHistoryRow(Base):
    __tablename__ = "supplier_request_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sreq_id: Mapped[str] = mapped_column(String(40), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict] = mapped_column(JSON)
    at: Mapped[datetime] = mapped_column(DateTime)


class LinkRow(Base):
    __tablename__ = "magic_links"
    token_hash: Mapped[str] = mapped_column(String(80), primary_key=True)
    sreq_id: Mapped[str] = mapped_column(String(40), index=True)
    email: Mapped[str] = mapped_column(String(320))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    issued_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    origins: Mapped[list] = mapped_column(JSON, default=list)  # 已见 IP 哈希（PLAUS-035）


class DeviceRow(Base):
    __tablename__ = "link_devices"
    device_hash: Mapped[str] = mapped_column(String(80), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(80), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class OtpRow(Base):
    __tablename__ = "link_otps"
    token_hash: Mapped[str] = mapped_column(String(80), primary_key=True)
    code_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DraftRow(Base):
    __tablename__ = "drafts"
    sreq_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    draft_version: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class UploadRow(Base):
    __tablename__ = "uploads"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sreq_id: Mapped[str] = mapped_column(String(40), index=True)
    sha256: Mapped[str] = mapped_column(String(80), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(Text)
    scan: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class SubmissionRow(Base):
    __tablename__ = "submissions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sreq_id: Mapped[str] = mapped_column(String(40), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    package: Mapped[dict] = mapped_column(JSON)
    package_hash: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class QuestionRow(Base):
    __tablename__ = "questions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sreq_id: Mapped[str] = mapped_column(String(40), index=True)
    datapoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class DeclineRow(Base):
    __tablename__ = "declines"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sreq_id: Mapped[str] = mapped_column(String(40), index=True)
    reason: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class WebhookRow(Base):
    __tablename__ = "webhook_outbox"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class EgressSendRow(Base):
    __tablename__ = "egress_sends"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(40), index=True)
    counterparty_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    recipient: Mapped[str] = mapped_column(String(320), index=True)
    sreq_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)


class SuppressionRow(Base):
    __tablename__ = "egress_suppressions"
    recipient: Mapped[str] = mapped_column(String(320), primary_key=True)
    reason: Mapped[str] = mapped_column(String(64))


class AuditRow(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    sreq_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    data: Mapped[dict] = mapped_column(JSON)
    at: Mapped[datetime] = mapped_column(DateTime)


class TemplateReviewRow(Base):
    __tablename__ = "template_reviews"
    template_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime)


class FlagRow(Base):
    __tablename__ = "flags"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[bool] = mapped_column(Boolean, default=False)


def make_engine(url: str) -> Engine:
    kw: dict = {}
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        from pathlib import Path

        Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            from sqlalchemy.pool import StaticPool

            kw["poolclass"] = StaticPool
    engine = create_engine(url, **kw)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragma(dbapi_conn, _rec):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    Base.metadata.create_all(engine)
    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
