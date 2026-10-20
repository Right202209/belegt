# Spec: 05 §9（测试向量格式与容差）；FR-209（计算一致性测试套件）
"""测试向量加载与校验。

向量文件格式（每个 TV 一个 JSON；多个子情形放在 ``cases``）::

    {"id": "TV-01", "title": "...", "spec": "05 §9 TV-01",
     "cases": [{"id": "TV-01", "description": "...",
                "request": {method_id, methodology_version, factor_set_version, boundary, period, params, inputs,
                            customer_scope?},
                "factor_set_ref": {"id": "fs:...", "path": "fs/<file>.yaml", "rows_hash": "sha256:..."},
                "expected": {"result": {...}, "breakdown": [...], "input_hash": "sha256:...",
                             "output_hash"?: "...", "error_code"?: "CALC_..."},
                "tolerance": "0",
                "unit_checks"?: [{"value", "from", "to", "expected"}]}]}

也接受单情形写法（顶层直接是 ``request``/``factor_set_ref``/``expected``）。
``factor_set_ref.path`` 先相对向量目录解析，找不到时回退到随包分发的同名因子库文件。
容差为 0：``result`` 与 ``breakdown`` 逐位相等（05 §9）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .engine import calculate
from .errors import CalcError
from .factors import bundled_dir, load_factor_set
from .types import FactorSet
from .units import convert
from .dec import norm


@dataclass
class CaseResult:
    file: str
    case_id: str
    ok: bool
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"file": self.file, "case": self.case_id, "ok": self.ok, "failures": self.failures}


def _resolve_fs(ref: dict, base: Path, cache: dict[str, FactorSet]) -> FactorSet:
    path = ref.get("path")
    candidates: list[Path] = []
    if path:
        candidates.append(base / path)
        candidates.append(bundled_dir() / Path(path).name)
    key = None
    for c in candidates:
        if c.exists():
            key = str(c.resolve())
            break
    if key is None:
        if ref.get("id"):
            fs = cache.get(ref["id"]) or load_factor_set(ref["id"])
            cache[ref["id"]] = fs
            return fs
        raise FileNotFoundError(f"factor set {ref} not found")
    if key not in cache:
        cache[key] = load_factor_set(key)
    return cache[key]


def iter_cases(doc: dict) -> list[dict]:
    if "cases" in doc:
        return list(doc["cases"])
    return [doc]


def verify_case(case: dict, base: Path, file: str, cache: dict[str, FactorSet] | None = None) -> CaseResult:
    cache = {} if cache is None else cache
    cid = case.get("id", "?")
    fails: list[str] = []
    tol = case.get("tolerance", "0")
    if tol != "0":
        fails.append(f"tolerance {tol!r}: belegt-calc requires exact equality (05 §9)")
    try:
        fs = _resolve_fs(case["factor_set_ref"], base, cache)
    except Exception as e:  # noqa: BLE001
        return CaseResult(file, cid, False, [f"factor set: {e}"])
    ref_hash = case["factor_set_ref"].get("rows_hash")
    if ref_hash and ref_hash != fs.rows_hash:
        fails.append(f"rows_hash {fs.rows_hash} != factor_set_ref {ref_hash}")
    req = dict(case["request"])
    if req.get("factor_set_version") not in (None, fs.id):
        fails.append(f"request factor_set_version {req.get('factor_set_version')} != {fs.id}")
    exp = case.get("expected", {})
    try:
        res = calculate(
            req["method_id"], req["methodology_version"], fs, req["inputs"], req.get("params") or {},
            req["boundary"], req["period"], req.get("customer_scope", "all"),
        )
    except CalcError as e:
        if exp.get("error_code") == e.code:
            pass
        else:
            fails.append(f"unexpected CalcError {e.code}: {e.detail}")
        for uc in case.get("unit_checks", []):
            _unit_check(uc, fails)
        return CaseResult(file, cid, not fails, fails)
    if exp.get("error_code"):
        fails.append(f"expected error {exp['error_code']}, got result {res.result['value']}")
    else:
        if "result" in exp and exp["result"] != res.result:
            fails.append(f"result {res.result} != expected {exp['result']}")
        if "breakdown" in exp and exp["breakdown"] != res.breakdown:
            fails.append(f"breakdown {res.breakdown} != expected {exp['breakdown']}")
        if "input_hash" in exp and exp["input_hash"] != res.input_hash:
            fails.append(f"input_hash {res.input_hash} != expected {exp['input_hash']}")
        if "output_hash" in exp and exp["output_hash"] != res.output_hash:
            fails.append(f"output_hash {res.output_hash} != expected {exp['output_hash']}")
    for uc in case.get("unit_checks", []):
        _unit_check(uc, fails)
    return CaseResult(file, cid, not fails, fails)


def _unit_check(uc: dict, fails: list[str]) -> None:
    got = norm(convert(uc["value"], uc["from"], uc["to"]))
    if got != uc["expected"]:
        fails.append(f"unit {uc['value']} {uc['from']}→{uc['to']}: {got} != {uc['expected']}")


def verify_file(path: Path, cache: dict[str, FactorSet] | None = None) -> list[CaseResult]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return [verify_case(c, Path(path).parent, Path(path).name, cache) for c in iter_cases(doc)]


def verify_dir(directory: Path) -> list[CaseResult]:
    cache: dict[str, FactorSet] = {}
    out: list[CaseResult] = []
    files = sorted(p for p in Path(directory).glob("*.json"))
    if not files:
        raise FileNotFoundError(f"no vector files in {directory}")
    for p in files:
        out.extend(verify_file(p, cache))
    return out


__all__ = ["CaseResult", "iter_cases", "verify_case", "verify_dir", "verify_file", "Any"]
