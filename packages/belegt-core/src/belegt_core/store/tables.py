# Spec: 03 §6.2（主要表）、§6.3（索引）；可移植布局（SQLite 与 PostgreSQL 均可运行）
"""GraphStore 使用的可移植表布局。

与 03 §6 物理设计的对应：``graph_object_version`` ≈ 各 ``graph.*_v`` 表的并集（``body`` 权威 + 冗余热点列），
``graph_object_head`` / ``graph_edge`` / ``graph_outbox`` / ``graph_idempotency`` / ``graph_effective_answer`` /
``audit_event`` / ``obs_trace`` / ``doc_blob`` / ``ref_factor`` 同名对应。生产 PostgreSQL 的分区、RLS、
``graph.commit_version()`` 见 ``migrations/postgres/``。
"""
from __future__ import annotations

from sqlalchemy import (
    JSON, BigInteger, Column, Index, Integer, LargeBinary, MetaData, PrimaryKeyConstraint, String, Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
JSONT = JSON().with_variant(JSONB(), "postgresql")

object_version = Table(
    "graph_object_version", metadata,
    Column("org_id", String(64), nullable=False),
    Column("id", String(160), nullable=False),
    Column("version", Integer, nullable=False),
    Column("type", String(32), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("trust_zone", String(16), nullable=False),
    Column("provenance", String(64), nullable=False),
    Column("supersedes", String(200)),
    Column("schema_version", String(40), nullable=False),
    Column("content_hash", String(80), nullable=False),
    Column("status", String(32)),
    Column("body", JSONT, nullable=False),
    # 冗余热点列（写入函数从 body 填充）
    Column("answer_key", String(80)),
    Column("datapoint_base", String(200)),
    Column("period_start", String(10)),
    Column("period_end", String(10)),
    Column("valid_from", String(10)),
    Column("valid_until", String(10)),
    Column("request_id", String(64)),
    PrimaryKeyConstraint("org_id", "id", "version", name="pk_object_version"),
)
Index("ix_ov_org_type_created", object_version.c.org_id, object_version.c.type, object_version.c.created_at)
Index("ix_ov_org_answer_key", object_version.c.org_id, object_version.c.answer_key)
Index("ix_ov_id", object_version.c.id)
Index("ix_ov_org_request", object_version.c.org_id, object_version.c.request_id)

object_head = Table(
    "graph_object_head", metadata,
    Column("org_id", String(64), nullable=False),
    Column("id", String(160), nullable=False),
    Column("type", String(32), nullable=False),
    Column("head_version", Integer, nullable=False),
    Column("status", String(32)),
    Column("trust_zone", String(16), nullable=False),
    Column("updated_at", String(40), nullable=False),
    PrimaryKeyConstraint("org_id", "id", name="pk_object_head"),
)
Index("ix_head_org_type_status", object_head.c.org_id, object_head.c.type, object_head.c.status)
Index("ix_head_id", object_head.c.id)

edge = Table(
    "graph_edge", metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("org_id", String(64), nullable=False),
    Column("edge_type", String(32), nullable=False),
    Column("from_id", String(160), nullable=False),
    Column("from_version", Integer, nullable=False),
    Column("to_id", String(200), nullable=False),
    Column("to_version", Integer),
    Column("attrs", JSONT),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint("org_id", "edge_type", "from_id", "from_version", "to_id", "to_version", name="uq_edge"),
)
Index("ix_edge_to", edge.c.org_id, edge.c.to_id, edge.c.edge_type)
Index("ix_edge_from", edge.c.org_id, edge.c.from_id, edge.c.from_version)

outbox = Table(
    "graph_outbox", metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("org_id", String(64), nullable=False),
    Column("object_ref", String(200), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("payload", JSONT, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("published_at", String(40)),
)

idempotency = Table(
    "graph_idempotency", metadata,
    Column("org_id", String(64), nullable=False),
    Column("idempotency_key", String(300), nullable=False),
    Column("request_hash", String(80), nullable=False),
    Column("response", JSONT, nullable=False),
    Column("expires_at", String(40), nullable=False),
    PrimaryKeyConstraint("org_id", "idempotency_key", name="pk_idempotency"),
)

effective_answer = Table(
    "graph_effective_answer", metadata,
    Column("org_id", String(64), nullable=False),
    Column("answer_key", String(80), nullable=False),
    Column("answer_id", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    PrimaryKeyConstraint("org_id", "answer_key", name="pk_effective_answer"),
)

audit_event = Table(
    "audit_event", metadata,
    Column("org_id", String(64), nullable=False),
    Column("seq", BigInteger().with_variant(Integer, "sqlite"), nullable=False),
    Column("id", String(64), nullable=False, unique=True),
    Column("created_at", String(40), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("action", String(32), nullable=False),
    Column("object_ref", String(200)),
    Column("content_hash", String(80)),
    Column("trace_id", String(64), nullable=False),
    Column("prev_hash", String(80), nullable=False),
    Column("event_hash", String(80), nullable=False),
    Column("ip", String(64)),
    Column("user_agent", String(300)),
    Column("details", JSONT),
    PrimaryKeyConstraint("org_id", "seq", name="pk_audit_event"),
)
Index("ix_audit_object", audit_event.c.org_id, audit_event.c.object_ref)

trace = Table(
    "obs_trace", metadata,
    Column("org_id", String(64), nullable=False),
    Column("id", String(64), nullable=False),
    Column("span_id", String(64), nullable=False),
    Column("started_at", String(40), nullable=False),
    Column("body", JSONT, nullable=False),
    PrimaryKeyConstraint("org_id", "id", "span_id", name="pk_trace"),
)

blob = Table(
    "doc_blob", metadata,
    Column("org_id", String(64), nullable=False),
    Column("sha256", String(80), nullable=False),
    Column("mime", String(200)),
    Column("size", Integer, nullable=False),
    Column("content", LargeBinary, nullable=False),
    Column("uploaded_by", String(128)),
    Column("created_at", String(40), nullable=False),
    PrimaryKeyConstraint("org_id", "sha256", name="pk_blob"),
)

factor_row = Table(
    "ref_factor", metadata,
    Column("factor_set_id", String(200), nullable=False),
    Column("source_row_id", String(200), nullable=False),
    Column("factor_key", String(200), nullable=False),
    Column("geography", String(32), nullable=False),
    Column("year", String(16), nullable=False),
    Column("value", String(48), nullable=False),
    Column("unit", String(32), nullable=False),
    PrimaryKeyConstraint("factor_set_id", "source_row_id", name="pk_factor"),
)

principal = Table(
    "graph_principal", metadata,
    Column("org_id", String(64), nullable=False),
    Column("actor", String(128), nullable=False),
    Column("roles", JSONT, nullable=False),
    Column("updated_at", String(40), nullable=False),
    PrimaryKeyConstraint("org_id", "actor", name="pk_principal"),
)

