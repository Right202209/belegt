# Spec: 04 §6.3（A2A：belegt.subscribe_datapoints —— 订阅 ID；后续 push {answer_ref, value, unit, period, approval_ref}；同意按
#       DataPoint × 客户 × 期限授予、可撤回，撤回后订阅停止推送；每次交付写 AuditEvent action=egress.send）、07 §4.3（cp_scope）、
#       07 §2（客户只见授权范围）、03 §3.12 Counterparty.data_sharing_grant
"""客户订阅：某客户（``Counterparty``）订阅一组 DataPoint 的已批准答案变更，经 webhook 推送（HMAC-SHA256 签名）。

存储：与证据图同库的两张独立表（不改 GraphStore 表结构）：``a2a_subscription``、``a2a_delivery``（``(subscription_id, answer_ref)``
唯一 → 每个已批准答案版本每个订阅最多成功推送一次）。

推送用"状态差分"而非消费 outbox：``SubscriptionService.dispatch()`` 以订阅客户的视角（``cp_scope=[客户]``、``approved_only``）读取
已批准/已提交答案，未成功推送过的版本逐条 POST（失败记录并在下次 dispatch 重试，最多 ``max_attempts`` 次）。由调度器/worker 或
``POST /a2a/subscriptions/dispatch`` 触发；不会与其他 outbox 消费者争用游标。

``data_sharing_grant``（Counterparty 字段，03 未规定内部结构；本实现约定）::

    {"datapoints": ["dp:env.ghg.scope1", ...] | ["*"], "valid_until": "2027-12-31", "purposes": [...], "revoked": false}

签名：``X-Belegt-Signature: sha256=<hex>``，``hex = HMAC_SHA256(secret, f"{timestamp}.{body}")``；``X-Belegt-Timestamp``（Unix 秒）、
``X-Belegt-Subscription``、``X-Belegt-Delivery``。接收方用 ``verify_signature()`` 校验（默认 300 s 容差防重放）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Callable
from urllib.parse import urlparse

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, Text, UniqueConstraint, insert, select, update
from ulid import ULID

from ..authz import Actor
from ..errors import GraphError

metadata = MetaData()
a2a_subscription = Table(
    "a2a_subscription", metadata,
    Column("id", String(40), primary_key=True), Column("org_id", String(64), nullable=False, index=True),
    Column("counterparty_id", String(64), nullable=False), Column("datapoint_ids", JSON, nullable=False),
    Column("period", JSON), Column("webhook_url", Text, nullable=False), Column("secret", String(200), nullable=False),
    Column("status", String(20), nullable=False), Column("created_by", String(120), nullable=False),
    Column("created_at", String(40), nullable=False), Column("updated_at", String(40), nullable=False), Column("note", Text),
)
a2a_delivery = Table(
    "a2a_delivery", metadata,
    Column("id", String(40), primary_key=True), Column("subscription_id", String(40), nullable=False, index=True),
    Column("answer_ref", String(80), nullable=False), Column("status", String(20), nullable=False),
    Column("attempts", Integer, nullable=False, default=0), Column("http_status", Integer), Column("error", Text),
    Column("created_at", String(40), nullable=False), Column("updated_at", String(40), nullable=False),
    UniqueConstraint("subscription_id", "answer_ref", name="uq_a2a_delivery"),
)

SIGNATURE_HEADER = "X-Belegt-Signature"
TIMESTAMP_HEADER = "X-Belegt-Timestamp"
MANAGE_ROLES = ("compliance_owner", "org_admin")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sign(secret: str, body: bytes, timestamp: int | str) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("ascii") + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def verify_signature(secret: str, body: bytes, timestamp: str | int, signature: str, *, tolerance_s: int = 300,
                     now: float | None = None) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if tolerance_s and abs((now if now is not None else time.time()) - ts) > tolerance_s:
        return False
    return hmac.compare_digest(sign(secret, body, ts), signature or "")


def grant_allows(grant: dict[str, Any] | None, datapoint_ids: list[str], *, today: str) -> tuple[bool, str]:
    """同意检查：未授权 / 已撤回 / 已过期 / 数据点超出白名单。"""
    if not grant:
        return False, "consent_required"
    if grant.get("revoked"):
        return False, "consent_revoked"
    if grant.get("valid_until") and str(grant["valid_until"])[:10] < today:
        return False, "consent_expired"
    allowed = {d.split("@", 1)[0] for d in grant.get("datapoints") or []}
    if "*" in allowed:
        return True, "ok"
    missing = sorted({d.split("@", 1)[0] for d in datapoint_ids} - allowed)
    if missing:
        return False, "datapoints_not_granted:" + ",".join(missing)
    return True, "ok"


class SubscriptionService:
    def __init__(self, store: Any, *, http_client: Any = None, allow_http: bool = False, allowed_hosts: list[str] | None = None,
                 clock: Callable[[], dt.datetime] | None = None, max_attempts: int = 5, timeout_s: float = 10.0) -> None:
        self.store = store
        self.engine = store.engine
        metadata.create_all(self.engine)
        self.http = http_client
        self.allow_http = allow_http
        self.allowed_hosts = [h.lower() for h in allowed_hosts or []]
        self.clock = clock
        self.max_attempts = max_attempts
        self.timeout_s = timeout_s

    def today(self) -> str:
        return (self.clock() if self.clock else dt.datetime.now(dt.timezone.utc)).date().isoformat()

    # ------------------------------------------------------------------ authz helpers
    def _customer_for(self, actor: Actor, counterparty_id: str | None) -> str:
        if actor.has_role(*MANAGE_ROLES) or actor.is_service:
            if not counterparty_id:
                raise GraphError("GRAPH_SCHEMA_INVALID", "counterparty_id required", path="/counterparty_id")
            return counterparty_id
        if not actor.has_role("customer_buyer") or not actor.cp_scope:
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "subscriptions require customer_buyer with cp_scope", rule_id="AUTHZ")
        if counterparty_id is None:
            if len(actor.cp_scope) != 1:
                raise GraphError("GRAPH_SCHEMA_INVALID", "counterparty_id required for multi-customer scope", path="/counterparty_id")
            return actor.cp_scope[0]
        if counterparty_id not in actor.cp_scope:
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"{counterparty_id} outside cp_scope", rule_id="AUTHZ", path="/counterparty_id")
        return counterparty_id

    def _counterparty(self, org_id: str, cp_id: str) -> dict[str, Any]:
        cp = self.store.get(cp_id, org_id=org_id) if cp_id.startswith("cp_") else None
        if cp is None or cp.get("role") != "customer":
            raise GraphError("GRAPH_NOT_FOUND", f"customer {cp_id} not found", path="/counterparty_id")
        return cp

    def _check_url(self, url: str) -> None:
        u = urlparse(url)
        if u.scheme not in ("https",) + (("http",) if self.allow_http else ()) or not u.hostname:
            raise GraphError("GRAPH_SCHEMA_INVALID", "webhook_url must be an https URL", path="/webhook_url")
        if self.allowed_hosts and not any(u.hostname == h or u.hostname.endswith("." + h) for h in self.allowed_hosts):
            raise GraphError("GRAPH_SCHEMA_INVALID", f"webhook host {u.hostname} not allowed", path="/webhook_url")

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        return {k: row[k] for k in ("id", "org_id", "counterparty_id", "datapoint_ids", "period", "webhook_url", "status",
                                    "created_by", "created_at", "updated_at", "note")} | {"subscription_id": row["id"]}

    # ------------------------------------------------------------------ CRUD
    def subscribe(self, actor: Actor, *, datapoint_ids: list[str], webhook_url: str, counterparty_id: str | None = None,
                  period: dict[str, Any] | None = None, secret: str | None = None) -> dict[str, Any]:
        if not datapoint_ids:
            raise GraphError("GRAPH_SCHEMA_INVALID", "datapoint_ids required", path="/datapoint_ids")
        cp_id = self._customer_for(actor, counterparty_id)
        cp = self._counterparty(actor.org_id, cp_id)
        ok, why = grant_allows(cp.get("data_sharing_grant"), datapoint_ids, today=self.today())
        if not ok:
            self.store.record_audit(actor, "authz.deny", object_ref=f"{cp['id']}#v{cp['version']}",
                                    details={"event": "a2a.subscribe", "reason": why})
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"data sharing grant does not allow this subscription ({why})",
                             rule_id="AUTHZ", details={"reason": why})
        self._check_url(webhook_url)
        if secret is not None and len(secret) < 16:
            raise GraphError("GRAPH_SCHEMA_INVALID", "secret must be at least 16 characters", path="/secret")
        generated = secret is None
        secret = secret or secrets.token_urlsafe(32)
        sid = "a2s_" + str(ULID())
        now = _now_iso()
        row = {"id": sid, "org_id": actor.org_id, "counterparty_id": cp_id,
               "datapoint_ids": sorted({d.split("@", 1)[0] for d in datapoint_ids}), "period": period, "webhook_url": webhook_url,
               "secret": secret, "status": "active", "created_by": actor.id, "created_at": now, "updated_at": now, "note": None}
        with self.engine.begin() as c:
            c.execute(insert(a2a_subscription).values(**row))
        self.store.record_audit(actor, "authz.change", object_ref=f"{cp['id']}#v{cp['version']}",
                                details={"event": "a2a.subscribe", "subscription_id": sid, "datapoints": row["datapoint_ids"]})
        out = self._view(row)
        if generated:
            out["secret"] = secret  # 只在创建时返回一次
        return out

    def _rows(self, org_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(select(a2a_subscription).where(a2a_subscription.c.org_id == org_id))]

    def list(self, actor: Actor) -> list[dict[str, Any]]:
        rows = self._rows(actor.org_id)
        if not (actor.has_role(*MANAGE_ROLES) or actor.is_service):
            scope = set(actor.cp_scope or ())
            rows = [r for r in rows if r["counterparty_id"] in scope]
        return [self._view(r) for r in sorted(rows, key=lambda r: r["created_at"])]

    def get(self, actor: Actor, sid: str) -> dict[str, Any]:
        for r in self.list(actor):
            if r["id"] == sid:
                return r
        raise GraphError("GRAPH_NOT_FOUND", f"subscription {sid} not found")

    def cancel(self, actor: Actor, sid: str) -> dict[str, Any]:
        self.get(actor, sid)
        with self.engine.begin() as c:
            c.execute(update(a2a_subscription).where(a2a_subscription.c.id == sid).values(status="cancelled", updated_at=_now_iso()))
        self.store.record_audit(actor, "authz.change", details={"event": "a2a.unsubscribe", "subscription_id": sid})
        return self.get(actor, sid)

    def deliveries(self, sid: str) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(select(a2a_delivery).where(a2a_delivery.c.subscription_id == sid)
                                                         .order_by(a2a_delivery.c.created_at))]

    # ------------------------------------------------------------------ dispatch
    def _customer_reader(self, org_id: str, cp_id: str) -> Actor:
        return Actor.of("service:graph-api", org_id, cp_scope=(cp_id,), approved_only=True)

    def payload(self, sub: dict[str, Any], a: dict[str, Any], delivery_id: str) -> dict[str, Any]:
        return {"event": "answer.approved", "subscription_id": sub["id"], "delivery_id": delivery_id,
                "answer_ref": f"{a['id']}#v{a['version']}", "datapoint_id": a["datapoint_id"], "value": a.get("value"),
                "value_type": a.get("value_type"), "unit": a.get("unit"), "period": a.get("period"), "boundary": a.get("boundary"),
                "is_estimate": a.get("is_estimate", False), "status": a.get("status"), "approval_ref": a.get("approval_ref")}

    def _post(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        if self.http is not None:
            return int(self.http.post(url, content=body, headers=headers, timeout=self.timeout_s).status_code)
        import httpx

        with httpx.Client(timeout=self.timeout_s) as c:
            return int(c.post(url, content=body, headers=headers).status_code)

    def dispatch(self, org_id: str, *, subscription_id: str | None = None) -> dict[str, Any]:
        """推送全部未送达的已批准答案版本；返回 ``{delivered, failed, skipped, revoked}``。"""
        from ..mcp.service import APPROVED

        egress = Actor.of("agent:egress-gateway", org_id)
        summary = {"delivered": 0, "failed": 0, "skipped": 0, "revoked": []}
        today = self.today()
        for sub in self._rows(org_id):
            if sub["status"] != "active" or (subscription_id and sub["id"] != subscription_id):
                continue
            cp = self.store.get(sub["counterparty_id"], org_id=org_id) or {}
            ok, why = grant_allows(cp.get("data_sharing_grant"), sub["datapoint_ids"], today=today)
            if not ok:  # 撤回/过期即时生效：停止推送（04 §6.3）
                with self.engine.begin() as c:
                    c.execute(update(a2a_subscription).where(a2a_subscription.c.id == sub["id"])
                              .values(status="suspended", note=why, updated_at=_now_iso()))
                self.store.record_audit(egress, "egress.block", details={"event": "a2a.push", "subscription_id": sub["id"],
                                                                         "reason": why})
                summary["revoked"].append(sub["id"])
                continue
            done = {d["answer_ref"]: d for d in self.deliveries(sub["id"])}
            wanted = set(sub["datapoint_ids"])
            reader = self._customer_reader(org_id, sub["counterparty_id"])
            answers = [a for a in self.store.query("Answer", actor=reader) if a.get("status") in APPROVED
                       and a["datapoint_id"].split("@", 1)[0] in wanted]
            if sub.get("period"):
                p = sub["period"]
                answers = [a for a in answers if a.get("period") and a["period"]["start"] <= p.get("end", "9999")
                           and a["period"]["end"] >= p.get("start", "")]
            for a in sorted(answers, key=lambda x: (x.get("created_at", ""), x["id"])):
                aref = f"{a['id']}#v{a['version']}"
                prev = done.get(aref)
                if prev and (prev["status"] == "delivered" or prev["attempts"] >= self.max_attempts):
                    summary["skipped"] += 1
                    continue
                did = prev["id"] if prev else "a2d_" + str(ULID())
                body = json.dumps(self.payload(sub, a, did), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                ts = int(time.time())
                headers = {"Content-Type": "application/json", SIGNATURE_HEADER: sign(sub["secret"], body, ts),
                           TIMESTAMP_HEADER: str(ts), "X-Belegt-Subscription": sub["id"], "X-Belegt-Delivery": did}
                status, code, err = "failed", None, None
                try:
                    code = self._post(sub["webhook_url"], body, headers)
                    status = "delivered" if 200 <= code < 300 else "failed"
                    err = None if status == "delivered" else f"http {code}"
                except Exception as e:  # noqa: BLE001 - 网络错误：下次重试
                    err = f"{type(e).__name__}: {e}"[:500]
                now = _now_iso()
                with self.engine.begin() as c:
                    if prev:
                        c.execute(update(a2a_delivery).where(a2a_delivery.c.id == did).values(
                            status=status, attempts=prev["attempts"] + 1, http_status=code, error=err, updated_at=now))
                    else:
                        c.execute(insert(a2a_delivery).values(id=did, subscription_id=sub["id"], answer_ref=aref, status=status,
                                                              attempts=1, http_status=code, error=err, created_at=now,
                                                              updated_at=now))
                if status == "delivered":
                    self.store.record_audit(egress, "egress.send", object_ref=aref, details={
                        "event": "a2a.push", "subscription_id": sub["id"], "counterparty_id": sub["counterparty_id"],
                        "delivery_id": did, "payload_hash": "sha256:" + hashlib.sha256(body).hexdigest()})
                    summary["delivered"] += 1
                else:
                    summary["failed"] += 1
        return summary


__all__ = ["SubscriptionService", "sign", "verify_signature", "grant_allows", "SIGNATURE_HEADER", "TIMESTAMP_HEADER"]
