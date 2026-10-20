# Spec: 02 §1（period_type、sensitivity、tags）、§2（validity_rule、evidence_requirements、related_datapoints）；06 §1.2（字典派生规则）
"""规则引擎需要的 DataPoint 元数据（只读）。

来源优先级：``belegt_dictionary.load_dictionary()``（按不带 SemVer 的 ID 取最新版本，或精确 ``id@semver``）→
本模块内置的最小集合（使字典缺失时 CR-003/CR-022/CR-033 等仍可测）→ ``register()``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..ids import datapoint_base


@dataclass(frozen=True)
class Plausibility:
    expr: str
    severity: str = "warn"
    note: str | None = None


@dataclass(frozen=True)
class DPMeta:
    id: str  # 不带 SemVer
    version: str = "1.0.0"
    value_type: str = "number"
    unit: str | None = None
    period_type: str | None = None  # reporting_period | calendar_year | fiscal_year | point_in_time | validity_range
    sensitivity: str = "customer_shared"
    tags: tuple[str, ...] = ()
    min: Any = None
    max: Any = None
    regex: str | None = None
    enum_ref: str | None = None
    expires_by: str | None = None
    expires_after_days: int | None = None
    max_age_days: int | None = None
    plausibility: tuple[Plausibility, ...] = ()
    sum_of: tuple[str, ...] = ()  # 分项（related_datapoints.relation=sum_of）
    enum_values: tuple[str, ...] | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


def _v(o: Any, name: str, default: Any = None) -> Any:
    if o is None:
        return default
    if isinstance(o, dict):
        return o.get(name, default)
    return getattr(o, name, default)


def _enum(x: Any) -> Any:
    return getattr(x, "value", x)


def adapt(dp: Any, enums: Any = None) -> DPMeta | None:
    dp_id = _v(dp, "id")
    if not dp_id:
        return None
    vr = _v(dp, "validity_rule")
    er = _v(dp, "evidence_requirements")
    rel = _v(dp, "related_datapoints") or []
    plaus = tuple(Plausibility(_v(p, "expr"), str(_enum(_v(p, "severity", "warn"))), _v(p, "note"))
                  for p in (_v(vr, "plausibility") or []) if _v(p, "expr"))
    enum_ref = _v(vr, "enum_ref") or _v(dp, "enum_ref")
    enum_values = None
    if enum_ref and enums is not None:
        e = enums.get(enum_ref) if isinstance(enums, dict) else None
        vals = _v(e, "values") if e is not None else None
        if vals:
            enum_values = tuple(str(_v(x, "code", None) or _v(x, "key", None) or x) for x in vals)
    return DPMeta(
        id=datapoint_base(dp_id), version=str(_v(dp, "version") or _v(dp, "semver") or "1.0.0"),
        value_type=str(_enum(_v(dp, "value_type", "number"))), unit=_v(dp, "unit"),
        period_type=_enum(_v(dp, "period_type")), sensitivity=str(_enum(_v(dp, "sensitivity", "customer_shared"))),
        tags=tuple(_v(dp, "tags") or ()), min=_v(vr, "min"), max=_v(vr, "max"), regex=_v(vr, "regex"),
        enum_ref=enum_ref, expires_by=_enum(_v(vr, "expires_by")), expires_after_days=_v(vr, "expires_after_days"),
        max_age_days=_v(er, "max_age_days"), plausibility=plaus,
        sum_of=tuple(datapoint_base(_v(r, "id")) for r in rel if str(_enum(_v(r, "relation"))) == "sum_of"),
        enum_values=enum_values,
    )


def _m(id_: str, unit: str | None = None, vt: str = "number", pt: str = "reporting_period", **kw: Any) -> DPMeta:
    return DPMeta(id=id_, unit=unit, value_type=vt, period_type=pt, **kw)


_GHG = ("ghg",)
BUILTIN_META: list[DPMeta] = [
    _m("dp:env.ghg.scope1_total", "tCO2e", tags=_GHG, min=0, max_age_days=550),
    _m("dp:env.ghg.scope2_location_based", "tCO2e", tags=_GHG, min=0, max_age_days=550),
    _m("dp:env.ghg.scope2_market_based", "tCO2e", tags=_GHG, min=0, max_age_days=550),
    _m("dp:env.ghg.scope3_total", "tCO2e", tags=_GHG, min=0),
    _m("dp:energy.consumption.total", "MWh", min=0, max_age_days=550),
    _m("dp:energy.consumption.electricity_total", "MWh", min=0, max_age_days=550),
    _m("dp:energy.consumption.renewable_share", "%", min=0, max=100),
    _m("dp:energy.procurement.eac_volume", "MWh", min=0),
    _m("dp:energy.generation.onsite_renewable", "MWh", min=0),
    _m("dp:env.water.withdrawal_total", "m3", min=0, sensitivity="confidential"),
    _m("dp:env.waste.generated", "t", min=0,
       sum_of=("dp:env.waste.generated.hazardous", "dp:env.waste.generated.non_hazardous")),
    _m("dp:env.waste.generated.hazardous", "t", min=0),
    _m("dp:env.waste.generated.non_hazardous", "t", min=0),
    _m("dp:org.employees.headcount", "1", pt="point_in_time", min=0, regex="^[0-9]+$", expires_after_days=400),
    _m("dp:social.workforce.headcount.female", "1", pt="point_in_time", min=0, regex="^[0-9]+$"),
    _m("dp:social.workforce.headcount.male", "1", pt="point_in_time", min=0, regex="^[0-9]+$"),
    _m("dp:prod.pcf.cradle_to_gate", "kgCO2e/kg", pt="validity_range"),
    _m("dp:prod.pcf.declared_unit", None, vt="enum", pt="point_in_time"),
    _m("dp:prod.cbam.see_direct", "tCO2e/t", pt="calendar_year", min=0),
    _m("dp:prod.cbam.see_indirect", "tCO2e/t", pt="calendar_year", min=0),
    _m("dp:prod.cbam.specific_electricity_consumption", "MWh/t", pt="calendar_year", min=0),
    _m("dp:prod.battery.recycled_content.cobalt", "%", min=0, max=100),
    _m("dp:env.ghg_target.has_target", None, vt="bool", pt="point_in_time"),
    _m("dp:env.ghg_target.target_year", None, vt="date", pt="point_in_time"),
    _m("dp:cert.iso14001.valid_until", None, vt="date", pt="validity_range", sensitivity="public",
       tags=("certificate",), expires_by="value"),
    _m("dp:cert.iso14001.certificate_file", None, vt="file", pt="validity_range", sensitivity="public",
       tags=("certificate",)),
    _m("dp:mat.reach.candidate_list_version", None, vt="date", pt="point_in_time"),
    _m("dp:mat.reach.svhc_present", None, vt="bool", pt="point_in_time"),
    _m("dp:supply.eudr.geolocation", None, vt="geo", pt="point_in_time", sensitivity="restricted"),
    _m("dp:gov.policy.code_of_conduct", None, vt="text", pt="point_in_time"),
]


class MetaProvider:
    def __init__(self, items: Iterable[DPMeta] = (), *, use_dictionary: bool = True) -> None:
        self._by_ref: dict[str, DPMeta] = {}
        self._latest: dict[str, DPMeta] = {}
        self.sources: list[str] = []
        for m in items:
            self.register(m)
        if use_dictionary:
            n = 0
            for m in _dictionary_meta():
                self.register(m)
                n += 1
            if n:
                self.sources.append("belegt_dictionary")

    def register(self, m: DPMeta) -> None:
        self._by_ref[m.ref] = m
        cur = self._latest.get(m.id)
        if cur is None or _semver_key(m.version) >= _semver_key(cur.version):
            self._latest[m.id] = m

    def get(self, dp: str | None) -> DPMeta | None:
        if not dp:
            return None
        if "@" in dp:
            return self._by_ref.get(dp) or self._latest.get(datapoint_base(dp))
        return self._latest.get(dp)

    def all(self) -> list[DPMeta]:
        return list(self._latest.values())

    @classmethod
    def default(cls) -> "MetaProvider":
        p = cls(BUILTIN_META)
        p.sources.append("builtin")
        return p


def _semver_key(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


_DICT_META: list[DPMeta] | None = None


def _dictionary_meta() -> list[DPMeta]:
    global _DICT_META
    if _DICT_META is None:
        out: list[DPMeta] = []
        try:
            import belegt_dictionary  # type: ignore

            d = belegt_dictionary.load_dictionary()
            enums = getattr(d, "enums", None)
            for dp in (d.datapoints.values() if isinstance(d.datapoints, dict) else d.datapoints):
                m = adapt(dp, enums)
                if m is not None:
                    out.append(m)
        except Exception:
            out = []
        _DICT_META = out
    return _DICT_META


__all__ = ["DPMeta", "Plausibility", "MetaProvider", "BUILTIN_META", "adapt"]
