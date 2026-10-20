# Spec: 08 §2.3（状态机）、§4（催办）、§6.2–§6.6（供应商端功能与 API）、§7（安全）；03 §3.10
"""供应商端业务层（API 与 Web 共用）。

- 门户侧：token 解析（签名 → 库记录 → 吊销/过期/只读 → 限流 → 设备绑定）、草稿、上传、解析、提问、无法提供、提交、补发链接、OTP。
- 追数侧（单机/自托管或被 core 复用）：建草稿、首次发送（需已批准 Approval）、定时催办 tick、Webhook 外发队列。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select

from . import chase as chase_mod
from . import tokens
from .catalog import base_id
from .catalog import resolve as resolve_datapoints
from .config import Settings
from .core_client import CoreClient
from .db import (
    AuditRow, DeclineRow, DeviceRow, DraftRow, EgressSendRow, LinkRow, OtpRow, QuestionRow, RequestHistoryRow,
    RequestRow, SubmissionRow, SuppressionRow, TemplateReviewRow, UploadRow, WebhookRow, aware, make_engine,
    make_sessionmaker, utcnaive,
)
from .decimal_util import normalize_user_number
from .egress import (
    DefaultEgressGateway, EgressDenied, MemoryTransport, OutboundMessage, OutboxTransport, SmtpTransport, Transport,
)
from .i18n import Translator
from .ids import new_id
from .io_bridge import get_cbam_parser
from .localefmt import date_long
from .messages import TemplateError, TemplateRegistry
from .models import (
    ApprovalInfo, Attestation, BlobRef, CounterpartyInfo, DataPointSpec, Period, RequestManifest, SubmissionPackage,
    SubmissionSource, SubmissionValue, SupplierRequest,
)
from .plausibility import CLIENT_SIDE_RULES, PlausContext, PlausValue, run_plausibility, scan_file, strip_controls
from .plausibility.model import GeoOracle
from .ratelimit import (
    MAGIC_LINK_PER_EMAIL_PER_HOUR, OTP_LOCK_SECONDS, OTP_MAX_FAILURES, TOKEN_PER_MINUTE, TOKEN_SUBMITS_TOTAL,
    TOKEN_UPLOADS_PER_DAY, UNKNOWN_TOKEN_PER_IP_PER_MINUTE, RateLimiter,
)
from .signing import SignatureError, canonical_json, jws_sign_detached, jws_verify, sha256_hex
from .statemachine import TransitionError, transition
from .workcal import BusinessCalendar

ATTESTATION_STATEMENT = "belegt-supplier-attestation@1.0.0"
DECLINE_REASONS = ("not_available", "not_responsible", "confidential", "no_longer_supplier", "other")
OTP_TTL = timedelta(minutes=10)
ORIGIN_FREEZE_THRESHOLD = 10
REMINDER_FAMILY = {"cbam_see_request": "cbam_see_reminder"}
OPEN_STATUSES = ("sent", "reminded", "escalated", "responded", "reviewed")


class PortalError(Exception):
    def __init__(self, status: int, code: str, message: str = "", *, retry_after: float | None = None,
                 details: Any = None) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.retry_after, self.details = status, code, message, retry_after, details

    def body(self) -> dict:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            err["details"] = self.details
        if self.retry_after is not None:
            err["retry_after_s"] = round(self.retry_after, 1)
        return {"error": err}


@dataclass
class Access:
    sreq: SupplierRequest
    manifest: RequestManifest
    token: str
    token_hash: str
    email: str
    expires_at: datetime
    readonly: bool
    new_device_cookie: str | None = None


@dataclass
class FieldIssue:
    key: str
    code: str


@dataclass
class ChaseResult:
    sreq_id: str
    action: str
    ok: bool
    detail: dict = field(default_factory=dict)


def field_key(dp_id: str, product_id: str | None) -> str:
    return f"{base_id(dp_id)}|{product_id or '_'}"


def split_key(key: str) -> tuple[str, str | None]:
    dp, _, pid = key.partition("|")
    return dp, (None if pid in ("", "_") else pid)


class SqlLedger:
    """EgressLedger 的数据库实现（UPC-LIM-02/03/05/07）。"""

    def __init__(self, sm) -> None:
        self.sm = sm

    def record(self, *, org_id, counterparty_id, recipient, sreq_id, at):
        with self.sm.begin() as s:
            s.add(EgressSendRow(org_id=org_id, counterparty_id=counterparty_id, recipient=recipient.lower(),
                                sreq_id=sreq_id, at=utcnaive(at)))

    def last_to_recipient(self, recipient):
        with self.sm() as s:
            row = s.execute(select(EgressSendRow.at).where(EgressSendRow.recipient == recipient.lower())
                            .order_by(EgressSendRow.at.desc()).limit(1)).scalar()
            return aware(row)

    def counterparty_sends_since(self, counterparty_id, since):
        with self.sm() as s:
            rows = s.execute(select(EgressSendRow.at).where(EgressSendRow.counterparty_id == counterparty_id,
                                                            EgressSendRow.at >= utcnaive(since))).scalars().all()
            return sorted(aware(r) for r in rows)

    def tenant_sends_since(self, org_id, since):
        with self.sm() as s:
            return len(s.execute(select(EgressSendRow.id).where(EgressSendRow.org_id == org_id,
                                                                EgressSendRow.at >= utcnaive(since))).all())

    def tenant_first_send(self, org_id):
        with self.sm() as s:
            row = s.execute(select(EgressSendRow.at).where(EgressSendRow.org_id == org_id)
                            .order_by(EgressSendRow.at).limit(1)).scalar()
            return aware(row)

    def is_suppressed(self, recipient):
        with self.sm() as s:
            return s.get(SuppressionRow, recipient.lower()) is not None

    def suppress(self, recipient, reason):
        with self.sm.begin() as s:
            s.merge(SuppressionRow(recipient=recipient.lower(), reason=reason))


class SupplierService:
    def __init__(self, settings: Settings, *, core: CoreClient | None = None, transport: Transport | None = None,
                 registry: TemplateRegistry | None = None, clock: Callable[[], datetime] | None = None,
                 limiter: RateLimiter | None = None, geo_oracle: GeoOracle | None = None,
                 virus_scanner: Callable[[bytes], str | None] | None = None) -> None:
        self.settings = settings.ensure_secrets()
        self.engine = make_engine(settings.db_url)
        self.sm = make_sessionmaker(self.engine)
        self.core = core
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.limiter = limiter or RateLimiter(clock=lambda: self.clock().timestamp())
        self.registry = registry or TemplateRegistry.load_builtin()
        self._load_reviews()
        self.transport = transport or self._default_transport()
        self.geo_oracle = geo_oracle
        self.virus_scanner = virus_scanner
        link_domains = [settings.portal_host, *settings.extra_link_domains]
        self.egress = DefaultEgressGateway(self.registry, self.transport, ledger=SqlLedger(self.sm), audit=self._audit_sink,
                                           allowed_link_domains=link_domains, internal_domains=settings.internal_domains,
                                           default_from=settings.mail_from)
        (Path(settings.data_dir) / "blobs").mkdir(parents=True, exist_ok=True)

    # ================================================================ infra
    def _default_transport(self) -> Transport:
        s = self.settings
        if s.smtp_host:
            return SmtpTransport(s.smtp_host, s.smtp_port, username=s.smtp_user, password=s.smtp_password, starttls=s.smtp_starttls)
        if s.outbox_dir:
            return OutboxTransport(s.outbox_dir)
        return OutboxTransport(Path(s.data_dir) / "outbox")

    def _load_reviews(self) -> None:
        with self.sm() as s:
            for r in s.execute(select(TemplateReviewRow)).scalars():
                if self.registry.is_registered(r.template_id):
                    self.registry.overrides[r.template_id] = (r.status, r.reviewed_by)  # type: ignore[assignment]

    def review_template(self, template_id: str, reviewer: str) -> None:
        self.registry.review(template_id, reviewer)
        with self.sm.begin() as s:
            s.merge(TemplateReviewRow(template_id=template_id, status="reviewed", reviewed_by=reviewer, at=utcnaive(self.clock())))

    def _audit_sink(self, ev: dict) -> str:
        with self.sm.begin() as s:
            s.add(AuditRow(id=ev["id"], action=ev["action"], sreq_id=ev.get("sreq_id"), data=ev, at=utcnaive(self.clock())))
        return ev["id"]

    def audit(self, action: str, sreq_id: str | None, **data: Any) -> str:
        ev = {"id": new_id("aud"), "action": action, "sreq_id": sreq_id, "at": self.clock().isoformat(), **data}
        return self._audit_sink(ev)

    def portal_link(self, token: str) -> str:
        return f"{self.settings.public_url}/r/{token}"

    # ================================================================ requests
    def _row(self, s, sreq_id: str) -> RequestRow:
        row = s.get(RequestRow, sreq_id)
        if row is None:
            raise PortalError(404, "NOT_FOUND", "unknown request")
        return row

    @staticmethod
    def _sreq(row: RequestRow) -> SupplierRequest:
        return SupplierRequest.model_validate(row.data)

    @staticmethod
    def _manifest(row: RequestRow) -> RequestManifest:
        return RequestManifest.model_validate(row.manifest)

    def get_request(self, sreq_id: str) -> tuple[SupplierRequest, RequestManifest, dict]:
        with self.sm() as s:
            row = self._row(s, sreq_id)
            return self._sreq(row), self._manifest(row), dict(row.chase or {})

    def _save(self, s, row: RequestRow, new: SupplierRequest, *, history: bool = True) -> None:
        now = utcnaive(self.clock())
        if new.status != row.status:
            row.status_changed_at = now
        row.data = new.model_dump(mode="json")
        row.version = new.version
        row.status = new.status
        row.updated_at = now
        if history:
            s.add(RequestHistoryRow(sreq_id=new.id, version=new.version, status=new.status, data=row.data, at=now))

    def verify_manifest(self, jws: str) -> RequestManifest:
        try:
            payload = jws_verify(jws, self.settings.manifest_secret)
        except SignatureError as exc:
            raise PortalError(401, "MANIFEST_SIGNATURE_INVALID", exc.code) from exc
        return RequestManifest.model_validate(payload)

    def ingest_manifest(self, manifest: RequestManifest, *, status: str = "sent", approval_ref: str | None = None,
                        reissue: bool = False) -> dict:
        """客户侧推送请求清单（08 §6.5）：建立/更新本地 SupplierRequest 并返回门户链接。

        非 draft 状态必须带已批准 Approval 的引用（字段存在且格式正确；裁决本身由客户侧负责，03 §3.10）。
        """
        if status != "draft" and not approval_ref:
            raise PortalError(422, "GRAPH_APPROVAL_REQUIRED", "non-draft manifest requires approval_ref")
        now = self.clock()
        mhash = "sha256:" + sha256_hex(canonical_json(manifest.model_dump(mode="json")))
        with self.sm.begin() as s:
            row = s.get(RequestRow, manifest.sreq_id)
            data = {
                "id": manifest.sreq_id, "org_id": manifest.org_id, "version": int(manifest.sreq_ref.split("#v")[1]),
                "counterparty_id": manifest.counterparty_id,
                "datapoint_ids": [d if isinstance(d, str) else d.datapoint_id for d in manifest.datapoints],
                "product_ids": [p.product_id for p in manifest.products], "period": manifest.period.model_dump(mode="json"),
                "language": manifest.locale, "template_id": manifest.template_id or self._default_template_id(manifest),
                "channel": "supplier_portal", "due_date": manifest.due_date.isoformat(), "status": status,
                "approval_ref": approval_ref, "contact_email": manifest.contact_email,
            }
            if row is None:
                sreq = SupplierRequest.model_validate(data)
                row = RequestRow(id=sreq.id, org_id=sreq.org_id, version=sreq.version, status=sreq.status,
                                 data=sreq.model_dump(mode="json"), manifest=manifest.model_dump(mode="json"),
                                 manifest_hash=mhash, chase={}, status_changed_at=utcnaive(now),
                                 created_at=utcnaive(now), updated_at=utcnaive(now))
                s.add(row)
                s.add(RequestHistoryRow(sreq_id=sreq.id, version=sreq.version, status=sreq.status, data=row.data, at=utcnaive(now)))
            else:
                old = self._sreq(row)
                upd = {k: data[k] for k in ("datapoint_ids", "product_ids", "language", "template_id", "contact_email")}
                upd["period"] = manifest.period
                upd["due_date"] = manifest.due_date
                if old.status == "draft" and status != "draft":
                    upd.update({"status": status, "approval_ref": approval_ref})
                merged = old.model_copy(update=upd)
                SupplierRequest.model_validate(merged.model_dump())
                row.manifest = manifest.model_dump(mode="json")
                row.manifest_hash = mhash
                self._save(s, row, merged, history=merged.status != old.status)
            s.flush()
            if status == "draft":
                return {"sreq_ref": manifest.sreq_ref, "portal_link": None, "link_expires_at": None}
            issued = self._ensure_link(s, row, manifest, reissue=reissue)
        return {"sreq_ref": manifest.sreq_ref, "portal_link": self.portal_link(issued.token),
                "link_expires_at": issued.expires_at.isoformat()}

    def _default_template_id(self, m: RequestManifest) -> str:
        bases = {base_id(d if isinstance(d, str) else d.datapoint_id) for d in m.datapoints}
        fam = "eudr_geo_request" if any(b.startswith("dp:supply.eudr") for b in bases) else \
            "pcf_request" if any(b.startswith("dp:prod.pcf") for b in bases) else "cbam_see_request"
        t, _ = self.registry.resolve(fam, m.locale, require_reviewed=False)
        return t.id

    def _active_link(self, s, sreq_id: str) -> LinkRow | None:
        return s.execute(select(LinkRow).where(LinkRow.sreq_id == sreq_id, LinkRow.revoked_at.is_(None))
                         .order_by(LinkRow.generation.desc()).limit(1)).scalar()

    def _ensure_link(self, s, row: RequestRow, manifest: RequestManifest, *, reissue: bool = False) -> tokens.IssuedToken:
        now = self.clock()
        link = self._active_link(s, row.id)
        if link is not None and not reissue and aware(link.expires_at) > now:
            return tokens.derive(self.settings.instance_secret, row.id, link.generation, aware(link.expires_at))
        gen = 1
        if link is not None:
            gen = link.generation + 1
        last_gen = s.execute(select(LinkRow.generation).where(LinkRow.sreq_id == row.id)
                             .order_by(LinkRow.generation.desc()).limit(1)).scalar()
        gen = max(gen, (last_gen or 0) + 1)
        for old in s.execute(select(LinkRow).where(LinkRow.sreq_id == row.id, LinkRow.revoked_at.is_(None))).scalars():
            old.revoked_at = utcnaive(now)
            old.revoke_reason = "reissued"
        exp = tokens.default_expiry(now, manifest.due_date, manifest.link_ttl_days)
        issued = tokens.derive(self.settings.instance_secret, row.id, gen, exp)
        s.add(LinkRow(token_hash=issued.token_hash, sreq_id=row.id, email=manifest.contact_email.lower(), generation=gen,
                      issued_at=utcnaive(now), expires_at=utcnaive(issued.expires_at), origins=[]))
        sreq = self._sreq(row).model_copy(update={"portal_token_hash": issued.token_hash})
        row.data = sreq.model_dump(mode="json")
        return issued

    def current_link(self, sreq_id: str, *, reissue: bool = False) -> dict:
        with self.sm.begin() as s:
            row = self._row(s, sreq_id)
            issued = self._ensure_link(s, row, self._manifest(row), reissue=reissue)
        return {"portal_link": self.portal_link(issued.token), "link_expires_at": issued.expires_at.isoformat(),
                "token": issued.token}

    def revoke_links(self, sreq_id: str, reason: str = "revoked") -> int:
        n = 0
        with self.sm.begin() as s:
            for link in s.execute(select(LinkRow).where(LinkRow.sreq_id == sreq_id, LinkRow.revoked_at.is_(None))).scalars():
                link.revoked_at = utcnaive(self.clock())
                link.revoke_reason = reason
                n += 1
        self.audit("link.revoke", sreq_id, reason=reason, count=n)
        return n

    # ================================================================ token resolution
    def _ip_key(self, ip: str | None) -> str:
        return hashlib.sha256((ip or "-").encode()).hexdigest()[:16]

    def _unknown(self, ip: str | None) -> PortalError:
        wait = self.limiter.hit(f"unk:{self._ip_key(ip)}", UNKNOWN_TOKEN_PER_IP_PER_MINUTE, 60, backoff=True)
        if wait is not None:
            return PortalError(429, "RATE_LIMITED", "too many invalid links", retry_after=wait)
        return PortalError(404, "LINK_INVALID", "link not valid")

    def resolve(self, token: str, *, ip: str | None = None, device_cookie: str | None = None,
                check_device: bool = True, count: bool = True) -> Access:
        now = self.clock()
        if tokens.verify_signature(self.settings.instance_secret, token) is None:
            raise self._unknown(ip)
        th = tokens.token_hash(token)
        with self.sm.begin() as s:
            link = s.get(LinkRow, th)
            if link is None or link.revoked_at is not None:
                raise self._unknown(ip)
            if count:
                wait = self.limiter.hit(f"tok:{th}", TOKEN_PER_MINUTE, 60)
                if wait is not None:
                    raise PortalError(429, "RATE_LIMITED", "too many requests", retry_after=wait)
            if aware(link.expires_at) <= now:
                raise PortalError(410, "LINK_EXPIRED", "link expired")
            row = self._row(s, link.sreq_id)
            sreq, manifest = self._sreq(row), self._manifest(row)
            readonly = False
            if sreq.status in ("closed", "expired"):
                if aware(row.status_changed_at) + timedelta(days=tokens.READONLY_DAYS_AFTER_CLOSE) <= now:
                    raise PortalError(410, "LINK_EXPIRED", "request closed")
                readonly = True
            if sreq.status == "draft":
                raise self._unknown(ip)
            # 来源迹象（PLAUS-035）
            frozen = False
            ipk = self._ip_key(ip)
            origins = list(link.origins or [])
            if ipk not in origins:
                origins.append(ipk)
                link.origins = origins
                if len(origins) >= ORIGIN_FREEZE_THRESHOLD:
                    link.revoked_at = utcnaive(now)
                    link.revoke_reason = "PLAUS-035"
                    self._enqueue(s, "link.frozen", {"sreq_ref": sreq.ref, "rule_id": "PLAUS-035", "origins": len(origins)})
                    frozen = True
            new_cookie = None
            if not frozen and check_device and self.settings.device_binding:
                if link.first_opened_at is None:
                    new_cookie = secrets.token_urlsafe(32)
                    s.add(DeviceRow(device_hash=tokens.token_hash(new_cookie), token_hash=th, created_at=utcnaive(now)))
                else:
                    dev = s.get(DeviceRow, tokens.token_hash(device_cookie)) if device_cookie else None
                    if dev is None or dev.token_hash != th:
                        raise PortalError(401, "OTP_REQUIRED", "new device: verification code required")
            first_open = False
            if not frozen and link.first_opened_at is None:
                first_open = True
                link.first_opened_at = utcnaive(now)
                if sreq.link_opened_at is None:
                    sreq = sreq.model_copy(update={"link_opened_at": now})
                    row.data = sreq.model_dump(mode="json")
                    self._enqueue(s, "link.opened", {"sreq_ref": sreq.ref, "at": now.isoformat()})
            result = Access(sreq=sreq, manifest=manifest, token=token, token_hash=th, email=link.email,
                            expires_at=aware(link.expires_at), readonly=readonly, new_device_cookie=new_cookie)
        if first_open:
            self.deliver_webhooks()
        if frozen:  # 已提交冻结，再统一 404 并通知租户
            self.audit("link.frozen", sreq.id, rule_id="PLAUS-035")
            self.deliver_webhooks()
            raise PortalError(404, "LINK_INVALID", "link not valid")
        return result

    # ================================================================ OTP（新设备）
    def send_otp(self, token: str, ip: str | None = None) -> None:
        acc = self.resolve(token, ip=ip, check_device=False)
        now = self.clock()
        with self.sm.begin() as s:
            otp = s.get(OtpRow, acc.token_hash) or OtpRow(token_hash=acc.token_hash, failures=0)
            if otp.locked_until and aware(otp.locked_until) > now:
                raise PortalError(429, "OTP_LOCKED", "too many attempts", retry_after=(aware(otp.locked_until) - now).total_seconds())
            wait = self.limiter.hit(f"otp-send:{acc.token_hash}", 3, 3600)
            if wait is not None:
                raise PortalError(429, "RATE_LIMITED", "too many codes", retry_after=wait)
            code = f"{secrets.randbelow(1_000_000):06d}"
            otp.code_hash = tokens.token_hash(acc.token_hash + code)
            otp.expires_at = utcnaive(now + OTP_TTL)
            s.merge(otp)
        t = Translator(acc.manifest.locale)
        self._send_transactional(acc.email, t("otp_mail_subject"), t("otp_mail_body", customer=acc.manifest.customer_name, code=code))
        self.audit("portal.otp_sent", acc.sreq.id)

    def verify_otp(self, token: str, code: str, ip: str | None = None) -> str:
        acc = self.resolve(token, ip=ip, check_device=False)
        now = self.clock()
        with self.sm.begin() as s:
            otp = s.get(OtpRow, acc.token_hash)
            if otp is None or otp.code_hash is None:
                raise PortalError(400, "OTP_NOT_REQUESTED", "request a code first")
            if otp.locked_until and aware(otp.locked_until) > now:
                raise PortalError(429, "OTP_LOCKED", "too many attempts", retry_after=(aware(otp.locked_until) - now).total_seconds())
            ok = otp.expires_at and aware(otp.expires_at) > now and \
                secrets.compare_digest(otp.code_hash, tokens.token_hash(acc.token_hash + (code or "").strip()))
            failure: PortalError | None = None
            cookie = ""
            if not ok:
                otp.failures = (otp.failures or 0) + 1
                if otp.failures >= OTP_MAX_FAILURES:
                    otp.locked_until = utcnaive(now + timedelta(seconds=OTP_LOCK_SECONDS))
                    otp.failures = 0
                    otp.code_hash = None
                    failure = PortalError(429, "OTP_LOCKED", "too many attempts", retry_after=OTP_LOCK_SECONDS)
                else:
                    failure = PortalError(401, "OTP_INVALID", "wrong code")
            else:
                otp.code_hash = None
                otp.failures = 0
                cookie = secrets.token_urlsafe(32)
                s.add(DeviceRow(device_hash=tokens.token_hash(cookie), token_hash=acc.token_hash, created_at=utcnaive(now)))
        if failure is not None:  # 先提交失败计数，再抛出
            self.audit("portal.otp_failed", acc.sreq.id)
            raise failure
        self.audit("portal.device_added", acc.sreq.id)
        return cookie

    def _send_transactional(self, to: str, subject: str, body: str) -> None:
        from email.message import EmailMessage

        em = EmailMessage()
        em["From"] = self.settings.mail_from
        em["To"] = to
        em["Subject"] = subject
        em.set_content(body)
        self.transport.send(em)

    # ================================================================ magic link reissue
    def reissue_link(self, email: str, old_token: str, ip: str | None = None) -> None:
        """08 §6.6 `/auth/magic-link`：邮箱须为原收件人；同一邮箱 3 次/小时；旧 token 立即失效。始终静默成功（防枚举）。"""
        email = (email or "").strip().lower()
        wait = self.limiter.hit(f"ml:{email}", MAGIC_LINK_PER_EMAIL_PER_HOUR, 3600)
        if wait is not None:
            raise PortalError(429, "RATE_LIMITED", "too many link requests", retry_after=wait)
        if tokens.verify_signature(self.settings.instance_secret, old_token) is None:
            return
        th = tokens.token_hash(old_token)
        with self.sm.begin() as s:
            link = s.get(LinkRow, th)
            if link is None or link.email != email or (link.revoke_reason not in (None, "reissued")):
                return
            row = self._row(s, link.sreq_id)
            sreq, manifest = self._sreq(row), self._manifest(row)
            if sreq.status in ("closed", "expired", "draft"):
                return
            issued = self._ensure_link(s, row, manifest, reissue=True)
        t = Translator(manifest.locale)
        self._send_transactional(email, t("link_mail_subject", customer=manifest.customer_name),
                                 t("link_mail_body", link=self.portal_link(issued.token), expires=date_long(issued.expires_at.date(), manifest.locale)))
        self.audit("link.reissue", sreq.id)

    # ================================================================ views
    def datapoint_specs(self, manifest: RequestManifest) -> list[DataPointSpec]:
        return resolve_datapoints(list(manifest.datapoints))

    def form_groups(self, manifest: RequestManifest) -> list[dict]:
        """按产品分组的数据点卡片（08 §6.3）。"""
        specs = self.datapoint_specs(manifest)
        groups = []
        prod_specs = [d for d in specs if d.scope == "product"]
        org_specs = [d for d in specs if d.scope != "product"]
        if prod_specs and manifest.products:
            for p in manifest.products:
                groups.append({"product": p.model_dump(), "cards": [
                    {"key": field_key(d.datapoint_id, p.product_id), "spec": d} for d in prod_specs]})
        elif prod_specs:
            org_specs = prod_specs + org_specs
        if org_specs:
            groups.append({"product": None, "cards": [{"key": field_key(d.datapoint_id, None), "spec": d} for d in org_specs]})
        return groups

    def request_view(self, acc: Access) -> dict:
        m = acc.manifest
        draft_version, draft = self.get_draft(acc.sreq.id)
        return {
            "sreq_ref": acc.sreq.ref, "status": acc.sreq.status, "readonly": acc.readonly,
            "customer_name": m.customer_name, "supplier_name": m.supplier_name, "locale": m.locale,
            "period": m.period.model_dump(mode="json"), "due_date": m.due_date.isoformat(),
            "link_expires_at": acc.expires_at.isoformat(),
            "products": [p.model_dump() for p in m.products],
            "datapoints": [d.model_dump() for d in self.datapoint_specs(m)],
            "fields": [c["key"] for g in self.form_groups(m) for c in g["cards"]],
            "draft_version": draft_version, "draft": draft,
            "uploads": self.list_uploads(acc.sreq.id),
            "submissions": self.submission_count(acc.sreq.id),
        }

    # ================================================================ draft
    def get_draft(self, sreq_id: str) -> tuple[int, dict]:
        with self.sm() as s:
            d = s.get(DraftRow, sreq_id)
            return (d.draft_version, dict(d.data or {})) if d else (0, {"values": {}})

    def _validate_fields(self, manifest: RequestManifest, values: dict, *, strict: bool) -> tuple[dict, list[FieldIssue]]:
        specs = {base_id(d.datapoint_id): d for d in self.datapoint_specs(manifest)}
        allowed_keys = {c["key"] for g in self.form_groups(manifest) for c in g["cards"]}
        clean: dict[str, dict] = {}
        issues: list[FieldIssue] = []
        for key, entry in (values or {}).items():
            if key not in allowed_keys:
                issues.append(FieldIssue(key, "UNKNOWN_FIELD"))
                continue
            entry = dict(entry or {}) if isinstance(entry, dict) else {"value": entry}
            dp, _pid = split_key(key)
            spec = specs[dp]
            raw = entry.get("value")
            if isinstance(raw, str):
                raw = strip_controls(raw).strip()
            if raw in (None, ""):
                if entry.get("note"):
                    clean[key] = {"value": None, "note": strip_controls(str(entry["note"]))[:2000]}
                continue
            out: dict[str, Any] = {}
            if spec.value_type == "number":
                num = normalize_user_number(str(raw), manifest.locale)
                if num is None:
                    issues.append(FieldIssue(key, "INVALID_NUMBER"))
                    out["value"] = str(raw)
                else:
                    out["value"] = num
                unit = entry.get("unit") or spec.unit
                if spec.allowed_units and unit not in spec.allowed_units:
                    issues.append(FieldIssue(key, "UNIT_NOT_ALLOWED"))
                out["unit"] = unit
            elif spec.value_type == "enum":
                val = str(raw)
                if dp == "dp:supply.eudr.country_of_production":
                    val = val.upper()
                    if not re.fullmatch(r"[A-Z]{2}", val):
                        issues.append(FieldIssue(key, "INVALID_CHOICE"))
                elif spec.enum_values and val not in spec.enum_values:
                    issues.append(FieldIssue(key, "INVALID_CHOICE"))
                out["value"] = val
            elif spec.value_type == "date":
                val = str(raw)
                ok = re.fullmatch(r"\d{4}-\d{2}-\d{2}(/\d{4}-\d{2}-\d{2})?", val)
                if ok:
                    try:
                        for part in val.split("/"):
                            date.fromisoformat(part)
                    except ValueError:
                        ok = None
                if not ok:
                    issues.append(FieldIssue(key, "INVALID_DATE"))
                out["value"] = val
            elif spec.value_type == "geo":
                val = raw if isinstance(raw, str) else json.dumps(raw)
                try:
                    json.loads(val)
                except json.JSONDecodeError:
                    issues.append(FieldIssue(key, "INVALID_GEOJSON"))
                out["value"] = val
            elif spec.value_type == "bool":
                out["value"] = str(raw).lower() in ("1", "true", "yes", "on")
            else:
                out["value"] = str(raw)[:500]
            if entry.get("note"):
                out["note"] = strip_controls(str(entry["note"]))[:2000]
            for k in ("period_start", "period_end"):
                if entry.get(k):
                    out[k] = str(entry[k])
            src = entry.get("source")
            if isinstance(src, dict) and src.get("kind") in ("form", "excel", "reuse"):
                out["source"] = {k: src.get(k) for k in ("kind", "sheet", "cell") if src.get(k)}
            clean[key] = out
        if strict and issues:
            raise PortalError(422, "VALIDATION_FAILED", "invalid fields",
                              details=[{"field": i.key, "code": i.code} for i in issues])
        return clean, issues

    def save_draft(self, acc: Access, values: dict, draft_version: int | None, *, merge: bool = False) -> dict:
        if acc.readonly:
            raise PortalError(410, "READ_ONLY", "request closed")
        clean, issues = self._validate_fields(acc.manifest, values, strict=False)
        now = self.clock()
        with self.sm.begin() as s:
            d = s.get(DraftRow, acc.sreq.id)
            current = d.draft_version if d else 0
            if draft_version is not None and draft_version != current:
                raise PortalError(409, "DRAFT_CONFLICT", "draft_version mismatch", details={"current": current})
            data = dict(d.data or {}) if (d and merge) else {}
            vals = dict(data.get("values") or {}) if merge else {}
            vals.update(clean)
            data["values"] = vals
            if d is None:
                d = DraftRow(sreq_id=acc.sreq.id, draft_version=1, data=data, updated_at=utcnaive(now))
                s.add(d)
            else:
                d.draft_version = current + 1
                d.data = data
                d.updated_at = utcnaive(now)
            new_version = d.draft_version
        return {"draft_version": new_version, "issues": [{"field": i.key, "code": i.code} for i in issues],
                "hints": self.hints(acc, vals)}

    # ================================================================ plausibility hints（08 §6.4 子集）
    def plaus_context(self, manifest: RequestManifest, *, has_report: bool = False) -> PlausContext:
        from decimal import Decimal

        ref = manifest.reference
        return PlausContext(
            submission_date=self.clock().date(), request_period_start=manifest.period.start,
            request_period_end=manifest.period.end,
            requested_cn_codes={p.product_id: p.cn_code for p in manifest.products if p.cn_code},
            cbam_defaults={k: Decimal(v) for k, v in ref.cbam_defaults.items()},
            grid_factors={k: Decimal(v) for k, v in ref.grid_factors.items()},
            installation_country={p.product_id: manifest.supplier_country for p in manifest.products if manifest.supplier_country},
            origin_country={p.product_id: (p.country_of_origin or manifest.supplier_country) for p in manifest.products
                            if (p.country_of_origin or manifest.supplier_country)},
            pcf_benchmarks={k: Decimal(v) for k, v in ref.pcf_benchmarks.items()},
            geo_oracle=self.geo_oracle, has_verification_report=has_report,
        )

    @staticmethod
    def to_plaus_values(values: dict) -> list[PlausValue]:
        out = []
        for key, e in (values or {}).items():
            if not isinstance(e, dict) or e.get("value") in (None, ""):
                continue
            dp, pid = split_key(key)
            ps = date.fromisoformat(e["period_start"]) if e.get("period_start") else None
            pe = date.fromisoformat(e["period_end"]) if e.get("period_end") else None
            out.append(PlausValue(dp, e.get("value"), e.get("unit"), pid, ps, pe, e.get("note"),
                                  e.get("value") if isinstance(e.get("value"), str) else None))
        return out

    def hints(self, acc: Access, values: dict) -> list[dict]:
        rep = run_plausibility(self.to_plaus_values(values), self.plaus_context(acc.manifest), only=CLIENT_SIDE_RULES)
        t = Translator(acc.manifest.locale)
        out = []
        for h in rep.hits:
            msg = t(f"hint.{h.rule_id}", **h.params)
            if msg == f"hint.{h.rule_id}":
                msg = h.message
            out.append({"rule_id": h.rule_id, "severity": h.severity, "field": field_key(h.datapoint_id, h.product_id) if h.datapoint_id else None,
                        "message": msg, "params": h.params})
        return out

    # ================================================================ uploads
    def list_uploads(self, sreq_id: str) -> list[dict]:
        with self.sm() as s:
            rows = s.execute(select(UploadRow).where(UploadRow.sreq_id == sreq_id).order_by(UploadRow.created_at)).scalars().all()
            return [{"upload_id": r.id, "sha256": r.sha256, "filename": r.filename, "mime": r.mime, "size": r.size,
                     "scan": r.scan} for r in rows]

    def upload(self, acc: Access, filename: str, data: bytes, mime: str | None) -> dict:
        if acc.readonly:
            raise PortalError(410, "READ_ONLY", "request closed")
        filename = re.sub(r"[^\w.\- ]", "_", Path(filename or "upload").name)[:200] or "upload"
        if len(data) > self.settings.max_upload_bytes:
            self.audit("portal.upload_rejected", acc.sreq.id, rule_id="PLAUS-033", reason="size")
            raise PortalError(413, "UPLOAD_TOO_LARGE", "file larger than 25 MB", details={"rule_id": "PLAUS-033"})
        total = sum(u["size"] for u in self.list_uploads(acc.sreq.id))
        if total + len(data) > self.settings.max_request_upload_bytes:
            raise PortalError(413, "UPLOAD_QUOTA", "request upload quota (200 MB) exceeded")
        wait = self.limiter.hit(f"upl:{acc.token_hash}", TOKEN_UPLOADS_PER_DAY, 86400)
        if wait is not None:
            raise PortalError(429, "RATE_LIMITED", "upload limit per day", retry_after=wait)
        scan = scan_file(filename, data, mime)
        hits = [{"rule_id": h.rule_id, "severity": h.severity, "message": h.message, "params": h.params} for h in scan.hits]
        av = "not_configured"
        if self.virus_scanner is not None:
            verdict = self.virus_scanner(data)
            av = "clean" if verdict is None else "infected"
            if verdict is not None:
                hits.append({"rule_id": "PLAUS-033", "severity": "block", "message": f"malware: {verdict}", "params": {}})
        if scan.rejected or av == "infected":
            self.audit("portal.upload_rejected", acc.sreq.id, hits=hits)
            raise PortalError(422, "UPLOAD_REJECTED", "file rejected", details=hits)
        digest = hashlib.sha256(data).hexdigest()
        path = Path(self.settings.data_dir) / "blobs" / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        uid = new_id("upl")
        rec = {"status": "accepted", "hits": hits, "av": av}
        with self.sm.begin() as s:
            s.add(UploadRow(id=uid, sreq_id=acc.sreq.id, sha256="sha256:" + digest, filename=filename,
                            mime=mime or "application/octet-stream", size=len(data), path=str(path), scan=rec,
                            created_at=utcnaive(self.clock())))
        self.audit("portal.upload", acc.sreq.id, sha256="sha256:" + digest, size=len(data))
        return {"upload_id": uid, "sha256": "sha256:" + digest, "filename": filename, "size": len(data), "scan": rec}

    def parse_cbam(self, acc: Access, sha256: str) -> dict:
        with self.sm() as s:
            up = s.execute(select(UploadRow).where(UploadRow.sreq_id == acc.sreq.id, UploadRow.sha256 == sha256)).scalar()
        if up is None:
            raise PortalError(404, "UPLOAD_NOT_FOUND", "upload not found")
        parser = get_cbam_parser()
        if parser is None:
            raise PortalError(501, "PARSER_UNAVAILABLE", "belegt_io CBAM communication parser not installed")
        try:
            items = parser(up.path)
        except Exception as exc:  # noqa: BLE001
            raise PortalError(422, "PARSE_FAILED", f"template not recognised: {type(exc).__name__}") from exc
        by_cn = {p.cn_code: p.product_id for p in acc.manifest.products if p.cn_code}
        found, seen_products = [], set()
        for it in items:
            pid = it.get("product_id") or by_cn.get(str(it.get("cn_code") or ""))
            if pid:
                seen_products.add(pid)
            found.append({"field": field_key(it["datapoint_id"], pid) if it.get("datapoint_id") else None,
                          "datapoint_id": it.get("datapoint_id"), "value": it.get("value"), "unit": it.get("unit"),
                          "source": {"kind": "excel", "sheet": it.get("sheet"), "cell": it.get("cell")}})
        missing = [p.product_id for p in acc.manifest.products if p.product_id not in seen_products]
        return {"template": "tm:cbam_communication@2024", "found": found, "missing_products": missing}

    # ================================================================ questions / decline / pause
    def ask_question(self, acc: Access, text: str, datapoint_id: str | None = None) -> dict:
        text = strip_controls(text or "").strip()
        if not text or len(text) > 4000:
            raise PortalError(422, "VALIDATION_FAILED", "question text 1–4000 chars")
        qid = new_id("sq")
        now = self.clock()
        with self.sm.begin() as s:
            s.add(QuestionRow(id=qid, sreq_id=acc.sreq.id, datapoint_id=datapoint_id, text=text, created_at=utcnaive(now)))
            self._enqueue(s, "question.created", {"id": qid, "sreq_ref": acc.sreq.ref, "datapoint_id": datapoint_id,
                                                  "text": text, "locale": acc.manifest.locale, "created_at": now.isoformat()})
        self.deliver_webhooks()
        return {"question_id": qid}

    def decline(self, acc: Access, reason: str, text: str = "") -> dict:
        if acc.readonly:
            raise PortalError(410, "READ_ONLY", "request closed")
        if reason not in DECLINE_REASONS:
            raise PortalError(422, "VALIDATION_FAILED", f"reason must be one of {DECLINE_REASONS}")
        did = new_id("sdc")
        now = self.clock()
        text = strip_controls(text or "")[:4000]
        with self.sm.begin() as s:
            s.add(DeclineRow(id=did, sreq_id=acc.sreq.id, reason=reason, text=text, created_at=utcnaive(now)))
            self._enqueue(s, "decline.created", {"id": did, "sreq_ref": acc.sreq.ref, "reason": reason, "text": text,
                                                 "created_at": now.isoformat()})
        self.deliver_webhooks()
        return {"decline_id": did}

    def pause_reminders(self, acc: Access) -> None:
        """UPC-LIM-07：“暂停提醒”链接生效后停止催办并通知租户（无需登录）。"""
        with self.sm.begin() as s:
            row = self._row(s, acc.sreq.id)
            sreq = self._sreq(row).model_copy(update={"reminders_paused": True})
            row.data = sreq.model_dump(mode="json")
            self._enqueue(s, "reminders.paused", {"sreq_ref": sreq.ref, "at": self.clock().isoformat()})
        self.deliver_webhooks()

    # ================================================================ submit
    def submission_count(self, sreq_id: str) -> int:
        with self.sm() as s:
            return len(s.execute(select(SubmissionRow.id).where(SubmissionRow.sreq_id == sreq_id)).all())

    def submit(self, acc: Access, *, attestation: bool, idempotency_key: str | None = None,
               draft_version: int | None = None, values: dict | None = None) -> dict:
        if acc.readonly:
            raise PortalError(410, "READ_ONLY", "request closed")
        if idempotency_key:
            with self.sm() as s:
                prev = s.execute(select(SubmissionRow).where(SubmissionRow.sreq_id == acc.sreq.id,
                                                             SubmissionRow.idempotency_key == idempotency_key)).scalar()
            if prev is not None:
                return {"sreq_ref": prev.package["sreq_ref"], "package_hash": prev.package_hash, "seq": prev.seq,
                        "submitted_at": prev.package["submitted_at"], "replayed": True, "hints": []}
        if not attestation:
            raise PortalError(422, "ATTESTATION_REQUIRED", "submission statement must be confirmed")
        if self.submission_count(acc.sreq.id) >= TOKEN_SUBMITS_TOTAL:
            raise PortalError(429, "SUBMIT_LIMIT", "at most 10 submissions per request")
        if values is not None:
            self.save_draft(acc, values, draft_version, merge=True)
            draft_version = None
        cur_version, draft = self.get_draft(acc.sreq.id)
        if draft_version is not None and draft_version != cur_version:
            raise PortalError(409, "DRAFT_CONFLICT", "draft_version mismatch", details={"current": cur_version})
        clean, _ = self._validate_fields(acc.manifest, draft.get("values") or {}, strict=True)
        uploads = self.list_uploads(acc.sreq.id)
        if not any(v.get("value") not in (None, "") for v in clean.values()) and not uploads:
            raise PortalError(422, "EMPTY_SUBMISSION", "no values and no files")
        now = self.clock()
        m = acc.manifest
        specs = {base_id(d.datapoint_id): d for d in self.datapoint_specs(m)}
        sub_values = []
        for key, e in clean.items():
            if e.get("value") in (None, ""):
                continue
            dp, pid = split_key(key)
            full_dp = specs[dp].datapoint_id
            period = Period(start=date.fromisoformat(e["period_start"]), end=date.fromisoformat(e["period_end"])) \
                if e.get("period_start") and e.get("period_end") else m.period
            val = e["value"]
            if specs[dp].value_type == "geo" and isinstance(val, str):
                val = json.loads(val)
            sub_values.append(SubmissionValue(
                datapoint_id=full_dp, value=val, unit=e.get("unit"), period=period,
                boundary=f"product:{pid}" if pid else "organization",
                source=SubmissionSource(**(e.get("source") or {"kind": "form"})), note=e.get("note")))
        pkg = SubmissionPackage(
            request_manifest_hash=self._manifest_hash(acc.sreq.id), sreq_ref=acc.sreq.ref, submitted_at=now,
            locale=m.locale, values=sub_values,
            blobs=[BlobRef(sha256=u["sha256"], mime=u["mime"], filename=u["filename"]) for u in uploads],
            attestation=Attestation(by=acc.email, statement_version=ATTESTATION_STATEMENT))
        unsigned = pkg.unsigned_payload()
        pkg.signature = jws_sign_detached(unsigned, self.settings.instance_secret, kid=self.settings.portal_host)
        package = {**unsigned, "signature": pkg.signature}
        body = canonical_json(package)
        phash = "sha256:" + sha256_hex(body)
        has_report = bool(uploads)
        rep = run_plausibility(self.to_plaus_values(clean), self.plaus_context(m, has_report=has_report), only=CLIENT_SIDE_RULES)
        with self.sm.begin() as s:
            seq = len(s.execute(select(SubmissionRow.id).where(SubmissionRow.sreq_id == acc.sreq.id)).all()) + 1
            s.add(SubmissionRow(id=new_id("ssub"), sreq_id=acc.sreq.id, seq=seq, package=package, package_hash=phash,
                                idempotency_key=idempotency_key, created_at=utcnaive(now)))
            row = self._row(s, acc.sreq.id)
            sreq = self._sreq(row)
            if sreq.status in ("sent", "reminded", "escalated"):
                self._save(s, row, transition(sreq, "responded", at=now))
            self._enqueue(s, "submission.created", {"package": package, "package_hash": phash})
        self.audit("portal.submit", acc.sreq.id, package_hash=phash, seq=seq)
        self.deliver_webhooks()
        return {"sreq_ref": acc.sreq.ref, "package_hash": phash, "seq": seq, "submitted_at": now.isoformat(),
                "replayed": False, "hints": [{"rule_id": h.rule_id, "severity": h.severity} for h in rep.hits]}

    def _manifest_hash(self, sreq_id: str) -> str:
        with self.sm() as s:
            return self._row(s, sreq_id).manifest_hash

    def verify_package(self, package: dict) -> None:
        """供客户侧或测试复核提交包签名。"""
        from .signing import jws_verify_detached

        unsigned = {k: v for k, v in package.items() if k != "signature"}
        jws_verify_detached(package["signature"], unsigned, self.settings.instance_secret)

    # ================================================================ webhooks（外发到客户侧）
    def _enqueue(self, s, event: str, payload: dict) -> None:
        s.add(WebhookRow(id=new_id("whk"), event=event, payload=payload, status="pending", attempts=0,
                         created_at=utcnaive(self.clock())))

    def deliver_webhooks(self, max_attempts: int = 10) -> dict:
        if self.core is None:
            return {"delivered": 0, "pending": self.pending_webhooks()}
        delivered = failed = 0
        with self.sm.begin() as s:
            rows = s.execute(select(WebhookRow).where(WebhookRow.status == "pending").order_by(WebhookRow.created_at)).scalars().all()
            for w in rows:
                try:
                    if w.event == "submission.created":
                        self.core.push_submission(w.payload["package"])
                    elif w.event == "question.created":
                        self.core.push_question(w.payload)
                    else:
                        self.core.push_event(w.event, w.payload)
                    w.status = "delivered"
                    delivered += 1
                except Exception as exc:  # noqa: BLE001
                    w.attempts += 1
                    w.last_error = str(exc)[:500]
                    if w.attempts >= max_attempts:
                        w.status = "failed"
                    failed += 1
        return {"delivered": delivered, "failed": failed, "pending": self.pending_webhooks()}

    def pending_webhooks(self) -> int:
        with self.sm() as s:
            return len(s.execute(select(WebhookRow.id).where(WebhookRow.status == "pending")).all())

    # ================================================================ core → supplier 事件
    def apply_core_event(self, event: str, payload: dict) -> dict:
        now = self.clock()
        if event == "request.status":
            sreq_id = payload["sreq_id"]
            approval = ApprovalInfo.model_validate(payload["approval"]) if payload.get("approval") else None
            with self.sm.begin() as s:
                row = self._row(s, sreq_id)
                sreq = self._sreq(row)
                try:
                    new = transition(sreq, payload["status"], at=now, approval=approval,
                                     egress_audit_ref=payload.get("egress_audit_ref"),
                                     reminder_cap=int((row.chase or {}).get("cap", 5)))
                except TransitionError as exc:
                    raise PortalError(409, exc.code, str(exc)) from exc
                self._save(s, row, new)
            return {"sreq_ref": new.ref, "status": new.status}
        if event == "link.revoke":
            return {"revoked": self.revoke_links(payload["sreq_id"], payload.get("reason", "tenant"))}
        raise PortalError(422, "UNKNOWN_EVENT", event)

    # ================================================================ 追数侧：草稿、首次发送、催办
    def create_draft(self, manifest: RequestManifest, *, tier: str = "B", sender: dict | None = None,
                     counterparty: CounterpartyInfo | None = None) -> SupplierRequest:
        self.ingest_manifest(manifest, status="draft")
        with self.sm.begin() as s:
            row = self._row(s, manifest.sreq_id)
            chase = dict(row.chase or {})
            chase.update({"tier": tier, "sender": sender or {}, "done": [], "cap": chase_mod.reminder_cap(tier)})  # type: ignore[arg-type]
            if counterparty:
                chase["counterparty"] = counterparty.model_dump()
            row.chase = chase
            return self._sreq(row)

    def _calendar(self, manifest: RequestManifest) -> BusinessCalendar:
        return BusinessCalendar(country=manifest.supplier_country or "", tz=manifest.supplier_timezone or "UTC")

    def _counterparty(self, manifest: RequestManifest, chase: dict) -> CounterpartyInfo | None:
        if chase.get("counterparty"):
            return CounterpartyInfo.model_validate(chase["counterparty"])
        if self.core is not None:
            return self.core.get_counterparty(manifest.counterparty_id)
        return None

    def _render_ctx(self, sreq: SupplierRequest, manifest: RequestManifest, chase: dict, portal_link: str,
                    expires: datetime) -> dict:
        sender = chase.get("sender") or {}
        return {
            "supplier_name": manifest.supplier_name, "contact_name": manifest.contact_name,
            "customer_name": manifest.customer_name, "products": [p.model_dump() for p in manifest.products] or
            [{"name": manifest.supplier_name}], "datapoints": list(manifest.datapoints), "period": manifest.period,
            "due_date": sreq.due_date, "link_expires_at": expires.date(), "portal_link": portal_link,
            "sender_name": sender.get("name"), "sender_role": sender.get("role"), "sreq_id": sreq.id,
            "privacy_note_link": manifest.privacy_note_link or f"{self.settings.public_url}/privacy",
            "custom_note": sender.get("custom_note"), "custom_note_confirmed_by": sender.get("custom_note_confirmed_by"),
            "reminder_count": sreq.reminder_count, "calc": chase.get("calc"),
        }

    def send_first(self, sreq_id: str, approval: ApprovalInfo, *, sender_name: str | None = None,
                   sender_role: str | None = None) -> dict:
        """draft → sent：已批准 Approval + egress 放行（04 §2.15、03 §3.10）。"""
        now = self.clock()
        with self.sm.begin() as s:
            row = self._row(s, sreq_id)
            sreq, manifest = self._sreq(row), self._manifest(row)
            if sreq.status != "draft":
                raise PortalError(409, "GRAPH_STATE_INVALID", f"status is {sreq.status}")
            # 先做状态机前置校验（不落库），避免无审批就外发
            try:
                transition(sreq, "sent", at=now, approval=approval, egress_audit_ref="aud_precheck")
            except TransitionError as exc:
                raise PortalError(409, exc.code, str(exc)) from exc
            chase = dict(row.chase or {})
            if sender_name or sender_role:
                chase["sender"] = {**chase.get("sender", {}), "name": sender_name, "role": sender_role}
            row.chase = chase
            issued = self._ensure_link(s, row, manifest)
            sreq = self._sreq(row)
        ctx = self._render_ctx(sreq, manifest, chase, self.portal_link(issued.token), issued.expires_at)
        try:
            rendered = self.registry.render(sreq.template_id, ctx, allowed_link_domains=self.egress.allowed_link_domains)
        except TemplateError as exc:
            raise PortalError(422, exc.code, str(exc)) from exc
        cp = self._counterparty(manifest, chase)
        msg = OutboundMessage(org_id=sreq.org_id, sreq_id=sreq.id, recipient=manifest.contact_email, rendered=rendered,
                              counterparty=cp, first_send=True, approval=approval, kind="first", sreq_sent_count=0,
                              reminder_cap=int(chase.get("cap", 3)), calendar=self._calendar(manifest),
                              reply_to=None, pause_link=f"{self.settings.public_url}/pause/{issued.token}")
        try:
            receipt = self.egress.send(msg, now)
        except EgressDenied as d:
            return {"ok": False, "denied": d.to_dict(), "audit_ref": d.audit_ref}
        with self.sm.begin() as s:
            row = self._row(s, sreq_id)
            new = transition(self._sreq(row), "sent", at=now, approval=approval, egress_audit_ref=receipt.audit_ref)
            self._save(s, row, new)
            chase = dict(row.chase or {})
            sent_on = self._calendar(manifest).local_date(now)
            chase.update({"sent_on": sent_on.isoformat(), "done": [], "sent_count": 1})
            row.chase = chase
        return {"ok": True, "sreq_ref": new.ref, "audit_ref": receipt.audit_ref, "message_id": receipt.message_id}

    def chase_tick(self, now: datetime | None = None) -> list[ChaseResult]:
        """定时调用：对每个进行中的请求执行 chase-policy@1.0.0 的下一步（最多一步）。"""
        now = now or self.clock()
        results: list[ChaseResult] = []
        with self.sm() as s:
            ids = [r for r in s.execute(select(RequestRow.id).where(RequestRow.status.in_(("sent", "reminded")))).scalars()]
        for sreq_id in ids:
            results.append(self._chase_one(sreq_id, now))
        return [r for r in results if r.action != "none"]

    def _chase_one(self, sreq_id: str, now: datetime) -> ChaseResult:
        sreq, manifest, chase = self.get_request(sreq_id)
        if not chase.get("sent_on"):
            return ChaseResult(sreq_id, "none", True)
        if chase.get("retry_after") and datetime.fromisoformat(chase["retry_after"]) > now:
            return ChaseResult(sreq_id, "none", True, {"waiting_until": chase["retry_after"]})
        cal = self._calendar(manifest)
        sched = chase_mod.build_schedule(date.fromisoformat(chase["sent_on"]), sreq.due_date, cal,
                                         chase.get("tier", "B"), chase.get("cap"))
        paused_until = date.fromisoformat(chase["paused_until"]) if chase.get("paused_until") else None
        state = chase_mod.ChaseState(status=sreq.status, reminder_count=sreq.reminder_count,
                                     done_steps=frozenset(chase.get("done", [])), link_opened=sreq.link_opened_at is not None,
                                     submitted=self.submission_count(sreq_id) > 0, paused_until=paused_until,
                                     reminders_stopped=sreq.reminders_paused)
        act = chase_mod.next_action(sched, state, now, cal)
        if act is None:
            return ChaseResult(sreq_id, "none", True)
        detail: dict[str, Any] = {"step": act.step.key, "variant": act.variant}
        if act.external and act.variant:
            fam = sreq.template_id.split(":", 1)[1].split(".", 1)[0]
            rem_fam = REMINDER_FAMILY.get(fam)
            if rem_fam is None:
                self._mark(sreq_id, act.step.key, event=("reminder.no_template", {"sreq_ref": sreq.ref, "family": fam}))
                return ChaseResult(sreq_id, "skipped", False, {**detail, "reason": "no reminder template for family"})
            try:
                tmpl, fallback = self.registry.resolve(rem_fam, manifest.locale)
            except TemplateError as exc:
                return ChaseResult(sreq_id, "error", False, {**detail, "code": exc.code})
            link = self.current_link(sreq_id)
            ctx = self._render_ctx(sreq, manifest, chase, link["portal_link"], datetime.fromisoformat(link["link_expires_at"]))
            try:
                rendered = self.registry.render(tmpl.id, ctx, variant=act.variant, allowed_link_domains=self.egress.allowed_link_domains)
            except TemplateError as exc:
                return ChaseResult(sreq_id, "error", False, {**detail, "code": exc.code})
            msg = OutboundMessage(org_id=sreq.org_id, sreq_id=sreq.id, recipient=manifest.contact_email, rendered=rendered,
                                  counterparty=self._counterparty(manifest, chase), kind="reminder",
                                  sreq_sent_count=int(chase.get("sent_count", 1)), reminder_cap=sched.cap, calendar=cal,
                                  pause_link=f"{self.settings.public_url}/pause/{link['token']}")
            try:
                receipt = self.egress.send(msg, now)
            except EgressDenied as d:
                if d.retry_after is not None:
                    self._update_chase(sreq_id, retry_after=d.retry_after.isoformat())
                    return ChaseResult(sreq_id, "deferred", True, {**detail, **d.to_dict()})
                if d.escalate and sreq.status == "reminded":
                    self._transition(sreq_id, "escalated", now)
                    self._mark(sreq_id, act.step.key, event=("request.escalated", {"sreq_ref": sreq.ref, "rule_id": d.rule_id}))
                    return ChaseResult(sreq_id, "escalated", True, {**detail, **d.to_dict()})
                self._mark(sreq_id, None, event=("egress.denied", {"sreq_ref": sreq.ref, **d.to_dict()}))
                return ChaseResult(sreq_id, "denied", False, {**detail, **d.to_dict()})
            self._transition(sreq_id, "reminded", now, egress_audit_ref=receipt.audit_ref, cap=sched.cap)
            self._update_chase(sreq_id, retry_after=None, inc_sent=True, fallback_from=fallback)
            if act.target_status == "escalated":
                self._transition(sreq_id, "escalated", now)
                self._mark(sreq_id, act.step.key, event=("request.escalated", {"sreq_ref": sreq.ref, "notify": list(act.notify)}))
                return ChaseResult(sreq_id, "escalated", True, {**detail, "audit_ref": receipt.audit_ref})
            self._mark(sreq_id, act.step.key, event=("request.reminded", {"sreq_ref": sreq.ref, "variant": act.variant,
                                                                          "notify": list(act.notify)}) if act.notify else None)
            return ChaseResult(sreq_id, "reminded", True, {**detail, "audit_ref": receipt.audit_ref})
        # 非外发动作
        if act.target_status == "expired":
            self._transition(sreq_id, "expired", now)
            self._mark(sreq_id, act.step.key, event=("request.expired", {"sreq_ref": sreq.ref}))
            return ChaseResult(sreq_id, "expired", True, detail)
        if act.target_status == "escalated" and sreq.status == "reminded":
            self._transition(sreq_id, "escalated", now)
            self._mark(sreq_id, act.step.key, event=("request.escalated", {"sreq_ref": sreq.ref, "notify": list(act.notify),
                                                                           "reason": act.reason}))
            return ChaseResult(sreq_id, "escalated", True, detail)
        self._mark(sreq_id, act.step.key, event=(f"chase.{act.step.kind}", {"sreq_ref": sreq.ref, "notify": list(act.notify),
                                                                            "reason": act.reason}))
        return ChaseResult(sreq_id, act.step.kind, True, detail)

    def _transition(self, sreq_id: str, to: str, now: datetime, *, egress_audit_ref: str | None = None, cap: int = 5) -> None:
        with self.sm.begin() as s:
            row = self._row(s, sreq_id)
            self._save(s, row, transition(self._sreq(row), to, at=now, egress_audit_ref=egress_audit_ref, reminder_cap=cap))  # type: ignore[arg-type]

    def _update_chase(self, sreq_id: str, *, retry_after: str | None = None, inc_sent: bool = False,
                      fallback_from: str | None = None) -> None:
        with self.sm.begin() as s:
            row = self._row(s, sreq_id)
            chase = dict(row.chase or {})
            chase["retry_after"] = retry_after
            if inc_sent:
                chase["sent_count"] = int(chase.get("sent_count", 1)) + 1
            if fallback_from:
                chase["fallback_from"] = fallback_from
            row.chase = chase

    def _mark(self, sreq_id: str, step_key: str | None, *, event: tuple[str, dict] | None = None) -> None:
        with self.sm.begin() as s:
            row = self._row(s, sreq_id)
            chase = dict(row.chase or {})
            if step_key:
                chase["done"] = sorted(set(chase.get("done", [])) | {step_key})
            row.chase = chase
            if event:
                self._enqueue(s, event[0], event[1])
        self.deliver_webhooks()

    def pause_chasing(self, sreq_id: str, days: int = chase_mod.PAUSE_DAYS_I_WILL_CONTACT) -> None:
        """采购“我来联系”：暂停自动催办 14 天（08 §4.5）。"""
        with self.sm.begin() as s:
            row = self._row(s, sreq_id)
            chase = dict(row.chase or {})
            chase["paused_until"] = (self.clock().date() + timedelta(days=days)).isoformat()
            row.chase = chase
