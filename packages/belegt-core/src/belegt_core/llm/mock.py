# Spec: CONTRACTS（确定性 MockLLM：规则/脚本化回复）、04 §2.2–§2.13 各 Agent 输出 schema、07 §3（注入：MockLLM 不执行数据块内指令）
"""``MockLLM``：确定性、离线、无 key 的 LLM 替身，足以跑通 demo 与测试。

按 ``LLMRequest.task`` 路由到规则处理器，只读 ``req.data``（结构化，与 user 文本同源）：
- ``triage``：问卷关键词 + 可见文本中的日期/年份；
- ``map_questions``：字典标签词元重叠 + 别名/术语表（``GLOSSARY``）+ 嵌入近似（hints 中的 similarity），
  ``datapoint_id`` 只从候选集中选（与真实模型的 enum 约束一致）；
- ``extract_fields`` / ``reply_extract``：``belegt_io.extract_quantities`` 在标签附近取数（只抽取，不计算）；
- ``draft_answer``：从证据/历史答案取值，句子必带 ``cites``；
- ``explain_conflict``、``chase_slots``、``injection_classify``、``reg_radar``、``sales_answer``、``portal_locate``。
``script={task: dict | list | callable(req)->dict}`` 可覆盖任意任务（测试用：如让模型"试图写数字"）；
``fail_tasks`` 模拟模型不可用；``invalid_first`` 让某任务第一次返回不合 schema 的输出（测试重试）。
token 计数按字符粗估，模型名为 ``mock-<档位默认模型>``，成本按 10 §4.3 方案 B 价格计算。
"""
from __future__ import annotations

import json
import re
import threading
from functools import lru_cache
from decimal import Decimal
from typing import Any, Callable

from .base import LLMRequest, LLMResponse, ModelUnavailable, estimate_tokens
from .cost import TIER_DEFAULT_MODEL, PriceTable
from .embedding import fuzzy_cover, normalize, tokens

# 别名/术语表：DataPoint → 触发短语（规范化后全部词元出现即命中）。是 MockLLM 的"领域知识"，真实模型不需要。
GLOSSARY: dict[str, list[str]] = {
    "dp:env.ghg.scope1_total": ["scope 1 emission", "scope1 emission"],
    "dp:env.ghg.scope2_location_based": ["scope 2 standortbasiert", "scope 2 location"],
    "dp:env.ghg.scope2_market_based": ["scope 2 marktbasiert", "scope 2 market"],
    "dp:env.ghg.scope3_category.c01": ["scope 3 kategorie 1", "scope 3 category 1"],
    "dp:energy.consumption.total": ["gesamtenergieverbrauch", "total energy consumption"],
    "dp:energy.consumption.electricity_total": ["stromverbrauch berichtsjahr", "electricity consumption"],
    "dp:energy.consumption.natural_gas": ["erdgasverbrauch", "natural gas consumption"],
    "dp:energy.consumption.renewable_share": ["anteil erneuerbar", "renewable share"],
    "dp:energy.generation.onsite_renewable": ["eigenerzeugung photovoltaik", "onsite renewable"],
    "dp:energy.procurement.eac_volume": ["herkunftsnachweis", "energy attribute certificate"],
    "dp:env.water.withdrawal_total": ["wasserentnahme", "water withdrawal"],
    "dp:env.waste.generated": ["abfallaufkommen gesamt", "total waste"],
    "dp:env.waste.generated.hazardous": ["gefahrlich abfall", "hazardous waste"],
    "dp:env.waste.diverted_share": ["recyclingquote abfall", "verwertungsquote"],
    "dp:env.ghg_target.has_target": ["reduktionsziel treibhausgas", "reduction target"],
    "dp:env.ghg_target.reduction_pct": ["angestrebte reduktion", "target reduction"],
    "dp:org.identity.legal_name": ["firmenname", "legal name"],
    "dp:org.identity.nace_code": ["nace code"],
    "dp:org.employees.headcount": ["anzahl mitarbeitend", "headcount"],
    "dp:org.financials.turnover": ["umsatz geschaftsjahr", "turnover"],
    "dp:prod.pcf.cradle_to_gate": ["pcf cradle to gate", "product carbon footprint cradle"],
    "dp:prod.identity.cn_code": ["kn code", "cn code"],
    "dp:prod.cbam.installation_id": ["installation id", "kennung produktionsanlage"],
    "dp:prod.cbam.production_route": ["produktionsroute", "production route"],
    "dp:prod.cbam.see_direct": ["direkte graue emission", "see direkt"],
    "dp:prod.cbam.see_indirect": ["indirekte graue emission", "see indirekt"],
    "dp:prod.cbam.specific_electricity_consumption": ["spezifischer stromverbrauch"],
    "dp:prod.cbam.carbon_price_paid": ["gezahlter co2 preis", "carbon price paid"],
    "dp:mat.reach.svhc_present": ["svhc kandidatenliste", "svhc candidate list"],
    "dp:mat.reach.svhc_list": ["bezeichnung cas nummer svhc"],
    "dp:mat.reach.svhc_concentration": ["konzentration svhc"],
    "dp:mat.scip.scip_number": ["scip nummer", "scip number"],
    "dp:mat.rohs.compliant": ["rohs konform", "rohs compliant"],
    "dp:mat.recycled_content.steel_share": ["rezyklatanteil"],
    "dp:prod.mass.net_per_unit": ["nettomasse produkt", "net mass"],
    "dp:cert.iso14001.valid_until": ["iso 14001 gultig bis"],
    "dp:cert.iso14001.certificate_file": ["iso 14001 zertifikat hochladen", "iso 14001 certificate upload"],
    "dp:cert.iso50001.valid_until": ["iso 50001 gultig bis"],
    "dp:cert.iatf16949.valid_until": ["iatf 16949 gultig bis"],
    "dp:supply.code_of_conduct.supplier_coc_in_place": ["verhaltenskodex lieferant", "supplier code of conduct"],
    "dp:social.health_safety.recordable_accidents": ["anzahl meldepflichtig arbeitsunfall"],
    "dp:social.health_safety.recordable_accident_rate": ["unfallhaufigkeitsrate", "ltifr"],
    "dp:social.training.avg_hours_per_employee": ["weiterbildungsstund"],
    "dp:social.remuneration.min_wage_compliance": ["mindestlohn", "minimum wage"],
    "dp:gov.anticorruption.fines_amount": ["bussgeld korruption", "fines corruption"],
    "dp:gov.policy.human_rights_policy": ["kinder zwangsarbeit", "human rights policy"],
}

# 中文术语（中文问卷常见问法；子串匹配）。与字典 zh 标签互补：字典标签进入 hints 检索，这里是 MockLLM 的领域知识。
GLOSSARY_ZH: dict[str, list[str]] = {
    "dp:env.ghg.scope1_total": ["范围一", "范围1排放", "直接温室气体排放"],
    "dp:env.ghg.scope2_location_based": ["范围二排放基于位置", "范围二基于位置", "范围2基于位置"],
    "dp:env.ghg.scope2_market_based": ["范围二排放基于市场", "范围二基于市场", "范围2基于市场"],
    "dp:env.ghg.scope3_total": ["范围三排放总量", "范围3排放总量"],
    "dp:energy.consumption.total": ["总能耗", "能源消耗总量"],
    "dp:energy.consumption.electricity_total": ["电力消耗", "用电量", "耗电量"],
    "dp:energy.consumption.natural_gas": ["天然气消耗", "天然气用量"],
    "dp:energy.consumption.renewable_share": ["可再生能源占比", "可再生能源比例"],
    "dp:energy.generation.onsite_renewable": ["自发可再生电力", "自发电", "光伏发电量"],
    "dp:energy.procurement.eac_volume": ["绿证", "原产地证明电量", "能源属性证书"],
    "dp:env.water.withdrawal_total": ["取水总量", "取水量"],
    "dp:env.waste.generated": ["废弃物总量", "废物产生量"],
    "dp:env.waste.generated.hazardous": ["危险废物", "危废"],
    "dp:env.ghg_target.has_target": ["减排目标"],
    "dp:env.ghg_target.target_year": ["目标年"],
    "dp:env.ghg_target.reduction_pct": ["减排比例", "减排幅度"],
    "dp:org.identity.legal_name": ["公司名称", "企业名称", "法定名称"],
    "dp:org.identity.nace_code": ["nace行业代码", "nace代码"],
    "dp:org.employees.headcount": ["员工人数", "员工总数"],
    "dp:org.financials.turnover": ["营业额", "营业收入", "销售额"],
    "dp:prod.identity.cn_code": ["海关编码", "cn编码"],
    "dp:prod.mass.net_per_unit": ["产品净重", "单位净重"],
    "dp:prod.pcf.cradle_to_gate": ["产品碳足迹", "摇篮到大门"],
    "dp:prod.cbam.see_direct": ["直接隐含排放"],
    "dp:prod.cbam.see_indirect": ["间接隐含排放"],
    "dp:mat.rohs.compliant": ["rohs合规", "符合rohs"],
    "dp:mat.reach.svhc_present": ["高关注物质", "svhc"],
    "dp:social.remuneration.min_wage_compliance": ["最低工资"],
    "dp:social.collective_bargaining.coverage_rate": ["集体协议覆盖率", "集体谈判覆盖率"],
    "dp:social.workforce.temporary_count": ["临时合同员工", "临时员工人数"],
    "dp:social.health_safety.recordable_accidents": ["工伤事故", "可记录事故"],
    "dp:social.training.avg_hours_per_employee": ["人均培训时长", "人均培训小时"],
    "dp:gov.anticorruption.fines_amount": ["腐败罚款"],
    "dp:gov.policy.climate_transition_plan": ["气候转型计划"],
    "dp:gov.policy.human_rights_policy": ["人权政策"],
    "dp:supply.code_of_conduct.supplier_coc_in_place": ["供应商行为准则"],
}
# 德/英常见同义问法（v0.2 补充：Firmierung、Stromverbrauch gesamt、Herstellungsverfahren、Tarifbindung…）
GLOSSARY_EXTRA: dict[str, list[str]] = {
    "dp:org.identity.legal_name": ["firmierung", "firmenbezeichnung", "company name"],
    "dp:energy.consumption.electricity_total": ["stromverbrauch gesamt", "gesamtstromverbrauch", "power consumption", "strombezug"],
    "dp:prod.cbam.specific_electricity_consumption": ["electricity per tonne", "electricity consumed per tonne",
                                                      "strom pro tonne", "stromverbrauch je tonne"],
    "dp:energy.consumption.fuels_total": ["brennstoffeinsatz", "fuel consumption"],
    "dp:prod.cbam.production_route": ["herstellungsverfahren", "herstellungsroute", "production process"],
    "dp:env.ghg_target.target_year": ["zieljahr", "target year", "bis zu welchem jahr"],
    "dp:social.collective_bargaining.coverage_rate": ["tarifbindung", "tarifvertrag", "collective bargaining"],
    "dp:social.health_safety.recordable_accident_rate": ["accident rate", "unfallrate"],
    "dp:env.pollution.emissions_reported": ["pollutant emission", "schadstoffemission", "luftschadstoff"],
    "dp:cert.iso14001.valid_until": ["iso 14001 gultig", "iso 14001 valid until"],
    "dp:cert.iso50001.valid_until": ["iso 50001 gultig", "iso 50001 valid until"],
    "dp:cert.iatf16949.valid_until": ["iatf 16949 gultig", "iatf 16949 valid until"],
    "dp:mat.reach.svhc_present": ["svhc present", "frei von svhc", "svhc enthalten"],
    "dp:energy.procurement.eac_volume": ["guarantees of origin", "renewable energy certificates"],
    "dp:mat.recycled_content.steel_share": ["recyclingstahl", "recycled steel"],
    "dp:prod.pcf.reference_period": ["pcf reference period", "pcf bezugszeitraum"],
    "dp:env.waste.generated.non_hazardous": ["nicht gefahrlich abfall", "non hazardous waste"],
    # 行业常用缩写（OEM 表格里常见：S1/S2 LB/MB、THG、VZÄ、HR-Nr.、PDS）
    "dp:env.ghg.scope1_total": ["thg s1", "ghg s1", "s1 emission"],
    "dp:env.ghg.scope2_location_based": ["s2 lb", "scope 2 lb", "ghg s2 lb"],
    "dp:env.ghg.scope2_market_based": ["s2 mb", "scope 2 mb", "ghg s2 mb"],
    "dp:org.employees.fte": ["vza", "vollzeitaquivalent", "full time equivalent"],
    "dp:org.identity.registration_number": ["hr nr", "handelsregisternummer"],
    "dp:prod.pcf.primary_data_share": ["pds", "primary data share"],
}
for _src in (GLOSSARY_ZH, GLOSSARY_EXTRA):
    for _dp, _ps in _src.items():
        GLOSSARY.setdefault(_dp, []).extend(_ps)

_DATE_DE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b")
_DATE_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_YEAR = re.compile(r"\b(20\d{2})\b")
QUESTIONNAIRE_WORDS = ("fragebogen", "questionnaire", "selbstauskunft", "nachhaltigkeit", "sustainability", "cbam", "cdp",
                       "esg", "datenabfrage", "lieferantenabfrage", "pcf", "co2", "emission")


def instruction_hits(text: str) -> list[str]:
    from ..guard.injection import instruction_hits as _hits  # 规则只维护一处（07 §3.3）

    return _hits(text)


def _units_compatible(a: str | None, b: str | None) -> bool | None:
    if not a or not b:
        return None
    try:
        from ..units import default_units

        return bool(default_units().compatible(a, b))
    except Exception:
        return a == b


def _format_compatible(fmt: str | None, vt: str | None) -> float:
    if not fmt or not vt:
        return 1.0
    if fmt == vt:
        return 1.0
    if fmt == "text":  # belegt-io 无任何线索时的兜底题型（置信度 0.5），只是弱信号
        return 0.9
    pairs = {("enum", "bool"): 0.8, ("bool", "enum"): 0.7, ("text", "enum"): 0.8, ("enum", "text"): 0.6,
             ("number", "date"): 0.55, ("text", "file"): 0.5, ("file", "text"): 0.5}
    return pairs.get((fmt, vt), 0.35)


@lru_cache(maxsize=4096)
def phrase_regex(phrase: str) -> re.Pattern[str]:
    """短语 → 正则：各词按词首前缀匹配、按顺序出现，词间最多隔 3 个词。"""
    ws = [re.escape(w) for w in normalize(phrase).split() if w]
    gap = r"\w*(?:\W+\w+){0,3}?\W+"
    return re.compile(r"\b" + gap.join(ws) + r"\w*" if ws else r"(?!x)x")


_CJK_CHAR = re.compile("[\u3400-\u4dbf\u4e00-\u9fff]")


def alias_hit(text: str, phrases: list[str]) -> bool:
    qn = normalize(text)
    qn_compact = qn.replace(" ", "")
    for p in phrases:
        if not p:
            continue
        if _CJK_CHAR.search(p):  # 中文短语：无词界，按规范化后的子串匹配
            if normalize(p).replace(" ", "") in qn_compact:
                return True
        else:
            for m in phrase_regex(p).finditer(qn):
                # 否定前缀（"nicht gefährliche Abfälle"、"non-hazardous"）：短语本身未被否定时才算命中
                if _NEG_BEFORE.search(qn[:m.start()]) and not _NEG_RE.search(normalize(p)):
                    continue
                return True
    return False


_NEG_RE = re.compile(r"\b(nicht|non|kein\w*|not|no|un)\b")
_NEG_BEFORE = re.compile(r"\b(nicht|non|kein\w*|not|no)\s*$")


def score_candidate(q: dict[str, Any], c: dict[str, Any], *, lexical: bool = True) -> float:
    """MockLLM 的映射打分（0–1）：别名命中、标签词元覆盖、嵌入相似度、单位与题型兼容。``lexical=False`` 不计别名命中。"""
    qtext = " ".join(str(v) for v in (q.get("text") or {}).values()) if isinstance(q.get("text"), dict) else str(q.get("text", ""))
    qtok = set(tokens(qtext))
    phrases = list(GLOSSARY.get(c["datapoint_id"], [])) + list(c.get("aliases") or [])
    lex = 1.0 if lexical and alias_hit(qtext, phrases) else 0.0
    ltoks = set(tokens(c.get("label_de") or ""))
    etoks = set(tokens(c.get("label_en") or ""))
    ztoks = set(tokens(c.get("label_zh") or ""))

    def cover(lt: set[str]) -> float:  # 精确/前缀/错别字（Damerau ≤ 1）覆盖
        return fuzzy_cover(qtok, lt)
    overlap = max(cover(ltoks), cover(etoks), cover(ztoks))
    sim = float(c.get("similarity") or 0.0)
    if lex:
        conf = 0.88 + 0.05 * overlap + 0.03 * max(sim, 0.0)
    else:
        conf = 0.20 + 0.55 * overlap + 0.25 * max(sim, 0.0)
    uc = _units_compatible(q.get("unit_requested"), c.get("unit"))
    if uc is True:
        conf += 0.03
    elif uc is False:
        conf *= 0.6
    fc = _format_compatible(q.get("answer_format"), c.get("value_type"))
    # 题型是切题时启发式推断的（"0,1 %"、"2025" 会让是非题被判为 number）：别名命中时题型不符只降到自动采用线以下
    # （仍转人工确认），不把正确候选挤出 Top-1
    conf *= max(fc, 0.92) if lex and fc < 1.0 else fc
    return round(max(0.0, min(conf, 0.98)), 3)


DISAGREE_CAP = 0.80     # < PROMO-MAP-0.85：词面与语义不一致时必须转人工
DISAGREE_MARGIN = 0.03  # 另一候选的嵌入相似度高出 Top-1 的幅度
SWAP_MARGIN = 0.05      # 不计别名时另一候选须明显更好才交换排序


def _question_coverage(q: dict[str, Any], c: dict[str, Any]) -> float:
    """题干词元中能被候选的标签（德/英/中）、别名与术语表短语解释的比例（近似匹配）。"""
    from .embedding import near

    qtext = " ".join(str(v) for v in (q.get("text") or {}).values()) if isinstance(q.get("text"), dict) else str(q.get("text", ""))
    qtok = set(tokens(qtext))
    if not qtok:
        return 0.0
    vocab: set[str] = set()
    for s in [c.get("label_de"), c.get("label_en"), c.get("label_zh")] + list(GLOSSARY.get(c["datapoint_id"], [])) + \
            list(c.get("aliases") or []):
        vocab |= set(tokens(s or ""))
    return sum(1 for t in qtok if any(near(t, v) for v in vocab)) / len(qtok)


def _self_consistency(scored: list[dict[str, Any]], cands: list[dict[str, Any]], q: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """自检：Top-1 只靠别名/词面命中，而另一候选在语义（嵌入相似度）上明显更近 → 两路证据矛盾，置信度封顶 0.80。

    典型是错别字题（"Net mass of the relevant commdoity?"：别名 "net mass" 命中产品净重，但字符 3-gram 相似度更接近
    EUDR 商品数量），模型不应在这种情况下给出可自动采用的高置信度（11 M-MAP-04 ★ 低置信度路由召回）。"""
    if not scored:
        return scored
    sim = {c["datapoint_id"]: float(c.get("similarity") or 0.0) for c in cands}
    top = scored[0]
    best_other = max((s for d, s in sim.items() if d != top["datapoint_id"]), default=0.0)
    if top["confidence"] >= 0.85 and best_other - sim.get(top["datapoint_id"], 0.0) > DISAGREE_MARGIN:
        order = list(scored)
        if q is not None and len(order) > 1:
            # 词面证据被语义证据否定：若另一候选能"解释"明显更多的题干词元（含错别字近似），则它排第一（但都 < 自动采用线）
            by_id = {c["datapoint_id"]: c for c in cands}
            a, b = by_id.get(order[0]["datapoint_id"]), by_id.get(order[1]["datapoint_id"])
            if a and b and _question_coverage(q, b) >= _question_coverage(q, a) + SWAP_MARGIN:
                order[0], order[1] = order[1], order[0]
        # 整个列表都压到自动采用线以下，并保持上面的顺序（严格递减）
        out, ceiling = [], DISAGREE_CAP
        for c in order:
            conf = round(min(c["confidence"], ceiling), 3)
            out.append({**c, "confidence": conf})
            ceiling = max(0.0, conf - 0.01)
        return out
    return scored


class MockLLM:
    provider = "mock"

    def __init__(self, script: dict[str, Any] | None = None, *, fail_tasks: set[str] | None = None,
                 invalid_first: set[str] | None = None, prices: PriceTable | None = None) -> None:
        self.script = dict(script or {})
        self.fail_tasks = set(fail_tasks or ())
        self.invalid_first = set(invalid_first or ())
        self._invalid_done: set[str] = set()
        self.prices = prices or PriceTable()
        self.calls: list[LLMRequest] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def complete(self, req: LLMRequest) -> LLMResponse:
        with self._lock:
            self.calls.append(req)
        if req.task in self.fail_tasks or "*" in self.fail_tasks:
            raise ModelUnavailable(f"mock: {req.task} unavailable")
        model = "mock-" + TIER_DEFAULT_MODEL.get(req.tier, "mistral-large-3")
        if req.task in self.invalid_first and req.task not in self._invalid_done:
            self._invalid_done.add(req.task)
            out: Any = {"__invalid__": True}
        elif req.task in self.script:
            s = self.script[req.task]
            if callable(s):
                out = s(req)
            elif isinstance(s, list):
                out = s.pop(0) if len(s) > 1 else s[0]
            else:
                out = s
        else:
            handler: Callable[[LLMRequest], Any] | None = getattr(self, "_t_" + req.task, None)
            if handler is None:
                raise ModelUnavailable(f"mock: no handler for task {req.task!r}")
            out = handler(req)
        content = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        tin = estimate_tokens(req.system) + estimate_tokens(req.user or json.dumps(req.data, ensure_ascii=False, default=str))
        tout = estimate_tokens(content)
        return LLMResponse(content=content, model=model, provider=self.provider, tokens_in=tin, tokens_out=tout,
                           cost_eur=self.prices.cost_eur(model, tin, tout, req.tier))

    # ------------------------------------------------------------------ handlers
    def _t_triage(self, req: LLMRequest) -> dict[str, Any]:
        d = req.data
        visible = " ".join(str(d.get(k) or "") for k in ("subject", "body"))
        preview = " ".join(d.get("attachment_preview") or [])
        text = (visible + " " + preview + " " + " ".join(d.get("filenames") or [])).lower()
        is_q = any(w in text for w in QUESTIONNAIRE_WORDS) or bool(d.get("attachment_parsable"))
        due = None
        m = _DATE_DE.search(visible)
        if m:
            due = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        else:
            m2 = _DATE_ISO.search(visible)
            if m2:
                due = m2.group(0)
        years = [int(y) for y in _YEAR.findall(preview + " " + visible)]
        period = None
        cand = [y for y in years if not due or str(y) != due[:4]] or years
        if cand:
            y = min(cand)
            period = {"start": f"{y}-01-01", "end": f"{y}-12-31"}
        lang = d.get("language_hint") or ("de" if re.search(r"\b(und|der|die|bitte|ihre?)\b", text) else "en")
        conf = 0.6 + (0.2 if is_q else 0) + (0.1 if d.get("known_customer") else 0) + (0.05 if d.get("attachment_parsable") else 0)
        title = (d.get("subject") or (d.get("filenames") or ["Fragebogen"])[0])[:300]
        return {"is_questionnaire": is_q, "title": title, "language": lang, "due_date": due, "reporting_period": period,
                "priority": "high" if due and due[:7] <= "2026-12" else "normal", "confidence": round(min(conf, 0.97), 2)}

    def _t_map_questions(self, req: LLMRequest) -> dict[str, Any]:
        out = []
        for q in req.data.get("questions") or []:
            cands = req.data.get("candidates", {}).get(q["qid"], [])
            scored = sorted(({"datapoint_id": c["datapoint_id"], "confidence": score_candidate(q, c)} for c in cands),
                            key=lambda x: (-x["confidence"], x["datapoint_id"]))[:3]
            scored = _self_consistency(scored, cands, q)
            scored.sort(key=lambda x: (-x["confidence"], x["datapoint_id"]))
            out.append({"qid": q["qid"], "candidates": scored})
        return {"mappings": out}

    def _t_extract_fields(self, req: LLMRequest) -> dict[str, Any]:
        from belegt_io import extract_quantities

        d = req.data
        chunk = d.get("chunk_text") or ""
        labels = [normalize(x) for x in d.get("labels") or [] if x]
        best = None
        for line in chunk.splitlines():
            ln = normalize(line)
            if labels and not any(all(t in ln for t in lab.split()) for lab in labels):
                continue
            for q in extract_quantities(line, lang=d.get("lang") or "de"):
                if q.value is None:
                    continue
                if d.get("unit_expected") and q.unit and not _units_compatible(q.unit, d["unit_expected"]):
                    continue
                best = {"value_text": q.raw.split()[0] if q.raw else str(q.value), "unit_text": q.unit_raw or q.unit or "",
                        "page": d.get("page"), "quote": line.strip()[:200]}
                break
            if best:
                break
        return best or {"value_text": None, "unit_text": None, "page": d.get("page"), "quote": ""}

    def _t_reply_extract(self, req: LLMRequest) -> dict[str, Any]:
        from belegt_io import extract_quantities

        d = req.data
        text = d.get("reply_text") or ""
        vals = []
        for dp in d.get("datapoints") or []:
            kw = [normalize(k) for k in dp.get("keywords") or []]
            for line in text.splitlines():
                ln = normalize(line)
                if kw and not any(all(t in ln for t in k.split()) for k in kw):
                    continue
                qs = [q for q in extract_quantities(line, lang=d.get("lang") or "en") if q.value is not None]
                if qs:
                    q = qs[0]
                    vals.append({"datapoint_id": dp["datapoint_id"], "value_text": q.raw.split()[0], "unit_text": q.unit_raw or q.unit or "",
                                 "quote": line.strip()[:200]})
                    break
        return {"values": vals}

    def _t_draft_answer(self, req: LLMRequest) -> dict[str, Any]:
        d = req.data
        vt = d.get("value_type")
        src = (d.get("reused") or []) + (d.get("evidence") or [])
        if not src:
            return {"value": None, "sentences": [], "legal_commitment": False, "is_estimate": False, "abstain": True}
        s = src[0]
        val = s.get("value")
        if vt == "bool":
            val = val if isinstance(val, bool) else str(val).strip().lower() in ("true", "ja", "yes", "1")
        elif vt == "enum" and d.get("options") and isinstance(val, str):
            opts = d["options"]
            hit = next((o for o in opts if normalize(o) == normalize(val)), None) or \
                next((o for o in opts if normalize(val) in normalize(o) or normalize(o) in normalize(val)), None)
            val = hit or val
        return {"value": val, "sentences": [{"text": d.get("note_template") or "", "cites": [s["ref"]]}] if d.get("note_template") else [],
                "legal_commitment": False, "is_estimate": False, "abstain": False}

    def _t_explain_conflict(self, req: LLMRequest) -> dict[str, Any]:
        rid = req.data.get("rule_id", "")
        return {"de": f"Widerspruch nach Regel {rid}: die Werte der verknüpften Objekte weichen voneinander ab.",
                "en": f"Conflict under rule {rid}: the values of the linked objects differ."}

    def _t_chase_slots(self, req: LLMRequest) -> dict[str, Any]:
        d = req.data
        allowed = d.get("allowed_templates") or []
        lang = d.get("language") or "en"
        tid = next((t for t in allowed if f".{lang}@" in t), allowed[0] if allowed else None)
        return {"template_id": tid, "language": lang, "slots": {"product_summary": d.get("product_summary") or ""}}

    def _t_injection_classify(self, req: LLMRequest) -> dict[str, Any]:
        hits = instruction_hits(req.data.get("text") or "")
        return {"is_instruction": bool(hits), "score": 0.95 if hits else 0.02}

    def _t_reg_radar(self, req: LLMRequest) -> dict[str, Any]:
        src = req.data.get("source") or {}
        text = req.data.get("source_text") or src.get("text") or ""  # 原文经 external 数据块传入（RunContext.ask）
        m = _DATE_ISO.search(text) or _DATE_DE.search(text)
        date = None
        quote = ""
        if m:
            date = m.group(0) if "-" in m.group(0) else f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            s = max(0, m.start() - 60)
            quote = text[s:m.end() + 20]
        art = re.search(r"(Art(?:icle|ikel)?\.?\s*\d+[a-z]?)", text)
        return {"celex": src.get("celex"), "article": art.group(1) if art else None, "effective_date": date,
                "affected_template_ids": src.get("template_hints") or [],
                "affected_datapoint_ids": src.get("datapoint_hints") or [], "quote": quote}

    def _t_sales_answer(self, req: LLMRequest) -> dict[str, Any]:
        ans = req.data.get("answers") or []
        if not ans:
            return {"answer_template": "NO_AUTHORIZED_DATA", "cites": []}
        return {"answer_template": " ".join("{{ref:%s}}" % a["ref"] for a in ans), "cites": [a["ref"] for a in ans]}

    def _t_portal_locate(self, req: LLMRequest) -> dict[str, Any]:
        target = normalize(req.data.get("target_label") or "")
        for el in req.data.get("elements") or []:
            if target and target in normalize(el.get("label") or ""):
                return {"element_id": el["id"]}
        return {"element_id": None}


def decimal_or_none(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v))
    except Exception:
        return None
