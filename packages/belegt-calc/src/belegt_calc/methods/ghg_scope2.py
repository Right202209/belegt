# Spec: 05 §3（Scope 2：location-based §3.1、market-based §3.2、边界情况 §3.3）
"""families ``ghg-scope2-location@1.0.0`` 与 ``ghg-scope2-market@1.0.0``。

location-based（``ghg.scope2.location_based``）
  输入：``electricity_purchased``（或多条 ``electricity_purchased_<suffix>``，同一计算内先求和后舍入，TV-10）；
  可选 ``heat_purchased[_<suffix>]``（外购热/蒸汽/冷，因子 ``heat.location.<region>``）。
  因子：``grid.electricity.location.<grid_region>``（``params.grid_factor_key`` 可显式指定其他键，如 TV-10 的
  ``test.tie_factor``）。自发自用可再生电不是输入（§3.1）。
  仅含 CO2 的因子（``quality.gas_scope=co2_only``）结果按 CO2e 记账，breakdown 标 ``gas_scope_co2_only=1``；
  因子年份回退时 breakdown 标 ``factor_year_gap``（§1.3、§7.4-2）。

market-based（``ghg.scope2.market_based``）按 Scope 2 Guidance Table 6.3 层级依次扣减（§3.2）：
  1 ``electricity_contractual_renewable``（EAC/HKN 注销）→ ``contractual.electricity.goo``
  2 ``electricity_ppa``                                  → ``contractual.electricity.ppa``（或 ``params.ppa_factor_key``）
  3 ``electricity_supplier_specific``                    → ``supplier.electricity.product_mix``（或 ``params.supplier_factor_key``）
  4 剩余电量                                              → ``residual_mix.electricity.<grid_region>``
  5 无残余组合行时回退到 ``grid.electricity.location.<grid_region>``，``breakdown.residual_mix_unavailable=1``
  合同电量之和不得超过 ``electricity_purchased``（否则 ``CALC_ALLOCATION_IMBALANCE``）。是否满足 8 项
  Quality Criteria 是人工判断（CQ-METH-04）：只有判定满足的工具才作为输入进入层级 1–3。
"""
from __future__ import annotations

from ..dec import ONE, ZERO, Rat
from ..engine import Ctx, Item, MethodOutput
from ..errors import CALC_ALLOCATION_IMBALANCE, CALC_INPUT_MISSING, CALC_METHODOLOGY_UNSUPPORTED, CalcError
from ..registry import register
from ..types import MethodSpec
from .ghg_scope1 import CONSOLIDATION

OPT = ("rounding_digits", "decision_ref", "delta", "country")


def _region(ctx: Ctx) -> str:
    r = ctx.params.get("grid_region")
    if not r:
        raise CalcError(CALC_INPUT_MISSING, "params.grid_region is required (Site.grid_region master data)")
    return r


def _mult(ctx: Ctx) -> Rat:
    c = ctx.param("consolidation", choices=CONSOLIDATION, decision="CQ-METH-03")
    if c == "equity_share":
        return ctx.inp("equity_share", "1")
    return ONE


def _flags(ctx: Ctx, items: list[Item]) -> None:
    if ctx.annotations.get("gas_scope_co2_only"):
        items.append(Item("gas_scope_co2_only", ONE, "1"))
    gaps = ctx.annotations.get("factor_year_gap") or {}
    if gaps:
        items.append(Item("factor_year_gap", Rat(max(gaps.values())), "1"))


@register(MethodSpec(
    method_id="ghg.scope2.location_based", family="ghg-scope2-location@1.0.0",
    required_inputs=("electricity_purchased[_<suffix>]",), optional_inputs=("heat_purchased[_<suffix>]", "equity_share"),
    required_params=("consolidation", "grid_region"), optional_params=OPT + ("grid_factor_key", "heat_factor_key"),
    default_digits=3, result_unit="tCO2e", result_datapoints=("dp:env.ghg.scope2_location_based",),
    description="E_loc = Σ_r E_el,r × EF_grid,r + Σ_h Q_h × EF_heat,h（05 §3.1）",
))
def location_based(ctx: Ctx) -> MethodOutput:
    mult = _mult(ctx)
    region = _region(ctx)
    el = [n for n in ctx.input_names if n == "electricity_purchased" or n.startswith("electricity_purchased_")]
    heat = [n for n in ctx.input_names if n == "heat_purchased" or n.startswith("heat_purchased_")]
    if not el and not heat:
        raise CalcError(CALC_INPUT_MISSING, "electricity_purchased is required")
    items: list[Item] = []
    total = ZERO
    if el:
        ef_key = ctx.params.get("grid_factor_key") or f"grid.electricity.location.{region}"
        ef = ctx.factor(ef_key, "tCO2e/MWh", geography=region)
        consumption = ZERO
        for n in el:
            e = ctx.inp(n, "MWh") * mult
            consumption = consumption + e
            if len(el) > 1:
                items.append(Item(f"{n}_emissions", e * ef, "tCO2e"))
        items.insert(0, Item("electricity_consumption", consumption, "MWh"))
        total = total + consumption * ef
    if heat:
        hkey = ctx.params.get("heat_factor_key") or f"heat.location.{region}"
        hef = ctx.factor(hkey, "tCO2e/MWh", geography=region)
        hq = ZERO
        for n in heat:
            hq = hq + ctx.inp(n, "MWh") * mult
        items.append(Item("heat_consumption", hq, "MWh"))
        items.append(Item("heat_emissions", hq * hef, "tCO2e"))
        total = total + hq * hef
    _flags(ctx, items)
    items.append(Item("emissions_unrounded", total, "tCO2e"))
    return MethodOutput(total, "tCO2e", items)


TIERS = (
    # (输入名, breakdown 前缀, 默认因子键, 覆盖参数)
    ("electricity_contractual_renewable", "eac", "contractual.electricity.goo", "eac_factor_key"),
    ("electricity_ppa", "ppa", "contractual.electricity.ppa", "ppa_factor_key"),
    ("electricity_supplier_specific", "supplier_specific", "supplier.electricity.product_mix", "supplier_factor_key"),
)


@register(MethodSpec(
    method_id="ghg.scope2.market_based", family="ghg-scope2-market@1.0.0",
    required_inputs=("electricity_purchased",),
    optional_inputs=("electricity_contractual_renewable", "electricity_ppa", "electricity_supplier_specific", "equity_share"),
    required_params=("consolidation", "grid_region"),
    optional_params=OPT + ("eac_factor_key", "ppa_factor_key", "supplier_factor_key"),
    default_digits=3, result_unit="tCO2e", result_datapoints=("dp:env.ghg.scope2_market_based",),
    description="E_mkt = Σ_k E_k × EF_k，按合同工具层级扣减，余量用残余组合（无则回退电网平均并披露）（05 §3.2）",
))
def market_based(ctx: Ctx) -> MethodOutput:
    mult = _mult(ctx)
    region = _region(ctx)
    total_el = ctx.inp("electricity_purchased", "MWh") * mult
    items: list[Item] = []
    remaining = total_el
    emissions = ZERO
    for name, label, key, override in TIERS:
        if not ctx.has(name):
            continue
        vol = ctx.inp(name, "MWh") * mult
        ef = ctx.factor(ctx.params.get(override) or key, "tCO2e/MWh", geography=region)
        remaining = remaining - vol
        e = vol * ef
        emissions = emissions + e
        items.append(Item(f"{label}_consumption", vol, "MWh"))
        items.append(Item(f"{label}_emissions", e, "tCO2e"))
    if remaining < 0:
        raise CalcError(
            CALC_ALLOCATION_IMBALANCE,
            "contractual instruments exceed electricity consumption (05 §3.2: 合同电量不得超过当期消耗)",
        )
    rm_key = f"residual_mix.electricity.{region}"
    if ctx.has_factor(rm_key):
        rm = ctx.factor(rm_key, "tCO2e/MWh", geography=region)
        unavailable = False
    else:
        rm = ctx.factor(f"grid.electricity.location.{region}", "tCO2e/MWh", geography=region)
        unavailable = True
        ctx.notes.append("residual mix unavailable: fell back to grid average; disclose in answer (05 §3.2 tier 5)")
    e_rm = remaining * rm
    emissions = emissions + e_rm
    items.append(Item("residual_mix_consumption", remaining, "MWh"))
    items.append(Item("residual_mix_emissions", e_rm, "tCO2e"))
    if unavailable:
        items.append(Item("residual_mix_unavailable", ONE, "1"))
    _flags(ctx, items)
    items.append(Item("emissions_unrounded", emissions, "tCO2e"))
    return MethodOutput(emissions, "tCO2e", items)


__all__ = ["location_based", "market_based"]
