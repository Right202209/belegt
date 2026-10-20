# Spec: 08 §3.1（模板 ID 与版本化、status/reviewed_by/glossary_version）、§3.2（变量规范与模板 lint）、§3.4（回退与审校规则）
"""消息模板库：`stpl:<family>.<locale>@<semver>`，YAML + Jinja2。

- 模板源：包内 `data/templates/<family>/<locale>.yaml`（可追加外部目录）。
- 审核状态：`draft|reviewed|deprecated`；只有 `reviewed` 才能外发（`egress` 校验 `EGRESS_DENIED_TEMPLATE_UNREVIEWED`）。
- en 主模板 MAJOR 高于某 locale 时，该 locale 有效状态自动降为 `draft`（§3.4 规则 3）。
- 渲染：StrictUndefined；变量先按 §3.2 校验类型/约束，再本地化格式化；渲染后正文 ≤ 6000 字符。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml
from jinja2 import Environment, StrictUndefined, meta
from jinja2.exceptions import UndefinedError

from . import localefmt
from .catalog import resolve as resolve_datapoints
from .ids import short_code

TemplateStatus = Literal["draft", "reviewed", "deprecated"]
STPL_RE = re.compile(r"^stpl:(?P<family>[a-z0-9_]+)\.(?P<locale>[a-z]{2,3}(?:-[A-Z]{2})?)@(?P<version>\d+\.\d+\.\d+)$")

# 08 §3.2 变量全集
SPEC_VARIABLES = frozenset({
    "supplier_name", "contact_name", "customer_name", "product_list", "product_summary", "datapoint_list",
    "period_label", "due_date", "portal_link", "link_expires_at", "sender_name", "sender_role", "request_ref",
    "privacy_note_link", "custom_note", "reg",
})
# 族级扩展变量（08 §4.5 内部通知示例用 calc；certificate_renewal 为本包扩展族）
FAMILY_EXTRA_VARIABLES: dict[str, frozenset[str]] = {
    "chase_escalation_internal": frozenset({"calc", "reminder_count"}),
    "certificate_renewal": frozenset({"evidence_label", "valid_until"}),
}
URL_VARS = ("portal_link", "privacy_note_link")
DATE_VARS = ("due_date", "link_expires_at", "valid_until")
MAX_RENDERED_CHARS = 6000
MAX_PRODUCT_ROWS = 20
MAX_SUMMARY_CHARS = 60
MAX_CUSTOM_NOTE = 500
URL_IN_TEXT_RE = re.compile(r"(https?://|www\.)[^\s)>\]]+", re.I)
# 硬编码法规常量的检测（lint）：截止日或加价比例字面量
HARDCODED_REG_RE = re.compile(r"2027-09-30|30\.09\.2027|\b(?:10|20|30)\s?%|%(?:10|20|30)\b")
UNTRANSLATED_MARK = " [EN]"
FALLBACK_LOCALES = {"it", "pl", "hi", "vi"}


class TemplateError(Exception):
    def __init__(self, code: str, message: str, template_id: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.template_id = template_id


@dataclass(frozen=True)
class MessageTemplate:
    id: str
    family: str
    locale: str
    version: str
    status: TemplateStatus
    reviewed_by: str | None
    glossary_version: str | None
    external: bool
    required: tuple[str, ...]
    optional: tuple[str, ...]
    variants: dict[str, dict[str, str]]  # variant -> {subject, body}；单体模板 variant="default"
    source: str = ""

    @property
    def major(self) -> int:
        return int(self.version.split(".")[0])

    @property
    def template_hash(self) -> str:
        payload = json.dumps({"id": self.id, "variants": self.variants, "required": list(self.required),
                              "optional": list(self.optional)}, sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def declared(self) -> frozenset[str]:
        return frozenset(self.required) | frozenset(self.optional) | {"reg"}


@dataclass
class RenderedMessage:
    template_id: str
    template_hash: str
    variant: str
    locale: str
    subject: str
    body_text: str
    body_html: str
    body_sha256: str
    variables_sha256: str
    links: list[str]
    fallback_from: str | None = None
    warnings: list[str] = field(default_factory=list)


def _load_reg_constants() -> dict[str, Any]:
    return yaml.safe_load(resources.files("belegt_supplier").joinpath("data/regulatory_constants.yaml").read_text("utf-8"))


def parse_template_yaml(text: str, source: str = "") -> MessageTemplate:
    raw = yaml.safe_load(text)
    tid = raw["id"]
    m = STPL_RE.match(tid)
    if not m:
        raise TemplateError("TEMPLATE_ID_INVALID", tid, tid)
    if (m.group("family"), m.group("locale"), m.group("version")) != (raw["family"], raw["locale"], str(raw["version"])):
        raise TemplateError("TEMPLATE_ID_MISMATCH", "id does not match family/locale/version", tid)
    if raw.get("status") not in ("draft", "reviewed", "deprecated"):
        raise TemplateError("TEMPLATE_STATUS_INVALID", str(raw.get("status")), tid)
    if "variants" in raw:
        variants = {k: {"subject": v["subject"], "body": v["body"]} for k, v in raw["variants"].items()}
    else:
        variants = {"default": {"subject": raw["subject"], "body": raw["body"]}}
    var = raw.get("variables") or {}
    return MessageTemplate(
        id=tid, family=raw["family"], locale=raw["locale"], version=str(raw["version"]),
        status=raw["status"], reviewed_by=raw.get("reviewed_by"), glossary_version=raw.get("glossary_version"),
        external=bool(raw.get("external", True)), required=tuple(var.get("required") or ()),
        optional=tuple(var.get("optional") or ()), variants=variants, source=source,
    )


class TemplateRegistry:
    """模板注册表。`review()` 的结果覆盖文件中的 status（可由宿主持久化，见 store.TemplateReviewRow）。"""

    def __init__(self, templates: Iterable[MessageTemplate] = (), reg_constants: dict[str, Any] | None = None,
                 review_overrides: dict[str, tuple[TemplateStatus, str | None]] | None = None) -> None:
        self._t: dict[str, MessageTemplate] = {}
        for t in templates:
            self.add(t)
        self.reg_constants = reg_constants or _load_reg_constants()
        self.overrides: dict[str, tuple[TemplateStatus, str | None]] = dict(review_overrides or {})
        self._env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=False)
        self._html_env = Environment(undefined=StrictUndefined, autoescape=True)

    # ------------------------------------------------------------ loading
    @classmethod
    def load_builtin(cls, extra_dirs: Iterable[Path] = ()) -> "TemplateRegistry":
        reg = cls()
        root = resources.files("belegt_supplier").joinpath("data/templates")
        for fam in sorted(root.iterdir(), key=lambda p: p.name):
            if not fam.is_dir():
                continue
            for f in sorted(fam.iterdir(), key=lambda p: p.name):
                if f.name.endswith(".yaml"):
                    reg.add(parse_template_yaml(f.read_text("utf-8"), source=f"builtin:{fam.name}/{f.name}"))
        for d in extra_dirs:
            for f in sorted(Path(d).glob("*/*.yaml")):
                reg.add(parse_template_yaml(f.read_text("utf-8"), source=str(f)))
        return reg

    def add(self, t: MessageTemplate) -> None:
        self._t[t.id] = t

    def all(self) -> list[MessageTemplate]:
        return sorted(self._t.values(), key=lambda t: t.id)

    def get(self, template_id: str) -> MessageTemplate:
        try:
            return self._t[template_id]
        except KeyError:
            raise TemplateError("TEMPLATE_UNREGISTERED", template_id, template_id) from None

    def is_registered(self, template_id: str, template_hash: str | None = None) -> bool:
        t = self._t.get(template_id)
        return t is not None and (template_hash is None or t.template_hash == template_hash)

    # ------------------------------------------------------------ review status
    def effective_status(self, template_id: str) -> TemplateStatus:
        t = self.get(template_id)
        status, _by = self.overrides.get(template_id, (t.status, t.reviewed_by))
        if status == "reviewed" and t.locale != "en":
            en = self._latest(t.family, "en")
            if en is not None and en.major > t.major:
                return "draft"  # §3.4 规则 3
        return status

    def reviewed_by(self, template_id: str) -> str | None:
        t = self.get(template_id)
        return self.overrides.get(template_id, (t.status, t.reviewed_by))[1]

    def review(self, template_id: str, reviewer: str) -> None:
        """审校通过（人工动作）。lint 不通过的模板不得标为 reviewed。"""
        issues = [i for i in self.lint(template_id) if i.severity == "error"]
        if issues:
            raise TemplateError("TEMPLATE_LINT_FAILED", "; ".join(i.message for i in issues), template_id)
        if not reviewer:
            raise TemplateError("TEMPLATE_REVIEWER_REQUIRED", "reviewer required", template_id)
        self.overrides[template_id] = ("reviewed", reviewer)

    def deprecate(self, template_id: str) -> None:
        self.overrides[template_id] = ("deprecated", self.reviewed_by(template_id))

    def is_sendable(self, template_id: str) -> bool:
        return template_id in self._t and self.effective_status(template_id) == "reviewed"

    # ------------------------------------------------------------ resolution
    def _latest(self, family: str, locale: str) -> MessageTemplate | None:
        cands = [t for t in self._t.values() if t.family == family and t.locale == locale]
        if not cands:
            return None
        return max(cands, key=lambda t: tuple(int(x) for x in t.version.split(".")))

    def resolve(self, family: str, locale: str, *, require_reviewed: bool = True) -> tuple[MessageTemplate, str | None]:
        """按 family + locale 选模板；返回 (模板, fallback_from)。

        - 有该 locale 且（需要时）已审校 → 用之；
        - 否则回退 en（§3.4：it/pl/hi/vi 未审校时发 en；其他缺译同样回退）。
        """
        loc = locale.split("-")[0].lower()
        t = self._latest(family, loc)
        if t is not None and (not require_reviewed or self.effective_status(t.id) == "reviewed"):
            return t, None
        en = self._latest(family, "en")
        if en is None:
            raise TemplateError("TEMPLATE_UNREGISTERED", f"{family}.{loc}")
        return en, loc if loc != "en" else None

    # ------------------------------------------------------------ lint
    @dataclass
    class LintIssue:
        template_id: str
        severity: Literal["error", "warning"]
        code: str
        message: str

    def _used_vars(self, t: MessageTemplate) -> set[str]:
        used: set[str] = set()
        for v in t.variants.values():
            for part in (v["subject"], v["body"]):
                used |= meta.find_undeclared_variables(self._env.parse(part))
        return used

    def lint(self, template_id: str | None = None) -> list["TemplateRegistry.LintIssue"]:
        issues: list[TemplateRegistry.LintIssue] = []
        targets = [self.get(template_id)] if template_id else self.all()
        for t in targets:
            allowed = SPEC_VARIABLES | FAMILY_EXTRA_VARIABLES.get(t.family, frozenset())
            undeclared_spec = t.declared - allowed
            if undeclared_spec:
                issues.append(self.LintIssue(t.id, "error", "VAR_NOT_IN_SPEC", f"variables not in 08 §3.2: {sorted(undeclared_spec)}"))
            try:
                used = self._used_vars(t)
            except Exception as exc:  # noqa: BLE001
                issues.append(self.LintIssue(t.id, "error", "JINJA_SYNTAX", str(exc)))
                continue
            if used - t.declared:
                issues.append(self.LintIssue(t.id, "error", "VAR_UNDECLARED", f"undeclared placeholders: {sorted(used - t.declared)}"))
            missing_req = set(t.required) - used
            if missing_req:
                issues.append(self.LintIssue(t.id, "error", "VAR_REQUIRED_UNUSED", f"required but unused: {sorted(missing_req)}"))
            for name, v in t.variants.items():
                if HARDCODED_REG_RE.search(v["body"]) or HARDCODED_REG_RE.search(v["subject"]):
                    issues.append(self.LintIssue(t.id, "error", "REG_HARDCODED", f"variant {name}: regulatory constant hard-coded"))
                if "portal_link" in t.declared and "{{ portal_link }}" not in v["body"] and t.external:
                    issues.append(self.LintIssue(t.id, "error", "PORTAL_LINK_MISSING", f"variant {name}"))
                if "portal_link" in v["subject"] or "custom_note" in v["subject"]:
                    issues.append(self.LintIssue(t.id, "error", "VAR_POSITION", f"variant {name}: link/custom note not allowed in subject"))
            # 跨 locale：同 family 同 MAJOR 的占位符集合一致（§3.2 "每个 locale 中出现"）
            for other in self._t.values():
                if other.family == t.family and other.id != t.id and other.major == t.major:
                    if set(other.variants) != set(t.variants):
                        issues.append(self.LintIssue(t.id, "error", "VARIANT_MISMATCH", f"variants differ from {other.id}"))
                    elif self._used_vars(other) != used:
                        issues.append(self.LintIssue(t.id, "warning", "LOCALE_VAR_DIFF", f"placeholders differ from {other.id}"))
        return issues

    # ------------------------------------------------------------ rendering
    def build_variables(self, t: MessageTemplate, ctx: dict[str, Any], locale: str) -> dict[str, Any]:
        """把原始上下文按 §3.2 校验并格式化为模板变量。"""
        errors: list[str] = []
        out: dict[str, Any] = {}
        loc = localefmt.base_locale(locale)

        def text(name: str) -> None:
            v = ctx.get(name)
            if v is None or v == "":
                if name in t.required:
                    errors.append(f"{name}: required")
                elif name in t.declared:
                    out[name] = None
                return
            if not isinstance(v, str):
                errors.append(f"{name}: must be text")
                return
            out[name] = v

        for name in ("supplier_name", "customer_name", "sender_name", "sender_role", "contact_name", "evidence_label"):
            if name in t.declared:
                text(name)

        if "request_ref" in t.declared:
            rr = ctx.get("request_ref") or (short_code(ctx["sreq_id"]) if ctx.get("sreq_id") else None)
            if not rr:
                errors.append("request_ref: required")
            out["request_ref"] = rr

        for name in URL_VARS:
            if name in t.declared:
                v = ctx.get(name)
                if not v:
                    errors.append(f"{name}: required")
                elif not re.match(r"^https://[^\s]+$|^http://(localhost|127\.0\.0\.1)(:\d+)?/[^\s]*$", str(v)):
                    errors.append(f"{name}: must be an https URL")
                out[name] = v

        for name in DATE_VARS:
            if name in t.declared:
                v = ctx.get(name)
                if isinstance(v, datetime):
                    v = v.date()
                if isinstance(v, str):
                    try:
                        v = date.fromisoformat(v)
                    except ValueError:
                        errors.append(f"{name}: must be ISO date")
                        continue
                if not isinstance(v, date):
                    if name in t.required:
                        errors.append(f"{name}: required date")
                    continue
                out[name] = localefmt.date_long(v, loc)

        if "period_label" in t.declared:
            if ctx.get("period_label"):
                out["period_label"] = ctx["period_label"]
            elif ctx.get("period"):
                p = ctx["period"]
                s = p["start"] if isinstance(p, dict) else p.start
                e = p["end"] if isinstance(p, dict) else p.end
                s = date.fromisoformat(s) if isinstance(s, str) else s
                e = date.fromisoformat(e) if isinstance(e, str) else e
                out["period_label"] = localefmt.period_label(s, e, loc)
            else:
                errors.append("period_label: required")

        products = ctx.get("products") or []
        if "product_list" in t.declared:
            if not products:
                errors.append("product_list: at least one product")
            lines = []
            for p in products[:MAX_PRODUCT_ROWS]:
                line = f"- {p['name']}"
                if p.get("cn_code"):
                    line += f" (CN {p['cn_code']})"
                if p.get("net_mass"):
                    line += f", {localefmt.mass(str(p['net_mass']), p.get('net_mass_unit') or 't', loc)}"
                lines.append(line)
            if len(products) > MAX_PRODUCT_ROWS:
                lines.append(localefmt.fold_note(len(products), loc))
            out["product_list"] = "\n".join(lines)
        if "product_summary" in t.declared:
            summ = ctx.get("product_summary")
            if not summ and products:
                first = products[0]
                summ = first["name"] + (f" (CN {first['cn_code']})" if first.get("cn_code") else "")
                if len(products) > 1:
                    summ += f" +{len(products) - 1}"
            if not summ:
                errors.append("product_summary: required")
            elif len(summ) > MAX_SUMMARY_CHARS:
                summ = summ[: MAX_SUMMARY_CHARS - 1] + "…"
            out["product_summary"] = summ

        if "datapoint_list" in t.declared:
            dps = ctx.get("datapoints") or []
            if not dps:
                errors.append("datapoint_list: at least one datapoint")
            lines = []
            for spec in resolve_datapoints(list(dps)):
                label, fb = spec.label_for(loc)
                unit = f" [{spec.unit}]" if spec.unit else ""
                lines.append(f"- {label}{unit}{UNTRANSLATED_MARK if fb and loc != 'en' else ''}")
            out["datapoint_list"] = "\n".join(lines)

        if "custom_note" in t.declared:
            note = ctx.get("custom_note")
            if note:
                if len(note) > MAX_CUSTOM_NOTE:
                    errors.append("custom_note: > 500 chars")
                if URL_IN_TEXT_RE.search(note):
                    errors.append("custom_note: links not allowed")
                if not ctx.get("custom_note_confirmed_by"):
                    errors.append("custom_note: requires human confirmation (custom_note_confirmed_by)")
            out["custom_note"] = note or None

        if "reminder_count" in t.declared:
            out["reminder_count"] = str(ctx.get("reminder_count", 0))
        if "calc" in t.declared:
            calc = ctx.get("calc")
            if not isinstance(calc, dict) or not calc.get("result") or not calc.get("ref"):
                errors.append("calc: requires {result, low, high, ref} from a Calculation (R-NUM)")
            else:
                out["calc"] = {k: str(calc.get(k, "—")) for k in ("result", "low", "high", "ref")}

        rc = self.reg_constants
        app_date = rc["eudr"]["application_date_micro_small" if ctx.get("eudr_micro_small") else "application_date_large"]
        out["reg"] = {
            "cbam": {
                "declaration_deadline": localefmt.date_long(rc["cbam"]["declaration_deadline"], loc),
                "markup_2026": localefmt.percent(str(rc["cbam"]["markup_2026"]), loc),
                "markup_2027": localefmt.percent(str(rc["cbam"]["markup_2027"]), loc),
            },
            "eudr": {"application_date": localefmt.date_long(app_date, loc)},
        }
        if errors:
            raise TemplateError("TEMPLATE_VARIABLES_INVALID", "; ".join(errors), t.id)
        return out

    def render(self, template_id: str, ctx: dict[str, Any], *, variant: str | None = None,
               allowed_link_domains: Iterable[str] | None = None) -> RenderedMessage:
        t = self.get(template_id)
        vname = variant or ("default" if "default" in t.variants else next(iter(t.variants)))
        if vname not in t.variants:
            raise TemplateError("TEMPLATE_VARIANT_UNKNOWN", vname, template_id)
        variables = self.build_variables(t, ctx, t.locale)
        v = t.variants[vname]
        try:
            subject = self._env.from_string(v["subject"]).render(**variables).strip()
            body = self._env.from_string(v["body"]).render(**variables).strip() + "\n"
            body = re.sub(r"\n{3,}", "\n\n", body)
            html_vars = dict(variables)
            html_body = self._html_env.from_string(v["body"]).render(**html_vars).strip()
        except UndefinedError as exc:
            raise TemplateError("TEMPLATE_VARIABLES_INVALID", str(exc), template_id) from exc
        if len(body) > MAX_RENDERED_CHARS:
            raise TemplateError("TEMPLATE_TOO_LONG", f"{len(body)} > {MAX_RENDERED_CHARS}", template_id)
        links = URL_IN_TEXT_RE.findall(body) and [m.group(0) for m in URL_IN_TEXT_RE.finditer(body)]
        links = list(links or [])
        if allowed_link_domains is not None:
            allowed = {d.lower() for d in allowed_link_domains}
            for link in links:
                host = re.sub(r"^https?://", "", link, flags=re.I).split("/")[0].split(":")[0].lower()
                if not any(host == d or host.endswith("." + d) for d in allowed):
                    raise TemplateError("TEMPLATE_LINK_DOMAIN", f"link domain not allowed: {host}", template_id)
        html = "<div style=\"white-space:pre-wrap;font-family:sans-serif\">" + html_body + "</div>"
        var_snapshot = json.dumps({k: variables[k] for k in sorted(variables) if k != "portal_link"},
                                  sort_keys=True, ensure_ascii=False, default=str)
        return RenderedMessage(
            template_id=t.id, template_hash=t.template_hash, variant=vname, locale=t.locale, subject=subject,
            body_text=body, body_html=html, body_sha256="sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
            variables_sha256="sha256:" + hashlib.sha256(var_snapshot.encode("utf-8")).hexdigest(), links=links,
        )
