# Spec: 03 §3.14 AuditEvent、§6.8（拒绝也记审计）、07 §7.1–§7.3（内容、哈希链、只插入）、_baseline §M（action 枚举）
"""AuditEvent 哈希链。

- ``event_hash = sha256(prev_hash ‖ JCS(event 去掉 event_hash))``；``‖`` 为 UTF-8 字节拼接，``prev_hash`` 取
  ``"sha256:<hex>"`` 字符串形式；租户内首条事件的 ``prev_hash`` 为创世值 ``sha256:000…0``（本包约定）。
- ``seq`` 租户内单调递增；只插入，任何角色无 UPDATE/DELETE（PostgreSQL 见 migrations）。
- 每条事件只存 ``object_ref`` 与 ``content_hash``，不存业务值（07 §7.1）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import Connection, func, insert, select

from .canonical import GENESIS_HASH, canonicalize, sha256_tag
from .ids import new_id
from .models.logs import AUDIT_ACTIONS
from .store.tables import audit_event


def compute_event_hash(event: dict[str, Any]) -> str:
    body = {k: v for k, v in event.items() if k != "event_hash"}
    return sha256_tag(event["prev_hash"].encode("utf-8") + canonicalize(body).encode("utf-8"))


@dataclass
class ChainReport:
    org_id: str
    ok: bool
    count: int
    first_bad_seq: int | None = None
    problems: list[str] = field(default_factory=list)
    head_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _row_to_event(row: Any) -> dict[str, Any]:
    d = dict(row._mapping)
    d["version"] = 1
    return {k: v for k, v in d.items() if v is not None}


def append_event(
    conn: Connection, *, org_id: str, action: str, created_by: str, trace_id: str, created_at: str,
    object_ref: str | None = None, content_hash: str | None = None, details: dict[str, Any] | None = None,
    ip: str | None = None, user_agent: str | None = None,
) -> dict[str, Any]:
    """在给定连接（事务）内追加一条审计事件并返回它。"""
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unknown audit action {action!r}")
    last = conn.execute(
        select(audit_event.c.seq, audit_event.c.event_hash)
        .where(audit_event.c.org_id == org_id).order_by(audit_event.c.seq.desc()).limit(1)
    ).first()
    seq = (last.seq + 1) if last else 1
    prev = last.event_hash if last else GENESIS_HASH
    ev: dict[str, Any] = {
        "id": new_id("aud_"), "org_id": org_id, "version": 1, "created_at": created_at, "created_by": created_by,
        "seq": seq, "action": action, "object_ref": object_ref, "content_hash": content_hash, "trace_id": trace_id,
        "prev_hash": prev, "ip": ip, "user_agent": user_agent, "details": details,
    }
    ev = {k: v for k, v in ev.items() if v is not None}
    ev["event_hash"] = compute_event_hash(ev)
    row = {k: v for k, v in ev.items() if k != "version"}
    conn.execute(insert(audit_event).values(**row))
    return ev


def iter_events(conn: Connection, org_id: str, start_seq: int = 1, end_seq: int | None = None) -> Iterable[dict[str, Any]]:
    q = select(audit_event).where(audit_event.c.org_id == org_id, audit_event.c.seq >= start_seq)
    if end_seq is not None:
        q = q.where(audit_event.c.seq <= end_seq)
    for row in conn.execute(q.order_by(audit_event.c.seq)):
        yield _row_to_event(row)


def verify_events(events: list[dict[str, Any]], *, org_id: str = "", expected_prev: str | None = None) -> ChainReport:
    """校验一段连续事件：seq 连续、prev_hash 衔接、event_hash 可复算。``expected_prev`` 为片段前一条的 hash。"""
    rep = ChainReport(org_id=org_id, ok=True, count=len(events))
    prev_hash = expected_prev
    prev_seq = None
    for ev in events:
        seq = ev["seq"]
        if prev_seq is not None and seq != prev_seq + 1:
            rep.problems.append(f"seq gap before {seq}")
        if prev_hash is not None and ev["prev_hash"] != prev_hash:
            rep.problems.append(f"seq {seq}: prev_hash does not link")
        if prev_hash is None and prev_seq is None and seq == 1 and ev["prev_hash"] != GENESIS_HASH:
            rep.problems.append("seq 1: prev_hash is not genesis")
        if compute_event_hash(ev) != ev["event_hash"]:
            rep.problems.append(f"seq {seq}: event_hash mismatch (tampered)")
        if rep.problems and rep.first_bad_seq is None:
            rep.first_bad_seq = seq
        prev_hash, prev_seq = ev["event_hash"], seq
    rep.ok = not rep.problems
    rep.head_hash = prev_hash
    return rep


def verify_chain(conn: Connection, org_id: str) -> ChainReport:
    return verify_events(list(iter_events(conn, org_id)), org_id=org_id)


def chain_head(conn: Connection, org_id: str) -> tuple[int, str] | None:
    row = conn.execute(
        select(audit_event.c.seq, audit_event.c.event_hash).where(audit_event.c.org_id == org_id)
        .order_by(audit_event.c.seq.desc()).limit(1)
    ).first()
    return (row.seq, row.event_hash) if row else None


def count_events(conn: Connection, org_id: str) -> int:
    return conn.execute(select(func.count()).select_from(audit_event).where(audit_event.c.org_id == org_id)).scalar_one()
