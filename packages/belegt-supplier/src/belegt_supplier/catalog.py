# Spec: 02 §5（DataPoint 摘要）、08 §6.2（按数据点的引导表单，单位只允许 allowed_units）
"""内置数据点摘要；可选从 belegt-dictionary 覆盖（不硬依赖）。"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources

import yaml

from .models import DataPointSpec


def base_id(datapoint_id: str) -> str:
    return datapoint_id.split("@", 1)[0]


@lru_cache(maxsize=1)
def _builtin() -> dict[str, DataPointSpec]:
    raw = yaml.safe_load(resources.files("belegt_supplier").joinpath("data/datapoints.yaml").read_text("utf-8"))
    return {d["datapoint_id"]: DataPointSpec(**d) for d in raw["datapoints"]}


def load_catalog(use_dictionary: bool = False) -> dict[str, DataPointSpec]:
    cat = dict(_builtin())
    if use_dictionary:
        try:  # pragma: no cover - 取决于是否安装 belegt-dictionary
            from belegt_dictionary import load_dictionary  # type: ignore

            d = load_dictionary()
            for dp_id, spec in list(cat.items()):
                src = d.datapoints.get(dp_id)
                if src is None:
                    continue
                upd: dict = {}
                if getattr(src, "unit", None):
                    upd["unit"] = src.unit
                if getattr(src, "allowed_units", None):
                    upd["allowed_units"] = list(src.allowed_units)
                cat[dp_id] = spec.model_copy(update=upd)
        except Exception:  # noqa: BLE001
            pass
    return cat


def resolve(datapoints: list[DataPointSpec | str]) -> list[DataPointSpec]:
    """把清单里的 dp ID 或部分规格补全为 DataPointSpec。未知 ID 生成 text 卡片（不丢需求）。"""
    cat = _builtin()
    out: list[DataPointSpec] = []
    for item in datapoints:
        if isinstance(item, str):
            spec = cat.get(base_id(item))
            out.append(spec.model_copy(update={"datapoint_id": item}) if spec else
                       DataPointSpec(datapoint_id=item, value_type="text", label={"en": base_id(item)}))
        else:
            known = cat.get(item.base_id)
            if known:
                merged = known.model_dump()
                merged.update({k: v for k, v in item.model_dump(exclude_unset=True).items()})
                out.append(DataPointSpec(**merged))
            else:
                out.append(item)
    return out
