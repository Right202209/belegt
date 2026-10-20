# Spec: 03 §3.1（DataPoint 在图中依赖的字段）、02 §1.1、§4.3（SemVer 影响）
"""DataPoint 注册表。

证据图只依赖 DataPoint 的 ``semver``、``value_type``、``unit``、``dimension``、``allowed_boundaries``、
``requires_consolidation``、``status``（03 §3.1）。来源：
1. ``belegt_dictionary.load_dictionary()``（可导入时，按 ``id@semver`` 精确匹配；02 字段名 ``version``/``boundary``/
   ``consolidation`` 自动适配）；
2. 本模块内置的最小集合（02 §5 子集，含 03 §5.6 示例用的 ``dp:env.ghg.scope2_market_based@1.1.0``）；
3. 运行期 ``register()``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .ids import datapoint_base


@dataclass(frozen=True)
class DataPointInfo:
    id: str  # 不带 semver：dp:env.ghg.scope2_market_based
    semver: str
    value_type: str
    unit: str | None = None
    dimension: str | None = None
    allowed_boundaries: tuple[str, ...] = ("organization",)
    consolidation: tuple[str, ...] = ()
    requires_consolidation: bool = False
    status: str = "active"
    sensitivity: str = "customer_shared"
    allowed_units: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.semver}"

    def boundary_allowed(self, boundary: str) -> bool:
        kind = boundary.split(":", 1)[0]
        return kind in self.allowed_boundaries or boundary in self.allowed_boundaries


_C3 = ("operational_control", "financial_control", "equity_share")


def _num(id_: str, unit: str, semver: str = "1.0.0", boundaries=("organization", "site"), consolidation=_C3, **kw):
    return DataPointInfo(
        id=id_, semver=semver, value_type="number", unit=unit,
        allowed_boundaries=tuple(boundaries), consolidation=tuple(consolidation),
        requires_consolidation="organization" in boundaries and bool(consolidation), **kw,
    )


BUILTIN_DATAPOINTS: list[DataPointInfo] = [
    _num("dp:env.ghg.scope1_total", "tCO2e"),
    _num("dp:env.ghg.scope2_location_based", "tCO2e"),
    _num("dp:env.ghg.scope2_location_based", "tCO2e", semver="1.1.0"),
    _num("dp:env.ghg.scope2_market_based", "tCO2e"),
    _num("dp:env.ghg.scope2_market_based", "tCO2e", semver="1.1.0"),
    _num("dp:env.ghg.scope3_total", "tCO2e"),
    _num("dp:energy.consumption.total", "MWh"),
    _num("dp:energy.consumption.electricity_total", "MWh"),
    _num("dp:energy.consumption.renewable_share", "%"),
    _num("dp:energy.procurement.eac_volume", "MWh"),
    _num("dp:env.water.withdrawal_total", "m3"),
    _num("dp:env.waste.generated", "t"),
    _num("dp:org.employees.headcount", "1", consolidation=()),
    _num("dp:prod.pcf.cradle_to_gate", "kgCO2e/kg", boundaries=("product", "product_per_unit"), consolidation=()),
    _num("dp:prod.cbam.see_direct", "tCO2e/t", boundaries=("product", "product_per_unit"), consolidation=()),
    _num("dp:prod.cbam.see_indirect", "tCO2e/t", boundaries=("product", "product_per_unit"), consolidation=()),
    DataPointInfo("dp:env.ghg_target.has_target", "1.0.0", "bool", allowed_boundaries=("organization",)),
    DataPointInfo("dp:env.ghg_target.target_year", "1.0.0", "date", allowed_boundaries=("organization",)),
    DataPointInfo("dp:cert.iso14001.valid_until", "1.0.0", "date", allowed_boundaries=("organization", "site"), sensitivity="public"),
    DataPointInfo("dp:gov.policy.code_of_conduct", "1.0.0", "text", allowed_boundaries=("organization",)),
    DataPointInfo("dp:env.pollution.emissions_reported", "1.0.0", "file", allowed_boundaries=("organization",)),
    DataPointInfo("dp:supply.eudr.geolocation", "1.0.0", "geo", allowed_boundaries=("product", "site"), sensitivity="restricted"),
    # 03 §4.3 用：已弃用示例
    _num("dp:env.ghg.scope2_total_legacy", "tCO2e", status="deprecated"),
]


def _adapt_dictionary_dp(dp: Any, units: Any = None) -> DataPointInfo | None:
    g = (lambda name, default=None: dp.get(name, default)) if isinstance(dp, dict) else (
        lambda name, default=None: getattr(dp, name, default))
    dp_id = g("id")
    if not dp_id:
        return None
    semver = g("semver") or g("version")
    if not isinstance(semver, str):
        return None
    boundaries = g("allowed_boundaries") or g("boundary") or ("organization",)
    consolidation = tuple(g("consolidation") or ())
    req = g("requires_consolidation")
    if req is None:
        req = "organization" in boundaries and bool(consolidation)
    status = g("status") or "active"
    status = getattr(status, "value", status)
    vt = g("value_type")
    vt = getattr(vt, "value", vt)
    sens = g("sensitivity") or "customer_shared"
    sens = getattr(sens, "value", sens)
    return DataPointInfo(
        id=datapoint_base(dp_id), semver=semver, value_type=str(vt), unit=g("unit"),
        dimension=g("dimension"), allowed_boundaries=tuple(str(getattr(b, "value", b)) for b in boundaries),
        consolidation=tuple(str(getattr(c, "value", c)) for c in consolidation),
        requires_consolidation=bool(req), status=str(status), sensitivity=str(sens),
        allowed_units=tuple(g("allowed_units") or ()),
    )


class DataPointRegistry:
    def __init__(self, items: Iterable[DataPointInfo] = ()) -> None:
        self._items: dict[str, DataPointInfo] = {}
        self.sources: list[str] = []
        for it in items:
            self.register(it)

    def register(self, info: DataPointInfo, *, override: bool = True) -> None:
        if override or info.ref not in self._items:
            self._items[info.ref] = info

    def get(self, dp_ref: str) -> DataPointInfo | None:
        """``dp_ref`` 必须钉住 SemVer（03 §3.1）。"""
        return self._items.get(dp_ref)

    def versions(self, base_id: str) -> list[DataPointInfo]:
        return [v for v in self._items.values() if v.id == base_id]

    def __contains__(self, dp_ref: str) -> bool:
        return dp_ref in self._items

    def __len__(self) -> int:
        return len(self._items)

    @classmethod
    def builtin(cls) -> "DataPointRegistry":
        r = cls(BUILTIN_DATAPOINTS)
        r.sources.append("builtin")
        return r

    @classmethod
    def default(cls) -> "DataPointRegistry":
        """字典包优先（精确 SemVer），内置集合补缺。每次返回新注册表（字典只加载一次）。"""
        r = cls.builtin()
        infos = _dictionary_infos()
        for info in infos:
            r.register(info, override=True)
        if infos:
            r.sources.insert(0, "belegt_dictionary")
        return r


_DICT_CACHE: list[DataPointInfo] | None = None


def _dictionary_infos() -> list[DataPointInfo]:
    global _DICT_CACHE
    if _DICT_CACHE is None:
        out: list[DataPointInfo] = []
        try:
            import belegt_dictionary  # type: ignore

            d = belegt_dictionary.load_dictionary()
            dps = getattr(d, "datapoints", {}) or {}
            for dp in (dps.values() if isinstance(dps, dict) else dps):
                info = _adapt_dictionary_dp(dp)
                if info is not None:
                    out.append(info)
        except Exception:
            out = []
        _DICT_CACHE = out
    return _DICT_CACHE
