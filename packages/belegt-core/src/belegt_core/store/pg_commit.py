# Spec: 03 §6.1（belegt_writer 只能 EXECUTE graph.commit_version()/commit_batch()）、§6.5（RLS 会话变量）、
#       §6.8 第 8 步（graph.commit_version()）、§1.5 SSOT-2/3/5；10 §8.4（迁移由 belegt_owner 执行）
"""PostgreSQL 适配器：让 ``GraphStore`` 在同一事务里调用 ``migrations/postgres`` 的 ``graph.commit_version()``。

设计（不破坏 SQLite 路径，不改 ``graph_store.py``）：

- ``GraphStore`` 仍按原流程完成全部写入校验（R-SCHEMA…R-SUBMIT）并写可移植表（``graph_object_version`` 等）；
  Python 侧的读接口（get/query/lineage/impact/as-of）继续读可移植表。
- 适配器在 ``_commit`` 之后、**同一连接同一事务**内：
  ``set_config('app.org_id'|'app.actor'|'app.zone', …, true)`` → ``SET LOCAL ROLE belegt_writer``（可配）→
  ``SELECT graph.commit_version(type, body, expected_version, edges)`` → ``SET LOCAL ROLE NONE``。
  数据库层因此再执行一遍最后防线：租户/执行者一致、信任区、乐观锁、表级 CHECK（R-NUM 第三道防线）、R-ONE、
  content_hash 复核（JCS）、分区表 + RLS（FORCE，对 SECURITY DEFINER 的 owner 同样生效）、``audit.audit_event`` 哈希链、outbox。
  任何一步失败都会让整个事务回滚（可移植表的写入也一起撤销），并映射为 03 §6.8 的 ``GraphError``。
- 模式 ``mirror``（默认，也是目前唯一模式）：可移植表是 Python 读路径，物理分区表是数据库层权威副本。
  ``put_factor_rows``/``put_blob``/``record_trace`` 不经 ``commit_version``，不镜像到 ``ref.factor``/``doc.blob``/``obs.trace_span``。

用法::

    from belegt_core.store.pg_commit import PgCommitGraphStore
    store = PgCommitGraphStore("postgresql+psycopg://belegt_app:…@db/belegt")   # 迁移已由 belegt_owner 执行
    # 或给已有实例挂上：attach_pg_commit(store)

连接账号需要：可移植表的读写权限 + ``GRANT belegt_writer TO <login>``（若 ``writer_role`` 非空）。
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ..authz import Actor
from ..errors import ERROR_CODES, GraphError
from .edges import derive_edges
from .graph_store import GraphStore

# 表级 CHECK 约束 → 03 §6.8 错误码（数据库层最后防线被触发时）
CHECK_CODES: dict[str, str] = {
    "r_num": "GRAPH_NUM_UNREFERENCED",
    "num_has_value": "GRAPH_SCHEMA_INVALID",
    "period_ok": "GRAPH_SCHEMA_INVALID",
    "answer_trusted": "GRAPH_TRUST_ZONE_VIOLATION",
    "calc_trusted": "GRAPH_TRUST_ZONE_VIOLATION",
    "ev_quarantine_candidate": "GRAPH_TRUST_ZONE_VIOLATION",
    "ev_validity": "GRAPH_SCHEMA_INVALID",
    "l3_no_batch": "GRAPH_APPROVAL_REQUIRED",
    "numeric_edges_pinned": "GRAPH_SCHEMA_INVALID",
}

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def session_zone(actor: Actor) -> str:
    """``app.zone``：隔离区执行者 → quarantine；可信区与横切（guard、egress-gateway）→ trusted。"""
    return "quarantine" if actor.zone == "quarantine" else "trusted"


def _json(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"), default=str)


def map_db_error(e: DBAPIError) -> GraphError:
    """把 ``graph.fail()``（SQLSTATE BG000）、CHECK、唯一约束、RLS 拒绝映射为 ``GraphError``。"""
    orig = getattr(e, "orig", None)
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    diag = getattr(orig, "diag", None)
    primary = (getattr(diag, "message_primary", None) or str(orig or e)).strip()
    detail_raw = getattr(diag, "message_detail", None)
    constraint = getattr(diag, "constraint_name", None)
    if state == "BG000":
        code = primary.split()[0] if primary else "GRAPH_SCHEMA_INVALID"
        message, details = primary, {}
        if detail_raw:
            try:
                d = json.loads(detail_raw)
                message, details = d.get("message") or primary, d.get("details") or {}
            except ValueError:
                message = detail_raw
        if code not in ERROR_CODES:
            details = {**details, "db_code": code}
            code = "GRAPH_SCHEMA_INVALID"
        return GraphError(code, f"[db] {message}", rule_id="DB", details={**details, "layer": "graph.commit_version"})
    if state == "23514":  # check_violation
        code = CHECK_CODES.get(constraint or "", "GRAPH_SCHEMA_INVALID")
        return GraphError(code, f"[db] check constraint {constraint} violated", rule_id="DB-CHECK",
                          details={"constraint": constraint, "layer": "table"})
    if state == "23505":  # unique_violation
        return GraphError("GRAPH_VERSION_CONFLICT", f"[db] unique constraint {constraint} violated", rule_id="DB",
                          details={"constraint": constraint})
    if state == "42501":  # insufficient_privilege / RLS WITH CHECK
        return GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"[db] {primary}", rule_id="RLS", details={"layer": "rls"})
    return GraphError("GRAPH_SCHEMA_INVALID", f"[db] {state or ''} {primary}".strip(), rule_id="DB",
                      details={"sqlstate": state})


@dataclass
class PgCommitConfig:
    #: commit_version 调用期间 ``SET LOCAL ROLE`` 的角色；None 表示不切换（连接账号本身须有 EXECUTE）。
    writer_role: str | None = "belegt_writer"
    #: 写入后在同一事务里复核物理 head 与可移植 head 一致（调试/测试用，生产可关）
    verify_head: bool = False

    def __post_init__(self) -> None:
        if self.writer_role is not None and not _IDENT.match(self.writer_role):
            raise ValueError(f"invalid role name {self.writer_role!r}")


def _set_session(conn: Any, org_id: str, actor_id: str, zone: str, *, cp_scope: str = "*",
                 approved_only: bool = False) -> None:
    conn.execute(text("SELECT set_config('app.org_id', :o, true), set_config('app.actor', :a, true), "
                      "set_config('app.zone', :z, true), set_config('app.cp_scope', :c, true), "
                      "set_config('app.approved_only', :p, true)"),
                 {"o": org_id, "a": actor_id, "z": zone, "c": cp_scope, "p": "on" if approved_only else "off"})


def pg_commit_version(conn: Any, type_name: str, body: dict[str, Any], expected_version: int,
                      edges: list[dict[str, Any]], actor: Actor, config: PgCommitConfig | None = None) -> dict[str, Any]:
    """在当前事务里调用 ``graph.commit_version()``；返回其 jsonb 结果。失败抛 ``GraphError``。"""
    cfg = config or PgCommitConfig()
    org_for_session = body.get("org_id") if type_name == "FactorSet" else actor.org_id
    _set_session(conn, org_for_session or actor.org_id, actor.id, session_zone(actor))
    if cfg.writer_role:
        conn.execute(text(f"SET LOCAL ROLE {cfg.writer_role}"))
    try:
        with conn.begin_nested():  # SAVEPOINT：失败时只回滚到此处，再由外层事务整体回滚
            row = conn.execute(
                text("SELECT graph.commit_version(:t, CAST(:b AS jsonb), :ev, CAST(:e AS jsonb))"),
                {"t": type_name, "b": _json(body), "ev": int(expected_version), "e": _json(edges)},
            ).first()
    except DBAPIError as e:
        raise map_db_error(e) from e
    finally:
        if cfg.writer_role:
            conn.execute(text("SET LOCAL ROLE NONE"))
    res = row[0] if row else {}
    return json.loads(res) if isinstance(res, str) else dict(res or {})


def attach_pg_commit(store: GraphStore, config: PgCommitConfig | None = None) -> GraphStore:
    """给已有 ``GraphStore`` 实例挂上 ``graph.commit_version()`` 镜像（仅 PostgreSQL；SQLite 实例原样返回）。"""
    if store.engine.dialect.name != "postgresql":
        return store
    cfg = config or PgCommitConfig()
    orig = store._commit  # bound method

    def _commit(conn: Any, reader: Any, type_name: str, body: dict[str, Any], prev: dict[str, Any] | None,
                head: Any, actor: Actor) -> str:
        ref = orig(conn, reader, type_name, body, prev, head, actor)
        org_id = body["org_id"]
        edges = derive_edges(type_name, body, lambda rid: reader.head_version_any(org_id, rid))
        res = pg_commit_version(conn, type_name, body, head.head_version if head is not None else 0, edges, actor, cfg)
        if res.get("object_ref") != ref:
            raise GraphError("GRAPH_VERSION_CONFLICT",
                             f"physical head diverged from portable head: {res.get('object_ref')} != {ref}",
                             rule_id="DB", details={"portable": ref, "physical": res.get("object_ref")})
        if res.get("content_hash") != body["content_hash"]:  # 函数已复核，这里是双保险
            raise GraphError("GRAPH_HASH_MISMATCH", "graph.commit_version content_hash differs", rule_id="DB")
        return ref

    store._commit = _commit  # type: ignore[method-assign]
    store.pg_commit_config = cfg  # type: ignore[attr-defined]
    return store


class PgCommitGraphStore(GraphStore):
    """``GraphStore`` + ``graph.commit_version()``（PostgreSQL）。在 SQLite 上与 ``GraphStore`` 完全相同。"""

    def __init__(self, url_or_engine: Any = "sqlite+pysqlite:///:memory:", *, pg_commit: PgCommitConfig | None = None,
                 **kw: Any) -> None:
        super().__init__(url_or_engine, **kw)
        attach_pg_commit(self, pg_commit)

    @property
    def pg_commit_enabled(self) -> bool:
        return self.engine.dialect.name == "postgresql"


# ---------------------------------------------------------------------------- RLS 读会话（API 层读物理表）
@contextmanager
def rls_session(store_or_engine: Any, *, org_id: str, actor: str, zone: str = "trusted", cp_scope: str = "*",
                approved_only: bool = False, role: str | None = None) -> Iterator[Any]:
    """打开一个只读事务，按 03 §6.5 设置 ``app.*`` 会话变量并可选 ``SET LOCAL ROLE``（如 belegt_reader_quarantine）。"""
    engine = getattr(store_or_engine, "engine", store_or_engine)
    if role is not None and not _IDENT.match(role):
        raise ValueError(f"invalid role name {role!r}")
    with engine.begin() as conn:
        if role:
            conn.execute(text(f"SET LOCAL ROLE {role}"))
        _set_session(conn, org_id, actor, zone, cp_scope=cp_scope, approved_only=approved_only)
        yield conn


def physical_heads(conn: Any) -> dict[str, tuple[str, int, str | None, str]]:
    """当前 RLS 可见的 ``graph.object_head``：``{id: (type, head_version, status, trust_zone)}``。"""
    rows = conn.execute(text("SELECT id, type, head_version, status, trust_zone FROM graph.object_head"))
    return {r.id: (r.type, r.head_version, r.status, r.trust_zone) for r in rows}


def verify_physical_audit_chain(conn: Any, org_id: str | None = None) -> list[int]:
    """调用 ``audit.verify_chain()``（0005，SECURITY DEFINER）在数据库内重算哈希链，返回断链的 seq（空 = 连续）。

    校验的是当前会话 ``app.org_id`` 的链；给出 ``org_id`` 时先在本事务设置它。"""
    if org_id is not None:
        conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": org_id})
    return [r[0] for r in conn.execute(text("SELECT * FROM audit.verify_chain()"))]


__all__ = ["PgCommitConfig", "PgCommitGraphStore", "attach_pg_commit", "pg_commit_version", "map_db_error",
           "rls_session", "physical_heads", "verify_physical_audit_chain", "session_zone", "CHECK_CODES"]
