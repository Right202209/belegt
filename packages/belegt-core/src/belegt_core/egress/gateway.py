# Spec: 04 §2.15（egress-gateway：唯一出站通道、规则、错误码表、retry_after、不排队重试）、§3.4（外发幂等：sreq_…#vN 只发一次）、
#       §5.1 GP-07、07 §3.2 D7（收件人/模板白名单、频率、DLP）、08 §4.3（UPC-LIM-01…07）、§4.4（静默期）、06 §4.4（首次外发审批）
"""``CoreEgressGateway``：证据图感知的出口网关，与 ``belegt_supplier.EgressGateway`` 协议兼容
（``send(msg, now) -> EgressReceipt``；``msg`` 可以是本模块的 ``EgressMessage``，也可以是 ``belegt_supplier.OutboundMessage``）。

检查顺序（任一失败即拒绝，写 ``AuditEvent action=egress.block``，含错误码与规则 ID；放行写 ``egress.send``）：
1. ``EGRESS_DENIED_HALTED``：guard 冻结外发（``policy.halt``）或所属 Request 处于 ``halted``；
2. ``EGRESS_DENIED_TEMPLATE_UNREGISTERED`` / ``_UNREVIEWED``：模板 ID + 模板哈希已注册且 ``reviewed``（GP-07）；
3. ``EGRESS_DENIED_RECIPIENT_NOT_ALLOWED``：收件域在图中 ``Counterparty.domains`` 且地址在 ``contacts`` 中经人工确认
   （UPC-LIM-06、D7）；被暂停/退信的地址（UPC-LIM-07）；
4. ``EGRESS_DENIED_APPROVAL_MISSING``：首次外发需图中已批准的 ``Approval(subject_ref=sreq_…)``；``restricted`` 载荷需已批准
   ``apr_`` 引用（06 §4.4）；
5. ``EGRESS_DENIED_DLP``：``confidential``/``restricted`` 数据点的**值**出现在未审批载荷中，或正文链接域名不在白名单；
6. ``EGRESS_DENIED_RATE_LIMITED``：UPC-LIM-01（每 SupplierRequest 1+N 封，``escalate=True``）/ -02（同一联系人 72 h）/
   -03（同一供应商 7 天 3 封）/ -05（租户日上限），附 ``retry_after``（UPC-LIM-01 除外）；
7. ``EGRESS_DENIED_QUIET_PERIOD``：收件方时区非工作时段/节假日/静默期，附 ``retry_after``。
网关自身不排队重试（避免绕过频率）；``retry_after`` 由调用方工作流的 timer 在该时间后重新提交（不计失败、不更新 last_sent_at）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Protocol
from zoneinfo import ZoneInfo

from ..authz import Actor
from ..store import GraphStore

EGRESS_DENIED_TEMPLATE_UNREGISTERED = "EGRESS_DENIED_TEMPLATE_UNREGISTERED"
EGRESS_DENIED_TEMPLATE_UNREVIEWED = "EGRESS_DENIED_TEMPLATE_UNREVIEWED"
EGRESS_DENIED_RECIPIENT_NOT_ALLOWED = "EGRESS_DENIED_RECIPIENT_NOT_ALLOWED"
EGRESS_DENIED_RATE_LIMITED = "EGRESS_DENIED_RATE_LIMITED"
EGRESS_DENIED_QUIET_PERIOD = "EGRESS_DENIED_QUIET_PERIOD"
EGRESS_DENIED_APPROVAL_MISSING = "EGRESS_DENIED_APPROVAL_MISSING"
EGRESS_DENIED_DLP = "EGRESS_DENIED_DLP"
EGRESS_DENIED_HALTED = "EGRESS_DENIED_HALTED"
EGRESS_CODES = (EGRESS_DENIED_TEMPLATE_UNREGISTERED, EGRESS_DENIED_TEMPLATE_UNREVIEWED, EGRESS_DENIED_RECIPIENT_NOT_ALLOWED,
                EGRESS_DENIED_RATE_LIMITED, EGRESS_DENIED_QUIET_PERIOD, EGRESS_DENIED_APPROVAL_MISSING, EGRESS_DENIED_DLP,
                EGRESS_DENIED_HALTED)
# 调用方处理（04 §2.15 表）
CALLER_HANDLING = {
    EGRESS_DENIED_TEMPLATE_UNREGISTERED: "fail_alert",
    EGRESS_DENIED_TEMPLATE_UNREVIEWED: "fail_review_queue",
    EGRESS_DENIED_RECIPIENT_NOT_ALLOWED: "fail_closed_question",
    EGRESS_DENIED_RATE_LIMITED: "defer_retry_after",
    EGRESS_DENIED_QUIET_PERIOD: "defer_retry_after",
    EGRESS_DENIED_APPROVAL_MISSING: "fail_approval_inbox",
    EGRESS_DENIED_DLP: "fail_alert_compliance_owner",
    EGRESS_DENIED_HALTED: "wait_resume",
}

CONTACT_GAP = dt.timedelta(hours=72)    # UPC-LIM-02 / 07 D7
ORG_WINDOW = dt.timedelta(days=7)        # UPC-LIM-03
ORG_MAX = 3
TENANT_DAILY_MAX = 300                   # UPC-LIM-05
DEFAULT_REMINDER_CAP = 3


class EgressDenied(Exception):
    def __init__(self, code: str, rule_id: str, message: str = "", *, retry_after: dt.datetime | None = None,
                 escalate: bool = False, audit_ref: str | None = None) -> None:
        super().__init__(f"{code} [{rule_id}] {message}")
        self.code, self.rule_id, self.message = code, rule_id, message
        self.retry_after, self.escalate, self.audit_ref = retry_after, escalate, audit_ref

    @property
    def handling(self) -> str:
        return CALLER_HANDLING.get(self.code, "fail_alert")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "rule_id": self.rule_id, "message": self.message, "escalate": self.escalate,
                             "handling": self.handling}
        if self.retry_after:
            d["retry_after"] = self.retry_after.astimezone(dt.timezone.utc).isoformat()
        return d


@dataclass
class PayloadItem:
    datapoint_id: str
    sensitivity: str = "customer_shared"   # public|customer_shared|internal|confidential|restricted
    value_included: bool = False


@dataclass
class EgressMessage:
    org_id: str
    recipient: str
    template_id: str
    template_hash: str
    subject: str = ""
    body_text: str = ""
    links: list[str] = field(default_factory=list)
    channel: Literal["email", "supplier_portal", "portal_http", "a2a", "share_link"] = "email"
    subject_ref: str | None = None          # 去重键（sreq_…#vN / apr_…）
    sreq_id: str | None = None
    request_id: str | None = None
    counterparty_id: str | None = None
    first_send: bool = False
    kind: Literal["first", "reminder"] = "first"
    internal: bool = False
    sent_count: int = 0
    reminder_cap: int = DEFAULT_REMINDER_CAP
    payload: list[PayloadItem] = field(default_factory=list)
    restricted_approval_ref: str | None = None
    timezone: str | None = None


@dataclass
class EgressReceipt:
    audit_ref: str
    message_id: str
    sent_at: dt.datetime
    deduplicated: bool = False


class TemplateRegistryLike(Protocol):
    def is_registered(self, template_id: str, template_hash: str | None = None) -> bool: ...
    def effective_status(self, template_id: str) -> str: ...


class StaticTemplateRegistry:
    """最小模板登记表：``{template_id: (template_hash, status)}``（belegt_supplier 未安装或测试时用）。"""

    def __init__(self, templates: dict[str, tuple[str, str]] | None = None) -> None:
        self.templates = dict(templates or {})

    def add(self, template_id: str, template_hash: str, status: str = "reviewed") -> None:
        self.templates[template_id] = (template_hash, status)

    def is_registered(self, template_id: str, template_hash: str | None = None) -> bool:
        t = self.templates.get(template_id)
        return t is not None and (template_hash is None or t[0] == template_hash)

    def effective_status(self, template_id: str) -> str:
        return self.templates.get(template_id, ("", "unregistered"))[1]


class Transport(Protocol):
    def send(self, msg: EgressMessage) -> str: ...


class MemoryTransport:
    def __init__(self) -> None:
        self.sent: list[EgressMessage] = []

    def send(self, msg: EgressMessage) -> str:
        self.sent.append(msg)
        return f"<{hashlib.sha256((msg.recipient + msg.body_text + str(len(self.sent))).encode()).hexdigest()[:16]}@belegt.local>"


class Ledger:
    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self.suppressed: set[str] = set()
        self.dedupe: dict[str, str] = {}
        self._lock = threading.Lock()

    def record(self, *, org_id: str, counterparty_id: str | None, recipient: str, subject_ref: str | None, at: dt.datetime,
               message_id: str) -> None:
        with self._lock:
            self.sends.append({"org": org_id, "cp": counterparty_id, "to": recipient.lower(), "at": at})
            if subject_ref:
                self.dedupe[subject_ref] = message_id

    def last_to(self, recipient: str) -> dt.datetime | None:
        ts = [s["at"] for s in self.sends if s["to"] == recipient.lower()]
        return max(ts) if ts else None

    def cp_since(self, cp: str, since: dt.datetime) -> list[dt.datetime]:
        return sorted(s["at"] for s in self.sends if s["cp"] == cp and s["at"] >= since)

    def tenant_since(self, org: str, since: dt.datetime) -> int:
        return sum(1 for s in self.sends if s["org"] == org and s["at"] >= since)


class BusinessCalendar:
    """收件方时区工作时段（周一至周五 08:00–18:00）+ 节假日/静默期（08 §4.4 的简化实现；
    ``belegt_supplier.BusinessCalendar`` 可直接替换，接口 ``in_send_window`` / ``next_send_window`` 相同）。"""

    def __init__(self, tz: str = "Europe/Berlin", start_hour: int = 8, end_hour: int = 18,
                 holidays: Iterable[dt.date] = (), quiet_ranges: Iterable[tuple[dt.date, dt.date]] = ()) -> None:
        self.tz = ZoneInfo(tz)
        self.start_hour, self.end_hour = start_hour, end_hour
        self.holidays = set(holidays)
        self.quiet = list(quiet_ranges)

    def _blocked_day(self, d: dt.date) -> bool:
        return d.weekday() >= 5 or d in self.holidays or any(a <= d <= b for a, b in self.quiet)

    def in_send_window(self, now: dt.datetime, kind: str = "first") -> bool:
        loc = now.astimezone(self.tz)
        return not self._blocked_day(loc.date()) and self.start_hour <= loc.hour < self.end_hour

    def next_send_window(self, now: dt.datetime, kind: str = "first") -> dt.datetime:
        loc = now.astimezone(self.tz)
        cand = loc
        for _ in range(400):
            if not self._blocked_day(cand.date()):
                if cand.hour < self.start_hour:
                    cand = cand.replace(hour=self.start_hour, minute=0, second=0, microsecond=0)
                if self.start_hour <= cand.hour < self.end_hour:
                    return cand.astimezone(dt.timezone.utc)
            cand = (cand + dt.timedelta(days=1)).replace(hour=self.start_hour, minute=0, second=0, microsecond=0)
        return cand.astimezone(dt.timezone.utc)  # pragma: no cover


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower().strip() if "@" in addr else ""


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].split(":")[0].lower()


def _matches(host: str, domains: Iterable[str]) -> bool:
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def recipient_hash(addr: str) -> str:
    return "sha256:" + hashlib.sha256(addr.lower().encode("utf-8")).hexdigest()


class CoreEgressGateway:
    actor_id = "agent:egress-gateway"

    def __init__(self, store: GraphStore, templates: TemplateRegistryLike, transport: Transport | None = None, *,
                 guard: Any = None, ledger: Ledger | None = None, calendar: Any = None,
                 allowed_link_domains: Iterable[str] = (), internal_domains: Iterable[str] = ()) -> None:
        self.store = store
        self.templates = templates
        self.transport = transport or MemoryTransport()
        self.guard = guard
        self.ledger = ledger or Ledger()
        self.calendar = calendar or BusinessCalendar()
        self.allowed_link_domains = list(allowed_link_domains)
        self.internal_domains = list(internal_domains)
        self.halted = False

    # ------------------------------------------------------------------ adapt belegt_supplier.OutboundMessage
    @staticmethod
    def adapt(msg: Any) -> EgressMessage:
        if isinstance(msg, EgressMessage):
            return msg
        r = msg.rendered
        cp = getattr(msg, "counterparty", None)
        appr = getattr(msg, "approval", None)
        return EgressMessage(
            org_id=msg.org_id, recipient=msg.recipient, template_id=r.template_id, template_hash=r.template_hash,
            subject=r.subject, body_text=r.body_text, links=list(r.links), channel="email",
            subject_ref=getattr(appr, "subject_ref", None) if msg.first_send else None, sreq_id=msg.sreq_id,
            counterparty_id=getattr(cp, "counterparty_id", None), first_send=msg.first_send, kind=msg.kind,
            internal=msg.internal, sent_count=msg.sreq_sent_count, reminder_cap=msg.reminder_cap,
            payload=[PayloadItem(p.datapoint_id, p.sensitivity, p.value_included) for p in msg.payload],
            restricted_approval_ref=msg.restricted_approval_ref,
            timezone=getattr(cp, "timezone", None))

    # ------------------------------------------------------------------ audit
    def _actor(self, org_id: str) -> Actor:
        return Actor.of(self.actor_id, org_id)

    def _audit(self, action: str, m: EgressMessage, **details: Any) -> str:
        obj = m.subject_ref or m.sreq_id or m.request_id
        ev = self.store.record_audit(self._actor(m.org_id), action, object_ref=obj, details={
            "channel": m.channel, "template_id": m.template_id, "template_hash": m.template_hash,
            "recipient_hash": recipient_hash(m.recipient), "recipient_domain": _domain(m.recipient),
            "body_sha256": "sha256:" + hashlib.sha256(m.body_text.encode("utf-8")).hexdigest(), **details})
        return ev["id"] if isinstance(ev, dict) else str(ev)

    def _deny(self, m: EgressMessage, code: str, rule_id: str, message: str = "", *, retry_after: dt.datetime | None = None,
              escalate: bool = False) -> EgressDenied:
        extra: dict[str, Any] = {"code": code, "rule_id": rule_id}
        if retry_after:
            extra["retry_after"] = retry_after.astimezone(dt.timezone.utc).isoformat()
        ref = self._audit("egress.block", m, **extra)
        return EgressDenied(code, rule_id, message, retry_after=retry_after, escalate=escalate, audit_ref=ref)

    # ------------------------------------------------------------------ checks
    def check(self, msg: Any, now: dt.datetime) -> EgressMessage:
        m = self.adapt(msg)
        st = self.store
        if self.halted or (self.guard is not None and getattr(self.guard, "egress_halted", False)):
            raise self._deny(m, EGRESS_DENIED_HALTED, "policy.halt", "egress queue frozen")
        if m.request_id:
            h = st.head(m.request_id, org_id=m.org_id)
            if (h and h.status == "halted") or (self.guard is not None and m.request_id in self.guard.halted_requests):
                raise self._deny(m, EGRESS_DENIED_HALTED, "policy.halt", f"request {m.request_id} is halted")
        if not self.templates.is_registered(m.template_id, m.template_hash):
            raise self._deny(m, EGRESS_DENIED_TEMPLATE_UNREGISTERED, "GP-07", "template id/hash not registered")
        if self.templates.effective_status(m.template_id) != "reviewed":
            raise self._deny(m, EGRESS_DENIED_TEMPLATE_UNREVIEWED, "GP-07", f"template {m.template_id} not reviewed")
        dom = _domain(m.recipient)
        if m.internal:
            if not dom or not _matches(dom, self.internal_domains):
                raise self._deny(m, EGRESS_DENIED_RECIPIENT_NOT_ALLOWED, "D7", "internal recipient not in tenant domains")
        else:
            cp = st.get(m.counterparty_id, org_id=m.org_id) if m.counterparty_id else None
            confirmed = {str(c.get("email", "")).lower() for c in (cp or {}).get("contacts") or []
                         if c.get("confirmed", True) and c.get("email")}
            if cp is None or not dom or not _matches(dom, cp.get("domains") or []) or m.recipient.lower() not in confirmed:
                raise self._deny(m, EGRESS_DENIED_RECIPIENT_NOT_ALLOWED, "UPC-LIM-06",
                                 "recipient domain not in Counterparty.domains or address not confirmed")
            if m.recipient.lower() in self.ledger.suppressed:
                raise self._deny(m, EGRESS_DENIED_RECIPIENT_NOT_ALLOWED, "UPC-LIM-07", "address suppressed (bounce/pause)")
        if m.first_send:
            sid = (m.subject_ref or m.sreq_id or "").split("#", 1)[0]
            ok = bool(sid) and any(a.get("subject_ref", "").split("#", 1)[0] == sid and a.get("status") == "approved"
                                   and a.get("decision") == "approved" for a in st.query("Approval", org_id=m.org_id))
            if not ok:
                raise self._deny(m, EGRESS_DENIED_APPROVAL_MISSING, "GP-07",
                                 "first send requires an approved Approval(subject_ref=sreq_…)")
        approved_ref = None
        if m.restricted_approval_ref:
            a = st.get(m.restricted_approval_ref, org_id=m.org_id)
            approved_ref = m.restricted_approval_ref if a and a.get("status") in ("approved",) else None
        if any(p.sensitivity == "restricted" for p in m.payload) and not approved_ref:
            raise self._deny(m, EGRESS_DENIED_APPROVAL_MISSING, "06 §4.4", "restricted payload requires an approved apr_ reference")
        leaked = [p.datapoint_id for p in m.payload if p.value_included and p.sensitivity in ("confidential", "restricted")]
        if leaked and not approved_ref:
            raise self._deny(m, EGRESS_DENIED_DLP, "D7", f"confidential/restricted values in unapproved payload: {leaked}")
        for link in m.links:
            if not _matches(_host(link), self.allowed_link_domains):
                raise self._deny(m, EGRESS_DENIED_DLP, "08 §3.2 lint", f"link domain not allowed: {_host(link)}")
        if self.guard is not None and getattr(self.guard, "config", None) is not None:
            from ..guard.injection import contains_canary

            if contains_canary(m.body_text + m.subject, self.guard.config.canaries):
                raise self._deny(m, EGRESS_DENIED_DLP, "LLM07", "canary string in outbound payload")
        if m.internal:
            return m
        if m.sent_count >= 1 + m.reminder_cap:
            raise self._deny(m, EGRESS_DENIED_RATE_LIMITED, "UPC-LIM-01", f"per-request cap 1+{m.reminder_cap} reached",
                             escalate=True)
        cal = self.calendar if m.timezone is None or not isinstance(self.calendar, BusinessCalendar) else \
            BusinessCalendar(m.timezone, self.calendar.start_hour, self.calendar.end_hour, self.calendar.holidays, self.calendar.quiet)
        if not cal.in_send_window(now, m.kind):
            raise self._deny(m, EGRESS_DENIED_QUIET_PERIOD, "UPC-LIM-04", "outside recipient business hours / quiet period",
                             retry_after=cal.next_send_window(now, m.kind))
        last = self.ledger.last_to(m.recipient)
        if last is not None and now - last < CONTACT_GAP:
            raise self._deny(m, EGRESS_DENIED_RATE_LIMITED, "UPC-LIM-02", "same contact within 72 h",
                             retry_after=cal.next_send_window(last + CONTACT_GAP, m.kind))
        if m.counterparty_id:
            recent = self.ledger.cp_since(m.counterparty_id, now - ORG_WINDOW)
            if len(recent) >= ORG_MAX:
                raise self._deny(m, EGRESS_DENIED_RATE_LIMITED, "UPC-LIM-03", "supplier org: 3 messages in 7 days",
                                 retry_after=cal.next_send_window(recent[-ORG_MAX] + ORG_WINDOW, m.kind))
        day0 = now.astimezone(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if self.ledger.tenant_since(m.org_id, day0) >= TENANT_DAILY_MAX:
            raise self._deny(m, EGRESS_DENIED_RATE_LIMITED, "UPC-LIM-05", "tenant daily limit",
                             retry_after=cal.next_send_window(day0 + dt.timedelta(days=1), m.kind))
        return m

    # ------------------------------------------------------------------ send（EgressGateway 协议）
    def send(self, msg: Any, now: dt.datetime | None = None) -> EgressReceipt:
        now = now or dt.datetime.strptime(self.store.peek_now(), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt.timezone.utc)
        m = self.adapt(msg)
        if m.subject_ref and m.subject_ref in self.ledger.dedupe:  # 04 §3.4：同一版本只发送一次
            return EgressReceipt(audit_ref="", message_id=self.ledger.dedupe[m.subject_ref], sent_at=now, deduplicated=True)
        m = self.check(m, now)
        mid = self.transport.send(m) if not hasattr(self.transport, "send_email") else self.transport.send_email(m)
        if not m.internal:
            self.ledger.record(org_id=m.org_id, counterparty_id=m.counterparty_id, recipient=m.recipient,
                               subject_ref=m.subject_ref, at=now, message_id=mid)
        ref = self._audit("egress.send", m, message_id=mid)
        return EgressReceipt(audit_ref=ref, message_id=mid, sent_at=now)

    # ------------------------------------------------------------------ SupplierRequest 外发（upstream-chaser → egress）
    def send_supplier_request(self, sreq_id: str, org_id: str, *, recipient: str, rendered: Any,
                              request_id: str | None = None, now: dt.datetime | None = None,
                              payload: list[PayloadItem] | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        """渲染后的 SupplierRequest 首发/催办：外发成功后由 ``agent:egress-gateway`` 写 SupplierRequest 新版本
        （``draft→sent`` 或 ``sent/reminded→reminded``，``last_sent_at``、``reminder_count``）。"""
        sreq = self.store.get(sreq_id, org_id=org_id)
        if sreq is None:
            raise KeyError(sreq_id)
        first = sreq["status"] == "draft"
        sent_count = 0 if first else 1 + int(sreq.get("reminder_count", 0))
        m = EgressMessage(org_id=org_id, recipient=recipient, template_id=rendered.template_id,
                          template_hash=rendered.template_hash, subject=rendered.subject, body_text=rendered.body_text,
                          links=list(rendered.links), subject_ref=f"{sreq_id}#v{sreq['version']}", sreq_id=sreq_id,
                          request_id=request_id, counterparty_id=sreq["counterparty_id"], first_send=first,
                          kind="first" if first else "reminder", sent_count=sent_count, payload=list(payload or []))
        rc = self.send(m, now)
        if rc.deduplicated:
            return {"receipt": rc, "sreq_ref": None}
        ts = rc.sent_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        patch: dict[str, Any] = {"id": sreq_id, "last_sent_at": ts}
        if first:
            patch["status"] = "sent"
        else:
            patch["status"] = "reminded"
            patch["reminder_count"] = int(sreq.get("reminder_count", 0)) + 1
        ref = self.store.write("SupplierRequest", patch, expected_version=sreq["version"], actor=self._actor(org_id),
                               patch_mode="merge_from_head", idempotency_key=idempotency_key)
        return {"receipt": rc, "sreq_ref": ref}
