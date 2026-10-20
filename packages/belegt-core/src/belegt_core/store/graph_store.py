# Spec: 03 §1.4（append-only 版本语义）、§4.2（对象版本规则）、§4.6（as-of）、§4.7（重放、影响分析）、§6.2–§6.3、§6.8
"""GraphStore：证据图的唯一写入/读取入口（SQLAlchemy 2.x，SQLite 与 PostgreSQL 均可运行）。

写入（单事务，03 §6.8）：幂等键 → 合并 patch/补服务端字段/规范化 → 预检 → 类型级授权 → R-SCHEMA →
R-ZONE/R-PROMO/R-REF/R-STATE → 类型规则 → content_hash/answer_key/conflict_key → 插版本行 + 推进 head（expected_version）
+ 派生边 + effective_answer + outbox + AuditEvent。任何一步失败都不落库，但另起事务写一条拒绝审计。
"""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from sqlalchemy import Engine, and_, create_engine, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from .. import audit as audit_mod
from ..authz import SYSTEM_ACTOR, Actor, can_read, check_write_type, validate_actor
from ..canonical import content_hash as compute_content_hash
from ..canonical import hash_obj, sha256_tag
from ..datapoints import DataPointRegistry
from ..errors import GraphError
from ..hashing import build_input_m, input_hash, output_hash
from ..ids import ORG_GLOBAL, ObjectRef, new_id, new_trace_id, type_of_id
from ..models import GRAPH_TYPES, SCHEMA_VERSION, GraphObject
from ..rules import check_schema, run_pre_schema, run_rules
from ..rules.context import RuleContext
from ..statemachines import get_state_machine
from ..units import UnitSystem, default_units
from . import tables as T
from .edges import derive_edges
from .normalize import SERVER_FIELDS, merge_patch, normalize_body

MAX_BATCH = 500
TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


@dataclass
class WriteItem:
    """``POST /graph/objects`` 请求体（03 §6.8）。"""

    type: str
    object: dict[str, Any] | GraphObject
    expected_version: int | None = None
    idempotency_key: str | None = None
    patch_mode: str = "replace"  # replace | merge_from_head
    trace_id: str | None = None
    # 进程内编排专用：批内单条覆盖执行者（如 03 §5.6.9/§5.6.10 Approval(user)+Answer(approval-router) 同事务）。
    # HTTP API 不暴露此字段（令牌即执行者）。
    actor: Actor | None = None


@dataclass
class HeadInfo:
    id: str
    org_id: str
    type: str
    version: int
    status: str | None
    trust_zone: str
    updated_at: str

    @property
    def ref(self) -> str:
        return f"{self.id}#v{self.version}"


@dataclass
class _Batch:
    ids: set[str] = field(default_factory=set)
    deferred: list[Callable[[], None]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)


class _DryRun(Exception):
    def __init__(self, payload: Any) -> None:
        self.payload = payload


def parse_ts(s: str) -> str:
    """ISO 8601 → 存储格式（UTC，微秒，Z），以便字符串比较。"""
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).strftime(TS_FMT)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class _Reader:
    """``GraphReader`` 实现：绑定到一个连接（事务内可见批量中先写的对象）。"""

    def __init__(self, store: "GraphStore", conn: Any) -> None:
        self.store, self.conn = store, conn

    def get_version(self, org_id: str, obj_id: str, version: int) -> dict[str, Any] | None:
        row = self.conn.execute(select(T.object_version.c.body).where(
            T.object_version.c.org_id == org_id, T.object_version.c.id == obj_id,
            T.object_version.c.version == version)).first()
        return dict(row.body) if row else None

    def head_row(self, org_id: str, obj_id: str) -> Any:
        return self.conn.execute(select(T.object_head).where(
            T.object_head.c.org_id == org_id, T.object_head.c.id == obj_id)).first()

    def get_head_body(self, org_id: str, obj_id: str) -> dict[str, Any] | None:
        h = self.head_row(org_id, obj_id)
        return self.get_version(org_id, obj_id, h.head_version) if h else None

    def head_version_any(self, org_id: str, obj_id: str) -> int | None:
        for org in (org_id, ORG_GLOBAL):
            h = self.head_row(org, obj_id)
            if h:
                return h.head_version
        return None

    def orgs_of(self, obj_id: str) -> list[str]:
        return [r.org_id for r in self.conn.execute(select(T.object_head.c.org_id).where(T.object_head.c.id == obj_id))]

    def heads_where(self, org_id: str, type_name: str, **eq: Any) -> list[dict[str, Any]]:
        q = select(T.object_version.c.body).select_from(
            T.object_head.join(T.object_version, and_(
                T.object_head.c.org_id == T.object_version.c.org_id, T.object_head.c.id == T.object_version.c.id,
                T.object_head.c.head_version == T.object_version.c.version))
        ).where(T.object_head.c.org_id == org_id, T.object_head.c.type == type_name)
        if "status" in eq:
            q = q.where(T.object_head.c.status == eq["status"])
        if "request_id" in eq:
            q = q.where(T.object_version.c.request_id == eq["request_id"])
        out = []
        for row in self.conn.execute(q):
            b = dict(row.body)
            if all(b.get(k) == v for k, v in eq.items()):
                out.append(b)
        return out

    def versions_of(self, org_id: str, obj_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(select(T.object_version.c.body).where(
            T.object_version.c.org_id == org_id, T.object_version.c.id == obj_id).order_by(T.object_version.c.version))
        return [dict(r.body) for r in rows]

    def effective_answer(self, org_id: str, answer_key: str) -> tuple[str, int] | None:
        row = self.conn.execute(select(T.effective_answer).where(
            T.effective_answer.c.org_id == org_id, T.effective_answer.c.answer_key == answer_key)).first()
        return (row.answer_id, row.version) if row else None

    def has_audit(self, org_id: str, action: str, object_ref_prefix: str) -> bool:
        row = self.conn.execute(select(T.audit_event.c.seq).where(
            T.audit_event.c.org_id == org_id, T.audit_event.c.action == action,
            T.audit_event.c.object_ref.like(object_ref_prefix + "%")).limit(1)).first()
        return row is not None

    def factor_rows(self, factor_set_id: str) -> list[dict[str, Any]] | None:
        rows = [dict(r._mapping) for r in self.conn.execute(
            select(T.factor_row).where(T.factor_row.c.factor_set_id == factor_set_id))]
        return rows or None

    def roles_of(self, org_id: str, actor_id: str) -> list[str] | None:
        row = self.conn.execute(select(T.principal.c.roles).where(
            T.principal.c.org_id == org_id, T.principal.c.actor == actor_id)).first()
        return list(row.roles) if row else None

    def now(self) -> str:
        return self.store.peek_now()


class GraphStore:
    """证据图存储。``write``/``write_batch`` 是唯一写入路径（SSOT-2）。"""

    def __init__(
        self,
        url_or_engine: str | Engine = "sqlite+pysqlite:///:memory:",
        *,
        datapoints: DataPointRegistry | None = None,
        units: UnitSystem | None = None,
        clock: Callable[[], dt.datetime] | None = None,
        policy_hook: Callable[[Actor, str, dict[str, Any]], None] | None = None,
        create_tables: bool = True,
        idempotency_ttl_hours: int = 24,
        audit_rejections: bool = True,
    ) -> None:
        if isinstance(url_or_engine, str):
            kw: dict[str, Any] = {}
            if url_or_engine.startswith("sqlite"):
                kw["connect_args"] = {"check_same_thread": False}
                if ":memory:" in url_or_engine or url_or_engine.rstrip("/").endswith("sqlite:"):
                    kw["poolclass"] = StaticPool
            self.engine = create_engine(url_or_engine, **kw)
        else:
            self.engine = url_or_engine
        if create_tables:
            T.metadata.create_all(self.engine)
        self.datapoints = datapoints or DataPointRegistry.default()
        self.units = units or default_units()
        self._clock = clock or _utcnow
        self._last_ts: str | None = None
        self.policy_hook = policy_hook
        self.idempotency_ttl = dt.timedelta(hours=idempotency_ttl_hours)
        self.audit_rejections = audit_rejections

    # ------------------------------------------------------------------ time
    def peek_now(self) -> str:
        return self._clock().astimezone(dt.timezone.utc).strftime(TS_FMT)

    def _now(self) -> str:
        ts = self.peek_now()
        if self._last_ts is not None and ts <= self._last_ts:  # 事务时间严格单调（as-of 需要）
            d = dt.datetime.strptime(self._last_ts, TS_FMT) + dt.timedelta(microseconds=1)
            ts = d.strftime(TS_FMT)
        self._last_ts = ts
        return ts

    # ------------------------------------------------------------------ write API
    def write(
        self, type: str, obj: dict[str, Any] | GraphObject, expected_version: int | None = None,
        idempotency_key: str | None = None, actor: Actor | None = None, *, patch_mode: str = "replace",
        trace_id: str | None = None,
    ) -> str:
        """写一个对象版本，返回 ``ObjectRef``（``<id>#v<version>``）。失败抛 ``GraphError``。"""
        return self.write_batch([WriteItem(type, obj, expected_version, idempotency_key, patch_mode, trace_id)],
                                actor=actor)[0]

    def write_batch(self, items: Iterable[WriteItem | dict[str, Any]], actor: Actor | None = None) -> list[str]:
        """原子批量写入（≤500）。批内后写的对象可见先写的对象。"""
        return [r["object_ref"] for r in self._run_batch(items, actor, dry_run=False)]

    def write_batch_detailed(self, items: Iterable[WriteItem | dict[str, Any]], actor: Actor | None = None) -> list[dict[str, Any]]:
        return self._run_batch(items, actor, dry_run=False)

    def validate(self, items: Iterable[WriteItem | dict[str, Any]] | WriteItem | dict[str, Any],
                 actor: Actor | None = None) -> list[dict[str, Any]]:
        """只校验不写入（``POST /graph/validate``）：完整执行写入流程后回滚。"""
        if isinstance(items, (WriteItem, dict)):
            items = [items]
        return self._run_batch(items, actor, dry_run=True)

    def _run_batch(self, items: Iterable[WriteItem | dict[str, Any]], actor: Actor | None, dry_run: bool) -> list[dict[str, Any]]:
        if actor is None:
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "actor is required", rule_id="AUTHZ")
        items = [i if isinstance(i, WriteItem) else WriteItem(**i) for i in items]
        if len(items) > MAX_BATCH:
            raise GraphError("GRAPH_BATCH_TOO_LARGE", f"batch size {len(items)} > {MAX_BATCH}")
        current: dict[str, Any] = {}
        try:
            validate_actor(actor)
            with self.engine.begin() as conn:
                reader = _Reader(self, conn)
                batch = _Batch()
                for item in items:
                    item_actor = item.actor or actor
                    if item.actor is not None:
                        validate_actor(item.actor)
                        if item.actor.org_id != actor.org_id:
                            raise GraphError("GRAPH_REF_CROSS_TENANT", "batch items must share one tenant")
                    current = {"type": item.type, "object": item.object, "trace_id": item.trace_id, "actor": item_actor}
                    batch.results.append(self._write_one(conn, reader, item, item_actor, batch))
                for fn in batch.deferred:
                    fn()
                if dry_run:
                    raise _DryRun(batch.results)
                return batch.results
        except _DryRun as d:
            return [{**r, "valid": True} for r in d.payload]
        except GraphError as e:
            if self.audit_rejections and not dry_run:
                self._audit_rejection(current.get("actor") or actor, current, e)
            raise

    def _audit_rejection(self, actor: Actor, current: dict[str, Any], e: GraphError) -> None:
        obj = current.get("object")
        body = obj.to_body() if isinstance(obj, GraphObject) else (obj or {})
        oid = body.get("id") if isinstance(body, dict) else None
        trace = (body.get("provenance") if isinstance(body, dict) else None) or current.get("trace_id") or new_trace_id()
        try:
            with self.engine.begin() as conn:
                audit_mod.append_event(
                    conn, org_id=actor.org_id, action="object.create", created_by=actor.id, trace_id=trace,
                    created_at=self._now(), object_ref=oid if isinstance(oid, str) else None,
                    details={"result": "rejected", "type": current.get("type"), "code": e.code, "rule_id": e.rule_id,
                             "path": e.path},
                    ip=actor.ip, user_agent=actor.user_agent,
                )
        except Exception:  # pragma: no cover - 审计失败不掩盖原错误
            pass

    # ------------------------------------------------------------------ core write
    def _write_one(self, conn: Any, reader: _Reader, item: WriteItem, actor: Actor, batch: _Batch) -> dict[str, Any]:
        type_name = item.type
        if type_name not in GRAPH_TYPES:
            raise GraphError("GRAPH_SCHEMA_INVALID", f"unknown object type {type_name!r}", path="/type")
        if item.patch_mode not in ("replace", "merge_from_head"):
            raise GraphError("GRAPH_SCHEMA_INVALID", "patch_mode must be replace|merge_from_head", path="/patch_mode")
        raw = item.object.to_body() if isinstance(item.object, GraphObject) else copy.deepcopy(dict(item.object))
        if self.policy_hook:
            self.policy_hook(actor, type_name, raw)

        # 1 幂等键
        req_hash = hash_obj({"type": type_name, "object": raw, "expected_version": item.expected_version,
                             "patch_mode": item.patch_mode})
        if item.idempotency_key:
            row = conn.execute(select(T.idempotency).where(
                T.idempotency.c.org_id == actor.org_id, T.idempotency.c.idempotency_key == item.idempotency_key)).first()
            if row and row.expires_at > self.peek_now():
                if row.request_hash != req_hash:
                    raise GraphError("GRAPH_IDEMPOTENCY_MISMATCH", "same idempotency_key with a different request body",
                                     details={"idempotency_key": item.idempotency_key})
                return {**dict(row.response), "idempotent_replay": True}
            if row:
                conn.execute(delete(T.idempotency).where(T.idempotency.c.org_id == actor.org_id,
                                                         T.idempotency.c.idempotency_key == item.idempotency_key))

        # 2 服务端字段
        client_created_by = raw.pop("created_by", None)
        if client_created_by and client_created_by != actor.id:
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"created_by {client_created_by} != authenticated actor {actor.id}",
                             rule_id="AUTHZ", path="/created_by")
        for k in ("created_at", "content_hash", "version"):
            raw.pop(k, None)
        obj_id = raw.get("id")
        if not obj_id:
            if type_name == "FactorSet":
                raise GraphError("GRAPH_SCHEMA_INVALID", "FactorSet requires id fs:<source>@<version>", path="/id")
            obj_id = new_id(type_name)
            raw["id"] = obj_id
        if type_of_id(obj_id) != type_name:
            raise GraphError("GRAPH_SCHEMA_INVALID", f"id {obj_id!r} does not match type {type_name}", path="/id")
        org_id = self._target_org(type_name, raw, actor)

        head = reader.head_row(org_id, obj_id)
        if head is None:
            others = [o for o in reader.orgs_of(obj_id) if o != org_id]
            if others:
                raise GraphError("GRAPH_REF_CROSS_TENANT", f"{obj_id} belongs to another tenant", path="/id")
        # R-IMM
        if head is not None:
            if item.expected_version != head.head_version:
                raise GraphError("GRAPH_VERSION_CONFLICT",
                                 f"expected_version {item.expected_version} != head {head.head_version}",
                                 details={"head_version": head.head_version, "expected_version": item.expected_version})
        elif item.expected_version not in (None, 0):
            raise GraphError("GRAPH_VERSION_CONFLICT", f"{obj_id} does not exist (expected_version {item.expected_version})",
                             details={"head_version": None})
        prev = reader.get_version(org_id, obj_id, head.head_version) if head else None
        if head is not None and head.type != type_name:
            raise GraphError("GRAPH_SCHEMA_INVALID", f"{obj_id} is a {head.type}", path="/type")

        if item.patch_mode == "merge_from_head" and prev is not None:
            base = {k: v for k, v in prev.items() if k not in SERVER_FIELDS + ("promotion", "provenance", "supersedes")}
            raw = merge_patch(base, raw)

        body = dict(raw)
        body["id"] = obj_id
        body["org_id"] = org_id
        body["version"] = (head.head_version + 1) if head else 1
        body["created_by"] = actor.id
        body.setdefault("schema_version", SCHEMA_VERSION)
        body["provenance"] = raw.get("provenance") or item.trace_id or new_trace_id()
        if not body.get("trust_zone"):
            body["trust_zone"] = prev["trust_zone"] if prev else ("quarantine" if actor.zone == "quarantine" else "trusted")
        if prev is not None:
            want = f"{obj_id}#v{prev['version']}"
            if raw.get("supersedes") not in (None, want):
                raise GraphError("GRAPH_SCHEMA_INVALID", f"supersedes must be {want} for a new version", path="/supersedes")
            body["supersedes"] = want
        body = normalize_body(type_name, body, lambda rid: reader.head_version_any(org_id, rid))

        # 3 预检 + 类型级授权 + R-SCHEMA（stored 模式）
        run_pre_schema(type_name, body)
        check_write_type(actor, type_name, body)
        created_at = self._now()
        body["created_at"] = created_at
        body["content_hash"] = compute_content_hash(body)
        check_schema(type_name, body)

        # 4–6 规则
        ctx = RuleContext(reader=reader, actor=actor, type_name=type_name, new=body, prev=prev,
                          datapoints=self.datapoints, units=self.units, batch_ids=batch.ids)
        run_rules(ctx)

        # 8 commit
        ref = self._commit(conn, reader, type_name, body, prev, head, actor)
        batch.ids.add(obj_id)
        result = {"object_ref": ref, "content_hash": body["content_hash"], "type": type_name, "status": body.get("status")}
        if item.idempotency_key:
            exp = (dt.datetime.strptime(created_at, TS_FMT) + self.idempotency_ttl).strftime(TS_FMT)
            conn.execute(insert(T.idempotency).values(org_id=actor.org_id, idempotency_key=item.idempotency_key,
                                                      request_hash=req_hash, response=result, expires_at=exp))
        self._effects(conn, reader, type_name, body, prev, actor, batch)
        return result

    def _target_org(self, type_name: str, raw: dict[str, Any], actor: Actor) -> str:
        want = actor.org_id
        if type_name == "Organization":
            want = raw.get("id")
            if actor.org_id != want:
                raise GraphError("GRAPH_REF_CROSS_TENANT", "Organization.org_id must equal its own id and the actor's tenant",
                                 path="/org_id")
        if type_name == "FactorSet" and raw.get("org_id") == ORG_GLOBAL:
            if actor.id != "service:factor-import":
                raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "only service:factor-import writes global factor sets",
                                 rule_id="AUTHZ", path="/org_id")
            return ORG_GLOBAL
        given = raw.get("org_id")
        if given is not None and given != want:
            raise GraphError("GRAPH_REF_CROSS_TENANT", f"org_id {given} != actor tenant {want}", path="/org_id")
        return want

    def _commit(self, conn: Any, reader: _Reader, type_name: str, body: dict[str, Any], prev: dict[str, Any] | None,
                head: Any, actor: Actor) -> str:
        org_id, obj_id, version = body["org_id"], body["id"], body["version"]
        period = body.get("period") or {}
        dpid = body.get("datapoint_id")
        req_id = body.get("request_id") or ((body.get("submission") or {}).get("request_id"))
        try:
            conn.execute(insert(T.object_version).values(
                org_id=org_id, id=obj_id, version=version, type=type_name, created_at=body["created_at"],
                created_by=body["created_by"], trust_zone=body["trust_zone"], provenance=body["provenance"],
                supersedes=body.get("supersedes"), schema_version=body["schema_version"],
                content_hash=body["content_hash"], status=body.get("status"), body=body,
                answer_key=body.get("answer_key"), datapoint_base=dpid.split("@", 1)[0] if isinstance(dpid, str) else None,
                period_start=period.get("start"), period_end=period.get("end"), valid_from=body.get("valid_from"),
                valid_until=body.get("valid_until"), request_id=req_id if isinstance(req_id, str) else None,
            ))
        except IntegrityError as e:
            raise GraphError("GRAPH_VERSION_CONFLICT", "concurrent write to the same object version") from e
        if head is None:
            conn.execute(insert(T.object_head).values(org_id=org_id, id=obj_id, type=type_name, head_version=version,
                                                      status=body.get("status"), trust_zone=body["trust_zone"],
                                                      updated_at=body["created_at"]))
        else:
            res = conn.execute(update(T.object_head).where(
                T.object_head.c.org_id == org_id, T.object_head.c.id == obj_id,
                T.object_head.c.head_version == head.head_version,
            ).values(head_version=version, status=body.get("status"), trust_zone=body["trust_zone"],
                     updated_at=body["created_at"]))
            if res.rowcount != 1:
                raise GraphError("GRAPH_VERSION_CONFLICT", "head moved concurrently")
        edges = derive_edges(type_name, body, lambda rid: reader.head_version_any(org_id, rid))
        for e in edges:
            self._insert_edge(conn, org_id, e, body["created_at"])
        if type_name == "Answer":
            self._update_effective(conn, body)
        promoted = bool(prev) and prev.get("trust_zone") == "quarantine" and body["trust_zone"] == "trusted"
        action = "object.promote" if promoted else ("object.create" if version == 1 else "object.version")
        ref = f"{obj_id}#v{version}"
        conn.execute(insert(T.outbox).values(
            org_id=org_id, object_ref=ref, event_type="graph.object.committed", created_at=body["created_at"],
            payload={"type": type_name, "id": obj_id, "version": version, "status": body.get("status"),
                     "prev_status": prev.get("status") if prev else None, "trust_zone": body["trust_zone"],
                     "content_hash": body["content_hash"]}))
        audit_mod.append_event(
            conn, org_id=org_id, action=action, created_by=actor.id, trace_id=body["provenance"],
            created_at=body["created_at"], object_ref=ref, content_hash=body["content_hash"],
            details={"type": type_name, "status": body.get("status"), "edges": len(edges)},
            ip=actor.ip if actor.is_user else None, user_agent=actor.user_agent if actor.is_user else None,
        )
        if actor.is_user:
            self._upsert_principal(conn, actor)
        return ref

    def _insert_edge(self, conn: Any, org_id: str, e: dict[str, Any], created_at: str) -> None:
        tv = T.edge.c.to_version
        exists = conn.execute(select(T.edge.c.seq).where(
            T.edge.c.org_id == org_id, T.edge.c.edge_type == e["edge_type"], T.edge.c.from_id == e["from_id"],
            T.edge.c.from_version == e["from_version"], T.edge.c.to_id == e["to_id"],
            (tv.is_(None) if e["to_version"] is None else tv == e["to_version"]))).first()
        if not exists:
            conn.execute(insert(T.edge).values(org_id=org_id, created_at=created_at, **e))

    def _update_effective(self, conn: Any, body: dict[str, Any]) -> None:
        org_id, key = body["org_id"], body["answer_key"]
        row = conn.execute(select(T.effective_answer).where(
            T.effective_answer.c.org_id == org_id, T.effective_answer.c.answer_key == key)).first()
        if body["status"] in ("approved", "submitted"):
            if row:
                conn.execute(update(T.effective_answer).where(
                    T.effective_answer.c.org_id == org_id, T.effective_answer.c.answer_key == key,
                ).values(answer_id=body["id"], version=body["version"]))
            else:
                conn.execute(insert(T.effective_answer).values(org_id=org_id, answer_key=key, answer_id=body["id"],
                                                               version=body["version"]))
        elif row and row.answer_id == body["id"]:
            conn.execute(delete(T.effective_answer).where(T.effective_answer.c.org_id == org_id,
                                                          T.effective_answer.c.answer_key == key))

    def _upsert_principal(self, conn: Any, actor: Actor) -> None:
        roles = sorted(actor.roles)
        row = conn.execute(select(T.principal.c.roles).where(
            T.principal.c.org_id == actor.org_id, T.principal.c.actor == actor.id)).first()
        if row is None:
            conn.execute(insert(T.principal).values(org_id=actor.org_id, actor=actor.id, roles=roles,
                                                    updated_at=self.peek_now()))
        elif list(row.roles) != roles:
            conn.execute(update(T.principal).where(T.principal.c.org_id == actor.org_id, T.principal.c.actor == actor.id)
                         .values(roles=roles, updated_at=self.peek_now()))

    # ------------------------------------------------------------------ 写入副作用（同事务）
    def _system_write(self, conn: Any, reader: _Reader, type_name: str, org_id: str, obj_id: str,
                      patch: dict[str, Any], batch: _Batch, trace_id: str) -> str | None:
        h = reader.head_row(org_id, obj_id)
        if h is None:
            return None
        sys_actor = Actor(id=SYSTEM_ACTOR, org_id=org_id)
        item = WriteItem(type_name, {"id": obj_id, **patch}, expected_version=h.head_version,
                         patch_mode="merge_from_head", trace_id=trace_id)
        return self._write_one(conn, reader, item, sys_actor, batch)["object_ref"]

    def _can_auto(self, type_name: str, src: str | None, dst: str) -> bool:
        sm = get_state_machine(type_name)
        return bool(sm and src != dst and any(SYSTEM_ACTOR in t.actors for t in sm.find(src, dst)))

    def _effects(self, conn: Any, reader: _Reader, type_name: str, body: dict[str, Any], prev: dict[str, Any] | None,
                 actor: Actor, batch: _Batch) -> None:
        org_id, trace = body["org_id"], body["provenance"]
        # (a) 跨对象替换：新对象 v1 的 supersedes 指向另一对象 → 旧对象自动追加 superseded 版本（03 §1.4-2）
        sup = body.get("supersedes")
        if prev is None and sup and type_name in ("Evidence", "Calculation"):
            old_id = sup.split("#", 1)[0]
            old = reader.get_head_body(org_id, old_id)
            if old and self._can_auto(type_name, old.get("status"), "superseded"):
                self._system_write(conn, reader, type_name, org_id, old_id, {"status": "superseded"}, batch, trace)
        if type_name == "Answer" and body.get("status") in ("approved", "submitted"):
            targets = set()
            for v in reader.versions_of(org_id, body["id"]):
                s = v.get("supersedes")
                if s and not s.startswith(body["id"] + "#"):
                    targets.add(s.split("#", 1)[0])
            for old_id in targets:
                old = reader.get_head_body(org_id, old_id)
                if old and self._can_auto("Answer", old.get("status"), "superseded"):
                    self._system_write(conn, reader, "Answer", org_id, old_id, {"status": "superseded"}, batch, trace)
        # (b) 人工更正：Evidence.corrects → 被更正证据 superseded
        if type_name == "Evidence" and body.get("corrects") and body.get("corrects") != (prev or {}).get("corrects"):
            old_id = body["corrects"].split("#", 1)[0]
            old = reader.get_head_body(org_id, old_id)
            if old and self._can_auto("Evidence", old.get("status"), "superseded"):
                self._system_write(conn, reader, "Evidence", org_id, old_id, {"status": "superseded"}, batch, trace)
        # (c) ClosedQuestion answered → 同事务追加目标对象新版本（03 §3.11），批内已手工写入目标者跳过
        if type_name == "ClosedQuestion" and body.get("status") == "answered" and (prev or {}).get("status") != "answered":
            cq_ref = f"{body['id']}#v{body['version']}"
            batch.deferred.append(lambda: self._derive_from_cq(conn, reader, body, cq_ref, batch))

    def _derive_from_cq(self, conn: Any, reader: _Reader, cq: dict[str, Any], cq_ref: str, batch: _Batch) -> None:
        org_id, trace = cq["org_id"], cq["provenance"]
        ans = cq.get("answer") or {}
        yes = ans.get("bool")
        plans: list[tuple[str, str, dict[str, Any]]] = []
        tid = cq["target_ref"].split("#", 1)[0]
        ttype = type_of_id(tid)
        if cq["kind"] == "single_choice":
            chosen = {o.get("ref", "").split("#", 1)[0] for o in cq.get("options") or [] if o["key"] == ans.get("choice")}
            for o in cq.get("options") or []:
                oid = (o.get("ref") or "").split("#", 1)[0]
                if type_of_id(oid) == "Mapping":
                    plans.append(("Mapping", oid, {"status": "confirmed" if oid in chosen else "rejected",
                                                   "decided_by_cq": cq["id"]}))
        elif cq["kind"] in ("yes_no", "numeric_confirm") and isinstance(yes, bool):
            if ttype == "Evidence":
                patch = {"status": "verified", "verification": {"method": "closed_question", "cq_ref": cq_ref,
                                                                "by": ans.get("answered_by")}} if yes else {"status": "rejected"}
                plans.append(("Evidence", tid, patch))
            elif ttype == "Mapping":
                plans.append(("Mapping", tid, {"status": "confirmed" if yes else "rejected", "decided_by_cq": cq["id"]}))
        for t, oid, patch in plans:
            if oid in batch.ids:
                continue  # 批内已由调用方写入目标新版本（如 03 §5.6 CQ v2 + Evidence v2 同事务）
            cur = reader.get_head_body(org_id, oid)
            if cur is None or not self._can_auto(t, cur.get("status"), patch["status"]):
                continue
            if cur.get("trust_zone") == "quarantine":
                # 晋升版本只允许改 trust_zone/status/promotion（PC-08），decided_by_cq 不写入；裁决由 CQ 的 resolves 边与
                # promotion.rule=human 追溯
                patch = {k: v for k, v in patch.items() if k != "decided_by_cq"}
                patch = {**patch, "trust_zone": "trusted",
                         "promotion": {"rule": "human", "schema_version": SCHEMA_VERSION, "checked_at": ans["answered_at"]}}
            self._system_write(conn, reader, t, org_id, oid, patch, batch, trace)

    # ------------------------------------------------------------------ read API
    def _org_for(self, obj_id: str, actor: Actor | None, org_id: str | None, conn: Any) -> str | None:
        if actor is not None:
            return actor.org_id
        if org_id is not None:
            return org_id
        orgs = _Reader(self, conn).orgs_of(obj_id)
        return orgs[0] if orgs else None

    def get(self, id: str, version: int | None = None, *, as_of_tx: str | None = None, as_of_valid: str | None = None,
            actor: Actor | None = None, org_id: str | None = None) -> dict[str, Any] | None:
        """取对象：默认 head；``version`` 指定版本；``as_of_tx``/``as_of_valid`` 双时间查询（03 §4.6）。"""
        if "#v" in id and version is None:
            r = ObjectRef.parse(id)
            id, version = r.id, r.version
        with self.engine.connect() as conn:
            org = self._org_for(id, actor, org_id, conn)
            if org is None:
                return None
            for o in (org, ORG_GLOBAL) if org != ORG_GLOBAL else (org,):
                body = self._get_in(conn, o, id, version, as_of_tx)
                if body is not None:
                    break
            if body is None:
                return None
            if as_of_valid and not _valid_on(body, as_of_valid):
                return None
            if not can_read(actor, type_of_id(id) or "", body):
                return None
            return body

    def _get_in(self, conn: Any, org: str, id: str, version: int | None, as_of_tx: str | None) -> dict[str, Any] | None:
        ov = T.object_version
        q = select(ov.c.body).where(ov.c.org_id == org, ov.c.id == id)
        if version is not None:
            q = q.where(ov.c.version == version)
        else:
            if as_of_tx:
                q = q.where(ov.c.created_at <= parse_ts(as_of_tx))
            q = q.order_by(ov.c.version.desc()).limit(1)
        row = conn.execute(q).first()
        return dict(row.body) if row else None

    def head(self, id: str, *, org_id: str | None = None, actor: Actor | None = None) -> HeadInfo | None:
        with self.engine.connect() as conn:
            org = self._org_for(id, actor, org_id, conn)
            if org is None:
                return None
            r = _Reader(self, conn)
            h = r.head_row(org, id) or r.head_row(ORG_GLOBAL, id)
            if h is None:
                return None
            if actor is not None:
                body = r.get_version(h.org_id, id, h.head_version)
                if not can_read(actor, h.type, body or {}):
                    return None
            return HeadInfo(h.id, h.org_id, h.type, h.head_version, h.status, h.trust_zone, h.updated_at)

    def history(self, id: str, *, org_id: str | None = None, actor: Actor | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            org = self._org_for(id, actor, org_id, conn)
            if org is None:
                return []
            out = _Reader(self, conn).versions_of(org, id)
            return [b for b in out if can_read(actor, type_of_id(id) or "", b)]

    def query(self, type: str, *, actor: Actor | None = None, org_id: str | None = None, as_of_tx: str | None = None,
              as_of_valid: str | None = None, include_history: bool = False, limit: int | None = None,
              **filters: Any) -> list[dict[str, Any]]:
        """按类型查询（默认每个对象取 head；给 ``as_of_tx`` 时取当时最新版本）。

        ``filters``：顶层字段相等；``datapoint_base=dp:...`` 匹配不带 SemVer 的数据点 ID。
        """
        org = actor.org_id if actor else org_id
        ov = T.object_version
        with self.engine.connect() as conn:
            q = select(ov.c.body, ov.c.id, ov.c.version).where(ov.c.type == type)
            if org is not None:
                q = q.where(ov.c.org_id.in_([org, ORG_GLOBAL]))
            if as_of_tx:
                q = q.where(ov.c.created_at <= parse_ts(as_of_tx))
            if "datapoint_base" in filters:
                q = q.where(ov.c.datapoint_base == filters.pop("datapoint_base"))
            if "request_id" in filters:
                q = q.where(ov.c.request_id == filters["request_id"])
            rows = conn.execute(q.order_by(ov.c.id, ov.c.version.desc())).all()
        latest: dict[str, dict[str, Any]] = {}
        out: list[dict[str, Any]] = []
        for r in rows:
            b = dict(r.body)
            if include_history:
                out.append(b)
            elif r.id not in latest:
                latest[r.id] = b
        if not include_history:
            out = list(latest.values())
        res = []
        for b in out:
            if any(b.get(k) != v for k, v in filters.items()):
                continue
            if as_of_valid and not _valid_on(b, as_of_valid):
                continue
            if not can_read(actor, type, b):
                continue
            res.append(b)
        res.sort(key=lambda b: (b.get("created_at", ""), b["id"]))
        return res[:limit] if limit else res

    def effective(self, org_id: str, answer_key: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = _Reader(self, conn)
            e = r.effective_answer(org_id, answer_key)
            return r.get_version(org_id, e[0], e[1]) if e else None

    # ------------------------------------------------------------------ lineage / impact（03 §4.6、§4.7）
    def edges(self, *, org_id: str, from_id: str | None = None, to_id: str | None = None,
              edge_type: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            q = select(T.edge).where(T.edge.c.org_id == org_id)
            if from_id:
                q = q.where(T.edge.c.from_id == from_id)
            if to_id:
                q = q.where(T.edge.c.to_id == to_id)
            if edge_type:
                q = q.where(T.edge.c.edge_type == edge_type)
            return [_edge_dict(r) for r in conn.execute(q.order_by(T.edge.c.seq))]

    def lineage(self, ref: str, depth: int = 8, *, actor: Actor | None = None, org_id: str | None = None,
                include_supersedes: bool = False) -> dict[str, Any]:
        """向上谱系：从 ``ref`` 沿出边（钉住版本）递归，返回节点与边。"""
        return self._walk(ref, depth, actor, org_id, up=True, include_supersedes=include_supersedes)

    def impact(self, ref: str, depth: int = 8, *, actor: Actor | None = None, org_id: str | None = None) -> dict[str, Any]:
        """向下影响分析：受某证据/因子/计算更正影响的 Calculation、Answer 与已提交 Request。"""
        res = self._walk(ref, depth, actor, org_id, up=False, include_supersedes=False)
        calcs, answers, requests = set(), set(), set()
        with self.engine.connect() as conn:
            rd = _Reader(self, conn)
            org = res["org_id"]
            for n in res["nodes"]:
                nid = n.split("#", 1)[0]
                t = type_of_id(nid)
                if t == "Calculation":
                    calcs.add(n)
                elif t == "Answer":
                    answers.add(n)
                    head = rd.get_head_body(org, nid)
                    sub = (head or {}).get("submission") or {}
                    if head and head.get("status") == "submitted" and sub.get("request_id"):
                        requests.add(sub["request_id"])
        res.update(calculations=sorted(calcs), answers=sorted(answers), submitted_requests=sorted(requests))
        return res

    def _walk(self, ref: str, depth: int, actor: Actor | None, org_id: str | None, *, up: bool,
              include_supersedes: bool) -> dict[str, Any]:
        r0 = ObjectRef.parse(ref)
        with self.engine.connect() as conn:
            rd = _Reader(self, conn)
            org = self._org_for(r0.id, actor, org_id, conn)
            if org is None:
                raise GraphError("GRAPH_REF_NOT_FOUND", f"{ref} not found")
            v0 = r0.version or rd.head_version_any(org, r0.id)
            if v0 is None or (actor and self.get(r0.id, v0, actor=actor) is None):
                raise GraphError("GRAPH_REF_NOT_FOUND", f"{ref} not found")
            start = f"{r0.id}#v{v0}"
            nodes, edges_out = {start}, []
            frontier = [(r0.id, v0)]
            for _ in range(max(depth, 0)):
                nxt = []
                for nid, nv in frontier:
                    if up:
                        q = select(T.edge).where(T.edge.c.org_id == org, T.edge.c.from_id == nid,
                                                 T.edge.c.from_version == nv)
                    else:
                        q = select(T.edge).where(T.edge.c.org_id == org, T.edge.c.to_id == nid).where(
                            (T.edge.c.to_version == nv) | T.edge.c.to_version.is_(None))
                    for e in conn.execute(q):
                        ed = _edge_dict(e)
                        if ed["edge_type"] == "supersedes" and not include_supersedes:
                            continue
                        if up:
                            tid, tv = ed["to_id"], ed["to_version"]
                            if tv is None and not tid.startswith("dp:"):
                                tv = rd.head_version_any(org, tid)
                        else:
                            tid, tv = ed["from_id"], ed["from_version"]
                        edges_out.append(ed)
                        key = f"{tid}#v{tv}" if tv else tid
                        if key not in nodes:
                            if actor is not None and tv and self.get(tid, tv, actor=actor) is None:
                                continue
                            nodes.add(key)
                            if tv:
                                nxt.append((tid, tv))
                frontier = nxt
                if not frontier:
                    break
            return {"root": start, "org_id": org, "direction": "up" if up else "down", "nodes": sorted(nodes),
                    "edges": edges_out}

    # ------------------------------------------------------------------ replay（03 §4.7）
    def replay_calculation(self, calc_ref: str, engine_version: str | None = None, *, actor: Actor | None = None,
                           org_id: str | None = None) -> dict[str, Any]:
        calc = self.get(calc_ref, actor=actor, org_id=org_id)
        if calc is None or not calc["id"].startswith("calc_"):
            raise GraphError("GRAPH_REF_NOT_FOUND", f"{calc_ref} not found")
        fs = self.get(calc["factor_set_version"], org_id=calc["org_id"])
        report: dict[str, Any] = {"calc_ref": f"{calc['id']}#v{calc['version']}", "engine_version": calc["engine_version"],
                                  "requested_engine": engine_version}
        if fs is None:
            report.update(status="error", reason="factor set missing")
            return report
        m = build_input_m(calc, fs["rows_hash"])
        ih, oh = input_hash(m), output_hash(calc["result"], calc.get("breakdown"))
        report.update(m=m, input_hash_ok=ih == calc["input_hash"], output_hash_ok=oh == calc["output_hash"],
                      recomputed_input_hash=ih, recomputed_output_hash=oh, engine_rerun=False)
        integrity = report["input_hash_ok"] and report["output_hash_ok"]
        report["hash_integrity_ok"] = integrity
        drift = False
        try:
            import belegt_calc  # type: ignore

            rows = self.factor_rows(fs["id"])
            if rows and hasattr(belegt_calc, "replay"):
                rr = belegt_calc.replay(calc, _factor_set_for_calc(belegt_calc, fs, rows))
                ok = getattr(rr, "ok", None)
                if ok is None and isinstance(rr, dict):
                    ok = rr.get("ok")
                report.update(engine_rerun=True, engine_ok=ok, engine_report=_jsonable(rr))
                if ok is False:
                    drift = True
        except Exception as e:  # 引擎不可用或接口不匹配：只做哈希复核
            report["engine_note"] = f"engine replay skipped: {type(e).__name__}"
        # drift：同一 input_hash 在当前引擎上得到不同 output_hash（03 §4.7，不写图，只报告）
        report["drift"] = drift
        report["status"] = "integrity_error" if not integrity else ("drift" if drift else "ok")
        return report

    def replay_answer(self, ans_ref: str, *, actor: Actor | None = None, org_id: str | None = None) -> dict[str, Any]:
        ans = self.get(ans_ref, actor=actor, org_id=org_id)
        if ans is None or not ans["id"].startswith("ans_"):
            raise GraphError("GRAPH_REF_NOT_FOUND", f"{ans_ref} not found")
        lin = self.lineage(f"{ans['id']}#v{ans['version']}", actor=actor, org_id=ans["org_id"])
        calcs = [n for n in lin["nodes"] if n.startswith("calc_")]
        reports = [self.replay_calculation(c, org_id=ans["org_id"]) for c in calcs]
        val_ok = True
        if ans.get("value_type") == "number" and ans.get("calculation_ref"):
            c = self.get(ans["calculation_ref"], org_id=ans["org_id"])
            from decimal import Decimal

            try:
                conv = self.units.convert(Decimal(c["result"]["value"]), c["result"]["unit"], ans["unit"])
                val_ok = conv.normalize() == Decimal(ans["value"]).normalize()
            except Exception:
                val_ok = False
        integrity = val_ok and all(r.get("hash_integrity_ok") for r in reports)
        drift = any(r.get("drift") for r in reports)
        return {"answer_ref": f"{ans['id']}#v{ans['version']}", "calculations": reports, "r_val_ok": val_ok,
                "hash_integrity_ok": integrity, "drift": drift,
                "status": "integrity_error" if not integrity else ("drift" if drift else "ok")}

    # ------------------------------------------------------------------ 审计 / outbox / blob / 因子 / trace
    def record_audit(self, actor: Actor, action: str, *, object_ref: str | None = None, content_hash: str | None = None,
                     details: dict[str, Any] | None = None, trace_id: str | None = None) -> dict[str, Any]:
        """非对象写入类审计（``egress.send``、``export.create``、``auth.login``、``authz.deny`` 等）。"""
        with self.engine.begin() as conn:
            return audit_mod.append_event(conn, org_id=actor.org_id, action=action, created_by=actor.id,
                                          trace_id=trace_id or new_trace_id(), created_at=self._now(),
                                          object_ref=object_ref, content_hash=content_hash, details=details,
                                          ip=actor.ip, user_agent=actor.user_agent)

    def audit_events(self, org_id: str, start_seq: int = 1, end_seq: int | None = None,
                     object_ref_prefix: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            evs = list(audit_mod.iter_events(conn, org_id, start_seq, end_seq))
        if object_ref_prefix:
            evs = [e for e in evs if str(e.get("object_ref", "")).startswith(object_ref_prefix)]
        return evs

    def verify_audit_chain(self, org_id: str) -> audit_mod.ChainReport:
        with self.engine.connect() as conn:
            return audit_mod.verify_chain(conn, org_id)

    def outbox_pending(self, limit: int = 100, org_id: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            q = select(T.outbox).where(T.outbox.c.published_at.is_(None))
            if org_id:
                q = q.where(T.outbox.c.org_id == org_id)
            return [dict(r._mapping) for r in conn.execute(q.order_by(T.outbox.c.seq).limit(limit))]

    def outbox_mark_published(self, seqs: Iterable[int]) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(T.outbox).where(T.outbox.c.seq.in_(list(seqs))).values(published_at=self.peek_now()))

    def put_blob(self, org_id: str, data: bytes, mime: str | None = None, uploaded_by: str | None = None) -> str:
        """内容寻址登记（03 §6.7 ``POST /blobs``）；返回 ``sha256:<hex>``。"""
        sha = sha256_tag(data)
        with self.engine.begin() as conn:
            exists = conn.execute(select(T.blob.c.sha256).where(T.blob.c.org_id == org_id, T.blob.c.sha256 == sha)).first()
            if not exists:
                conn.execute(insert(T.blob).values(org_id=org_id, sha256=sha, mime=mime, size=len(data), content=data,
                                                   uploaded_by=uploaded_by, created_at=self.peek_now()))
        return sha

    def get_blob(self, org_id: str, sha256: str) -> tuple[bytes, str | None] | None:
        with self.engine.connect() as conn:
            r = conn.execute(select(T.blob).where(T.blob.c.org_id == org_id, T.blob.c.sha256 == sha256)).first()
            return (bytes(r.content), r.mime) if r else None

    def put_factor_rows(self, factor_set_id: str, rows: Iterable[dict[str, Any]]) -> int:
        n = 0
        with self.engine.begin() as conn:
            for r in rows:
                conn.execute(insert(T.factor_row).values(
                    factor_set_id=factor_set_id, source_row_id=r["source_row_id"], factor_key=r["factor_key"],
                    geography=r.get("geography", ""), year=str(r.get("year", "")), value=str(r["value"]), unit=r["unit"]))
                n += 1
        return n

    def factor_rows(self, factor_set_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return _Reader(self, conn).factor_rows(factor_set_id) or []

    def record_trace(self, trace: dict[str, Any] | Any) -> None:
        body = trace.to_body() if hasattr(trace, "to_body") else dict(trace)
        from ..schemas import validate as sv

        errs = sv("trace.json", body)
        if errs:
            raise GraphError("GRAPH_SCHEMA_INVALID", errs[0]["message"], path=errs[0]["path"])
        with self.engine.begin() as conn:
            conn.execute(insert(T.trace).values(org_id=body["org_id"], id=body["id"], span_id=body["span_id"],
                                                started_at=body["started_at"], body=body))

    def traces(self, org_id: str, trace_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            q = select(T.trace.c.body).where(T.trace.c.org_id == org_id)
            if trace_ids is not None:
                q = q.where(T.trace.c.id.in_(list(trace_ids)))
            return [dict(r.body) for r in conn.execute(q)]

    # ------------------------------------------------------------------ SSOT-3 / SSOT-7
    def verify_edges(self, org_id: str) -> list[dict[str, Any]]:
        """夜间校验：按字段重新派生边，与 graph_edge 比较；返回不一致项（空 = 一致）。"""
        expected, problems = set(), []
        with self.engine.connect() as conn:
            rd = _Reader(self, conn)
            rows = conn.execute(select(T.object_version.c.type, T.object_version.c.body).where(
                T.object_version.c.org_id == org_id).order_by(T.object_version.c.created_at)).all()
            heads_at: dict[str, int] = {}
            for r in rows:
                b = dict(r.body)
                for e in derive_edges(r.type, b, lambda rid: heads_at.get(rid) or rd.head_version_any(org_id, rid)):
                    expected.add((e["edge_type"], e["from_id"], e["from_version"], e["to_id"], e["to_version"]))
                heads_at[b["id"]] = b["version"]
            actual = {(e["edge_type"], e["from_id"], e["from_version"], e["to_id"], e["to_version"])
                      for e in (_edge_dict(x) for x in conn.execute(select(T.edge).where(T.edge.c.org_id == org_id)))}
        for k in sorted(expected - actual, key=str):
            problems.append({"missing": k})
        for k in sorted(actual - expected, key=str):
            problems.append({"unexpected": k})
        return problems

    def rebuild_derived(self, org_id: str) -> dict[str, int]:
        """SSOT-7：由版本表完全重建派生结构（head、边、effective_answer）。返回各表重建行数。"""
        with self.engine.begin() as conn:
            rows = conn.execute(select(T.object_version.c.type, T.object_version.c.body).where(
                T.object_version.c.org_id == org_id).order_by(T.object_version.c.created_at, T.object_version.c.version)).all()
            conn.execute(delete(T.edge).where(T.edge.c.org_id == org_id))
            conn.execute(delete(T.object_head).where(T.object_head.c.org_id == org_id))
            conn.execute(delete(T.effective_answer).where(T.effective_answer.c.org_id == org_id))
            heads: dict[str, dict[str, Any]] = {}
            n_edges = 0
            for r in rows:
                b = dict(r.body)
                for e in derive_edges(r.type, b, lambda rid: heads[rid]["version"] if rid in heads else
                                      _Reader(self, conn).head_version_any(org_id, rid)):
                    self._insert_edge(conn, org_id, e, b["created_at"])
                    n_edges += 1
                heads[b["id"]] = {"type": r.type, **b}
            for oid, b in heads.items():
                conn.execute(insert(T.object_head).values(org_id=org_id, id=oid, type=b["type"], head_version=b["version"],
                                                          status=b.get("status"), trust_zone=b["trust_zone"],
                                                          updated_at=b["created_at"]))
                if b["type"] == "Answer" and b.get("status") in ("approved", "submitted"):
                    self._update_effective(conn, {k: v for k, v in b.items() if k != "type"})
            return {"heads": len(heads), "edges": n_edges}


def _edge_dict(r: Any) -> dict[str, Any]:
    d = dict(r._mapping)
    d.pop("seq", None)
    return d


def _valid_on(body: dict[str, Any], d: str) -> bool:
    t = type_of_id(body["id"])
    if t == "Evidence" or t == "FactorSet":
        return body.get("valid_from", "0000-01-01") <= d <= (body.get("valid_until") or "9999-12-31")
    if t in ("Answer", "Calculation"):
        p = body.get("period") or {}
        return p.get("start", "0000-01-01") <= d <= p.get("end", "9999-12-31")
    return True


def _jsonable(o: Any) -> Any:
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if hasattr(o, "__dict__"):
        return {k: _jsonable(v) for k, v in vars(o).items() if not k.startswith("_")}
    return str(o)


def _factor_set_for_calc(belegt_calc: Any, fs: dict[str, Any], rows: list[dict[str, Any]]) -> Any:
    """把图中的 FactorSet + ref_factor 行转换为 ``belegt_calc.FactorSet``（05 §7.1）。"""
    FR, FS = belegt_calc.FactorRow, belegt_calc.FactorSet
    frows = [FR(factor_key=r["factor_key"], geography=r["geography"], year=str(r["year"]), value=str(r["value"]),
                unit=r["unit"], source_row_id=r["source_row_id"]) for r in rows]
    return FS(id=fs["id"], source=fs["source"], license=fs["license"], published_at=fs["published_at"],
              valid_from=fs["valid_from"], rows=frows, rows_hash=fs["rows_hash"], status=fs.get("status", "active"),
              valid_until=fs.get("valid_until"), row_count=fs.get("row_count"))
