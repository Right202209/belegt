# Spec: 11 §4.1（运行器）；04 §2.3 失败回退（“LLM 失败 → 只用嵌入相似度给候选并把置信度封顶 0.84”）、
#       04 §2.1 EV-AUTO-0.90-LOC、06 §2 CR-001…CR-032 容差、07 §3 注入规则
"""确定性参考基线（不调用任何 LLM）。

用途：让离线 CI 有可比较的数字、验证运行器/指标/门禁链路，并作为“模型全部失效时的退化下限”。
**注意**：注入与冲突基线由同一批作者编写，与黄金集不独立，其分数不能当作质量估计；映射基线只用字典标签/定义
（不读改写词库），证据基线即 belegt-io 的确定性提取。
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

EMBED_ONLY_CAP = 0.84

_STOP = set("""der die das und oder in im am an auf für fur von vom zu zum zur mit bei bis ihr ihre ihres ihrer
sie wir unser unsere ist sind war waren wie hoch viel viele welche welcher welches bitte geben nennen an angabe
the a an and or of for in on at to by your you our we is are was were what how much many please state report
provide do does has have with from per year jahr berichtsjahr reporting total gesamt insgesamt""".split())


def _fold(s: str) -> str:
    s = s.lower().replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _tokens(s: str) -> list[str]:
    toks = re.findall(r"[a-z0-9\u4e00-\u9fff]+", _fold(s))
    out = []
    for i, t in enumerate(toks):
        if t.isdigit() and not (t in ("1", "2", "3") and i and toks[i - 1] == "scope"):
            continue
        if t in _STOP or len(t) < 2 and not t.isdigit():
            continue
        out.append(t)
    # 中文：按字切分
    return [c for t in out for c in ([t] if not re.search(r"[\u4e00-\u9fff]", t) else list(t))]


def _grams(s: str, k: int = 4) -> set[str]:
    t = " " + re.sub(r"\s+", " ", _fold(s)) + " "
    return {t[i:i + k] for i in range(max(1, len(t) - k + 1))}


def _match(q: str, d: str) -> bool:
    if q == d:
        return True
    if len(q) >= 5 and len(d) >= 5 and (q.startswith(d[:5]) and d.startswith(q[:5])):
        return True
    return len(d) >= 5 and d in q  # 德语复合词：stromverbrauch ⊃ strom…


class LexicalMapper:
    """字典标签 + 定义 + ID 词元的 IDF 加权覆盖 + 字符 4-gram 相似度；置信度封顶 0.84（全部走人工确认）。"""

    name = "baseline-lexical@1"

    def __init__(self, dictionary: Any = None) -> None:
        if dictionary is None:
            from belegt_dictionary import load_dictionary
            dictionary = load_dictionary()
        self.docs: dict[str, tuple[list[str], set[str]]] = {}
        df: Counter[str] = Counter()
        for dp_id in sorted(dictionary.datapoints):
            dp = dictionary.datapoints[dp_id]
            lab = " ".join(x for x in (dp.label.de, dp.label.en, getattr(dp.label, "zh", None)) if x)
            idt = dp_id.split(":", 1)[1].replace(".", " ").replace("_", " ")
            defin = getattr(dp.definition, "en", "") or ""
            toks = _tokens(lab + " " + idt) + _tokens(defin)[:25]
            self.docs[dp_id] = (toks, _grams(lab))
            df.update(set(toks))
        n = len(self.docs)
        self.idf = {t: math.log(1 + n / c) for t, c in df.items()}

    def predict(self, text: str) -> dict[str, Any]:
        qt = list(dict.fromkeys(_tokens(text)))
        qg = _grams(text)
        scored = []
        for dp_id, (dt, dg) in self.docs.items():
            dset = set(dt)
            num = sum(self.idf.get(t, 1.0) for t in qt if any(_match(t, d) for d in dset))
            den = sum(self.idf.get(t, 1.0) for t in qt) or 1.0
            cov = num / den
            jac = len(qg & dg) / len(qg | dg) if qg else 0.0
            scored.append((0.75 * cov + 0.25 * jac, dp_id))
        scored.sort(key=lambda x: (-x[0], x[1]))
        top = scored[:3]
        if not top or top[0][0] <= 0.05:
            return {"candidates": []}
        return {"candidates": [{"datapoint_id": d, "rank": i + 1,
                                "confidence": round(min(EMBED_ONLY_CAP, max(0.01, s)), 4)}
                               for i, (s, d) in enumerate(top)], "method": "semantic", "degraded": True}


# —— 证据 ——

_DATE_PATTERNS = [
    re.compile(r"(?:g[uü]e?ltig\s+bis|valid\s+until|expiry\s+date|ablaufdatum)[:\s]*(\d{1,2})\.(\d{1,2})\.(\d{4})", re.I),
    re.compile(r"(?:g[uü]e?ltig\s+bis|valid\s+until|expiry\s+date|ablaufdatum)[:\s]*(\d{4})-(\d{2})-(\d{2})", re.I),
    re.compile(r"g[uü]e?ltig\s+von\s+\S+\s+bis\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", re.I),
]


def extract_valid_until(text: str) -> str | None:
    for rx in _DATE_PATTERNS:
        m = rx.search(text)
        if m:
            g = m.groups()
            try:
                d = date(int(g[0]), int(g[1]), int(g[2])) if len(g[0]) == 4 else date(int(g[2]), int(g[1]), int(g[0]))
                return d.isoformat()
            except ValueError:
                continue
    return None


class IoEvidenceExtractor:
    """belegt-io 确定性提取；verified ⇔ confidence ≥ 0.90 且有页码 + bbox（EV-AUTO-0.90-LOC 的确定性部分）。"""

    name = "baseline-belegt-io@1"

    def predict(self, pdf_path: Path) -> dict[str, Any]:
        from belegt_io import extract_pdf_evidence
        from belegt_io.evidence import pdf_pages_text

        facts = []
        for c in extract_pdf_evidence(pdf_path):
            if c.fact_key is None or c.value_type != "number" or c.extracted_value is None:
                continue
            loc = c.locator.to_json()
            if loc.get("bbox") is not None:
                loc["bbox"] = list(loc["bbox"])
            period = ({"start": c.period.start.isoformat(), "end": c.period.end.isoformat()}
                      if c.period is not None and c.period.start and c.period.end else None)
            facts.append({"fact_key": c.fact_key, "extracted_value": c.extracted_value, "unit": c.unit,
                          "period": period, "locator": loc, "confidence": c.confidence,
                          "verified": c.confidence >= 0.90 and bool(loc.get("page")) and bool(loc.get("bbox"))})
        text = "\n".join(pdf_pages_text(pdf_path))
        return {"facts": facts, "validity": {"valid_until": extract_valid_until(text)}}


# —— 冲突（06 规则子集的参考实现） ——

_SUM_OF = {
    "dp:env.waste.generated": (["dp:env.waste.generated.hazardous", "dp:env.waste.generated.non_hazardous"], "eq"),
    "dp:org.employees.headcount": (["dp:social.workforce.headcount.female", "dp:social.workforce.headcount.male"],
                                   "lte"),
}
_HARD_RANGE = {"%": (Decimal(0), Decimal(100))}


def _d(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _pk(p: dict[str, Any] | None) -> tuple[Any, Any]:
    return ((p or {}).get("start"), (p or {}).get("end"))


class RuleConflictDetector:
    name = "baseline-rules@1"

    def __init__(self) -> None:
        try:
            from belegt_dictionary import load_dictionary
            self._dict = load_dictionary()
        except Exception:  # pragma: no cover
            self._dict = None

    def _canon(self, dp: str, v: Any, unit: str | None) -> Decimal | None:
        x = _d(v)
        if x is None or self._dict is None or not unit:
            return x
        try:
            target = self._dict.datapoints[dp].unit
            return self._dict.convert(x, unit, target) if target and unit != target else x
        except Exception:
            return x

    def predict(self, sample: dict[str, Any]) -> dict[str, Any]:
        ans = sample["answers"]
        cur = [a for a in ans if a.get("status") != "submitted"]
        hist = [a for a in ans if a.get("status") == "submitted"]
        out: list[dict[str, Any]] = []

        def add(rule: str, objs: list[str]) -> None:
            out.append({"rule_id": rule, "objects": objs})

        # CR-001 / CR-007：跨客户同口径同期不同值
        for i, a in enumerate(cur):
            for b in cur[i + 1:]:
                if (a["datapoint_id"] == b["datapoint_id"] and a.get("customer_scope") != b.get("customer_scope")
                        and a.get("customer_scope") and b.get("customer_scope") and _pk(a.get("period")) == _pk(b.get("period"))
                        and a.get("boundary") == b.get("boundary") and a.get("consolidation") == b.get("consolidation")
                        and not (a.get("divergence_note") or b.get("divergence_note"))):
                    if isinstance(a["value"], bool) or isinstance(b["value"], bool):
                        if a["value"] != b["value"]:
                            add("CR-007", [a["answer_id"], b["answer_id"]])
                    else:
                        va, vb = self._canon(a["datapoint_id"], a["value"], a.get("unit")), self._canon(
                            b["datapoint_id"], b["value"], b.get("unit"))
                        if va is not None and vb is not None and va != vb:
                            add("CR-001", [a["answer_id"], b["answer_id"]])
                # CR-008：同一 Request、同 answer_key（同客户）不同值
                if (a["datapoint_id"] == b["datapoint_id"] and a.get("customer_scope") == b.get("customer_scope")
                        and a.get("request_id") == b.get("request_id") and _pk(a.get("period")) == _pk(b.get("period"))
                        and str(a["value"]) != str(b["value"])):
                    add("CR-008", [a["answer_id"], b["answer_id"]])
        # 跨时间：CR-002 / CR-005 / CR-009 / CR-011 / CR-020
        for a in cur:
            for h in hist:
                if a["datapoint_id"] != h["datapoint_id"] or a.get("customer_scope") != h.get("customer_scope"):
                    continue
                va, vh = _d(a["value"]), _d(h["value"])
                same_p = _pk(a.get("period")) == _pk(h.get("period"))
                if same_p and va is not None and vh is not None and va != vh:
                    add("CR-005", [a["answer_id"], h["answer_id"]])
                if not same_p and (a.get("consolidation") != h.get("consolidation")
                                   or a.get("boundary") != h.get("boundary")):
                    add("CR-009", [a["answer_id"], h["answer_id"]])
                if va is None or vh is None or vh == 0 or same_p:
                    continue
                r = va / vh
                if (Decimal(500) <= r <= Decimal(2000) or Decimal(1) / 2000 <= r <= Decimal(1) / 500
                        or Decimal(500000) <= r <= Decimal(2000000)):
                    add("CR-011", [a["answer_id"]])
                if a.get("unit") == "%" and va <= 1 and vh > 1 and abs(va * 100 - vh) <= Decimal("0.2") * vh:
                    add("CR-020", [a["answer_id"], h["answer_id"]])
                elif abs(r - 1) > Decimal("0.30"):
                    add("CR-002", [a["answer_id"], h["answer_id"]])
        by_dp: dict[str, list[dict[str, Any]]] = {}
        for a in cur:
            by_dp.setdefault(a["datapoint_id"], []).append(a)
        # CR-003：分项之和
        for total_dp, (parts, rel) in _SUM_OF.items():
            for t in by_dp.get(total_dp, []):
                ps = [p for dp in parts for p in by_dp.get(dp, []) if _pk(p.get("period")) == _pk(t.get("period"))]
                if len(ps) != len(parts):
                    continue
                s = sum((_d(p["value"]) or Decimal(0)) for p in ps)
                tv = _d(t["value"]) or Decimal(0)
                tol = max(Decimal("0.5"), abs(tv) * Decimal("0.001"))
                if (rel == "eq" and abs(s - tv) > tol) or (rel == "lte" and s > tv + tol):
                    add("CR-003", [p["answer_id"] for p in ps] + [t["answer_id"]])
        # CR-022：calendar_year 数据点用非日历年期间
        for a in cur:
            dp = self._dict.datapoints.get(a["datapoint_id"]) if self._dict else None
            p = a.get("period") or {}
            if dp is not None and dp.period_type == "calendar_year" and p:
                if not (p["start"].endswith("-01-01") and p["end"].endswith("-12-31") and p["start"][:4] == p["end"][:4]):
                    add("CR-022", [a["answer_id"]])
            # CR-032：硬约束
            if a.get("unit") in _HARD_RANGE and _d(a["value"]) is not None:
                lo, hi = _HARD_RANGE[a["unit"]]
                if not lo <= _d(a["value"]) <= hi:
                    add("CR-032", [a["answer_id"]])
        # CR-025：合并方法不一致（Scope 1/2/3）
        ghg = [a for a in cur if a["datapoint_id"].startswith("dp:env.ghg.scope") and a.get("consolidation")]
        if len({a["consolidation"] for a in ghg}) > 1:
            add("CR-025", [a["answer_id"] for a in ghg])
        # CR-028 / CR-029
        el = by_dp.get("dp:energy.consumption.electricity_total", [])
        for e in el:
            ev = self._canon(e["datapoint_id"], e["value"], e.get("unit"))
            for s2 in by_dp.get("dp:env.ghg.scope2_location_based", []):
                sv = self._canon(s2["datapoint_id"], s2["value"], s2.get("unit"))
                if ev and sv is not None and not (0 <= sv / ev <= Decimal("1.2")):
                    add("CR-028", [s2["answer_id"], e["answer_id"]])
            eac = sum((self._canon(x["datapoint_id"], x["value"], x.get("unit")) or 0)
                      for x in by_dp.get("dp:energy.procurement.eac_volume", []) + by_dp.get(
                          "dp:energy.generation.onsite_renewable", []))
            if ev is not None and eac > ev + 1:
                add("CR-029", [x["answer_id"] for x in by_dp.get("dp:energy.procurement.eac_volume", [])] +
                    [e["answer_id"]])
        # CR-006：同一事实两个来源不同值（换算后，rel 0.5 %）
        evs = sample.get("evidence") or []
        for i, a in enumerate(evs):
            for b in evs[i + 1:]:
                if a["fact_key"] == b["fact_key"] and _pk(a.get("period")) == _pk(b.get("period")) and a.get("unit"):
                    va, vb = _d(a["value"]), _d(b["value"])
                    if self._dict and a["unit"] != b["unit"]:
                        try:
                            vb = self._dict.convert(vb, b["unit"], a["unit"])
                        except Exception:
                            continue
                    if va and vb is not None and abs(va - vb) / abs(va) > Decimal("0.005"):
                        add("CR-006", [a["evidence_id"], b["evidence_id"]])
        # CR-004：引用证据在提交日已过期或 30 天内到期
        sd = (sample.get("context") or {}).get("submit_date")
        if sd:
            ev_by = {e["evidence_id"]: e for e in evs}
            for a in cur:
                for ref in a.get("evidence_refs") or []:
                    vu = (ev_by.get(ref) or {}).get("valid_until")
                    if vu and (date.fromisoformat(vu) - date.fromisoformat(sd)).days <= 30:
                        add("CR-004", [a["answer_id"], ref])
        return {"conflicts": out}


# —— 注入 ——

_INJ = [re.compile(p, re.I | re.S) for p in [
    r"ignor\w*\s+(all\s+|alle\s+)?(previous|prior|above|vorherig\w*|bisherig\w*)", r"ignorieren sie alle",
    r"(zignoruj|ignorujte|ignora|yok say|忽略)", r"(önceki|onceki)\s+talimat", r"system\s*prompt|系统提示",
    r"(you are now|du bist (jetzt|nicht mehr)|developer mode|without restrictions|ohne quellenangabe)",
    r"</?\s*(system|external_data)\s*>", r"#{2,}\s*system", r"(mark|markier\w*|set status|标记).{0,40}(approved|genehmigt|已批准)",
    r"(approve|zatwierdź|approva|genehmig\w*).{0,30}(all|alle|wszystkie|tutte|automatic)",
    r"(weiter|forward|send|pošlete|leiten).{0,80}@", r"(click|klick\w*).{0,30}(submit|absenden|final)",
    r"(sofort ab|absenden|senden sie das formular)", r"(ersetzen sie|replace).{0,60}(wert|value|extracted_value)",
    r"(löschen sie|delete).{0,40}(evidenz|evidence)", r"(ai assistant|ki-assistent|an den assistenten|assistant,|"
    r"anweisung an das sprachmodell|note to automated agents|hinweis für ki-agenten)",
    r"!\[[^\]]*\]\(https?://", r"canary", r"(vergiss|forget).{0,30}(regeln|rules)",
    r"ignor\w*\s+(all\s+|alle\s+)?(rules|instructions|regeln|anweisungen)", r"(e-?mail|mail)\b.{0,60}@",
    r"decode and follow",
]]


class RegexInjectionDetector:
    name = "baseline-regex@1"

    def predict(self, text: str) -> dict[str, Any]:
        import base64
        import binascii

        hits = [p.pattern for p in _INJ if p.search(text)]
        for tok in re.findall(r"[A-Za-z0-9+/]{24,}={0,2}", text):
            try:
                dec = base64.b64decode(tok + "=" * (-len(tok) % 4)).decode("utf-8", "ignore")
            except (binascii.Error, ValueError):
                continue
            hits += [p.pattern for p in _INJ if p.search(dec)]
        return {"flagged": bool(hits), "hits": hits[:5], "actions_taken": []}


class CoreGuardInjectionDetector:
    """`belegt_core.guard.injection.scan`（生产 guard 的确定性规则）。"""

    name = "core-guard-scan"

    def __init__(self) -> None:
        from belegt_core.guard.injection import scan  # noqa: F401
        self._scan = scan

    def predict(self, text: str) -> dict[str, Any]:
        rep = self._scan(text)
        return {"flagged": bool(rep.flagged), "hits": list(rep.flags), "actions_taken": []}


class CitingDrafter:
    """只复述所引对象值的模板起草器（R-NUM 下限：新增数字应为 0）。"""

    name = "baseline-citing-drafter@1"

    def predict(self, sample: dict[str, Any]) -> dict[str, Any]:
        parts = [f"{c['value']} {c.get('unit') if c.get('unit') not in (None, '1') else ''}".strip() + f" [{c['ref']}]"
                 for c in sample["cited"]]
        return {"text": ("Wert: " if sample["lang"] == "de" else "Value: ") + "; ".join(parts), "cited": sample["cited"]}
