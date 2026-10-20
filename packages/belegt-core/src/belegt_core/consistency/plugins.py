# Spec: 06 §1.2（确定性 Python 插件：只读入参、不访问网络与图写入）、§2.1（通用约定）、§2.2（CR-001…CR-043 输入与判定）
"""规则插件：为每条规则选择输入集并规范化（单位换算、期间/口径归一），产出 ``Binding``；
判定本身由规则 YAML 中的 CEL ``assert`` 完成（插件只给出变量）。

插件约定：``fn(ctx: AuditContext, scope: Scope) -> Iterable[Binding]``。
- 业务上输入不存在（如未回答 Scope 2 不检 CR-027）→ 不产出 Binding 或产出 ``not_evaluable``（通过并留痕）；
- 输入应存在却读不到（引用解析失败）→ ``error``（引擎按 fail-closed 记 ``unknown``，06 §1.4）。
"""
from __future__ import annotations

import datetime as dt
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterable, Iterator

from ..canonical import normalize_dec
from ..ids import datapoint_base
from ..legal.flags import question_legal_flag
from ..rules.numtext import _readings, add_text_tokens, all_tokens, allowed_numbers, numeric_tokens  # noqa: F401
from .cel import CelError, compile_expr
from .context import (AuditContext, EFFECTIVE_FINAL, months_in, parse_date, ref_of, same_key_wo, split_ref,
                      tag_map)


# ====================================================================== 数据结构
@dataclass
class Scope:
    """求值范围：``request_ids``/``answer_ids`` 为 None 表示全租户（T3）。"""

    trigger: str = "daily_batch"
    request_ids: set[str] | None = None
    answer_ids: set[str] | None = None
    evidence_ids: set[str] | None = None

    def answer_in(self, ctx: AuditContext, a: dict[str, Any]) -> bool:
        if self.answer_ids is not None:
            return a["id"] in self.answer_ids
        if self.request_ids is not None:
            return bool(ctx.request_ids_of(a) & self.request_ids)
        return True

    def request_in(self, rid: str) -> bool:
        return self.request_ids is None or rid in self.request_ids


@dataclass
class Binding:
    vars: dict[str, Any]
    subjects: list[str]
    datapoint_id: str | None
    detail: dict[str, Any]
    request_id: str | None = None
    severity: str | None = None  # 覆盖规则严重度（字典派生实例）
    instance_id: str | None = None  # CR-003:dp:... / CR-033:dp:...
    assert_override: str | None = None  # 字典派生 CEL（CR-033）
    not_evaluable: str | None = None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


Plugin = Callable[[AuditContext, Scope], Iterable[Binding]]
PLUGINS: dict[str, Plugin] = {}


def plugin(name: str) -> Callable[[Plugin], Plugin]:
    def deco(fn: Plugin) -> Plugin:
        PLUGINS[name] = fn
        return fn

    return deco


# ====================================================================== 辅助
def dstr(d: Decimal) -> str:
    out = normalize_dec(format(d, "f"))
    return out if isinstance(out, str) else format(d, "f")


def norm_value(ctx: AuditContext, a: dict[str, Any]) -> str | None:
    """规范化值（数字：canonical unit 下的 DEC-STR；bool/enum/date/text：规范字符串）。file/geo 返回 None。"""
    vt = a.get("value_type")
    v = a.get("value")
    if vt == "number":
        c = ctx.canonical(a)
        return dstr(c[0]) if c else None
    if vt == "bool":
        return "true" if v is True else "false" if v is False else str(v).lower()
    if vt == "date":
        return str(v)[:10]
    if vt == "enum":
        return str(v)
    if vt == "text":
        return " ".join(unicodedata.normalize("NFC", str(v)).split())
    return None


def dv(ctx: AuditContext, obj: dict[str, Any], value: Any = None, unit: str | None = None, *,
       code: dict[str, str] | None = None) -> dict[str, Any]:
    """Conflict.detail.values[] 元素：数值写 DEC-STR；非数值写序号码（detail.observed 保留原值）。"""
    out: dict[str, Any] = {"ref": ref_of(obj)}
    if value is None:
        c = ctx.canonical(obj) if obj.get("value_type") == "number" else None
        if c is not None:
            value, unit = c[0], c[1]
        elif (obj.get("id") or "").startswith(("ev_", "calc_")) and ctx.numeric(obj):
            value, unit = ctx.numeric(obj)
    if isinstance(value, Decimal):
        out["value"] = dstr(value)
    elif isinstance(value, (int,)) and not isinstance(value, bool):
        out["value"] = str(value)
    elif isinstance(value, str) and isinstance(normalize_dec(value), str) and re.fullmatch(r"-?\d+(\.\d+)?", value):
        out["value"] = normalize_dec(value)
    else:
        key = norm_value(ctx, obj) if value is None else str(value)
        code = code if code is not None else {}
        out["value"] = code.setdefault(str(key), str(len(code) + 1))
    if unit:
        out["unit"] = unit
    if obj.get("customer_scope") is not None and (obj.get("id") or "").startswith("ans_"):
        out["customer_scope"] = obj["customer_scope"]
    return out


def observed(objs: Iterable[dict[str, Any]], ctx: AuditContext) -> list[dict[str, Any]]:
    out = []
    for o in objs:
        v = o.get("value") if "value" in o else o.get("extracted_value")
        out.append({"ref": ref_of(o), "value": v if isinstance(v, (str, bool, int)) else str(v)})
    return out


def working(a: dict[str, Any]) -> bool:
    return a.get("status") in ("draft", "in_review", "approved")


def req_id_of(ctx: AuditContext, a: dict[str, Any]) -> str | None:
    r = ctx.request_of(a)
    return r["id"] if r else None


def subjects_answers(ctx: AuditContext, scope: Scope, pred: Callable[[dict[str, Any]], bool] = working) -> list[dict[str, Any]]:
    return [a for a in ctx.heads("Answer") if pred(a) and scope.answer_in(ctx, a)]


def effective(ctx: AuditContext, scope: Scope) -> list[dict[str, Any]]:
    """比较池：全租户有效答案（approved/submitted + 范围内 Request 的 draft/in_review，06 §2.1）。"""
    if scope.request_ids is None and scope.answer_ids is None:
        return ctx.effective_answers(None)
    reqs = set(scope.request_ids or ())
    scoped = [a for a in ctx.heads("Answer") if scope.answer_ids and a["id"] in scope.answer_ids]
    for a in scoped:
        reqs |= ctx.request_ids_of(a)
    pool = ctx.effective_answers(reqs)
    ids = {a["id"] for a in pool}
    pool += [a for a in scoped if a["id"] not in ids and working(a)]
    return pool


def prev_period(ctx: AuditContext, a: dict[str, Any], pool: list[dict[str, Any]]) -> dict[str, Any] | None:
    cands = [b for b in pool if b["id"] != a["id"] and b.get("value_type") == "number" and same_key_wo(a, b, "period")
             and b["period"]["end"] < a["period"]["start"]]
    return max(cands, key=lambda b: b["period"]["end"]) if cands else None


def other_customers(ctx: AuditContext, a: dict[str, Any], pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in pool if b["id"] != a["id"] and b.get("value_type") == a.get("value_type")
            and same_key_wo(a, b, "customer_scope") and b.get("customer_scope") != a.get("customer_scope")]


def divergence_recorded(ctx: AuditContext, a: dict[str, Any], b: dict[str, Any]) -> bool:
    """存在 accepted_divergence（ADR/1）且两侧钉住的正是当前版本、review_by 未过（06 §3.3-4）。"""
    want = {ref_of(a), ref_of(b)}
    for c in ctx.heads("Conflict", status="accepted_divergence"):
        div = ((c.get("resolution") or {}).get("divergence")) or {}
        sides = {s.get("answer") for s in div.get("sides") or []}
        if want <= sides:
            rb = parse_date(div.get("review_by"))
            if rb is None or rb >= ctx.today:
                return True
    return False


def cited(ctx: AuditContext, a: dict[str, Any]) -> list[tuple[str, dict[str, Any] | None]]:
    return [(r, ctx.get(r)) for r in ctx.cited_evidence(a)]


def bind(vars: dict[str, Any], subjects: list[dict[str, Any] | str], dp: str | None, detail: dict[str, Any], **kw: Any) -> Binding:
    subs: list[str] = []
    for s in subjects:
        r = s if isinstance(s, str) else ref_of(s)
        if r not in subs:
            subs.append(r)
    return Binding(vars=vars, subjects=subs, datapoint_id=dp, detail=detail, **kw)


# ====================================================================== 跨客户
@plugin("cr_001")
def cr_001(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = [a for a in effective(ctx, scope) if a.get("value_type") == "number"]
    seen: set[tuple[str, str]] = set()
    for a in pool:
        if not scope.answer_in(ctx, a):
            continue
        for b in other_customers(ctx, a, pool):
            k = tuple(sorted((a["id"], b["id"])))
            if k in seen:
                continue
            seen.add(k)
            ca, cb = ctx.canonical(a), ctx.canonical(b)
            if ca is None or cb is None:
                continue  # CR-018
            x, y = sorted([a, b], key=lambda o: o["id"])
            cx, cy = (ca, cb) if x is a else (cb, ca)
            yield bind({"a": {"value": dstr(cx[0])}, "b": {"value": dstr(cy[0])},
                        "divergence_recorded": divergence_recorded(ctx, a, b)},
                       [x, y], x["datapoint_id"],
                       {"values": [dv(ctx, x), dv(ctx, y)], "delta": dstr(abs(cx[0] - cy[0]))},
                       request_id=req_id_of(ctx, a))


@plugin("cr_007")
def cr_007(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = [a for a in effective(ctx, scope) if a.get("value_type") in ("bool", "enum", "date", "text")]
    seen: set[tuple[str, str]] = set()
    for a in pool:
        if not scope.answer_in(ctx, a):
            continue
        for b in other_customers(ctx, a, pool):
            k = tuple(sorted((a["id"], b["id"])))
            if k in seen:
                continue
            seen.add(k)
            x, y = sorted([a, b], key=lambda o: o["id"])
            code: dict[str, str] = {}
            yield bind({"a": {"norm": norm_value(ctx, x)}, "b": {"norm": norm_value(ctx, y)},
                        "divergence_recorded": divergence_recorded(ctx, a, b)},
                       [x, y], x["datapoint_id"],
                       {"values": [dv(ctx, x, code=code), dv(ctx, y, code=code)], "value_encoding": "ordinal",
                        "observed": observed([x, y], ctx)}, request_id=req_id_of(ctx, a))


@plugin("cr_008")
def cr_008(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for req in ctx.heads("Request"):
        if not scope.request_in(req["id"]):
            continue
        groups: dict[str, list[dict[str, Any]]] = {}
        for a in ctx.request_answers(req["id"]):
            if working(a):
                groups.setdefault(ctx.answer_key(a), []).append(a)
        for key, answers in groups.items():
            if len(answers) < 2:
                continue
            vals = {norm_value(ctx, a) for a in answers}
            code: dict[str, str] = {}
            yield bind({"distinct_values": len(vals)}, answers, answers[0]["datapoint_id"],
                       {"values": [dv(ctx, a, code=code) if a.get("value_type") != "number" else dv(ctx, a) for a in answers],
                        "observed": observed(answers, ctx)}, request_id=req["id"])


_S2 = ("dp:env.ghg.scope2_location_based", "dp:env.ghg.scope2_market_based")


@plugin("cr_009")
def cr_009(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    hist = [a for a in ctx.heads("Answer") if a.get("status") == "submitted"]
    for a in subjects_answers(ctx, scope, lambda x: x.get("status") in ("draft", "in_review", "approved")):
        base = datapoint_base(a["datapoint_id"])
        family = _S2 if base in _S2 else (base,)
        for cp in sorted(ctx.customers_of(a)):
            cands = [h for h in hist if datapoint_base(h["datapoint_id"]) in family and cp in ctx.customers_of(h)
                     and h["period"]["end"] < a["period"]["start"] and h["boundary"].split(":")[0] == a["boundary"].split(":")[0]]
            if not cands:
                continue
            last = max(cands, key=lambda h: h["period"]["end"])
            meth = lambda o: datapoint_base(o["datapoint_id"]) if base in _S2 else ""  # noqa: E731
            code: dict[str, str] = {}
            yield bind({"last": {"boundary": last["boundary"], "consolidation": last.get("consolidation") or "",
                                 "method": meth(last)},
                        "cur": {"boundary": a["boundary"], "consolidation": a.get("consolidation") or "", "method": meth(a)}},
                       [last, a], a["datapoint_id"],
                       {"values": [dv(ctx, last, code=code), dv(ctx, a, code=code)], "customer": cp,
                        "basis": [{"ref": ref_of(o), "boundary": o["boundary"], "consolidation": o.get("consolidation")}
                                  for o in (last, a)]},
                       request_id=req_id_of(ctx, a))


# ====================================================================== 跨时间
def _theta(ctx: AuditContext, a: dict[str, Any]) -> Decimal:
    return ctx.config.yoy_overrides.get(datapoint_base(a["datapoint_id"]), ctx.config.yoy_change_ratio)


@plugin("cr_002")
def cr_002(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = effective(ctx, scope)
    for a in subjects_answers(ctx, scope):
        if a.get("value_type") != "number":
            continue
        p = prev_period(ctx, a, pool)
        ca = ctx.canonical(a)
        cp = ctx.canonical(p) if p else None
        if p is None or ca is None or cp is None:
            continue
        theta = _theta(ctx, a)
        detail = {"values": [dv(ctx, p), dv(ctx, a)], "threshold": dstr(theta)}
        if cp[0] != 0:
            detail["delta_ratio"] = dstr(((ca[0] / cp[0]) - 1).quantize(Decimal("0.0001")))
        yield bind({"cur": {"value": dstr(ca[0])}, "prev": {"value": dstr(cp[0])}, "theta": dstr(theta)},
                   [p, a], a["datapoint_id"], detail, request_id=req_id_of(ctx, a))


@plugin("cr_005")
def cr_005(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    hist = [h for h in ctx.heads("Answer") if h.get("status") == "submitted"]
    for a in subjects_answers(ctx, scope, lambda x: x.get("status") in ("draft", "in_review", "approved")):
        key = ctx.answer_key(a)
        for h in hist:
            if h["id"] == a["id"] or ctx.answer_key(h) != key:
                continue
            correction = bool((a.get("supersedes") and split_ref(a["supersedes"])[0] == h["id"])
                              or "correction" in tag_map(a))
            code: dict[str, str] = {}
            num = a.get("value_type") == "number"
            yield bind({"hist": {"norm": norm_value(ctx, h)}, "cur": {"norm": norm_value(ctx, a)}, "correction": correction},
                       [h, a], a["datapoint_id"],
                       {"values": [dv(ctx, h) if num else dv(ctx, h, code=code), dv(ctx, a) if num else dv(ctx, a, code=code)],
                        **({"delta": dstr(abs(ctx.canonical(a)[0] - ctx.canonical(h)[0]))} if num and ctx.canonical(a) and ctx.canonical(h) else {})},
                       request_id=req_id_of(ctx, a))


@plugin("cr_010")
def cr_010(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = [b for b in ctx.heads("Answer") if b.get("status") in EFFECTIVE_FINAL and b.get("value_type") == "number"]
    for a in subjects_answers(ctx, scope):
        if a.get("value_type") != "number":
            continue
        series = {}
        for b in pool:
            if b["id"] != a["id"] and same_key_wo(a, b, "period") and b["period"]["end"] < a["period"]["start"]:
                series[(b["period"]["start"], b["period"]["end"])] = b
        if len(series) < 3:
            continue
        hist = [series[k] for k in sorted(series)]
        vals = [ctx.canonical(b) for b in hist]
        ca = ctx.canonical(a)
        if ca is None or any(v is None for v in vals):
            continue
        xs = [v[0] for v in vals]  # type: ignore[index]
        med = Decimal(statistics.median(xs))
        mad = Decimal(statistics.median([abs(x - med) for x in xs]))
        yield bind({"x": dstr(ca[0]), "median": dstr(med), "mad": dstr(mad)}, hist + [a], a["datapoint_id"],
                   {"values": [dv(ctx, b) for b in hist] + [dv(ctx, a)], "median": dstr(med), "mad": dstr(mad)},
                   request_id=req_id_of(ctx, a))


# ====================================================================== 有效期
@plugin("cr_004")
def cr_004(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        for r, ev in cited(ctx, a):
            if ev is None:
                yield Binding({}, [ref_of(a)], a["datapoint_id"], {"values": [dv(ctx, a)]},
                              error=f"cited evidence {r} unreadable")
                continue
            head = ctx.head(ev["id"]) or ev
            vu = parse_date(head.get("valid_until") or ev.get("valid_until"))
            expired = head.get("status") == "expired"
            if vu is None and not expired:
                continue
            days = (vu - ctx.submit_date).days if vu else -1
            yield bind({"expired": expired, "days": days}, [a, r], a["datapoint_id"],
                       {"values": [{"ref": r, "value": str(days), "unit": "d"}], "threshold": "30",
                        "valid_until": head.get("valid_until"), "submit_date": ctx.submit_date.isoformat()},
                       request_id=req_id_of(ctx, a))


@plugin("cr_012")
def cr_012(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        meta = ctx.dp(a)
        if not meta or not meta.max_age_days:
            continue
        for r, ev in cited(ctx, a):
            if ev is None:
                continue
            ref_date = parse_date((ev.get("period") or {}).get("end") or ev.get("valid_from"))
            if ref_date is None:
                continue
            age = (ctx.submit_date - ref_date).days
            yield bind({"age_days": age, "max_age_days": int(meta.max_age_days)}, [a, r], a["datapoint_id"],
                       {"values": [{"ref": r, "value": str(age), "unit": "d"}], "threshold": str(meta.max_age_days)},
                       request_id=req_id_of(ctx, a))


_CERT_DATE = re.compile(r"^dp:cert\.([a-z0-9_]+)\.valid_until$")
_CERT_FILE = re.compile(r"^dp:cert\.([a-z0-9_]+)\.certificate_file$")


@plugin("cr_013")
def cr_013(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        if not _CERT_DATE.match(datapoint_base(a["datapoint_id"])):
            continue
        req = ctx.request_of(a)
        need = [req.get("due_date")] if req and req.get("due_date") else []
        for q in ctx.questions_of(a):
            if (q.get("period_requested") or {}).get("end"):
                need.append(q["period_requested"]["end"])
        if not need:
            continue
        required = max(need)
        yield bind({"valid_until": str(a["value"])[:10], "required_until": required}, [a], a["datapoint_id"],
                   {"values": [dv(ctx, a, code={})], "valid_until": str(a["value"])[:10], "required_until": required},
                   request_id=req_id_of(ctx, a))


@plugin("cr_014")
def cr_014(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    files = [f for f in ctx.effective_answers(None) if _CERT_FILE.match(datapoint_base(f["datapoint_id"]))]
    for a in subjects_answers(ctx, scope):
        m = _CERT_DATE.match(datapoint_base(a["datapoint_id"]))
        if not m:
            continue
        cert = m.group(1)
        fa = [f for f in files if _CERT_FILE.match(datapoint_base(f["datapoint_id"])).group(1) == cert  # type: ignore[union-attr]
              and f["boundary"] == a["boundary"]]
        ev_refs = list(a.get("evidence_refs") or []) + [r for f in fa for r in f.get("evidence_refs") or []]
        if not ev_refs:
            continue
        by_id: dict[str, set[int]] = {}
        for r in ev_refs:
            i, v = split_ref(r)
            by_id.setdefault(i, set()).add(v or 0)
        for ev_id, versions in by_id.items():
            head = ctx.head(ev_id)
            if head is None:
                yield Binding({}, [ref_of(a)], a["datapoint_id"], {"values": [dv(ctx, a, code={})]},
                              error=f"certificate evidence {ev_id} unreadable")
                continue
            pinned = ctx.get(f"{ev_id}#v{max(versions)}") or head
            mismatch = len(versions) > 1 or head.get("status") == "superseded" or ctx.current_successor(
                f"{ev_id}#v{max(versions)}") is not None
            cv = pinned.get("valid_until") or ""
            yield bind({"value": str(a["value"])[:10], "cert_valid_until": cv, "version_mismatch": mismatch},
                       [a, f"{ev_id}#v{max(versions)}"], a["datapoint_id"],
                       {"values": [dv(ctx, a, code={})], "answer_valid_until": str(a["value"])[:10],
                        "certificate_valid_until": cv, "version_mismatch": mismatch}, request_id=req_id_of(ctx, a))


@plugin("cr_015")
def cr_015(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        meta = ctx.dp(a)
        if not meta or meta.period_type != "point_in_time" or not meta.expires_after_days:
            continue
        req = ctx.request_of(a)
        target = None
        for q in ctx.questions_of(a):
            target = (q.get("period_requested") or {}).get("end") or target
        target = target or ((req or {}).get("reporting_period") or {}).get("end") or (req or {}).get("due_date")
        if not target:
            continue
        gap = (parse_date(target) - parse_date(a["period"]["end"])).days  # type: ignore[operator]
        yield bind({"gap_days": gap, "limit": int(meta.expires_after_days)}, [a], a["datapoint_id"],
                   {"values": [{"ref": ref_of(a), "value": str(gap), "unit": "d"}], "threshold": str(meta.expires_after_days),
                    "target_date": target}, request_id=req_id_of(ctx, a))


@plugin("cr_016")
def cr_016(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    fss = ctx.heads("FactorSet")
    for a in subjects_answers(ctx, scope):
        if not a.get("calculation_ref"):
            continue
        calc = ctx.get(a["calculation_ref"])
        if calc is None:
            yield Binding({}, [ref_of(a)], a["datapoint_id"], {"values": [dv(ctx, a)]},
                          error=f"calculation {a['calculation_ref']} unreadable")
            continue
        fs = ctx.head(calc["factor_set_version"])
        if fs is None:
            continue
        newer = [f for f in fss if f.get("source") == fs.get("source") and f["id"] != fs["id"]
                 and f.get("status") == "active" and (f.get("published_at") or "") > (fs.get("published_at") or "")]
        yield bind({"deprecated": fs.get("status") == "deprecated", "newer_available": bool(newer)},
                   [a, a["calculation_ref"]], a["datapoint_id"],
                   {"values": [dv(ctx, a)], "factor_set_version": fs["id"], "newer": [f["id"] for f in newer]},
                   request_id=req_id_of(ctx, a))


@plugin("cr_017")
def cr_017(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    latest = ctx.config.reach_candidate_list_latest
    for a in subjects_answers(ctx, scope):
        if datapoint_base(a["datapoint_id"]) != "dp:mat.reach.candidate_list_version":
            continue
        if not latest:
            yield Binding({}, [ref_of(a)], a["datapoint_id"], {"values": [dv(ctx, a, code={})]},
                          not_evaluable="reg-radar latest candidate list date unknown")
            continue
        yield bind({"answer_date": str(a["value"])[:10], "latest": latest}, [a], a["datapoint_id"],
                   {"values": [dv(ctx, a, code={})], "answer_list_date": str(a["value"])[:10], "latest_list_date": latest},
                   request_id=req_id_of(ctx, a))


# ====================================================================== 单位与量纲
_BANDS = ((Decimal(500), Decimal(2000)), (Decimal(1) / 2000, Decimal(1) / 500), (Decimal(500000), Decimal(2000000)))


@plugin("cr_011")
def cr_011(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = effective(ctx, scope)
    for a in subjects_answers(ctx, scope):
        if a.get("value_type") != "number":
            continue
        ca = ctx.canonical(a)
        if ca is None or ca[0] == 0:
            continue
        refs = [p for p in [prev_period(ctx, a, pool)] if p] + other_customers(ctx, a, [x for x in pool if x.get("value_type") == "number"])
        for ref in refs:
            cr = ctx.canonical(ref)
            if cr is None or cr[0] == 0:
                continue
            r = ca[0] / cr[0]
            yield bind({"r": dstr(r.quantize(Decimal("0.0000001")))}, [ref, a], a["datapoint_id"],
                       {"values": [dv(ctx, ref), dv(ctx, a)], "ratio": dstr(r.quantize(Decimal("0.0001")))},
                       request_id=req_id_of(ctx, a))


@plugin("cr_018")
def cr_018(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = [x for x in effective(ctx, scope) if x.get("value_type") == "number"]
    for a in subjects_answers(ctx, scope):
        if a.get("value_type") != "number":
            continue
        meta = ctx.dp(a)
        if meta and meta.unit:
            ok = ctx.convertible(a.get("unit"), meta.unit)
            yield bind({"convertible": ok}, [a], a["datapoint_id"],
                       {"values": [{"ref": ref_of(a), "value": dstr(ctx.numeric(a)[0]) if ctx.numeric(a) else "0", "unit": a.get("unit")}],
                        "units": [a.get("unit"), meta.unit]}, request_id=req_id_of(ctx, a))
        for b in pool:
            if b["id"] == a["id"] or not (same_key_wo(a, b, "customer_scope") or same_key_wo(a, b, "period")):
                continue
            if b.get("unit") == a.get("unit"):
                continue
            ok = ctx.convertible(a.get("unit"), b.get("unit"))
            if not ok:
                x, y = sorted([a, b], key=lambda o: o["id"])
                yield bind({"convertible": False}, [x, y], a["datapoint_id"],
                           {"values": [{"ref": ref_of(o), "value": dstr(ctx.numeric(o)[0]), "unit": o.get("unit")} for o in (x, y)],
                            "units": [x.get("unit"), y.get("unit")]}, request_id=req_id_of(ctx, a))
        # 只比较答案直接引用的证据（R-VAL：值即证据值经换算）；经 Calculation 谱系引用的活动数据（L、MWh…）由方法与
        # 因子换算到结果单位，不与答案直接比较，也不汇总，故不属于 CR-018 的"跨对象比较或汇总"。
        for r in a.get("evidence_refs") or []:
            ev = ctx.get(r)
            if ev is None or ev.get("value_type") != "number":
                continue
            if not ctx.convertible(ev.get("unit"), a.get("unit")):
                yield bind({"convertible": False}, [a, r], a["datapoint_id"],
                           {"values": [{"ref": r, "value": dstr(ctx.numeric(ev)[0]), "unit": ev.get("unit")}],
                            "units": [ev.get("unit"), a.get("unit")]}, request_id=req_id_of(ctx, a))


@plugin("cr_019")
def cr_019(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        meta = ctx.dp(a)
        dp_unit = (meta.unit if meta else None) or a.get("unit") or ""
        for r, ev in cited(ctx, a):
            if ev is None:
                continue
            raw = tag_map(ev).get("raw_unit") or ev.get("unit") or ""
            yield bind({"raw_unit": raw, "dp_unit": dp_unit}, [a, r], a["datapoint_id"],
                       {"values": [dv(ctx, ev)] if ctx.numeric(ev) else [dv(ctx, a)], "raw_unit": raw, "dp_unit": dp_unit},
                       request_id=req_id_of(ctx, a))


@plugin("cr_020")
def cr_020(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = effective(ctx, scope)
    for a in subjects_answers(ctx, scope):
        if a.get("value_type") != "number" or a.get("unit") != "%":
            continue
        refs = [p for p in [prev_period(ctx, a, pool)] if p] + other_customers(ctx, a, pool)
        for ref in refs:
            if ref.get("unit") != "%":
                continue
            yield bind({"x": dstr(ctx.numeric(a)[0]), "ref": dstr(ctx.numeric(ref)[0])}, [ref, a], a["datapoint_id"],
                       {"values": [dv(ctx, ref), dv(ctx, a)]}, request_id=req_id_of(ctx, a))


_DENOM = {"kg": "kg", "kilogram": "kg", "t": "t", "tonne": "t", "piece": "piece", "pieces": "piece", "pcs": "piece",
          "stück": "piece", "stueck": "piece", "unit": "piece", "item": "piece", "kwh": "kWh", "mwh": "MWh", "m2": "m2",
          "m3": "m3", "l": "L", "litre": "L", "liter": "L"}


def _denominator(unit: str) -> str:
    d = unit.split("/", 1)[1] if "/" in unit else ""
    return _DENOM.get(d.strip().lower(), d.strip())


@plugin("cr_021")
def cr_021(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        if a.get("value_type") != "number" or "/" not in (a.get("unit") or ""):
            continue
        if not (a["boundary"] == "product_per_unit" or datapoint_base(a["datapoint_id"]).startswith("dp:prod.pcf.")):
            continue
        rids = ctx.request_ids_of(a)
        decl = [d for d in ctx.effective_answers(rids or None) if datapoint_base(d["datapoint_id"]) == "dp:prod.pcf.declared_unit"
                and (ctx.request_ids_of(d) & rids or (d["boundary"] == a["boundary"] and d["boundary"] != "product_per_unit"))]
        for d in decl:
            declared = _DENOM.get(str(d["value"]).strip().lower(), str(d["value"]).strip())
            yield bind({"denominator": _denominator(a["unit"]), "declared": declared}, [a, d], a["datapoint_id"],
                       {"values": [dv(ctx, a)], "denominator": _denominator(a["unit"]), "declared_unit": declared},
                       request_id=req_id_of(ctx, a))


# ====================================================================== 期间与口径
@plugin("cr_022")
def cr_022(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        meta = ctx.dp(a)
        if not meta or meta.period_type != "calendar_year":
            continue
        p = a["period"]
        yield bind({"start": p["start"], "end": p["end"], "start_year": p["start"][:4], "end_year": p["end"][:4]},
                   [a], a["datapoint_id"], {"values": [dv(ctx, a)], "period": p}, request_id=req_id_of(ctx, a))


@plugin("cr_023")
def cr_023(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for req in ctx.heads("Request"):
        if not scope.request_in(req["id"]):
            continue
        ans = []
        for a in ctx.request_answers(req["id"]):
            meta = ctx.dp(a)
            if working(a) and a.get("value_type") == "number" and meta and meta.period_type in (
                    "reporting_period", "fiscal_year", "calendar_year"):
                ans.append(a)
        if len(ans) < 2:
            continue
        periods = {(a["period"]["start"], a["period"]["end"]) for a in ans}
        annotated = all("period_note" in tag_map(a) or a.get("drafting_note") for a in ans)
        yield bind({"distinct_periods": len(periods), "all_annotated": bool(annotated)}, ans, ans[0]["datapoint_id"],
                   {"values": [dv(ctx, a) for a in ans], "periods": sorted([list(p) for p in periods])}, request_id=req["id"])


@plugin("cr_024")
def cr_024(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        for q in ctx.questions_of(a):
            m = ctx.confirmed_mapping(q["id"])
            if not m or not m.get("boundary_hint"):
                continue
            yield bind({"hint": m["boundary_hint"], "boundary": a["boundary"]}, [a, m], a["datapoint_id"],
                       {"values": [dv(ctx, a, code={})], "boundary_hint": m["boundary_hint"], "boundary": a["boundary"]},
                       request_id=q.get("request_id"))


_ADDITIVE = re.compile(r"^dp:(env\.ghg\.scope[123]|energy\.)")


@plugin("cr_025")
def cr_025(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for req in ctx.heads("Request"):
        if not scope.request_in(req["id"]):
            continue
        ans = sorted([a for a in ctx.request_answers(req["id"]) if working(a) and a.get("consolidation")
                      and a["boundary"] == "organization" and _ADDITIVE.match(a["datapoint_id"])],
                     key=lambda a: (a["datapoint_id"], a["id"]))
        for i in range(len(ans)):
            for j in range(i + 1, len(ans)):
                x, y = ans[i], ans[j]
                if x["period"] != y["period"]:
                    continue
                yield bind({"c1": x["consolidation"], "c2": y["consolidation"]}, [x, y], x["datapoint_id"],
                           {"values": [dv(ctx, x), dv(ctx, y)], "consolidation": [x["consolidation"], y["consolidation"]]},
                           request_id=req["id"])


def _gaps(period: dict[str, str], parts: list[dict[str, str]]) -> int:
    s, e = parse_date(period["start"]), parse_date(period["end"])
    assert s and e
    covered = sorted((parse_date(p["start"]), parse_date(p["end"])) for p in parts)
    gaps, cur = 0, s
    for ps, pe in covered:
        if ps > cur:  # type: ignore[operator]
            gaps += 1
        cur = max(cur, pe + dt.timedelta(days=1))  # type: ignore[operator]
        if cur > e:
            break
    if cur <= e:
        gaps += 1
    return gaps


@plugin("cr_026")
def cr_026(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        meta = ctx.dp(a)
        if a.get("value_type") != "number" or not meta or meta.period_type not in ("reporting_period", "calendar_year", "fiscal_year"):
            continue
        evs = [ev for _, ev in cited(ctx, a) if ev and ev.get("period")]
        gaps = _gaps(a["period"], [ev["period"] for ev in evs]) if len(evs) >= 2 else 0
        months = months_in(a["period"])
        yield bind({"months": months, "gaps": gaps}, [a], a["datapoint_id"],
                   {"values": [dv(ctx, a)], "months": months, "gaps": gaps}, request_id=req_id_of(ctx, a))


# ====================================================================== 计算一致性
SUM_LTE = {"dp:org.employees.headcount": ("dp:social.workforce.headcount.female", "dp:social.workforce.headcount.male")}


def _sum_relations(ctx: AuditContext) -> list[tuple[str, tuple[str, ...], str]]:
    rels = [(m.id, m.sum_of, "eq") for m in ctx.meta.all() if m.sum_of]
    rels += [(t, parts, "lte") for t, parts in SUM_LTE.items()]
    return rels


@plugin("cr_003")
def cr_003(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = [a for a in effective(ctx, scope) if a.get("value_type") == "number"]
    for total_dp, parts_dp, relation in _sum_relations(ctx):
        for t in [a for a in pool if datapoint_base(a["datapoint_id"]) == total_dp]:
            parts = []
            for pdp in parts_dp:
                cand = [p for p in pool if datapoint_base(p["datapoint_id"]) == pdp and same_key_wo(t, p, "datapoint", "consolidation")
                        and (p.get("consolidation") in (None, t.get("consolidation")))]
                if cand:
                    parts.append(cand[0])
            if len(parts) != len(parts_dp):
                continue  # 业务上未回答分项：不检
            if not (scope.answer_in(ctx, t) or any(scope.answer_in(ctx, p) for p in parts)):
                continue
            ct = ctx.canonical(t)
            vals = [ctx.convert(ctx.numeric(p)[0], p.get("unit"), ct[1] if ct else None) for p in parts]
            if ct is None or any(v is None for v in vals):
                continue
            s = sum(vals, Decimal(0))  # type: ignore[arg-type]
            yield bind({"total": dstr(ct[0]), "parts_sum": dstr(s), "relation": relation}, [t] + parts, t["datapoint_id"],
                       {"values": [dv(ctx, t)] + [dv(ctx, p) for p in parts], "delta": dstr(abs(s - ct[0])),
                        "relation": relation, "parts_sum": dstr(s)},
                       request_id=req_id_of(ctx, t), instance_id=f"CR-003:{total_dp}")


TOTAL_REQUESTED = "dp:env.ghg.scope1_2_total_requested"


def _find(pool: list[dict[str, Any]], ref: dict[str, Any], dp: str) -> dict[str, Any] | None:
    for p in pool:
        if datapoint_base(p["datapoint_id"]) == dp and same_key_wo(ref, p, "datapoint"):
            return p
    return None


@plugin("cr_027")
def cr_027(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = [a for a in effective(ctx, scope) if a.get("value_type") == "number"]
    for t in subjects_answers(ctx, scope):
        if datapoint_base(t["datapoint_id"]) != TOTAL_REQUESTED:
            continue
        method = "location_based"
        for q in ctx.questions_of(t):
            method = tag_map(q).get("scope2_method", method)
        s1 = _find(pool, t, "dp:env.ghg.scope1_total")
        s2 = _find(pool, t, f"dp:env.ghg.scope2_{method}")
        if s1 is None or s2 is None:
            yield Binding({}, [ref_of(t)], t["datapoint_id"], {"values": [dv(ctx, t)]},
                          not_evaluable="scope 1 or scope 2 not answered")
            continue
        vt, v1, v2 = ctx.canonical(t), ctx.canonical(s1), ctx.canonical(s2)
        if None in (vt, v1, v2):
            continue
        yield bind({"total": dstr(vt[0]), "s1": dstr(v1[0]), "s2": dstr(v2[0])}, [t, s1, s2], t["datapoint_id"],  # type: ignore[index]
                   {"values": [dv(ctx, t), dv(ctx, s1), dv(ctx, s2)], "delta": dstr(abs(vt[0] - v1[0] - v2[0])),  # type: ignore[index]
                    "scope2_method": method}, request_id=req_id_of(ctx, t))


@plugin("cr_028")
def cr_028(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = [a for a in effective(ctx, scope) if a.get("value_type") == "number"]
    for s2 in pool:
        if datapoint_base(s2["datapoint_id"]) != "dp:env.ghg.scope2_location_based":
            continue
        el = _find(pool, s2, "dp:energy.consumption.electricity_total")
        if el is None or not (scope.answer_in(ctx, s2) or scope.answer_in(ctx, el)):
            continue
        v2, ve = ctx.convert(ctx.numeric(s2)[0], s2.get("unit"), "tCO2e"), ctx.convert(ctx.numeric(el)[0], el.get("unit"), "MWh")
        if v2 is None or ve in (None, 0):
            continue
        ratio = (v2 / ve).quantize(Decimal("0.0001"))  # type: ignore[operator]
        yield bind({"ratio": dstr(ratio)}, [s2, el], s2["datapoint_id"],
                   {"values": [dv(ctx, s2), dv(ctx, el)], "ratio": dstr(ratio), "threshold": "1.2"},
                   request_id=req_id_of(ctx, s2))


def _mwh(ctx: AuditContext, a: dict[str, Any] | None) -> Decimal | None:
    return None if a is None else ctx.convert(ctx.numeric(a)[0], a.get("unit"), "MWh")


@plugin("cr_029")
def cr_029(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = [a for a in effective(ctx, scope) if a.get("value_type") == "number"]
    for el in pool:
        if datapoint_base(el["datapoint_id"]) != "dp:energy.consumption.electricity_total":
            continue
        eac = _find(pool, el, "dp:energy.procurement.eac_volume")
        onsite = _find(pool, el, "dp:energy.generation.onsite_renewable")
        if eac is None and onsite is None:
            continue
        group = [x for x in (el, eac, onsite) if x]
        share = _find(pool, el, "dp:energy.consumption.renewable_share")
        total = _find(pool, el, "dp:energy.consumption.total")
        if not any(scope.answer_in(ctx, x) for x in group + [y for y in (share, total) if y]):
            continue
        ve, va, vo = _mwh(ctx, el), _mwh(ctx, eac) or Decimal(0), _mwh(ctx, onsite) or Decimal(0)
        if ve is None:
            continue
        base = {"eac": dstr(va), "onsite": dstr(vo), "elec": dstr(ve), "share": "0", "total": "0"}
        yield bind({**base, "check": "supply"}, group, (eac or onsite or el)["datapoint_id"],
                   {"values": [dv(ctx, x) for x in group], "delta": dstr(va + vo - ve)}, request_id=req_id_of(ctx, el))
        if share is not None and total is not None and _mwh(ctx, total) is not None:
            yield bind({**base, "check": "share", "share": dstr(ctx.numeric(share)[0]), "total": dstr(_mwh(ctx, total))},
                       group + [share, total], share["datapoint_id"], {"values": [dv(ctx, x) for x in group + [share, total]]},
                       request_id=req_id_of(ctx, share))


def _ev(ctx: AuditContext, fact_prefix: str, boundary: str) -> dict[str, Any] | None:
    for e in ctx.heads("Evidence", status="verified"):
        if e.get("fact_key", "").startswith(fact_prefix) and e.get("boundary") == boundary:
            return e
    return None


@plugin("cr_030")
def cr_030(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = [a for a in effective(ctx, scope) if a.get("value_type") == "number"]
    for pcf in subjects_answers(ctx, scope):
        if datapoint_base(pcf["datapoint_id"]) != "dp:prod.pcf.cradle_to_gate" or not pcf["boundary"].startswith("product:"):
            continue
        prod = ctx.head(pcf["boundary"].split(":", 1)[1])
        mass = _ev(ctx, "activity.production", pcf["boundary"])
        alpha = _ev(ctx, "allocation.", pcf["boundary"])
        if prod is None or mass is None or alpha is None:
            yield Binding({}, [ref_of(pcf)], pcf["datapoint_id"], {"values": [dv(ctx, pcf)]},
                          not_evaluable="production volume / allocation evidence missing")
            continue
        site_total, used = Decimal(0), []
        for sid in prod.get("site_ids") or []:
            for dpb in ("dp:env.ghg.scope1_total", "dp:env.ghg.scope2_location_based"):
                s = next((p for p in pool if datapoint_base(p["datapoint_id"]) == dpb and p["boundary"] == f"site:{sid}"
                          and (not mass.get("period") or p["period"] == mass["period"])), None)
                if s is not None:
                    site_total += ctx.convert(ctx.numeric(s)[0], s.get("unit"), "tCO2e") or Decimal(0)
                    used.append(s)
        if not used:
            yield Binding({}, [ref_of(pcf)], pcf["datapoint_id"], {"values": [dv(ctx, pcf)]},
                          not_evaluable="site scope 1/2 not answered")
            continue
        m_kg = ctx.convert(ctx.numeric(mass)[0], mass.get("unit"), "kg")
        pcf_v = ctx.convert(ctx.numeric(pcf)[0], pcf.get("unit"), "kgCO2e/kg")
        if m_kg is None or pcf_v is None:
            continue
        emb_t = pcf_v * m_kg / 1000
        own = ctx.numeric(alpha)[0] * site_total
        yield bind({"emb": dstr(emb_t), "own": dstr(own)}, [pcf, ref_of(mass), ref_of(alpha)] + used, pcf["datapoint_id"],
                   {"values": [dv(ctx, pcf), dv(ctx, mass), dv(ctx, alpha)] + [dv(ctx, s) for s in used],
                    "embodied_t": dstr(emb_t), "allocated_site_t": dstr(own)}, request_id=req_id_of(ctx, pcf))


@plugin("cr_031")
def cr_031(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = [a for a in effective(ctx, scope) if a.get("value_type") == "number"]
    for see in subjects_answers(ctx, scope):
        if datapoint_base(see["datapoint_id"]) != "dp:prod.cbam.see_indirect":
            continue
        sec = _find(pool, see, "dp:prod.cbam.specific_electricity_consumption")
        ef = None
        calc = ctx.get(see["calculation_ref"]) if see.get("calculation_ref") else None
        for f in (calc or {}).get("factors_used") or []:
            if f.get("unit") == "tCO2e/MWh":
                ef = Decimal(f["value"])
                break
        if ef is None:
            e = _ev(ctx, "factor.electricity", see["boundary"])
            ef = ctx.numeric(e)[0] if e and ctx.numeric(e) else None
        if sec is None or ef is None:
            continue
        expected = ctx.numeric(sec)[0] * ef
        yield bind({"see": dstr(ctx.numeric(see)[0]), "expected": dstr(expected)}, [see, sec], see["datapoint_id"],
                   {"values": [dv(ctx, see), dv(ctx, sec)], "factor": dstr(ef), "expected": dstr(expected)},
                   request_id=req_id_of(ctx, see))


@plugin("cr_032")
def cr_032(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        meta = ctx.dp(a)
        if not meta:
            continue
        num = a.get("value_type") == "number"
        has_min, has_max = num and meta.min is not None, num and meta.max is not None
        ok_regex = True
        if meta.regex and a.get("value_type") in ("number", "text"):
            ok_regex = re.fullmatch(meta.regex, str(a["value"])) is not None if not meta.regex.startswith("^") else \
                re.search(meta.regex, str(a["value"])) is not None
        ok_enum = True
        if a.get("value_type") == "enum" and meta.enum_values:
            ok_enum = str(a["value"]) in meta.enum_values
        if not (has_min or has_max or meta.regex or (a.get("value_type") == "enum" and meta.enum_values)):
            continue
        yield bind({"value": dstr(ctx.numeric(a)[0]) if num else "0", "has_min": has_min, "has_max": has_max,
                    "min": str(meta.min) if has_min else "0", "max": str(meta.max) if has_max else "0",
                    "ok_regex": ok_regex, "ok_enum": ok_enum}, [a], a["datapoint_id"],
                   {"values": [dv(ctx, a) if num else dv(ctx, a, code={})],
                    **({"threshold": str(meta.max)} if has_max else {}), "min": meta.min, "max": meta.max,
                    "regex": meta.regex}, request_id=req_id_of(ctx, a))


_PLAUS_SKIP = {"dp:env.ghg.scope2_location_based": "CR-028", "dp:energy.procurement.eac_volume": "CR-029",
               "dp:org.employees.headcount": "CR-003"}
_DP_RE = re.compile(r"dp:[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")
_NUM_RE = re.compile(r"(?<![\w.'])\d+(?:\.\d+)?(?![\w.'])")


def compile_plausibility(expr: str, self_numeric: bool, ref_types: dict[str, str]) -> tuple[str, list[str]]:
    """把 02 ``validity_rule.plausibility`` 表达式编译为 Decimal-CEL（06 §1.2 字典派生规则）。"""
    refs: list[str] = []

    def _ph(m: re.Match[str]) -> str:
        refs.append(m.group(0))
        return f"__P{len(refs) - 1}__"

    s = _DP_RE.sub(_ph, expr)
    s = _NUM_RE.sub(lambda m: f"dec('{m.group(0)}')", s)
    for i, r in enumerate(refs):
        s = s.replace(f"__P{i}__", f"dec(p{i})" if ref_types.get(r) == "number" else f"p{i}")
    if self_numeric:
        s = re.sub(r"\bvalue\b", "dec(value)", s)
    s = re.sub(r"\bsize\(([^()]*)\)", r"dec(size(\1))", s)  # size() 返回 int → Decimal，与 dec 字面量可比
    return s, refs


@plugin("cr_033")
def cr_033(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    pool = effective(ctx, scope)
    for a in pool:
        meta = ctx.dp(a)
        if not meta or not meta.plausibility or meta.id in _PLAUS_SKIP:
            continue
        for pl in meta.plausibility:
            ref_ids = _DP_RE.findall(pl.expr)
            refs = {r: _find(pool, a, r) or next((p for p in pool if datapoint_base(p["datapoint_id"]) == r
                                                   and same_key_wo(a, p, "datapoint", "consolidation", "period")), None)
                    for r in ref_ids}
            if not (scope.answer_in(ctx, a) or any(x and scope.answer_in(ctx, x) for x in refs.values())):
                continue
            if any(v is None for v in refs.values()):
                yield Binding({}, [ref_of(a)], a["datapoint_id"], {"values": [dv(ctx, a, code={})]},
                              not_evaluable="referenced datapoint not answered", instance_id=f"CR-033:{meta.id}")
                continue
            types = {r: refs[r]["value_type"] for r in ref_ids}  # type: ignore[index]
            try:
                src, order = compile_plausibility(pl.expr, a.get("value_type") == "number", types)
                compile_expr(f"({src})")
            except CelError as e:
                yield Binding({}, [ref_of(a)], a["datapoint_id"], {"values": [dv(ctx, a, code={})]},
                              error=f"plausibility not compilable: {e}", instance_id=f"CR-033:{meta.id}")
                continue

            def _val(o: dict[str, Any]) -> Any:
                if o.get("value_type") == "number":
                    c = ctx.canonical(o)
                    return dstr(c[0]) if c else str(o["value"])
                v = o.get("value")
                return list(v.keys()) if isinstance(v, dict) else v

            vars_ = {"value": _val(a), **{f"p{i}": _val(refs[r]) for i, r in enumerate(order)}}  # type: ignore[arg-type]
            others = [refs[r] for r in dict.fromkeys(order)]
            code: dict[str, str] = {}
            yield bind(vars_, [a] + others, a["datapoint_id"],  # type: ignore[list-item]
                       {"values": [dv(ctx, o) if o.get("value_type") == "number" else dv(ctx, o, code=code) for o in [a] + others],  # type: ignore[union-attr]
                        "expr": pl.expr}, request_id=req_id_of(ctx, a), severity=pl.severity if pl.severity in ("warn", "block") else "warn",
                       instance_id=f"CR-033:{meta.id}", assert_override=f"({src})")


# ====================================================================== 引用完整性
@plugin("cr_006")
def cr_006(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    evs = [e for e in ctx.heads("Evidence", status="verified") if e.get("value_type") == "number"]
    in_scope_ev: set[str] | None = None
    if scope.answer_ids is not None or scope.request_ids is not None:
        in_scope_ev = set(scope.evidence_ids or set())
        for a in subjects_answers(ctx, scope, lambda x: True):
            in_scope_ev |= {split_ref(r)[0] for r in ctx.cited_evidence(a)}
    citing: dict[str, str] = {}
    for a in ctx.heads("Answer"):
        for r in ctx.cited_evidence(a):
            citing.setdefault(split_ref(r)[0], a["datapoint_id"])
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for e in evs:
        p = e.get("period") or {}
        groups.setdefault((e.get("fact_key"), p.get("start"), p.get("end"), e.get("boundary")), []).append(e)
    for g in groups.values():
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                x, y = sorted([g[i], g[j]], key=lambda o: o["id"])
                if in_scope_ev is not None and not ({x["id"], y["id"]} & in_scope_ev):
                    continue
                vx = ctx.numeric(x)
                vy = ctx.convert(ctx.numeric(y)[0], y.get("unit"), vx[1]) if vx else None
                if vx is None or vy is None:
                    continue
                dp = x.get("datapoint_id") or y.get("datapoint_id") or citing.get(x["id"]) or citing.get(y["id"])
                yield bind({"v1": dstr(vx[0]), "v2": dstr(vy)}, [x, y], dp,
                           {"values": [dv(ctx, x), dv(ctx, y)], "delta": dstr(abs(vx[0] - vy)), "fact_key": x.get("fact_key")})


@plugin("cr_034")
def cr_034(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        if a.get("value_type") != "number":
            continue
        refs = list(a.get("evidence_refs") or []) + ([a["calculation_ref"]] if a.get("calculation_ref") else [])
        objs = [ctx.get(r) for r in refs]
        if any(o is None for o in objs):
            yield Binding({}, [ref_of(a)], a["datapoint_id"], {"values": [dv(ctx, a)]}, error="reference unreadable")
            continue
        bad = any(o.get("status") == "rejected" or o.get("trust_zone") == "quarantine" for o in objs)  # type: ignore[union-attr]
        src = None
        if a.get("calculation_ref"):
            src = ctx.numeric(ctx.get(a["calculation_ref"]))  # type: ignore[arg-type]
        else:
            nums = [ctx.numeric(o) for o in objs if o and o["id"].startswith("ev_") and ctx.numeric(o)]  # type: ignore[union-attr]
            src = nums[0] if len(nums) == 1 else None
        n = ctx.numeric(a)
        matches = bool(src and n and ctx.convert(src[0], src[1], n[1]) is not None
                       and ctx.convert(src[0], src[1], n[1]).normalize() == n[0].normalize())  # type: ignore[union-attr]
        yield bind({"has_refs": bool(refs), "bad_ref": bad, "value_matches": matches}, [a] + refs, a["datapoint_id"],
                   {"values": [dv(ctx, a)] + ([{"ref": a.get("calculation_ref") or refs[0], "value": dstr(src[0]),
                                                "unit": src[1]}] if src else [])},
                   request_id=req_id_of(ctx, a))


def _ev_value(ctx: AuditContext, e: dict[str, Any]) -> Any:
    n = ctx.numeric(e)
    return n[0].normalize() if n else e.get("extracted_value")


@plugin("cr_035")
def cr_035(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        for r in a.get("evidence_refs") or []:
            pinned = ctx.get(r)
            if pinned is None:
                yield Binding({}, [ref_of(a)], a["datapoint_id"], {"values": [dv(ctx, a)]}, error=f"{r} unreadable")
                continue
            succ = ctx.current_successor(r)
            if succ is None:
                continue
            changed = _ev_value(ctx, succ) != _ev_value(ctx, pinned)
            vals = [dv(ctx, pinned), dv(ctx, succ)] if ctx.numeric(pinned) and ctx.numeric(succ) else [dv(ctx, a)]
            yield bind({"superseded": True, "value_changed": changed}, [a, r, ref_of(succ)], a["datapoint_id"],
                       {"values": vals, "successor": ref_of(succ)}, request_id=req_id_of(ctx, a),
                       events=[{"type": "impact.rebuild_answer", "answer_ref": ref_of(a), "evidence_ref": r}])


@plugin("cr_036")
def cr_036(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        if not a.get("calculation_ref"):
            continue
        stack, seen = [a["calculation_ref"]], set()
        stale: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        missing = False
        while stack:
            cref = stack.pop()
            if cref in seen:
                continue
            seen.add(cref)
            calc = ctx.get(cref)
            if calc is None:
                missing = True
                break
            succ = ctx.current_successor(cref)
            if succ is not None and succ.get("output_hash") != calc.get("output_hash"):
                stale.append((cref, calc, succ))
            for inp in calc.get("inputs") or []:
                if inp["ref"].startswith("calc_"):
                    stack.append(inp["ref"])
                    continue
                node = ctx.get(inp["ref"])
                if node is None:
                    missing = True
                    continue
                s2 = ctx.current_successor(inp["ref"])
                if s2 is not None and _ev_value(ctx, s2) != _ev_value(ctx, node):
                    stale.append((inp["ref"], node, s2))
        if missing:
            yield Binding({}, [ref_of(a)], a["datapoint_id"], {"values": [dv(ctx, a)]}, error="lineage node unreadable")
            continue
        vals = [dv(ctx, a)] + [dv(ctx, n) for _, n, _ in stale if ctx.numeric(n)] + [dv(ctx, s) for _, _, s in stale if ctx.numeric(s)]
        yield bind({"stale_changed": bool(stale)}, [a, a["calculation_ref"]] + [r for r, _, _ in stale] + [ref_of(s) for _, _, s in stale],
                   a["datapoint_id"], {"values": vals, "stale_nodes": [r for r, _, _ in stale]}, request_id=req_id_of(ctx, a),
                   events=[{"type": "calc.recompute_requested", "calculation_ref": a["calculation_ref"], "answer_ref": ref_of(a)}])


@plugin("cr_037")
def cr_037(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        texts = []
        meta = ctx.dp(a)
        if a.get("value_type") == "text" and isinstance(a.get("value"), str) and not _identifier_value(meta, a["value"]):
            texts.append(a["value"])
        texts.extend((a.get("drafting_note") or {}).values())
        if not texts:
            continue
        objs = [o for o in (ctx.get(r) for r in ctx.cited_evidence(a)) if o]
        # L0 复用：reused_from 是 derived_from 边（03 §1.5），所引历史答案（及其证据）同样是"所引对象"
        seen_reuse: set[str] = set()
        r = a.get("reused_from")
        while r and r not in seen_reuse:
            seen_reuse.add(r)
            src = ctx.get(r)
            if not src:
                break
            objs.append(src)
            objs.extend(o for o in (ctx.get(x) for x in ctx.cited_evidence(src)) if o)
            r = src.get("reused_from")
        if a.get("calculation_ref"):
            stack = [a["calculation_ref"]]
            while stack:
                c = ctx.get(stack.pop())
                if c:
                    objs.append(c)
                    stack.extend(i["ref"] for i in c.get("inputs") or [] if i["ref"].startswith("calc_"))
        if a.get("value_type") == "number":
            objs.append({"value": a["value"], "period": a.get("period")})
        allowed = allowed_numbers(objs)
        for o in objs:  # 文字型来源（复用答案、文字证据）中的数字 token 视为有来源
            for k in ("value", "extracted_value"):
                if isinstance(o.get(k), str):
                    add_text_tokens(allowed, o[k])
        bad, bad_vals = [], []
        for t in texts:
            for tok in all_tokens(t):
                if not (_readings(tok) & allowed):
                    if tok.startswith("date:"):  # 日期：detail.values 只收 DEC-STR，用 YYYYMMDD 序号
                        bad.append(tok[5:])
                        bad_vals.append(str(int(tok[5:].replace("-", ""))))
                    else:
                        rd = sorted(_readings(tok))
                        bad.append(rd[0] if rd else tok)
                        bad_vals.append(rd[0] if rd else "0")
        vals = [{"ref": ref_of(a), "value": b} for b in dict.fromkeys(bad_vals)] or [{"ref": ref_of(a), "value": str(len(texts))}]
        yield bind({"unsourced": len(bad)}, [a], a["datapoint_id"],
                   {"values": vals, "unsourced_tokens": list(dict.fromkeys(bad))}, request_id=req_id_of(ctx, a))


_IDENTIFIER_TAGS = {"identity", "identifier"}


def _identifier_value(meta: Any, value: str) -> bool:
    """标识符类文字字段（KN-Code、USt-IdNr.、DUNS、IMDS-ID…）：值本身是编号而不是数量陈述，CR-037 只查其附注。

    判定：DataPoint 标签含 ``identity``/``identifier``，或有 ``validity_rule.regex`` 且值通过（格式已由 CR-032 管）。"""
    if meta is None:
        return False
    if _IDENTIFIER_TAGS & set(meta.tags or ()):
        return True
    if meta.regex:
        try:
            return re.fullmatch(meta.regex.lstrip("^").rstrip("$"), value) is not None
        except re.error:
            return False
    return False


# ====================================================================== 估算标识
@plugin("cr_038")
def cr_038(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        if not a.get("is_estimate"):
            continue
        req = ctx.request_of(a)
        has_flag = any("estimate_flag" in tag_map(q) or "template:estimate_flag" in (q.get("tags") or [])
                       for q in ctx.questions_of(a)) or "template_estimate_flag" in tag_map(req)
        has_note = "export_note" in tag_map(a) or "estimate_note" in tag_map(a)
        yield bind({"is_estimate": True, "has_flag": bool(has_flag), "has_note": bool(has_note)}, [a], a["datapoint_id"],
                   {"values": [dv(ctx, a) if a.get("value_type") == "number" else dv(ctx, a, code={})],
                    "estimate_method": a.get("estimate_method")}, request_id=req_id_of(ctx, a))


@plugin("cr_039")
def cr_039(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    evs = ctx.heads("Evidence", status="verified")
    for a in subjects_answers(ctx, scope):
        if not a.get("is_estimate"):
            continue
        cited_ids = {split_ref(r)[0] for r in ctx.cited_evidence(a)}
        facts = {ctx.get(r).get("fact_key") for r in ctx.cited_evidence(a) if ctx.get(r)}  # type: ignore[union-attr]
        dpb = datapoint_base(a["datapoint_id"])
        found = [e for e in evs if e["id"] not in cited_ids and e.get("period") and e.get("source_type") != "human_confirmation"
                 and (e.get("fact_key") in facts or (e.get("datapoint_id") and datapoint_base(e["datapoint_id"]) == dpb))
                 and e["period"]["start"] <= a["period"]["start"] and e["period"]["end"] >= a["period"]["end"]
                 and "estimate" not in tag_map(e)]
        yield bind({"measured_available": bool(found)}, [a] + found[:3], a["datapoint_id"],
                   {"values": [dv(ctx, a)] + [dv(ctx, e) for e in found[:3] if ctx.numeric(e)]}, request_id=req_id_of(ctx, a))


@plugin("cr_040")
def cr_040(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for req in ctx.heads("Request"):
        if not scope.request_in(req["id"]):
            continue
        nums = [a for a in ctx.request_answers(req["id"]) if working(a) and a.get("value_type") == "number"]
        if not nums:
            continue
        est = [a for a in nums if a.get("is_estimate")]
        yield bind({"estimates": len(est), "numbers": len(nums), "max_share": dstr(ctx.config.estimate_share_max)},
                   nums, nums[0]["datapoint_id"],
                   {"values": [{"ref": ref_of(nums[0]), "value": str(len(est))}, {"ref": ref_of(nums[-1]), "value": str(len(nums))}],
                    "threshold": dstr(ctx.config.estimate_share_max)}, request_id=req["id"])


# ====================================================================== 法律承诺
@plugin("cr_041")
def cr_041(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for a in subjects_answers(ctx, scope):
        qs = ctx.questions_of(a)
        flag = any(question_legal_flag(q) for q in qs)
        if not flag:
            continue
        yield bind({"legal_flag": True, "legal_commitment": bool(a.get("legal_commitment"))}, [a] + qs, a["datapoint_id"],
                   {"values": [dv(ctx, a) if a.get("value_type") == "number" else dv(ctx, a, code={})],
                    "legal_types": sorted({t for q in qs for t in (q.get("tags") or []) if t.startswith("legal:")})},
                   request_id=req_id_of(ctx, a))


@plugin("cr_042")
def cr_042(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    batched = set(ctx.staged_batch_answers)
    for apr in ctx.heads("Approval"):
        if apr.get("batch_id") and apr.get("subject_ref", "").startswith("ans_"):
            batched.add(split_ref(apr["subject_ref"])[0])
    for a in subjects_answers(ctx, scope):
        if not a.get("legal_commitment"):
            continue
        yield bind({"legal_commitment": True, "risk_level": a.get("risk_level") or "", "in_batch": a["id"] in batched},
                   [a], a["datapoint_id"],
                   {"values": [dv(ctx, a) if a.get("value_type") == "number" else dv(ctx, a, code={})],
                    "risk_level": a.get("risk_level"), "in_batch": a["id"] in batched,
                    "reused_from": a.get("reused_from")}, request_id=req_id_of(ctx, a))


# ====================================================================== 价值链上限
def _annex(ctx: AuditContext, dp: str) -> bool:
    if ctx.config.annex_ii_datapoints is not None:
        return datapoint_base(dp) in ctx.config.annex_ii_datapoints
    meta = ctx.dp(dp)
    return bool(meta and set(meta.tags) & set(ctx.config.annex_ii_tags))


@plugin("cr_043")
def cr_043(ctx: AuditContext, scope: Scope) -> Iterator[Binding]:
    for req in ctx.heads("Request"):
        if not scope.request_in(req["id"]):
            continue
        cp = ctx.counterparty(req.get("counterparty_id"))
        fy = tag_map(req).get("fy_start") or tag_map(cp).get("fy_start") or ""
        purpose = (tag_map(req).get("purpose") or "").lower()
        for q in ctx.heads("Question", request_id=req["id"]):
            m = ctx.confirmed_mapping(q["id"])
            if not m:
                continue
            yield bind({"fy_start": fy, "in_annex": _annex(ctx, m["datapoint_id"]),
                        "purpose": purpose, "exempt": purpose in ctx.config.cr043_exempt_purposes},
                       [m, q], m["datapoint_id"],
                       {"values": [{"ref": ref_of(m), "value": "1"}], "fy_start": fy, "purpose": purpose},
                       request_id=req["id"])


__all__ = ["PLUGINS", "Binding", "Scope", "plugin", "compile_plausibility", "norm_value", "dv", "dstr"]
