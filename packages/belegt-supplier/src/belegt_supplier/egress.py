# Spec: 04 §2.15（egress-gateway 规则与错误码 EGRESS_DENIED_*）、08 §4.3（UPC-LIM-01…07）、§4.4、§3.2 模板 lint、07 D7
"""出口网关：供应商端/追数侧唯一的外发通道。

- `EgressGateway` 协议 + `DefaultEgressGateway` 实现；网关自身**不排队重试**，
  限频/静默期拒绝附 `retry_after` 由调用方工作流延后（04 §2.15）。
- 每次放行/拒绝写 AuditEvent（`egress.send` / `egress.block`，含错误码与规则 ID）。
- 传输层：`OutboxTransport`（开发用，写 .eml 文件）与 `SmtpTransport`（标准库 smtplib）。
"""
from __future__ import annotations

import hashlib
import re
import smtplib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Callable, Literal, Protocol

from .ids import new_id
from .messages import MAX_RENDERED_CHARS, RenderedMessage, TemplateRegistry
from .models import ApprovalInfo, CounterpartyInfo
from .workcal import BusinessCalendar

# 04 §2.15 全系统唯一一套
EGRESS_DENIED_TEMPLATE_UNREGISTERED = "EGRESS_DENIED_TEMPLATE_UNREGISTERED"
EGRESS_DENIED_TEMPLATE_UNREVIEWED = "EGRESS_DENIED_TEMPLATE_UNREVIEWED"
EGRESS_DENIED_RECIPIENT_NOT_ALLOWED = "EGRESS_DENIED_RECIPIENT_NOT_ALLOWED"
EGRESS_DENIED_RATE_LIMITED = "EGRESS_DENIED_RATE_LIMITED"
EGRESS_DENIED_QUIET_PERIOD = "EGRESS_DENIED_QUIET_PERIOD"
EGRESS_DENIED_APPROVAL_MISSING = "EGRESS_DENIED_APPROVAL_MISSING"
EGRESS_DENIED_DLP = "EGRESS_DENIED_DLP"
EGRESS_DENIED_HALTED = "EGRESS_DENIED_HALTED"
ALL_CODES = (EGRESS_DENIED_TEMPLATE_UNREGISTERED, EGRESS_DENIED_TEMPLATE_UNREVIEWED, EGRESS_DENIED_RECIPIENT_NOT_ALLOWED,
             EGRESS_DENIED_RATE_LIMITED, EGRESS_DENIED_QUIET_PERIOD, EGRESS_DENIED_APPROVAL_MISSING, EGRESS_DENIED_DLP,
             EGRESS_DENIED_HALTED)

CONTACT_GAP = timedelta(hours=72)        # UPC-LIM-02
ORG_WINDOW = timedelta(days=7)           # UPC-LIM-03
ORG_MAX = 3
TENANT_DAILY_MAX = 300                   # UPC-LIM-05
TENANT_FIRST_WEEK_DAILY_MAX = 50


class EgressDenied(Exception):
    def __init__(self, code: str, rule_id: str, message: str = "", *, retry_after: datetime | None = None,
                 escalate: bool = False, audit_ref: str | None = None) -> None:
        super().__init__(f"{code} [{rule_id}] {message}")
        self.code = code
        self.rule_id = rule_id
        self.message = message
        self.retry_after = retry_after
        self.escalate = escalate
        self.audit_ref = audit_ref

    def to_dict(self) -> dict:
        d = {"code": self.code, "rule_id": self.rule_id, "message": self.message, "escalate": self.escalate}
        if self.retry_after:
            d["retry_after"] = self.retry_after.astimezone(timezone.utc).isoformat()
        return d


@dataclass
class PayloadDatapoint:
    datapoint_id: str
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    value_included: bool = False  # 载荷中是否包含该数据点的**值**（请求邮件通常只含名称）


@dataclass
class OutboundMessage:
    org_id: str
    sreq_id: str | None
    recipient: str
    rendered: RenderedMessage
    counterparty: CounterpartyInfo | None = None
    first_send: bool = False
    approval: ApprovalInfo | None = None
    kind: Literal["first", "reminder"] = "reminder"
    internal: bool = False
    sreq_sent_count: int = 0           # 本 SupplierRequest 已外发封数（含首次）
    reminder_cap: int = 3              # 1+cap
    calendar: BusinessCalendar | None = None
    payload: list[PayloadDatapoint] = field(default_factory=list)
    restricted_approval_ref: str | None = None
    from_addr: str | None = None
    reply_to: str | None = None
    pause_link: str | None = None


@dataclass
class EgressReceipt:
    audit_ref: str
    message_id: str
    sent_at: datetime


class Transport(Protocol):
    def send(self, msg: EmailMessage) -> None: ...


class EgressLedger(Protocol):
    def record(self, *, org_id: str, counterparty_id: str | None, recipient: str, sreq_id: str | None, at: datetime) -> None: ...
    def last_to_recipient(self, recipient: str) -> datetime | None: ...
    def counterparty_sends_since(self, counterparty_id: str, since: datetime) -> list[datetime]: ...
    def tenant_sends_since(self, org_id: str, since: datetime) -> int: ...
    def tenant_first_send(self, org_id: str) -> datetime | None: ...
    def is_suppressed(self, recipient: str) -> bool: ...
    def suppress(self, recipient: str, reason: str) -> None: ...


class InMemoryLedger:
    def __init__(self) -> None:
        self.sends: list[dict] = []
        self.suppressed: dict[str, str] = {}
        self._lock = threading.Lock()

    def record(self, *, org_id, counterparty_id, recipient, sreq_id, at):
        with self._lock:
            self.sends.append({"org_id": org_id, "cp": counterparty_id, "to": recipient.lower(), "sreq": sreq_id, "at": at})

    def last_to_recipient(self, recipient):
        ts = [s["at"] for s in self.sends if s["to"] == recipient.lower()]
        return max(ts) if ts else None

    def counterparty_sends_since(self, counterparty_id, since):
        return sorted(s["at"] for s in self.sends if s["cp"] == counterparty_id and s["at"] >= since)

    def tenant_sends_since(self, org_id, since):
        return sum(1 for s in self.sends if s["org_id"] == org_id and s["at"] >= since)

    def tenant_first_send(self, org_id):
        ts = [s["at"] for s in self.sends if s["org_id"] == org_id]
        return min(ts) if ts else None

    def is_suppressed(self, recipient):
        return recipient.lower() in self.suppressed

    def suppress(self, recipient, reason):
        self.suppressed[recipient.lower()] = reason


AuditSink = Callable[[dict], str]


class EgressGateway(Protocol):
    def send(self, msg: OutboundMessage, now: datetime) -> EgressReceipt: ...


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower().strip() if "@" in addr else ""


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].split(":")[0].lower()


def _matches(host: str, domains: list[str] | set[str]) -> bool:
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def _hash_addr(addr: str) -> str:
    return "sha256:" + hashlib.sha256(addr.lower().encode("utf-8")).hexdigest()


class DefaultEgressGateway:
    """确定性网关。`allowed_link_domains`：渲染正文中允许出现的链接域名（门户域名 + 租户隐私页域名）。"""

    def __init__(self, registry: TemplateRegistry, transport: Transport, *, ledger: EgressLedger | None = None,
                 audit: AuditSink | None = None, allowed_link_domains: list[str] | None = None,
                 internal_domains: list[str] | None = None, default_from: str = "no-reply@belegt.local",
                 halted: bool = False) -> None:
        self.registry = registry
        self.transport = transport
        self.ledger = ledger or InMemoryLedger()
        self.audit_events: list[dict] = []
        self._audit = audit
        self.allowed_link_domains = list(allowed_link_domains or [])
        self.internal_domains = list(internal_domains or [])
        self.default_from = default_from
        self.halted = halted

    # ------------------------------------------------------------ audit
    def _write_audit(self, action: str, msg: OutboundMessage, now: datetime, **extra) -> str:
        ev = {
            "id": new_id("aud"), "action": action, "at": now.astimezone(timezone.utc).isoformat(),
            "org_id": msg.org_id, "sreq_id": msg.sreq_id, "template_id": msg.rendered.template_id,
            "template_hash": msg.rendered.template_hash, "variant": msg.rendered.variant,
            "body_sha256": msg.rendered.body_sha256, "variables_sha256": msg.rendered.variables_sha256,
            "recipient_hash": _hash_addr(msg.recipient), "recipient_domain": _domain(msg.recipient),
            "actor": "service:egress-gateway", **extra,
        }
        self.audit_events.append(ev)
        if self._audit:
            return self._audit(ev)
        return ev["id"]

    def _deny(self, msg: OutboundMessage, now: datetime, code: str, rule_id: str, message: str = "", *,
              retry_after: datetime | None = None, escalate: bool = False) -> EgressDenied:
        extra = {"code": code, "rule_id": rule_id}
        if retry_after:
            extra["retry_after"] = retry_after.astimezone(timezone.utc).isoformat()
        ref = self._write_audit("egress.block", msg, now, **extra)
        return EgressDenied(code, rule_id, message, retry_after=retry_after, escalate=escalate, audit_ref=ref)

    # ------------------------------------------------------------ checks
    def check(self, msg: OutboundMessage, now: datetime) -> None:
        """全部规则；违反抛 EgressDenied（已写 egress.block）。"""
        r = msg.rendered
        if self.halted:
            raise self._deny(msg, now, EGRESS_DENIED_HALTED, "policy.halt", "egress halted")
        if not self.registry.is_registered(r.template_id, r.template_hash):
            raise self._deny(msg, now, EGRESS_DENIED_TEMPLATE_UNREGISTERED, "GP-07", "template id/hash not registered")
        if self.registry.effective_status(r.template_id) != "reviewed":
            raise self._deny(msg, now, EGRESS_DENIED_TEMPLATE_UNREVIEWED, "GP-07", f"template {r.template_id} not reviewed")

        dom = _domain(msg.recipient)
        if msg.internal:
            if not dom or not _matches(dom, self.internal_domains):
                raise self._deny(msg, now, EGRESS_DENIED_RECIPIENT_NOT_ALLOWED, "D7", "internal recipient not in tenant domains")
        else:
            cp = msg.counterparty
            confirmed = {a.lower() for a in (cp.confirmed_addresses if cp else [])}
            if cp is None or not dom or not _matches(dom, cp.domains) or msg.recipient.lower() not in confirmed:
                raise self._deny(msg, now, EGRESS_DENIED_RECIPIENT_NOT_ALLOWED, "UPC-LIM-06",
                                 "recipient domain not in Counterparty.domains or address not confirmed")
            if self.ledger.is_suppressed(msg.recipient):
                raise self._deny(msg, now, EGRESS_DENIED_RECIPIENT_NOT_ALLOWED, "UPC-LIM-07", "address suppressed (bounce/pause)")

        if msg.first_send:
            a = msg.approval
            if a is None or a.status != "approved" or a.decision != "approved" or \
                    (msg.sreq_id and a.subject_ref.split("#", 1)[0] != msg.sreq_id):
                raise self._deny(msg, now, EGRESS_DENIED_APPROVAL_MISSING, "GP-07", "first send requires approved Approval(subject_ref=sreq_…)")

        restricted = [p for p in msg.payload if p.sensitivity == "restricted"]
        if restricted and not msg.restricted_approval_ref:
            raise self._deny(msg, now, EGRESS_DENIED_APPROVAL_MISSING, "06 §4.4", "restricted payload requires apr_ reference")
        leaked = [p.datapoint_id for p in msg.payload if p.value_included and p.sensitivity in ("confidential", "restricted")]
        if leaked and not msg.restricted_approval_ref:
            raise self._deny(msg, now, EGRESS_DENIED_DLP, "D7", f"confidential values in payload: {leaked}")
        allowed_links = set(self.allowed_link_domains)
        for link in r.links:
            if not _matches(_host(link), allowed_links):
                raise self._deny(msg, now, EGRESS_DENIED_DLP, "08 §3.2 lint", f"link domain not allowed: {_host(link)}")
        if len(r.body_text) > MAX_RENDERED_CHARS:
            raise self._deny(msg, now, EGRESS_DENIED_DLP, "08 §3.2 lint", "rendered body > 6000 chars")

        if msg.internal:
            return
        if msg.sreq_sent_count >= 1 + msg.reminder_cap:
            raise self._deny(msg, now, EGRESS_DENIED_RATE_LIMITED, "UPC-LIM-01",
                             f"per-request cap 1+{msg.reminder_cap} reached", escalate=True)
        cal = msg.calendar or BusinessCalendar()
        if not cal.in_send_window(now, msg.kind):
            raise self._deny(msg, now, EGRESS_DENIED_QUIET_PERIOD, "UPC-LIM-04", "outside supplier business hours / quiet period",
                             retry_after=cal.next_send_window(now, msg.kind))
        last = self.ledger.last_to_recipient(msg.recipient)
        if last is not None and now - last < CONTACT_GAP:
            raise self._deny(msg, now, EGRESS_DENIED_RATE_LIMITED, "UPC-LIM-02", "same contact within 72 h",
                             retry_after=cal.next_send_window(last + CONTACT_GAP, msg.kind))
        cp_id = msg.counterparty.counterparty_id if msg.counterparty else None
        if cp_id:
            recent = self.ledger.counterparty_sends_since(cp_id, now - ORG_WINDOW)
            if len(recent) >= ORG_MAX:
                raise self._deny(msg, now, EGRESS_DENIED_RATE_LIMITED, "UPC-LIM-03", "supplier org: 3 messages in 7 days",
                                 retry_after=cal.next_send_window(recent[-ORG_MAX] + ORG_WINDOW, msg.kind))
        day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        first = self.ledger.tenant_first_send(msg.org_id)
        limit = TENANT_FIRST_WEEK_DAILY_MAX if (first is None or now - first < timedelta(days=7)) else TENANT_DAILY_MAX
        if self.ledger.tenant_sends_since(msg.org_id, day_start) >= limit:
            raise self._deny(msg, now, EGRESS_DENIED_RATE_LIMITED, "UPC-LIM-05", f"tenant daily limit {limit}",
                             retry_after=cal.next_send_window(day_start + timedelta(days=1), msg.kind))

    # ------------------------------------------------------------ send
    def send(self, msg: OutboundMessage, now: datetime) -> EgressReceipt:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self.check(msg, now)
        em = EmailMessage()
        em["From"] = msg.from_addr or self.default_from
        em["To"] = msg.recipient
        em["Subject"] = msg.rendered.subject
        em["Date"] = formatdate(now.timestamp(), usegmt=True)
        mid = make_msgid(domain=_domain(msg.from_addr or self.default_from) or "belegt.local")
        em["Message-ID"] = mid
        if msg.reply_to:
            em["Reply-To"] = msg.reply_to
        if msg.pause_link:
            em["List-Unsubscribe"] = f"<{msg.pause_link}>"  # UPC-LIM-07 暂停提醒（无需登录）
        em["X-Belegt-Template"] = f"{msg.rendered.template_id}#{msg.rendered.variant}"
        if msg.sreq_id:
            em["X-Belegt-Request"] = msg.sreq_id
        em.set_content(msg.rendered.body_text)
        em.add_alternative(msg.rendered.body_html, subtype="html")
        self.transport.send(em)
        cp_id = msg.counterparty.counterparty_id if msg.counterparty else None
        if not msg.internal:
            self.ledger.record(org_id=msg.org_id, counterparty_id=cp_id, recipient=msg.recipient, sreq_id=msg.sreq_id, at=now)
        ref = self._write_audit("egress.send", msg, now, message_id=mid)
        return EgressReceipt(audit_ref=ref, message_id=mid, sent_at=now)


# ---------------------------------------------------------------- transports
class OutboxTransport:
    """开发用：把每封邮件写成 `<outbox>/<UTC 时间>_<id>.eml`。"""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, msg: EmailMessage) -> None:
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + "_" + new_id("msg") + ".eml"
        (self.dir / name).write_bytes(bytes(msg))

    def messages(self) -> list[Path]:
        return sorted(self.dir.glob("*.eml"))


class MemoryTransport:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


class SmtpTransport:
    """标准库 SMTP 适配（STARTTLS 可选）。"""

    def __init__(self, host: str, port: int = 587, *, username: str | None = None, password: str | None = None,
                 starttls: bool = True, timeout: float = 30.0) -> None:
        self.host, self.port, self.username, self.password = host, port, username, password
        self.starttls, self.timeout = starttls, timeout

    def send(self, msg: EmailMessage) -> None:  # pragma: no cover - 需要真实 SMTP
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
            if self.starttls:
                s.starttls()
            if self.username:
                s.login(self.username, self.password or "")
            s.send_message(msg)
