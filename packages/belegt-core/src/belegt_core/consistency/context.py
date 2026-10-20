# Spec: 06 §1.3（触发范围）、§1.4（规范化：单位换算为 canonical unit，period/boundary 归一）、§2.1（通用约定："同口径"、"有效答案"、容差）
"""审计上下文：一次求值所需的只读图快照。

- 由 ``AuditContext.from_store(store, org_id, ...)`` 从 ``GraphStore`` 读取（全部版本，便于钉住引用与 head 比对），
  或由 ``AuditContext(org_id, objects, ...)`` 直接用对象字典构造（规则单元测试用）。
- 插件只通过本上下文读图，不能写图、不能访问网络（06 §1.2）。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from ..canonical import to_decimal
from ..hashing import answer_key_of
from ..ids import datapoint_base, type_of_id
from .meta import DPMeta, MetaProvider

EFFECTIVE_FINAL = ("approved", "submitted")
EFFECTIVE_WORKING = ("draft", "in_review")
AUDIT_TYPES = ("Answer", "Evidence", "Calculation", "Conflict", "Question", "Mapping", "Request", "FactorSet",
               "Counterparty", "Product", "Approval", "Site", "SupplierRequest", "ClosedQuestion")


def parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    return dt.date.fromisoformat(str(s)[:10])


def split_ref(ref: str) -> tuple[str, int | None]:
    if "#v" in ref:
        i, v = ref.split("#v", 1)
        return i, int(v)
    return ref, None


def ref_of(obj: dict[str, Any]) -> str:
    return f"{obj['id']}#v{obj['version']}"


def tag_map(obj: dict[str, Any] | None) -> dict[str, str]:
    """``tags`` 约定：``key:value`` → {key: value}；无冒号的标签映射为 ``{tag: ""}``。"""
    out: dict[str, str] = {}
    for t in (obj or {}).get("tags") or []:
        if ":" in t:
            k, v = t.split(":", 1)
            out.setdefault(k, v)
        else:
            out.setdefault(t, "")
    return out


@dataclass
class AuditConfig:
    """可调参数（租户只能在 approval-policy 覆盖中调整少数阈值，06 §1.6）。"""

    yoy_change_ratio: Decimal = Decimal("0.30")  # CR-002 θ
    yoy_overrides: dict[str, Decimal] = field(default_factory=dict)  # dp base → θ
    estimate_share_max: Decimal = Decimal("0.30")  # CR-040
    reach_candidate_list_latest: str | None = None  # CR-017（reg-radar 最新候选清单日期）
    annex_ii_tags: tuple[str, ...] = ("vsme_annex_ii", "vsme", "vsme_basic")  # CR-043（待决 4）
    annex_ii_datapoints: frozenset[str] | None = None
    cr043_exempt_purposes: frozenset[str] = frozenset({"cbam", "catenax", "cdp", "contract"})


class AuditContext:
    def __init__(self, org_id: str, objects: Iterable[dict[str, Any]], *, today: dt.date | str | None = None,
                 meta: MetaProvider | None = None, units: Any = None, config: AuditConfig | None = None,
                 submit_date: dt.date | str | None = None, staged_batch_answers: Iterable[str] = ()) -> None:
        self.org_id = org_id
        self.meta = meta or MetaProvider.default()
        if units is None:
            from ..units import default_units

            units = default_units()
        self.units = units
        self.config = config or AuditConfig()
        self.today = parse_date(today) if isinstance(today, str) else (today or dt.date.today())
        self.submit_date = parse_date(submit_date) if isinstance(submit_date, str) else (submit_date or self.today)
        self.staged_batch_answers = set(staged_batch_answers)
        self._versions: dict[str, dict[int, dict[str, Any]]] = {}
        for o in objects:
            v = o.get("version") or 1
            o.setdefault("version", v)
            self._versions.setdefault(o["id"], {})[v] = o
        self._heads = {i: vs[max(vs)] for i, vs in self._versions.items()}
        self._by_type: dict[str, list[dict[str, Any]]] = {}
        for h in self._heads.values():
            self._by_type.setdefault(type_of_id(h["id"]) or "?", []).append(h)
        for lst in self._by_type.values():
            lst.sort(key=lambda b: (b.get("created_at", ""), b["id"]))
        self._q_req: dict[str, str] = {q["id"]: q.get("request_id") for q in self._by_type.get("Question", [])}
        self._replaced_by: dict[str, str] = {}
        for i, vs in self._versions.items():
            for v in vs.values():
                for k in ("supersedes", "corrects"):
                    s = v.get(k)
                    if s and split_ref(s)[0] != i:
                        self._replaced_by[split_ref(s)[0]] = i

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_store(cls, store: Any, org_id: str, **kw: Any) -> "AuditContext":
        objs: list[dict[str, Any]] = []
        for t in AUDIT_TYPES:
            objs.extend(store.query(t, org_id=org_id, include_history=True))
        kw.setdefault("units", getattr(store, "units", None))
        if "today" not in kw:
            kw["today"] = dt.date.fromisoformat(store.peek_now()[:10])
        return cls(org_id, objs, **kw)

    # ------------------------------------------------------------------ reads
    def head(self, obj_id: str) -> dict[str, Any] | None:
        return self._heads.get(split_ref(obj_id)[0])

    def get(self, ref: str) -> dict[str, Any] | None:
        i, v = split_ref(ref)
        if v is None:
            return self._heads.get(i)
        return self._versions.get(i, {}).get(v)

    def versions(self, obj_id: str) -> list[dict[str, Any]]:
        vs = self._versions.get(split_ref(obj_id)[0], {})
        return [vs[k] for k in sorted(vs)]

    def heads(self, type_name: str, **eq: Any) -> list[dict[str, Any]]:
        return [h for h in self._by_type.get(type_name, []) if all(h.get(k) == v for k, v in eq.items())]

    def is_head(self, ref: str) -> bool:
        i, v = split_ref(ref)
        h = self._heads.get(i)
        return h is not None and (v is None or h["version"] == v)

    def dp(self, obj: dict[str, Any] | str | None) -> DPMeta | None:
        if isinstance(obj, dict):
            obj = obj.get("datapoint_id")
        return self.meta.get(obj)

    # ------------------------------------------------------------------ answers & requests
    def request_ids_of(self, ans: dict[str, Any]) -> set[str]:
        out = {self._q_req.get(q) for q in ans.get("question_refs") or []}
        if ans.get("submission"):
            out.add(ans["submission"].get("request_id"))
        return {r for r in out if r}

    def request_of(self, ans: dict[str, Any]) -> dict[str, Any] | None:
        for r in sorted(self.request_ids_of(ans)):
            h = self.head(r)
            if h:
                return h
        return None

    def questions_of(self, ans: dict[str, Any]) -> list[dict[str, Any]]:
        return [q for q in (self.head(x) for x in ans.get("question_refs") or []) if q]

    def confirmed_mapping(self, question_id: str) -> dict[str, Any] | None:
        ms = self.heads("Mapping", question_id=question_id, status="confirmed")
        return ms[0] if ms else None

    def request_answers(self, request_id: str) -> list[dict[str, Any]]:
        qids = {q["id"] for q in self.heads("Question", request_id=request_id)}
        return [a for a in self.heads("Answer") if a.get("status") not in ("superseded", "rejected")
                and (set(a.get("question_refs") or []) & qids or (a.get("submission") or {}).get("request_id") == request_id)]

    def customers_of(self, ans: dict[str, Any]) -> set[str]:
        cs = ans.get("customer_scope")
        if isinstance(cs, list):
            return set(cs)
        r = self.request_of(ans)
        return {r["counterparty_id"]} if r and r.get("counterparty_id") else set()

    def effective_answers(self, scope_requests: set[str] | None = None) -> list[dict[str, Any]]:
        """06 §2.1：approved/submitted 的 head + 当前 Request 内 draft/in_review 的 head（scope=None 时全部在途）。"""
        out = []
        for a in self.heads("Answer"):
            st = a.get("status")
            if st in EFFECTIVE_FINAL:
                out.append(a)
            elif st in EFFECTIVE_WORKING:
                if scope_requests is None or (self.request_ids_of(a) & scope_requests):
                    out.append(a)
        return out

    def answer_key(self, ans: dict[str, Any]) -> str:
        return ans.get("answer_key") or answer_key_of({**ans, "org_id": ans.get("org_id") or self.org_id})

    # ------------------------------------------------------------------ values
    def numeric(self, obj: dict[str, Any]) -> tuple[Decimal, str | None] | None:
        """Answer.value / Evidence.extracted_value / Calculation.result → (Decimal, unit)。"""
        try:
            if (obj.get("id") or "").startswith("calc_"):
                r = obj.get("result") or {}
                return to_decimal(r["value"]), r.get("unit")
            if obj.get("value_type") != "number":
                return None
            v = obj.get("value") if "value" in obj else obj.get("extracted_value")
            return to_decimal(v), obj.get("unit")
        except Exception:
            return None

    def convert(self, value: Decimal, from_unit: str | None, to_unit: str | None) -> Decimal | None:
        if from_unit == to_unit or not to_unit or not from_unit:
            return value
        try:
            if not self.units.compatible(from_unit, to_unit):
                return None
            return Decimal(self.units.convert(value, from_unit, to_unit))
        except Exception:
            return None

    def convertible(self, a: str | None, b: str | None) -> bool:
        if a == b:
            return True
        if not a or not b:
            return False
        try:
            return bool(self.units.compatible(a, b))
        except Exception:
            return False

    def canonical(self, ans: dict[str, Any]) -> tuple[Decimal, str | None] | None:
        """换算到 DataPoint canonical unit（06 §2.1）；不可换算返回 None（CR-018 负责）。"""
        n = self.numeric(ans)
        if n is None:
            return None
        meta = self.dp(ans)
        target = (meta.unit if meta and meta.unit else None) or n[1]
        v = self.convert(n[0], n[1], target)
        return None if v is None else (v, target)

    def cited_evidence(self, ans: dict[str, Any]) -> list[str]:
        """直接引用 + 经 Calculation 谱系引用的 Evidence ObjectRef（钉住版本）。"""
        refs = list(ans.get("evidence_refs") or [])
        seen: set[str] = set()
        stack = [ans["calculation_ref"]] if ans.get("calculation_ref") else []
        while stack:
            r = stack.pop()
            if r in seen:
                continue
            seen.add(r)
            calc = self.get(r)
            for inp in (calc or {}).get("inputs") or []:
                (refs if inp["ref"].startswith("ev_") else stack).append(inp["ref"])
        out: list[str] = []
        for r in refs:
            if r not in out:
                out.append(r)
        return out

    def replacement_of(self, obj_id: str) -> dict[str, Any] | None:
        """被替换对象的继任者：另一对象 v1 的 ``supersedes`` 或 Evidence ``corrects`` 指向它。"""
        nxt = self._replaced_by.get(obj_id)
        seen = {obj_id}
        while nxt and nxt in self._replaced_by and self._replaced_by[nxt] not in seen:  # 链式替换取最新
            seen.add(nxt)
            nxt = self._replaced_by[nxt]
        return self._heads.get(nxt) if nxt else None

    def current_successor(self, ref: str) -> dict[str, Any] | None:
        """钉住版本之后的当前有效版本：同对象更新版本，或（head 已 superseded 时）继任对象。"""
        i, v = split_ref(ref)
        h = self.head(i)
        if h is None:
            return None
        if h.get("status") == "superseded":
            rep = self.replacement_of(i)
            if rep is not None:
                return rep
        if v is not None and h["version"] != v:
            return h
        return None

    def conflicts_for(self, obj_id: str, status: str | None = None) -> list[dict[str, Any]]:
        out = []
        for c in self.heads("Conflict"):
            if status and c.get("status") != status:
                continue
            if obj_id in {split_ref(r)[0] for r in c.get("subject_refs") or []}:
                out.append(c)
        return out

    def counterparty(self, cp_id: str | None) -> dict[str, Any] | None:
        return self.head(cp_id) if cp_id else None


def same_key_wo(a: dict[str, Any], b: dict[str, Any], *drop: str) -> bool:
    """"同口径"：answer_key 分量中除 ``drop`` 外全部相同（SSOT-5）。"""
    fields = {"datapoint": lambda x: datapoint_base(x["datapoint_id"]), "boundary": lambda x: x.get("boundary"),
              "consolidation": lambda x: x.get("consolidation"),
              "period": lambda x: ((x.get("period") or {}).get("start"), (x.get("period") or {}).get("end")),
              "customer_scope": lambda x: _norm_scope(x.get("customer_scope"))}
    return all(f(a) == f(b) for k, f in fields.items() if k not in drop)


def _norm_scope(cs: Any) -> Any:
    if isinstance(cs, list):
        return tuple(sorted(set(cs)))
    return cs


def months_in(period: dict[str, str]) -> int:
    s, e = parse_date(period["start"]), parse_date(period["end"])
    assert s and e
    n = (e.year - s.year) * 12 + (e.month - s.month)
    nxt = e + dt.timedelta(days=1)
    if nxt.day == 1 and s.day == 1:
        n += 1
    return n


__all__ = ["AuditContext", "AuditConfig", "same_key_wo", "months_in", "parse_date", "split_ref", "ref_of", "tag_map",
           "EFFECTIVE_FINAL", "EFFECTIVE_WORKING"]
