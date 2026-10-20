# Spec: 03 §3.5（Locator.blob_sha256）、01 AC-501-3（export_input_hash）
"""文件哈希与规范化 JSON 哈希。"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

MIME_BY_SUFFIX = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".json": "application/json",
}


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def bytes_sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def text_sha256(text: str) -> str:
    return bytes_sha256(text.encode("utf-8"))


def mime_for(path: str | Path) -> str:
    return MIME_BY_SUFFIX.get(Path(path).suffix.lower(), "application/octet-stream")


def _canon(v: Any) -> Any:
    from .numbers import to_dec_str

    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, Decimal):
        return to_dec_str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return to_dec_str(Decimal(repr(v)))
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _canon(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if hasattr(v, "model_dump"):
        return _canon(v.model_dump(mode="python", exclude_none=True))
    return str(v)


def canonical_json(obj: Any) -> str:
    """数字统一 DEC-STR、键排序、无空白：保证同一答案集哈希一致（NFR-02、AC-501-3）。"""
    return json.dumps(_canon(obj), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_hash(obj: Any) -> str:
    return text_sha256(canonical_json(obj))
