# Spec: 03 §4.5（input_hash / output_hash）、SSOT-5（answer_key）、§3.9（conflict_key）、05 §8.1
"""业务哈希：``input_hash``、``output_hash``、``answer_key``、``conflict_key``。

R-HASH：``belegt_calc`` 可导入时调用 ``belegt_calc.input_hash(m)``（05 §8.1），否则用本模块的 03 §4.5 参考实现。
"""
from __future__ import annotations

from typing import Any

from .canonical import hash_obj, normalize_dec
from .ids import datapoint_base


def build_input_m(calc: dict[str, Any], factor_set_rows_hash: str) -> dict[str, Any]:
    """由 Calculation 字段构造 03 §4.5 的 M（inputs 按 name 升序、DEC-STR 规范化）。"""
    inputs = []
    for i in sorted(calc.get("inputs") or [], key=lambda x: x["name"]):
        inputs.append({"name": i["name"], "ref": i["ref"], "value": normalize_dec(i["value"]), "unit": i["unit"]})
    return {
        "method_id": calc.get("method_id"),
        "methodology_version": calc.get("methodology_version"),
        "factor_set_version": calc.get("factor_set_version"),
        "factor_set_rows_hash": factor_set_rows_hash,
        "boundary": calc.get("boundary"),
        "period": calc.get("period"),
        "params": calc.get("params") if calc.get("params") is not None else {},
        "inputs": inputs,
    }


def reference_input_hash(m: dict[str, Any]) -> str:
    """03 §4.5 参考实现：``sha256:`` + hex(SHA-256(UTF-8(JCS(M))))。"""
    m = dict(m)
    m["inputs"] = sorted(
        ({**i, "value": normalize_dec(i["value"])} for i in m.get("inputs") or []), key=lambda x: x["name"]
    )
    if m.get("params") is None:
        m["params"] = {}
    return hash_obj(m)


def calc_engine():
    try:
        import belegt_calc  # type: ignore

        if callable(getattr(belegt_calc, "input_hash", None)):
            return belegt_calc
    except Exception:
        pass
    return None


def input_hash(m: dict[str, Any]) -> str:
    """优先 ``belegt_calc.input_hash``；引擎拒绝输入（如 params 非字符串，05 §8.1）时抛出 ``ValueError``。"""
    eng = calc_engine()
    if eng is not None:
        try:
            return eng.input_hash(m)
        except Exception as e:  # CalcError 等
            raise ValueError(f"belegt_calc.input_hash rejected M: {getattr(e, 'code', '')} {e}") from e
    return reference_input_hash(m)


def input_hash_impl() -> str:
    return "belegt_calc.input_hash" if calc_engine() is not None else "belegt_core.hashing.reference_input_hash"


def output_hash(result: dict[str, Any], breakdown: list[dict[str, Any]] | None) -> str:
    eng = calc_engine()
    if eng is not None and callable(getattr(eng, "output_hash", None)):
        try:
            return eng.output_hash(result, breakdown if breakdown is not None else [])  # type: ignore[arg-type]
        except Exception:
            pass
    # 与 belegt_calc.output_hash 一致：缺省 breakdown 视为 []（03 §4.5-5 对 {result, breakdown} 求哈希）
    return hash_obj({"result": result, "breakdown": breakdown if breakdown is not None else []})


def normalize_customer_scope(cs: Any) -> Any:
    if cs == "all" or cs is None:
        return cs
    return sorted(set(cs))


def answer_key(org_id: str, datapoint_id: str, boundary: str, consolidation: str | None, period: dict,
               customer_scope: Any) -> str:
    """SSOT-5：``org_id + datapoint_id(不带 SemVer) + boundary + consolidation + period + customer_scope``。"""
    return hash_obj({
        "org_id": org_id,
        "datapoint_id": datapoint_base(datapoint_id),
        "boundary": boundary,
        "consolidation": consolidation,
        "period": period,
        "customer_scope": normalize_customer_scope(customer_scope),
    })


def answer_key_of(ans: dict[str, Any]) -> str:
    return answer_key(ans["org_id"], ans["datapoint_id"], ans["boundary"], ans.get("consolidation"),
                      ans["period"], ans["customer_scope"])


def conflict_key(rule_id: str, subject_refs: list[str]) -> str:
    """去重键：``rule_id + subject 集合``（按逻辑 ID 去版本、排序）。"""
    ids = sorted({r.split("#", 1)[0] for r in subject_refs})
    return hash_obj({"rule_id": rule_id, "subjects": ids})
