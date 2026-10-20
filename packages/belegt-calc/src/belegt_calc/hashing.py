# Spec: 03 §4.5（input_hash / output_hash）；05 §1.4（可复现四元组）、§7.1（rows_hash）
"""规范化与哈希。

- JCS：RFC 8785 JSON Canonicalization Scheme。Belegt 的哈希原文只含字符串、整数、布尔、null、
  对象与数组（数字一律 DEC-STR），因此本实现拒绝浮点数；整数按十进制写出（与 ES6 Number 序列化
  在 |n| < 2^53 时一致）。对象键按 UTF-16 码元排序；字符串先做 NFC 规范化（03 §4.5-3）。
- ``input_hash = "sha256:" + hex(SHA-256(UTF-8(JCS(M))))``，M 只取 03 §4.5 列出的 8 个键。
- ``output_hash`` 对 ``{"result": ..., "breakdown": ...}`` 求哈希（03 §3.6、§4.5-5）。
- ``rows_hash`` = JCS（按 ``source_row_id`` 升序的行数组）的 SHA-256（05 §1.4、§7.1）。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Iterable, Mapping

from .dec import norm_dec_str
from .errors import CALC_DEC_INVALID, CALC_INPUT_MISSING, CalcError

M_KEYS = (
    "method_id",
    "methodology_version",
    "factor_set_version",
    "factor_set_rows_hash",
    "boundary",
    "period",
    "params",
    "inputs",
)
_PINNED_REF = re.compile(r"^[a-z]+_[0-9A-Za-z]+#v[1-9][0-9]*$")
_MAX_SAFE_INT = 2**53 - 1


def _jcs_str(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    out = ['"']
    for ch in s:
        c = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif c < 0x20:
            out.append(f"\\u{c:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_key(k: str) -> bytes:
    return unicodedata.normalize("NFC", k).encode("utf-16-be")


def jcs(obj: Any) -> str:
    """RFC 8785 规范化（不支持浮点数）。"""
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, int):
        if abs(obj) > _MAX_SAFE_INT:
            raise CalcError(CALC_DEC_INVALID, "integer outside IEEE-754 safe range in canonical JSON")
        return str(obj)
    if isinstance(obj, float):
        raise CalcError(CALC_DEC_INVALID, "floats are not allowed in canonical JSON; use DEC-STR")
    if isinstance(obj, str):
        return _jcs_str(obj)
    if isinstance(obj, Mapping):
        items = []
        seen: set[str] = set()
        for k in obj:
            if not isinstance(k, str):
                raise TypeError("JSON object keys must be strings")
            nk = unicodedata.normalize("NFC", k)
            if nk in seen:
                raise ValueError(f"duplicate key after NFC normalization: {k!r}")
            seen.add(nk)
        for k in sorted(obj, key=_utf16_key):
            items.append(_jcs_str(k) + ":" + jcs(obj[k]))
        return "{" + ",".join(items) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(jcs(x) for x in obj) + "]"
    raise TypeError(f"type {type(obj).__name__} is not JSON-serializable for JCS")


def sha256_jcs(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(jcs(obj).encode("utf-8")).hexdigest()


def _drop_nulls(obj: Any) -> Any:
    """03 §4.5-3：``null`` 字段删除（空对象保留）。"""
    if isinstance(obj, Mapping):
        return {k: _drop_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_drop_nulls(x) for x in obj]
    return obj


def canonical_inputs(inputs: Iterable[Mapping[str, Any]]) -> list[dict]:
    """inputs 规范化：按 name 升序；name 唯一；ref 钉住版本；value 规范化 DEC-STR（03 §4.5-1/2）。"""
    out: list[dict] = []
    names: set[str] = set()
    for i in inputs:
        if hasattr(i, "to_dict"):
            i = i.to_dict()  # InputValue
        name = i.get("name")
        if not isinstance(name, str) or not name:
            raise CalcError(CALC_INPUT_MISSING, f"input without name: {i!r}")
        if name in names:
            raise CalcError(CALC_DEC_INVALID, f"duplicate input name {name!r}")
        names.add(name)
        ref = i.get("ref")
        if not isinstance(ref, str) or not _PINNED_REF.match(ref):
            raise CalcError(CALC_INPUT_MISSING, f"input {name!r}: ref must be a pinned ObjectRef '<id>#v<n>', got {ref!r}")
        d = {
            "name": name,
            "ref": ref,
            "value": norm_dec_str(i.get("value"), what=f"input {name}") if i.get("value") is not None else None,
            "unit": i.get("unit"),
        }
        if d["value"] is None:
            raise CalcError(CALC_INPUT_MISSING, f"input {name!r} has no value")
        if not isinstance(d["unit"], str) or not d["unit"]:
            raise CalcError(CALC_INPUT_MISSING, f"input {name!r} has no unit")
        out.append(d)
    out.sort(key=lambda x: _utf16_key(x["name"]))
    return out


def hash_preimage(m: Mapping[str, Any]) -> dict:
    """构造 03 §4.5 的原文 M（只取 8 个键，规范化 inputs 与 period，删除 null）。"""
    missing = [k for k in M_KEYS if k not in m or (m.get(k) is None and k != "params")]
    if missing:
        raise CalcError(CALC_INPUT_MISSING, f"input_hash preimage missing keys: {missing}")
    period = m["period"]
    if hasattr(period, "to_dict"):
        period = period.to_dict()
    params = m.get("params") or {}
    for k, v in params.items():
        if not isinstance(v, str):
            raise CalcError(CALC_DEC_INVALID, f"params[{k!r}] must be a string (05 §8.1)")
    pre = {
        "method_id": m["method_id"],
        "methodology_version": m["methodology_version"],
        "factor_set_version": m["factor_set_version"],
        "factor_set_rows_hash": m["factor_set_rows_hash"],
        "boundary": m["boundary"],
        "period": {"start": period["start"], "end": period["end"]},
        "params": dict(params),
        "inputs": canonical_inputs(m["inputs"]),
    }
    return _drop_nulls(pre)


def input_hash(m: Mapping[str, Any]) -> str:
    """03 §4.5 参考实现。``m`` 可以是完整请求/计算对象，只取 M 的 8 个键。"""
    return sha256_jcs(hash_preimage(m))


def output_hash(result: Mapping[str, Any], breakdown: list[Mapping[str, Any]] | None) -> str:
    """``sha256(JCS({"result": result, "breakdown": breakdown}))``（03 §3.6、§4.5-5）。"""
    return sha256_jcs({"result": dict(result), "breakdown": [dict(b) for b in (breakdown or [])]})


def rows_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    """FactorSet 行哈希：按 ``source_row_id`` 升序的行数组（完整行对象）JCS 后 SHA-256（05 §7.1）。"""
    rs = [dict(r) for r in rows]
    rs.sort(key=lambda r: _utf16_key(str(r["source_row_id"])))
    return sha256_jcs(rs)
