# Spec: 08 §6.2、§6.4（CBAM 沟通模板 Excel 自动解析，tm:cbam_communication@2024，带单元格定位）、§2.4
"""CBAM communication 模板解析入口：优先调用 belegt_io（可选依赖），否则返回"不可用"。

解析器约定：`parser(path: str) -> list[dict]`，每项
`{datapoint_id, value, unit?, sheet, cell, cn_code?, product_id?}`。
宿主可用 `set_cbam_parser()` 注入自定义实现（例如核对过单元格位置的新版模板）。
"""
from __future__ import annotations

from typing import Callable

CbamParser = Callable[[str], list[dict]]
_custom: CbamParser | None = None


def set_cbam_parser(parser: CbamParser | None) -> None:
    global _custom
    _custom = parser


def get_cbam_parser() -> CbamParser | None:
    if _custom is not None:
        return _custom
    try:
        import belegt_io  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    for name in ("parse_cbam_communication", "parse_cbam_comm"):
        fn = getattr(belegt_io, name, None)
        if callable(fn):
            return _adapt(fn)
    return None


def _adapt(fn: Callable) -> CbamParser:
    def run(path: str) -> list[dict]:
        res = fn(path)
        items = getattr(res, "values", None) or getattr(res, "fields", None) or res
        out = []
        for it in items:
            d = it if isinstance(it, dict) else (it.model_dump() if hasattr(it, "model_dump") else vars(it))
            loc = d.get("locator") or {}
            out.append({"datapoint_id": d.get("datapoint_id"), "value": d.get("value"), "unit": d.get("unit"),
                        "sheet": d.get("sheet") or loc.get("sheet"), "cell": d.get("cell") or loc.get("cell"),
                        "cn_code": d.get("cn_code"), "product_id": d.get("product_id")})
        return out
    return run
