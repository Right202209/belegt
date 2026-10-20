# Spec: 06 §1.5（LLM 解释的边界：只生成文字说明；输出经 guard 数字 token 扫描，不通过即回退确定性模板）、04 §2.6
"""冲突解释。

- ``Explainer`` 协议：``explain(conflict, rule, context) -> I18nText | None``。可注入 LLM 实现（由调用方基于
  ``belegt_core.llm`` 等自行包装）；本模块不依赖 llm 包。
- ``TemplateExplainer``：确定性德/英/中三语模板（只引用 ``detail.values`` 中的数值，不含规则号等数字 token）。
- ``guarded_explanation``：先试注入的解释器 → guard 扫描（与 ``GRAPH_NUM_IN_TEXT`` 同算法）→ 失败/异常回退模板。
"""
from __future__ import annotations

from typing import Any, Protocol

from ..rules.numtext import allowed_numbers, unsourced_numbers


class Explainer(Protocol):
    def explain(self, conflict: dict[str, Any], rule: Any, context: dict[str, Any] | None = None) -> dict[str, str] | None: ...


_CATEGORY = {
    "cross_customer": ("kundenübergreifende Abweichung", "cross-customer divergence", "跨客户不一致"),
    "cross_period": ("Abweichung zwischen Perioden", "period-over-period deviation", "跨期间偏差"),
    "validity": ("Gültigkeit/Aktualität", "validity / freshness", "有效期"),
    "unit": ("Einheit/Größenordnung", "unit / magnitude", "单位与量纲"),
    "period": ("Zeitraum/Abgrenzung", "period / boundary", "期间与口径"),
    "calculation": ("Rechenkonsistenz", "calculation consistency", "计算一致性"),
    "reference_integrity": ("Referenzintegrität", "reference integrity", "引用完整性"),
    "estimate": ("Schätzwert", "estimate", "估算标识"),
    "legal": ("rechtlich bindende Erklärung", "legally binding statement", "法律承诺"),
    "value_chain_cap": ("Wertschöpfungskettenobergrenze", "value-chain cap", "价值链上限"),
}

_HINT = {
    "cross_customer": ("Bitte Methodik vereinheitlichen oder die Abweichung mit Kundenvorgabe dokumentieren.",
                       "Align the methodology or document the divergence with the customer requirement.",
                       "请统一口径，或按客户要求记录差异。"),
    "unit": ("Bitte die ursprüngliche Einheit im Beleg prüfen.", "Please check the original unit in the source document.",
             "请核对原始凭证中的单位。"),
    "validity": ("Bitte einen gültigen Nachweis beschaffen oder die Verlängerung einleiten.",
                 "Please obtain valid evidence or start the renewal.", "请补充有效证据或发起续期。"),
    "legal": ("Bitte den Originaltext lesen und einzeln freigeben.", "Please read the original text and approve it individually.",
              "请阅读原文并单条确认。"),
}
_DEFAULT_HINT = ("Mögliche Ursachen: Einheit, Abgrenzung, Zeitraum oder Erfassungsfehler.",
                 "Possible causes: unit, boundary, period or data-entry error.", "可能原因：单位、口径、期间或录入错误。")


def _vals(detail: dict[str, Any], numeric_only: bool) -> list[str]:
    out = []
    for v in detail.get("values") or []:
        if numeric_only and detail.get("value_encoding") == "ordinal":
            continue
        s = v.get("value")
        if v.get("unit") and v.get("unit") != "d":
            s = f"{s} {v['unit']}"
        elif v.get("unit") == "d":
            s = f"{s} d"
        out.append(s)
    return out


class TemplateExplainer:
    """确定性模板（LLM 不可用或 guard 不通过时使用，06 §1.5）。"""

    def explain(self, conflict: dict[str, Any], rule: Any, context: dict[str, Any] | None = None) -> dict[str, str]:
        cat = getattr(rule, "category", None) or (context or {}).get("category") or "calculation"
        de_c, en_c, zh_c = _CATEGORY.get(cat, (cat, cat, cat))
        hint = _HINT.get(cat, _DEFAULT_HINT)
        vals = _vals(conflict.get("detail") or {}, numeric_only=True)
        dp = conflict.get("datapoint_id", "").split("@", 1)[0]
        sev = conflict.get("severity")
        if vals:
            v = ", ".join(vals)
            de = f"Prüfregel ({de_c}) für {dp}: die Werte {v} sind nicht konsistent. {hint[0]}"
            en = f"Consistency check ({en_c}) for {dp}: the values {v} are not consistent. {hint[1]}"
            zh = f"一致性检查（{zh_c}）{dp}：数值 {v} 不一致。{hint[2]}"
        else:
            de = f"Prüfregel ({de_c}) für {dp}: die Angaben sind nicht konsistent. {hint[0]}"
            en = f"Consistency check ({en_c}) for {dp}: the statements are not consistent. {hint[1]}"
            zh = f"一致性检查（{zh_c}）{dp}：相关答案不一致。{hint[2]}"
        if sev == "blocking":
            de += " Blockiert die Übermittlung."
            en += " Blocks submission."
            zh += "（阻断提交）"
        return {"de": de, "en": en, "zh": zh}


def guard_ok(text: dict[str, str], detail: dict[str, Any]) -> bool:
    """guard 数字扫描：解释中的每个数字 token 都必须出现在 ``detail`` 中（06 §1.5、04 §2.6）。"""
    if not isinstance(text, dict) or not text or not all(isinstance(v, str) for v in text.values()):
        return False
    return not unsourced_numbers(text.values(), allowed_numbers([detail]))


def guarded_explanation(conflict: dict[str, Any], rule: Any, explainer: Explainer | None = None,
                        context: dict[str, Any] | None = None) -> tuple[dict[str, str] | None, str]:
    """返回 (explanation, source)：source ∈ {explainer, template, none}。"""
    detail = conflict.get("detail") or {}
    if explainer is not None:
        try:
            out = explainer.explain(conflict, rule, context)
            if out and guard_ok(out, detail):
                return {k: v for k, v in out.items() if k in ("de", "en", "zh")}, "explainer"
        except Exception:
            pass
    tpl = TemplateExplainer().explain(conflict, rule, context)
    if guard_ok(tpl, detail):
        return tpl, "template"
    return None, "none"


__all__ = ["Explainer", "TemplateExplainer", "guarded_explanation", "guard_ok"]
