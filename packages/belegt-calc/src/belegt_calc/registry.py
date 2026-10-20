# Spec: 05 §1.5（methodology family 与 method_id）、§1.4（methodology_version 语义）
"""method_id / family 注册表。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .errors import CALC_METHODOLOGY_UNSUPPORTED, CALC_UNKNOWN_METHOD, CalcError
from .types import MethodSpec

MV_RE = re.compile(r"^([a-z0-9-]+)@((0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))$")


@dataclass(frozen=True)
class Family:
    name: str
    versions: tuple[str, ...]      # 本引擎可执行的版本
    standard: str
    status: str = "available"      # available | reserved

    @property
    def ids(self) -> list[str]:
        return [f"{self.name}@{v}" for v in self.versions]


# 05 §1.5
FAMILIES: dict[str, Family] = {
    f.name: f
    for f in [
        Family("ghg-scope1", ("1.0.0",), "GHG Protocol Corporate Standard (rev. 2004)"),
        Family("ghg-scope2-location", ("1.0.0",), "GHG Protocol Scope 2 Guidance (2015), location-based"),
        Family("ghg-scope2-market", ("1.0.0",), "GHG Protocol Scope 2 Guidance (2015), market-based + Quality Criteria"),
        Family("pcf-iso14067-pact", ("1.0.0",), "ISO 14067:2018 + PACT Methodology v3.0 + PACT Tech Spec v3.0.3"),
        Family("pcf-catenax", ("0.1.0",), "Catena-X PCF Rulebook v4.0（预留，V2）", status="reserved"),
        Family("cbam-see", ("1.0.0",), "Reg. (EU) 2023/956 Annex IV; Impl. Reg. (EU) 2025/2547; 2023/1773 Annex III"),
        Family("cbam-cost-gap", ("1.0.0",), "Reg. (EU) 2023/956 Art. 9, 21, 22, 31; 2025/2620; 2025/2621 (corr. 2026/1740)"),
        Family("cbam-threshold", ("1.0.0",), "Reg. (EU) 2023/956 Art. 2a, Annex VII"),
        Family("period-align", ("1.0.0",), "02 §3.4"),
    ]
}

MethodFn = Callable[..., object]


@dataclass(frozen=True)
class Method:
    spec: MethodSpec
    fn: MethodFn
    family_name: str


_METHODS: dict[str, Method] = {}
_RESERVED: dict[str, str] = {}   # method_id → family（只登记、不可执行的预留方法）


def register(spec: MethodSpec) -> Callable[[MethodFn], MethodFn]:
    fam = spec.family.split("@")[0]
    if fam not in FAMILIES:
        raise ValueError(f"unknown family {spec.family}")

    def deco(fn: MethodFn) -> MethodFn:
        if spec.method_id in _METHODS:
            raise ValueError(f"duplicate method {spec.method_id}")
        _METHODS[spec.method_id] = Method(spec=spec, fn=fn, family_name=fam)
        return fn

    return deco


def get_method(method_id: str) -> Method:
    _ensure_loaded()
    m = _METHODS.get(method_id)
    if m is None:
        raise CalcError(CALC_UNKNOWN_METHOD, f"unknown method_id {method_id!r}")
    return m


def check_methodology(method: Method, methodology_version: str) -> None:
    """``methodology_version`` 必须是该方法 family 下本引擎支持的版本。"""
    m = MV_RE.match(methodology_version or "")
    if not m:
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"invalid methodology_version {methodology_version!r}")
    fam, ver = m.group(1), m.group(2)
    if fam not in FAMILIES:
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"unknown methodology family {fam!r}")
    if FAMILIES[fam].status == "reserved":
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"family {fam} is reserved (not implemented in this engine)")
    if fam != method.family_name:
        raise CalcError(
            CALC_METHODOLOGY_UNSUPPORTED,
            f"method {method.spec.method_id} belongs to family {method.family_name}, not {fam}",
        )
    if ver not in FAMILIES[fam].versions:
        raise CalcError(
            CALC_METHODOLOGY_UNSUPPORTED,
            f"{fam}@{ver} not supported by this engine (supported: {', '.join(FAMILIES[fam].versions)})",
        )


def list_methods() -> list[MethodSpec]:
    """全部方法（含预留 family 的占位条目），按 method_id 排序（05 §8.1）。"""
    _ensure_loaded()
    specs = [m.spec for m in _METHODS.values()]
    specs.append(
        MethodSpec(
            method_id="pcf.cradle_to_gate",
            family="pcf-catenax@0.1.0",
            required_inputs=(),
            optional_inputs=(),
            required_params=(),
            optional_params=(),
            default_digits=4,
            result_unit="kgCO2e/kg",
            result_datapoints=("dp:prod.pcf.cradle_to_gate",),
            description="Catena-X PCF Rulebook v4.0 口径（预留，V2；本引擎不可执行）",
            status="reserved",
        )
    )
    return sorted(specs, key=lambda s: (s.method_id, s.family))


_loaded = False


def _ensure_loaded() -> None:
    global _loaded
    if not _loaded:
        _loaded = True
        from .methods import cbam, ghg_scope1, ghg_scope2, pcf, period  # noqa: F401
