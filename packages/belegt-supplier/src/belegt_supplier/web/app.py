# Spec: 08 §6.2–§6.6（供应商端功能、信息架构、线框、API supplier-api v1）、§7（安全：token、限流、隐私、noindex、CSP）
"""FastAPI 应用：`supplier-api` v1（JSON）+ 服务端渲染门户（Jinja2 + 少量原生 JS，零构建）。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import date, datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from .. import __version__
from ..config import Settings
from ..core_client import CoreClient, HttpCoreClient
from ..i18n import Translator, negotiate
from ..localefmt import date_long, period_label
from ..messages import TemplateError
from ..models import ApprovalInfo, CounterpartyInfo, RequestManifest
from ..service import DECLINE_REASONS, Access, PortalError, SupplierService
from ..signing import WEBHOOK_HEADER, SignatureError, verify_webhook

API = "/supplier-api/v1"
LANG_COOKIE = "bsl_lang"


# ---------------------------------------------------------------- bodies
class DraftBody(BaseModel):
    draft_version: int | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class ParseBody(BaseModel):
    sha256: str


class QuestionBody(BaseModel):
    text: str
    datapoint_id: str | None = None


class DeclineBody(BaseModel):
    reason: str
    text: str = ""


class SubmitBody(BaseModel):
    attestation: bool = False
    draft_version: int | None = None
    values: dict[str, Any] | None = None


class OtpBody(BaseModel):
    code: str


class MagicLinkBody(BaseModel):
    email: str
    token: str


class ManifestBody(BaseModel):
    manifest_jws: str
    status: str = "sent"
    approval_ref: str | None = None
    reissue: bool = False


class AdminDraftBody(BaseModel):
    manifest: RequestManifest
    tier: str = "B"
    sender: dict[str, Any] = Field(default_factory=dict)
    counterparty: CounterpartyInfo | None = None


class AdminSendBody(BaseModel):
    approval: ApprovalInfo
    sender_name: str | None = None
    sender_role: str | None = None


class ReviewBody(BaseModel):
    reviewer: str


class RenderBody(BaseModel):
    ctx: dict[str, Any]
    variant: str | None = None


# ---------------------------------------------------------------- helpers
def _device_cookie_name(token_hash: str) -> str:
    return "bsl_dev_" + token_hash.split(":", 1)[1][:16]


def create_app(settings: Settings | None = None, *, service: SupplierService | None = None,
               core: CoreClient | None = None) -> FastAPI:
    settings = (settings or Settings.from_env()).ensure_secrets()
    if service is None:
        if core is None and settings.core_url:
            core = HttpCoreClient(settings.core_url, settings.webhook_secret)
        service = SupplierService(settings, core=core)
    svc = service
    app = FastAPI(title="belegt-supplier", version=__version__, docs_url=f"{API}/docs", openapi_url=f"{API}/openapi.json",
                  redoc_url=None)
    app.state.service = svc
    tpl_dir = resources.files("belegt_supplier").joinpath("web/templates")
    env = Environment(loader=FileSystemLoader(str(tpl_dir)), autoescape=select_autoescape(["html"]))
    static_dir = Path(str(resources.files("belegt_supplier").joinpath("web/static")))
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    secure_cookie = settings.public_url.startswith("https://")

    # ------------------------------------------------------------ middleware
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp: Response = await call_next(request)
        bind = getattr(request.state, "bind_cookie", None)
        if bind and "set-cookie" not in resp.headers:  # 即使本次请求以错误结束，也不能丢失首次设备绑定
            resp.set_cookie(bind[0], bind[1], httponly=True, samesite="lax", secure=secure_cookie, max_age=90 * 86400, path="/")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith(("/r/", API, "/pause/")):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    def client_ip(request: Request) -> str | None:
        if settings.trust_proxy_headers and request.headers.get("x-forwarded-for"):
            return request.headers["x-forwarded-for"].split(",")[0].strip()
        return request.client.host if request.client else None

    def lang_for(request: Request, fallback: str) -> str:
        q = request.query_params.get("lang")
        if q in ("de", "en", "tr", "zh"):
            return q
        c = request.cookies.get(LANG_COOKIE)
        if c in ("de", "en", "tr", "zh"):
            return c
        return negotiate(request.headers.get("accept-language"), fallback if fallback in ("de", "en", "tr", "zh") else "en")

    def access(request: Request, token: str, *, count: bool = True) -> Access:
        th = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        cookie = request.cookies.get(_device_cookie_name(th))
        acc = svc.resolve(token, ip=client_ip(request), device_cookie=cookie, count=count)
        if acc.new_device_cookie:
            request.state.bind_cookie = (_device_cookie_name(acc.token_hash), acc.new_device_cookie)
        return acc

    def bind_cookie(resp: Response, acc: Access) -> None:
        if acc.new_device_cookie:
            resp.set_cookie(_device_cookie_name(acc.token_hash), acc.new_device_cookie, httponly=True, samesite="lax",
                            secure=secure_cookie, max_age=90 * 86400, path="/")

    def csrf_for(acc: Access, request: Request) -> str:
        dev = acc.new_device_cookie or request.cookies.get(_device_cookie_name(acc.token_hash)) or ""
        return hmac.new(settings.instance_secret, f"csrf|{acc.token_hash}|{dev}".encode(), hashlib.sha256).hexdigest()[:32]

    def check_csrf(acc: Access, request: Request, value: str | None) -> None:
        if not value or not hmac.compare_digest(value, csrf_for(acc, request)):
            raise PortalError(403, "CSRF_INVALID", "invalid form token")

    def require_admin(authorization: str | None) -> None:
        if not settings.admin_key:
            raise PortalError(403, "ADMIN_DISABLED", "BELEGT_SUPPLIER_ADMIN_KEY not configured")
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {settings.admin_key}"):
            raise PortalError(401, "UNAUTHORIZED", "admin key required")

    def json_resp(data: Any, acc: Access | None = None, status: int = 200) -> JSONResponse:
        r = JSONResponse(data, status_code=status)
        if acc:
            bind_cookie(r, acc)
        return r

    # ------------------------------------------------------------ errors
    @app.exception_handler(PortalError)
    async def portal_error(request: Request, exc: PortalError):
        headers = {"Retry-After": str(int(exc.retry_after) + 1)} if exc.retry_after else None
        path = request.url.path
        if path.startswith(("/r/", "/pause/")):
            token = path.split("/")[2] if len(path.split("/")) > 2 else ""
            if exc.code == "OTP_REQUIRED":
                return RedirectResponse(f"/r/{token}/verify", status_code=303)
            t = Translator(lang_for(request, settings.default_locale))
            key = {"LINK_EXPIRED": "link_expired", "RATE_LIMITED": "rate_limited", "READ_ONLY": "readonly_notice",
                   "OTP_INVALID": "otp_wrong", "OTP_LOCKED": "otp_locked"}.get(exc.code, "link_invalid")
            html = env.get_template("message.html").render(t=t, lang=t.locale, title=t(key), text="",
                                                            renew_token=token if exc.code == "LINK_EXPIRED" else None,
                                                            languages=Translator.languages(), code=exc.code)
            return HTMLResponse(html, status_code=exc.status, headers=headers)
        return JSONResponse(exc.body(), status_code=exc.status, headers=headers)

    # ================================================================ API
    api = APIRouter(prefix=API)

    @api.get("/requests/{token}")
    def api_get(token: str, request: Request):
        acc = access(request, token)
        return json_resp(svc.request_view(acc), acc)

    @api.put("/requests/{token}/draft")
    def api_draft(token: str, request: Request, body: DraftBody):
        acc = access(request, token)
        return json_resp(svc.save_draft(acc, body.values, body.draft_version), acc)

    @api.post("/requests/{token}/uploads")
    async def api_upload(token: str, request: Request, file: UploadFile = File(...)):
        acc = access(request, token)
        data = await file.read(settings.max_upload_bytes + 1)
        return json_resp(svc.upload(acc, file.filename or "upload", data, file.content_type), acc, 201)

    @api.post("/requests/{token}/parse/cbam-communication")
    def api_parse(token: str, request: Request, body: ParseBody):
        acc = access(request, token)
        return json_resp(svc.parse_cbam(acc, body.sha256), acc)

    @api.post("/requests/{token}/questions")
    def api_question(token: str, request: Request, body: QuestionBody):
        acc = access(request, token)
        return json_resp(svc.ask_question(acc, body.text, body.datapoint_id), acc, 201)

    @api.post("/requests/{token}/decline")
    def api_decline(token: str, request: Request, body: DeclineBody):
        acc = access(request, token)
        return json_resp(svc.decline(acc, body.reason, body.text), acc, 201)

    @api.post("/requests/{token}/submit")
    def api_submit(token: str, request: Request, body: SubmitBody, idempotency_key: str | None = Header(default=None)):
        acc = access(request, token)
        return json_resp(svc.submit(acc, attestation=body.attestation, idempotency_key=idempotency_key,
                                    draft_version=body.draft_version, values=body.values), acc, 201)

    @api.post("/requests/{token}/otp")
    def api_otp_send(token: str, request: Request):
        svc.send_otp(token, ip=client_ip(request))
        return {"sent": True}

    @api.post("/requests/{token}/otp/verify")
    def api_otp_verify(token: str, request: Request, body: OtpBody):
        cookie = svc.verify_otp(token, body.code, ip=client_ip(request))
        r = JSONResponse({"verified": True})
        th = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        r.set_cookie(_device_cookie_name(th), cookie, httponly=True, samesite="lax", secure=secure_cookie, max_age=90 * 86400)
        return r

    @api.post("/requests/{token}/pause")
    def api_pause(token: str, request: Request):
        acc = svc.resolve(token, ip=client_ip(request), check_device=False)
        svc.pause_reminders(acc)
        return {"paused": True}

    @api.post("/auth/magic-link", status_code=202)
    def api_magic_link(request: Request, body: MagicLinkBody):
        svc.reissue_link(body.email, body.token, ip=client_ip(request))
        return {"accepted": True}

    @api.get("/me/submissions")
    def api_me_submissions():
        raise PortalError(501, "NOT_IMPLEMENTED", "optional supplier accounts (FR-306) are not part of this release")

    @api.post("/me/shares")
    def api_me_shares():
        raise PortalError(501, "NOT_IMPLEMENTED", "sharing to multiple customers is V2 (FR-307)")

    @api.delete("/me/shares/{share_id}")
    def api_me_shares_delete(share_id: str):
        raise PortalError(501, "NOT_IMPLEMENTED", "sharing to multiple customers is V2 (FR-307)")

    @api.post("/manifests", status_code=201)
    def api_manifest(body: ManifestBody):
        manifest = svc.verify_manifest(body.manifest_jws)
        return svc.ingest_manifest(manifest, status=body.status, approval_ref=body.approval_ref, reissue=body.reissue)

    @api.post("/webhooks/core")
    async def api_core_webhook(request: Request):
        raw = await request.body()
        try:
            verify_webhook(settings.webhook_secret, raw, request.headers.get(WEBHOOK_HEADER))
        except SignatureError as exc:
            raise PortalError(401, exc.code, "webhook signature invalid") from exc
        import json as _json

        payload = _json.loads(raw)
        return svc.apply_core_event(payload.get("event", ""), payload.get("payload") or {})

    # ------------------------------------------------------------ admin（租户侧 / 运维）
    @api.get("/admin/templates")
    def admin_templates(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        reg = svc.registry
        return [{"id": t.id, "family": t.family, "locale": t.locale, "version": t.version,
                 "status": reg.effective_status(t.id), "reviewed_by": reg.reviewed_by(t.id), "variants": list(t.variants),
                 "template_hash": t.template_hash} for t in reg.all()]

    @api.post("/admin/templates/{template_id}/review")
    def admin_review(template_id: str, body: ReviewBody, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            svc.review_template(template_id, body.reviewer)
        except TemplateError as exc:
            raise PortalError(422, exc.code, str(exc)) from exc
        return {"id": template_id, "status": svc.registry.effective_status(template_id)}

    @api.post("/admin/templates/{template_id}/render")
    def admin_render(template_id: str, body: RenderBody, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            m = svc.registry.render(template_id, body.ctx, variant=body.variant)
        except TemplateError as exc:
            raise PortalError(422, exc.code, str(exc)) from exc
        return {"subject": m.subject, "body": m.body_text, "body_sha256": m.body_sha256, "template_hash": m.template_hash,
                "sendable": svc.registry.is_sendable(template_id)}

    @api.post("/admin/requests", status_code=201)
    def admin_create(body: AdminDraftBody, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        sreq = svc.create_draft(body.manifest, tier=body.tier, sender=body.sender, counterparty=body.counterparty)
        return sreq.model_dump(mode="json")

    @api.get("/admin/requests/{sreq_id}")
    def admin_get(sreq_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        sreq, _m, chase = svc.get_request(sreq_id)
        return {"request": sreq.model_dump(mode="json"), "chase": chase}

    @api.post("/admin/requests/{sreq_id}/send")
    def admin_send(sreq_id: str, body: AdminSendBody, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        res = svc.send_first(sreq_id, body.approval, sender_name=body.sender_name, sender_role=body.sender_role)
        return JSONResponse(res, status_code=200 if res.get("ok") else 409)

    @api.post("/admin/requests/{sreq_id}/link")
    def admin_link(sreq_id: str, reissue: bool = False, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        d = svc.current_link(sreq_id, reissue=reissue)
        d.pop("token", None)
        return d

    @api.post("/admin/requests/{sreq_id}/revoke")
    def admin_revoke(sreq_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"revoked": svc.revoke_links(sreq_id, "tenant")}

    @api.post("/admin/requests/{sreq_id}/pause-chasing")
    def admin_pause(sreq_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        svc.pause_chasing(sreq_id)
        return {"paused": True}

    @api.post("/admin/chase/tick")
    def admin_tick(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return [r.__dict__ for r in svc.chase_tick()]

    @api.post("/admin/webhooks/deliver")
    def admin_deliver(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return svc.deliver_webhooks()

    app.include_router(api)

    # ================================================================ pages
    def render(name: str, request: Request, acc: Access | None, lang: str, status: int = 200, **ctx) -> HTMLResponse:
        t = Translator(lang)
        base = {"t": t, "lang": lang, "languages": Translator.languages(), "path": request.url.path}
        if acc is not None:
            m = acc.manifest
            days = (m.due_date - date.today()).days
            base.update({
                "acc": acc, "m": m, "token": acc.token, "csrf": csrf_for(acc, request), "readonly": acc.readonly,
                "due_long": date_long(m.due_date, lang), "period_txt": period_label(m.period.start, m.period.end, lang),
                "days_left": days, "privacy_link": m.privacy_note_link,
            })
        resp = HTMLResponse(env.get_template(name).render(**base, **ctx), status_code=status)
        if acc is not None:
            bind_cookie(resp, acc)
        if request.query_params.get("lang") == lang:
            resp.set_cookie(LANG_COOKIE, lang, samesite="lax", secure=secure_cookie, max_age=365 * 86400)
        return resp

    @app.get("/r/{token}", response_class=HTMLResponse)
    def page_landing(token: str, request: Request):
        acc = access(request, token)
        return render("landing.html", request, acc, lang_for(request, acc.manifest.locale),
                      decline_reasons=DECLINE_REASONS, submissions=svc.submission_count(acc.sreq.id))

    @app.get("/r/{token}/form", response_class=HTMLResponse)
    def page_form(token: str, request: Request):
        acc = access(request, token)
        lang = lang_for(request, acc.manifest.locale)
        version, draft = svc.get_draft(acc.sreq.id)
        values = draft.get("values") or {}
        _, issues = svc._validate_fields(acc.manifest, values, strict=False)
        return render("form.html", request, acc, lang, groups=svc.form_groups(acc.manifest), values=values,
                      draft_version=version, hints=svc.hints(acc, values), issues={i.key: i.code for i in issues},
                      uploads=svc.list_uploads(acc.sreq.id), saved=request.query_params.get("saved"),
                      notice=request.query_params.get("notice"))

    def _values_from_form(form) -> dict:
        values: dict[str, dict] = {}
        for k, v in form.multi_items():
            if not isinstance(v, str):
                continue
            for prefix, field in (("v__", "value"), ("u__", "unit"), ("n__", "note")):
                if k.startswith(prefix):
                    values.setdefault(k[len(prefix):], {})[field] = v
        return values

    @app.post("/r/{token}/form")
    async def page_form_post(token: str, request: Request):
        acc = access(request, token)
        form = await request.form()
        check_csrf(acc, request, form.get("csrf"))
        dv = form.get("draft_version")
        try:
            svc.save_draft(acc, _values_from_form(form), int(dv) if dv not in (None, "") else None,
                           merge=form.get("merge") == "1")
        except PortalError as exc:
            if exc.code == "DRAFT_CONFLICT":
                return RedirectResponse(f"/r/{token}/form?notice=draft_conflict", status_code=303)
            raise
        target = "review" if form.get("action") == "review" else "form?saved=1"
        return RedirectResponse(f"/r/{token}/{target}", status_code=303)

    @app.post("/r/{token}/upload")
    async def page_upload(token: str, request: Request, csrf: str = Form(...), file: UploadFile = File(...)):
        acc = access(request, token)
        check_csrf(acc, request, csrf)
        data = await file.read(settings.max_upload_bytes + 1)
        try:
            svc.upload(acc, file.filename or "upload", data, file.content_type)
            notice = "upload_ok"
        except PortalError as exc:
            if exc.status in (413, 422):
                notice = "upload_rejected"
            else:
                raise
        return RedirectResponse(f"/r/{token}/form?notice={notice}", status_code=303)

    @app.post("/r/{token}/parse")
    async def page_parse(token: str, request: Request, csrf: str = Form(...), sha256: str = Form(...)):
        acc = access(request, token)
        check_csrf(acc, request, csrf)
        lang = lang_for(request, acc.manifest.locale)
        try:
            res = svc.parse_cbam(acc, sha256)
        except PortalError as exc:
            if exc.code == "PARSER_UNAVAILABLE":
                return RedirectResponse(f"/r/{token}/form?notice=parser_unavailable", status_code=303)
            raise
        return render("parsed.html", request, acc, lang, result=res)

    @app.get("/r/{token}/review", response_class=HTMLResponse)
    def page_review(token: str, request: Request):
        acc = access(request, token)
        lang = lang_for(request, acc.manifest.locale)
        version, draft = svc.get_draft(acc.sreq.id)
        values = draft.get("values") or {}
        _, issues = svc._validate_fields(acc.manifest, values, strict=False)
        return render("review.html", request, acc, lang, groups=svc.form_groups(acc.manifest), values=values,
                      hints=svc.hints(acc, values), issues={i.key: i.code for i in issues},
                      uploads=svc.list_uploads(acc.sreq.id), draft_version=version,
                      idem=secrets.token_urlsafe(16), n_values=sum(1 for v in values.values() if v.get("value") not in (None, "")))

    @app.post("/r/{token}/submit")
    async def page_submit(token: str, request: Request):
        acc = access(request, token)
        form = await request.form()
        check_csrf(acc, request, form.get("csrf"))
        dv = form.get("draft_version")
        try:
            rec = svc.submit(acc, attestation=form.get("attest") == "on", idempotency_key=form.get("idem") or None,
                             draft_version=int(dv) if dv else None)
        except PortalError as exc:
            if exc.code in ("ATTESTATION_REQUIRED", "VALIDATION_FAILED", "EMPTY_SUBMISSION", "DRAFT_CONFLICT"):
                return RedirectResponse(f"/r/{token}/review?error={exc.code}", status_code=303)
            raise
        return RedirectResponse(f"/r/{token}/receipt?h={rec['package_hash'].split(':', 1)[1]}", status_code=303)

    @app.get("/r/{token}/receipt", response_class=HTMLResponse)
    def page_receipt(token: str, request: Request, h: str = ""):
        acc = access(request, token)
        return render("receipt.html", request, acc, lang_for(request, acc.manifest.locale), package_hash=h)

    @app.post("/r/{token}/question")
    async def page_question(token: str, request: Request, csrf: str = Form(...), text: str = Form(...),
                            datapoint_id: str = Form(default="")):
        acc = access(request, token)
        check_csrf(acc, request, csrf)
        svc.ask_question(acc, text, datapoint_id or None)
        return render("message.html", request, acc, lang_for(request, acc.manifest.locale),
                      title=Translator(lang_for(request, acc.manifest.locale))("question_sent"), text="", back=True)

    @app.post("/r/{token}/decline")
    async def page_decline(token: str, request: Request, csrf: str = Form(...), reason: str = Form(...),
                           text: str = Form(default="")):
        acc = access(request, token)
        check_csrf(acc, request, csrf)
        svc.decline(acc, reason, text)
        lang = lang_for(request, acc.manifest.locale)
        return render("message.html", request, acc, lang,
                      title=Translator(lang)("decline_sent", customer=acc.manifest.customer_name), text="", back=True)

    @app.get("/r/{token}/verify", response_class=HTMLResponse)
    def page_verify(token: str, request: Request):
        lang = lang_for(request, settings.default_locale)
        return HTMLResponse(env.get_template("otp.html").render(t=Translator(lang), lang=lang, token=token,
                                                                languages=Translator.languages(),
                                                                sent=request.query_params.get("sent"),
                                                                error=request.query_params.get("error")))

    @app.post("/r/{token}/verify/send")
    def page_verify_send(token: str, request: Request):
        svc.send_otp(token, ip=client_ip(request))
        return RedirectResponse(f"/r/{token}/verify?sent=1", status_code=303)

    @app.post("/r/{token}/verify")
    async def page_verify_post(token: str, request: Request, code: str = Form(...)):
        try:
            cookie = svc.verify_otp(token, code, ip=client_ip(request))
        except PortalError as exc:
            if exc.code == "OTP_INVALID":
                return RedirectResponse(f"/r/{token}/verify?error=1", status_code=303)
            raise
        r = RedirectResponse(f"/r/{token}", status_code=303)
        th = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        r.set_cookie(_device_cookie_name(th), cookie, httponly=True, samesite="lax", secure=secure_cookie, max_age=90 * 86400)
        return r

    @app.get("/r/{token}/renew", response_class=HTMLResponse)
    def page_renew(token: str, request: Request):
        lang = lang_for(request, settings.default_locale)
        return HTMLResponse(env.get_template("renew.html").render(t=Translator(lang), lang=lang, token=token,
                                                                  languages=Translator.languages(), done=False))

    @app.post("/r/{token}/renew", response_class=HTMLResponse)
    def page_renew_post(token: str, request: Request, email: str = Form(...)):
        svc.reissue_link(email, token, ip=client_ip(request))
        lang = lang_for(request, settings.default_locale)
        return HTMLResponse(env.get_template("renew.html").render(t=Translator(lang), lang=lang, token=token,
                                                                  languages=Translator.languages(), done=True))

    @app.get("/pause/{token}", response_class=HTMLResponse)
    def page_pause(token: str, request: Request):
        acc = svc.resolve(token, ip=client_ip(request), check_device=False)
        lang = lang_for(request, acc.manifest.locale)
        t = Translator(lang)
        csrf = hmac.new(settings.instance_secret, f"pause|{acc.token_hash}".encode(), hashlib.sha256).hexdigest()[:32]
        return HTMLResponse(env.get_template("pause.html").render(t=t, lang=lang, token=token, csrf=csrf, done=False,
                                                                  m=acc.manifest, languages=Translator.languages()))

    @app.post("/pause/{token}", response_class=HTMLResponse)
    def page_pause_post(token: str, request: Request, csrf: str = Form(...)):
        acc = svc.resolve(token, ip=client_ip(request), check_device=False)
        expected = hmac.new(settings.instance_secret, f"pause|{acc.token_hash}".encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(csrf, expected):
            raise PortalError(403, "CSRF_INVALID", "invalid form token")
        svc.pause_reminders(acc)
        lang = lang_for(request, acc.manifest.locale)
        return HTMLResponse(env.get_template("pause.html").render(t=Translator(lang), lang=lang, token=token, csrf=csrf,
                                                                  done=True, m=acc.manifest, languages=Translator.languages()))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": __version__, "time": datetime.now(timezone.utc).isoformat()}

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        lang = lang_for(request, settings.default_locale)
        return HTMLResponse(env.get_template("message.html").render(
            t=Translator(lang), lang=lang, languages=Translator.languages(), title=Translator(lang)("footer_privacy"),
            text="belegt-supplier stores only contact name, e-mail and the requested data points (08 §7.3). No tracking.", code=None))

    return app
