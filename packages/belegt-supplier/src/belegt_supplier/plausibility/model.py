# Spec: 08 §5.1（严重度与 Evidence.plausibility 映射）、03 §3.5（plausibility.{flag,basis}）
"""PLAUS 规则的数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Literal, Protocol

Severity = Literal["block", "warn", "info"]
FlagKind = Literal["outlier", "unit_suspect"]


@dataclass(frozen=True)
class PlausHit:
    rule_id: str
    severity: Severity
    message: str
    params: dict[str, str] = field(default_factory=dict)
    datapoint_id: str | None = None
    product_id: str | None = None
    flag_kind: FlagKind = "outlier"

    def basis_token(self) -> str:
        if not self.params:
            return self.rule_id
        return self.rule_id + ":" + ",".join(f"{k}={v}" for k, v in sorted(self.params.items()))


@dataclass(frozen=True)
class PlausValue:
    """一个待校验的值（来自提交包或草稿）。value 为原始值（数字为 DEC-STR 字符串）。"""
    datapoint_id: str
    value: Any
    unit: str | None = None
    product_id: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    note: str | None = None
    raw_text: str | None = None  # geo：原始 GeoJSON 文本，用于检查小数位

    @property
    def base(self) -> str:
        return self.datapoint_id.split("@", 1)[0]


class GeoOracle(Protocol):
    """地理判定（PLAUS-016/017）。无法判定返回 None（规则跳过并记 info）。"""

    def is_land(self, lon: float, lat: float) -> bool | None: ...

    def in_country(self, lon: float, lat: float, iso2: str, buffer_m: float) -> bool | None: ...


@dataclass
class PlausContext:
    """规则所需的参考数据与阈值。全部可选；缺参考数据时相关规则不触发（不猜）。"""
    submission_date: date | None = None
    request_period_start: date | None = None
    request_period_end: date | None = None
    requested_cn_codes: dict[str, str] = field(default_factory=dict)  # product_id -> cn
    cbam_defaults: dict[str, Decimal] = field(default_factory=dict)  # cn -> tCO2e/t（direct，不含加价）
    cbam_defaults_indirect: dict[str, Decimal] = field(default_factory=dict)
    grid_factors: dict[str, Decimal] = field(default_factory=dict)  # ISO2 -> tCO2e/MWh
    grid_factor_source: str = "unspecified"
    installation_country: dict[str, str] = field(default_factory=dict)  # product_id -> ISO2
    previous_values: dict[tuple[str, str | None], Decimal] = field(default_factory=dict)  # (dp_base, product) -> 上一期已核验值
    other_channel_values: dict[tuple[str, str | None], list[Decimal]] = field(default_factory=dict)
    previous_installation_ids: dict[str | None, str] = field(default_factory=dict)
    carbon_pricing_countries: frozenset[str] = frozenset(
        {"GB", "CH", "NO", "IS", "LI", "CN", "KR", "CA", "NZ", "MX", "UA", "KZ", "JP", "SG", "ZA", "CO", "AR", "CL", "UY"}
    )  # [需核对] 由 reg-radar 维护
    origin_country: dict[str, str] = field(default_factory=dict)  # product_id -> ISO2
    has_verification_report: bool = False
    verification_expected_note: bool = False
    geo_oracle: GeoOracle | None = None
    pcf_benchmarks: dict[str, Decimal] = field(default_factory=dict)  # material -> kgCO2e/kg
    product_material: dict[str, str] = field(default_factory=dict)  # product_id -> material
    metal_products: frozenset[str] = frozenset()  # product_id 集合
    max_yield_kg_per_ha: Decimal | None = None
    installation_id_pattern: str = r"^[A-Z]{2}[A-Z0-9\-]{4,40}$"  # [需核对 CBAM 注册表格式]
    dds_reference_pattern: str = r"^[0-9]{2}[A-Z]{2}[A-Z0-9]{8,20}$"       # [需核对 EU IS 规则]
    dds_verification_pattern: str = r"^[A-Z0-9]{8,12}$"                     # [需核对]
    zero_allowed: frozenset[str] = frozenset({"dp:prod.cbam.carbon_price_paid"})
    currency_given: dict[str | None, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    severity: Severity  # 默认严重度（个别规则按情形升降，见 08 §5.2）
    applies_to: str
    description: str
    fn: Callable[[list[PlausValue], PlausContext], list[PlausHit]]
    client_side: bool = False  # 供应商端即时提示子集（08 §6.4）


@dataclass
class PlausReport:
    hits: list[PlausHit]

    @property
    def flag(self) -> Literal["ok", "outlier", "unit_suspect"]:
        material = [h for h in self.hits if h.severity in ("block", "warn")]
        if any(h.flag_kind == "unit_suspect" for h in material):
            return "unit_suspect"
        return "outlier" if material else "ok"

    @property
    def basis(self) -> str:
        return ";".join(h.basis_token() for h in self.hits)

    @property
    def blocked(self) -> bool:
        return any(h.severity == "block" for h in self.hits)

    def by_rule(self, rule_id: str) -> list[PlausHit]:
        return [h for h in self.hits if h.rule_id == rule_id]

    def evidence_plausibility(self) -> dict[str, str]:
        """写入 03 §3.5 `Evidence.plausibility`。"""
        return {"flag": self.flag, "basis": self.basis}
