# Spec: 03 §2.2（DEC-STR）；01 AC-503-1（导出前 schema 校验）
"""导出器公共件：Decimal 精确 JSON 序列化、随包 JSON Schema 校验、导出校验错误。"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft4Validator, Draft202012Validator, FormatChecker

from ..models import BelegtIOError, IOErrorCode
from ..numbers import to_dec_str


class ExportValidationError(BelegtIOError):
    def __init__(self, fmt: str, errors: list[str]):
        super().__init__(IOErrorCode.EXPORT_INVALID, f"{fmt} export invalid: " + "; ".join(errors[:10]))
        self.format = fmt
        self.errors = errors


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    return json.loads((resources.files("belegt_io") / "schemas" / name).read_text(encoding="utf-8"))


def _enc(v: Any, indent: int | None, level: int) -> str:
    nl = "\n" if indent is not None else ""
    pad = " " * (indent * (level + 1)) if indent is not None else ""
    pad_end = " " * (indent * level) if indent is not None else ""
    sep = "," + nl if indent is not None else ","
    colon = ": " if indent is not None else ":"
    if v is None or isinstance(v, bool):
        return json.dumps(v)
    if isinstance(v, Decimal):
        return to_dec_str(v)  # JSON number 字面量，不经 float
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError("non-finite float")
        return to_dec_str(Decimal(repr(v)))
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, datetime):
        return json.dumps(v.isoformat())
    if isinstance(v, date):
        return json.dumps(v.isoformat())
    if isinstance(v, dict):
        if not v:
            return "{}"
        items = [f"{pad}{json.dumps(str(k), ensure_ascii=False)}{colon}{_enc(x, indent, level + 1)}" for k, x in v.items()]
        return "{" + nl + sep.join(items) + nl + pad_end + "}"
    if isinstance(v, (list, tuple)):
        if not v:
            return "[]"
        items = [f"{pad}{_enc(x, indent, level + 1)}" for x in v]
        return "[" + nl + sep.join(items) + nl + pad_end + "]"
    raise TypeError(f"cannot serialize {type(v).__name__}")


def dumps_json(obj: Any, indent: int | None = 2) -> str:
    """JSON 序列化：`Decimal` 输出为精确数字字面量（Catena-X 用 number）；字符串十进制原样（PACT 用 string）。"""
    return _enc(obj, indent, 0)


def loads_json(s: str) -> Any:
    return json.loads(s, parse_float=Decimal, parse_int=Decimal)


def schema_errors(doc: Any, schema_name: str, draft: str = "2020-12") -> list[str]:
    schema = load_schema(schema_name)
    cls = Draft4Validator if draft == "4" else Draft202012Validator
    v = cls(schema, format_checker=FormatChecker())
    errs = []
    for e in sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in e.absolute_path)
        errs.append(f"{path}: {e.message}")
    return errs


def rfc3339(d: date | datetime) -> str:
    if isinstance(d, datetime):
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{d.isoformat()}T00:00:00Z"


def dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool):
        raise TypeError("bool")
    return Decimal(str(v))
