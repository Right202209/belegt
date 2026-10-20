# Spec: 05 §4（PCF：ISO 14067 / PACT v3：§4.2 公式、§4.3 PDS/DQR、§4.4 生物源碳、§4.5 供应商 PCF 校验）
#       05 §5（分摊规则：§5.1 顺序、§5.2 公式与守恒、§5.3 cut-off、§5.4 决策树）
"""family ``pcf-iso14067-pact@1.0.0``：``pcf.cradle_to_gate``、``pcf.allocation``、``pcf.data_quality``。

``pcf.cradle_to_gate``（单一产品 p，``boundary=product:<id>``）::

    PCF_p = (E_mat + E_inbound + E_direct + E_alloc + E_waste) / (Q_p / DU)

  物料：成对输入 ``<mat>_consumed``（含加工损耗）与 ``<mat>_pcf``（供应商 PCF，须已通过 §4.5 校验）；
        无供应商 PCF 时取次级因子行 ``material.<mat>.pcf``（PDS 记 0，见 ``pcf.data_quality``）。
  共用排放：``site_scope1`` + ``site_scope2_market``（``params.electricity_method=location_based`` 时用
        ``site_scope2_location``）− 可选 ``site_direct_attributed``（Σ_p E_direct,p），按 ``params.allocation`` 分摊：
        ``physical.machine_hours|physical.mass|physical.energy_metered|physical.volume`` → 输入 ``<key>_product`` / ``<key>_site``；
        ``economic`` → ``revenue_product`` / ``revenue_site``（Q×参考期加权平均出厂价）；
        ``direct_only`` → 无共用排放（已完全过程细分）。存在共用排放而未给出 → CQ-METH-05。
  可选：``inbound_transport_emissions``、``direct_emissions_product``、``waste_disposal_emissions``（tCO2e）、
        ``scrap_recycled``（t，cut-off 零负荷，仅记录）、``biogenic_co2_uptake``（tCO2e，≤0）、
        ``biogenic_carbon_content``（kgC/声明单位）、``exempted_emissions_percent``（%，PACT v3 ≤ 3）。
  ``params.recycling`` 只支持 ``cut_off``（§5.3）；``params.declared_unit ∈ {kg, piece}``。
  结果 = 含生物源 PCF（``dp:prod.pcf.cradle_to_gate``）；breakdown ``pcf_excluding_biogenic`` 对应
  ``dp:prod.pcf.cradle_to_gate.excluding_biogenic``（§4.4）。

``pcf.allocation``：多产品工厂共用排放按份额切分，结果为 ``params.product`` 的分摊量；守恒检查逐位相等，
  除法截断尾差加到份额最大的产品并记 ``rounding_residual``，否则 ``CALC_ALLOCATION_IMBALANCE``（§5.2）。

``pcf.data_quality``：``PDS = Σ|E_i|·PDS_i / Σ|E_i| × 100%``；``DQR_ind = Σ|E_i|·DQR_ind,i / Σ|E_i|``；
  成分输入 ``<c>_emissions``、``<c>_pds``（%）、``<c>_dqr_technological|geographical|temporal``；未知时按最坏情况
  PDS 0%、DQR 5（§4.3）。结果为 PDS（%）；DQR 以 RND-1 位数（2 位）写入 breakdown。
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Mapping

from .. import units as U
from ..dec import ONE, ZERO, Rat, parse_dec
from ..engine import Ctx, Item, MethodOutput, apply_ef
from ..errors import (
    CALC_ALLOCATION_IMBALANCE,
    CALC_INPUT_MISSING,
    CALC_METHODOLOGY_UNSUPPORTED,
    CALC_PARAM_REQUIRES_DECISION,
    CALC_UNIT_INCOMPATIBLE,
    CalcError,
)
from ..registry import register
from ..types import MethodSpec

FAMILY = "pcf-iso14067-pact@1.0.0"
PHYSICAL_KEYS = {
    "physical.machine_hours": ("machine_hours", "h"),
    "physical.mass": ("mass", "t"),
    "physical.energy_metered": ("energy_metered", "MWh"),
    "physical.volume": ("volume", "L"),
}
ALLOCATIONS = tuple(PHYSICAL_KEYS) + ("economic", "direct_only")
MAX_EXEMPTED_PERCENT = Rat(3)            # PACT v3（v2 为 5%）
ECONOMIC_RATIO_THRESHOLD = Rat(5)        # §5.1 第 3 步
DQR_INDICATORS = ("technological", "geographical", "temporal")
_MAT_RE = re.compile(r"^(?P<mat>[a-z0-9_]+?)_consumed$")


def _product_boundary(ctx: Ctx) -> None:
    if not (ctx.boundary.startswith("product:") or ctx.boundary == "product_per_unit"):
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"PCF requires a product boundary, got {ctx.boundary!r}")


def _recycling(ctx: Ctx) -> None:
    ctx.param("recycling", choices=("cut_off",), decision="CQ-METH-06")


def _du(ctx: Ctx) -> tuple[str, str]:
    du = ctx.param("declared_unit", choices=("kg", "piece"), decision="declared_unit")
    return du, ("kg" if du == "kg" else "piece")


@register(MethodSpec(
    method_id="pcf.cradle_to_gate", family=FAMILY,
    required_inputs=("<mat>_consumed + <mat>_pcf", "output_product"),
    optional_inputs=("site_scope1", "site_scope2_market|site_scope2_location", "site_direct_attributed",
                     "<key>_product/<key>_site", "revenue_product/revenue_site", "inbound_transport_emissions",
                     "direct_emissions_product", "waste_disposal_emissions", "scrap_recycled", "biogenic_co2_uptake",
                     "biogenic_carbon_content", "exempted_emissions_percent"),
    required_params=("declared_unit", "recycling"),
    optional_params=("allocation", "electricity_method", "pact_spec", "gwp_set", "rounding_digits", "decision_ref"),
    default_digits=4, result_unit="kgCO2e/kg",
    result_datapoints=("dp:prod.pcf.cradle_to_gate", "dp:prod.pcf.cradle_to_gate.excluding_biogenic"),
    description="PCF_p = (E_mat + E_inbound + E_alloc + E_waste) / (Q_p / DU)，cut-off，分摊按 §5（05 §4.2）",
))
def cradle_to_gate(ctx: Ctx) -> MethodOutput:
    _product_boundary(ctx)
    _recycling(ctx)
    du, du_unit = _du(ctx)
    pact = ctx.params.get("pact_spec")
    if pact is not None and not pact.startswith("3."):
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"pact_spec {pact!r}: only PACT v3.x is supported")
    items: list[Item] = []

    # 物料
    mats = sorted(m.group("mat") for n in ctx.input_names if (m := _MAT_RE.match(n)))
    if not mats:
        raise CalcError(CALC_INPUT_MISSING, "no material inputs '<mat>_consumed'")
    e_mat = ZERO
    for mat in mats:
        cons_name = f"{mat}_consumed"
        cons_unit = ctx.raw_unit(cons_name)
        if ctx.has(f"{mat}_pcf"):
            pcf_name = f"{mat}_pcf"
            p_unit = ctx.raw_unit(pcf_name)
            if "/" not in p_unit:
                raise CalcError(CALC_UNIT_INCOMPATIBLE, f"{pcf_name} unit {p_unit!r} must be <emission>/<declared unit>")
            num, den = p_unit.split("/", 1)
            qty = ctx.inp(cons_name, den)          # 量纲不符（如件 vs kg 且无单件质量）→ CALC_UNIT_INCOMPATIBLE
            e = U.convert_rat(qty * ctx.inp(pcf_name, p_unit), num, "tCO2e")
        else:
            row = ctx.factor_row(f"material.{mat}.pcf")
            e, u = apply_ef(ctx.inp(cons_name, cons_unit), cons_unit, row)
            e = U.convert_rat(e, u, "tCO2e")
            ctx.notes.append(f"material {mat}: secondary factor used (PDS 0 for this component; 05 §4.2)")
        e_mat = e_mat + e
    items.append(Item("material_emissions", e_mat, "tCO2e"))

    total = e_mat
    for opt, label in (("inbound_transport_emissions", "inbound_transport_emissions"),
                       ("direct_emissions_product", "direct_emissions")):
        v = ctx.inp_opt(opt, "tCO2e")
        if v is not None:
            items.append(Item(label, v, "tCO2e"))
            total = total + v

    # 共用排放与分摊
    em = ctx.param("electricity_method", choices=("market_based", "location_based"), default="market_based")
    s2_name = "site_scope2_market" if em == "market_based" else "site_scope2_location"
    other_s2 = "site_scope2_location" if em == "market_based" else "site_scope2_market"
    if ctx.has(other_s2):
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"input {other_s2} conflicts with electricity_method={em}")
    has_shared = ctx.has("site_scope1") or ctx.has(s2_name)
    alloc = ctx.params.get("allocation")
    if has_shared:
        if alloc is None:
            raise CalcError(CALC_PARAM_REQUIRES_DECISION, "params.allocation is required (CQ-METH-05)", decision="CQ-METH-05")
        if alloc not in ALLOCATIONS or alloc == "direct_only":
            raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"params.allocation={alloc!r} not applicable with shared emissions")
        s1 = ctx.inp("site_scope1", "tCO2e")
        s2 = ctx.inp(s2_name, "tCO2e")
        direct_all = ctx.inp_opt("site_direct_attributed", "tCO2e", ZERO)
        shared = s1 + s2 - direct_all
        if shared < 0:
            raise CalcError(CALC_ALLOCATION_IMBALANCE, "directly attributed emissions exceed site Scope 1 + 2")
        if alloc == "economic":
            kp = ctx.inp("revenue_product", "EUR")
            ks = ctx.inp("revenue_site", "EUR")
            k_unit = "EUR"
        else:
            key, k_unit = PHYSICAL_KEYS[alloc]
            kp = ctx.inp(f"{key}_product", k_unit)
            ks = ctx.inp(f"{key}_site", k_unit)
        if ks <= 0 or kp < 0 or kp > ks:
            raise CalcError(CALC_ALLOCATION_IMBALANCE, f"allocation key product {kp.to_decimal()} vs site {ks.to_decimal()}")
        share = kp / ks
        e_alloc = shared * kp / ks
        items.append(Item("shared_emissions", shared, "tCO2e"))
        items.append(Item("allocation_share", share, "1"))
        items.append(Item("allocated_shared_emissions", e_alloc, "tCO2e"))
        total = total + e_alloc
    else:
        if alloc not in (None, "direct_only"):
            raise CalcError(CALC_INPUT_MISSING, f"allocation {alloc!r} given but site_scope1/{s2_name} missing")

    w = ctx.inp_opt("waste_disposal_emissions", "tCO2e")
    if w is not None:
        items.append(Item("waste_disposal_emissions", w, "tCO2e"))
        total = total + w
    scrap = ctx.inp_opt("scrap_recycled", "t")
    if scrap is not None:
        items.append(Item("recycled_scrap", scrap, "t"))
        items.append(Item("recycled_scrap_burden", ZERO, "tCO2e"))   # cut-off（§5.3）
    items.append(Item("total_emissions", total, "tCO2e"))

    # 声明单位
    q = ctx.inp("output_product", du_unit)
    if q <= 0:
        raise CalcError(CALC_INPUT_MISSING, "output_product must be > 0")
    items.append(Item("declared_unit_count", q, du_unit))
    res_unit = f"kgCO2e/{du_unit}"
    pcf_excl = U.convert_rat(total, "tCO2e", "kgCO2e") / q
    uptake = ctx.inp_opt("biogenic_co2_uptake", "tCO2e", ZERO, allow_negative=True)
    if uptake > 0:
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, "biogenic_co2_uptake must be ≤ 0 (PACT biogenicCO2Uptake)")
    uptake_du = U.convert_rat(uptake, "tCO2e", "kgCO2e") / q
    pcf_incl = pcf_excl + uptake_du
    items.append(Item("pcf_excluding_biogenic", pcf_excl, res_unit))
    items.append(Item("biogenic_co2_uptake", uptake_du, res_unit))
    if ctx.has("biogenic_carbon_content"):
        items.append(Item("biogenic_carbon_content", ctx.inp("biogenic_carbon_content", f"kgC/{du_unit}"), f"kgC/{du_unit}"))
    ex = ctx.inp_opt("exempted_emissions_percent", "%")
    if ex is not None:
        ex_pct = U.convert_rat(ex, "%", "1") * 100
        if ex_pct > MAX_EXEMPTED_PERCENT:
            raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, "exempted emissions exceed 3% (PACT v3)")
        items.append(Item("exempted_emissions_percent", ex_pct, "%"))
    items.append(Item("pcf_unrounded", pcf_incl, res_unit))
    return MethodOutput(pcf_incl, res_unit, items)


@register(MethodSpec(
    method_id="pcf.allocation", family=FAMILY,
    required_inputs=("shared_emissions | site_scope1 + site_scope2_market", "<key>_<product> (每个产品)"),
    optional_inputs=("site_direct_attributed", "price_<product>"),
    required_params=("allocation", "product"), optional_params=("rounding_digits", "decision_ref", "electricity_method"),
    default_digits=3, result_unit="tCO2e", result_datapoints=(),
    description="s_p = k_p / Σ k_j（物理）或 Q_p·price_p / Σ Q_j·price_j（经济）；E_alloc,p = s_p × E_shared；守恒逐位相等（05 §5.2）",
))
def allocation(ctx: Ctx) -> MethodOutput:
    alloc = ctx.param("allocation", choices=tuple(PHYSICAL_KEYS) + ("economic",), decision="CQ-METH-05")
    target = ctx.param("product")
    if ctx.has("shared_emissions"):
        shared = ctx.inp("shared_emissions", "tCO2e")
    else:
        em = ctx.param("electricity_method", choices=("market_based", "location_based"), default="market_based")
        shared = ctx.inp("site_scope1", "tCO2e") + ctx.inp(
            "site_scope2_market" if em == "market_based" else "site_scope2_location", "tCO2e")
    shared = shared - ctx.inp_opt("site_direct_attributed", "tCO2e", ZERO)
    if shared < 0:
        raise CalcError(CALC_ALLOCATION_IMBALANCE, "directly attributed emissions exceed shared emissions")
    keys: dict[str, Rat] = {}
    prices: dict[str, Rat] = {}
    if alloc == "economic":
        for n in ctx.input_names:
            if n.startswith("output_"):
                p = n[len("output_"):]
                keys[p] = ctx.inp(n, ctx.raw_unit(n)) * ctx.inp(f"price_{p}", f"EUR/{ctx.raw_unit(n)}")
                prices[p] = ctx.inp(f"price_{p}", f"EUR/{ctx.raw_unit(n)}")
    else:
        prefix, unit = PHYSICAL_KEYS[alloc]
        prefix += "_"
        for n in ctx.input_names:
            if n.startswith(prefix):
                keys[n[len(prefix):]] = ctx.inp(n, unit)
        for n in ctx.input_names:
            if n.startswith("price_"):
                prices[n[len("price_"):]] = ctx.inp(n, ctx.raw_unit(n))
    if target not in keys:
        raise CalcError(CALC_INPUT_MISSING, f"no allocation key for product {target!r}")
    if any(v < 0 for v in keys.values()):
        raise CalcError(CALC_ALLOCATION_IMBALANCE, "negative allocation key")
    total_k = sum(keys.values(), ZERO)
    if total_k <= 0:
        raise CalcError(CALC_ALLOCATION_IMBALANCE, "sum of allocation keys must be > 0")
    # §5.1 第 3 步：经济价值比 > 5 用经济分摊，≤ 5 用物理分摊（价格齐全时检查）
    if prices and len(prices) == len(keys) and min(prices.values()) > 0:
        ratio = max(prices.values()) / min(prices.values())
        if alloc == "economic" and ratio <= ECONOMIC_RATIO_THRESHOLD:
            raise CalcError(CALC_PARAM_REQUIRES_DECISION, "economic value ratio ≤ 5: physical allocation required (CQ-METH-05)",
                            decision="CQ-METH-05")
        if alloc != "economic" and ratio > ECONOMIC_RATIO_THRESHOLD:
            raise CalcError(CALC_PARAM_REQUIRES_DECISION, "economic value ratio > 5: economic allocation required (05 §5.1)",
                            decision="CQ-METH-05")
    from ..dec import quantize, BREAKDOWN_DIGITS

    alloc_q: dict[str, Rat] = {}
    for p in sorted(keys):
        alloc_q[p] = Rat.of(quantize(shared * keys[p] / total_k, BREAKDOWN_DIGITS))
    residual = shared - sum(alloc_q.values(), ZERO)
    largest = sorted(keys, key=lambda p: (-keys[p].fraction, p))[0]
    alloc_q[largest] = alloc_q[largest] + residual
    if sum(alloc_q.values(), ZERO) != shared:
        raise CalcError(CALC_ALLOCATION_IMBALANCE, "Σ allocated ≠ shared emissions")
    items = [Item("shared_emissions", shared, "tCO2e")]
    for p in sorted(keys):
        items.append(Item(f"share_{p}", keys[p] / total_k, "1"))
        items.append(Item(f"allocated_{p}", alloc_q[p], "tCO2e"))
    items.append(Item("rounding_residual", residual, "tCO2e"))
    return MethodOutput(alloc_q[target], "tCO2e", items)


@register(MethodSpec(
    method_id="pcf.data_quality", family=FAMILY,
    required_inputs=("<c>_emissions",), optional_inputs=("<c>_pds", "<c>_dqr_technological", "<c>_dqr_geographical",
                                                         "<c>_dqr_temporal"),
    required_params=(), optional_params=("rounding_digits", "pact_spec", "decision_ref"),
    default_digits=2, result_unit="%", result_datapoints=("dp:prod.pcf.primary_data_share",),
    description="PDS = Σ|E_i|·PDS_i / Σ|E_i|；DQR 同式加权；未知按 PDS 0%、DQR 5（05 §4.3）",
))
def data_quality(ctx: Ctx) -> MethodOutput:
    _product_boundary(ctx)
    comps = sorted(n[: -len("_emissions")] for n in ctx.input_names if n.endswith("_emissions"))
    if not comps:
        raise CalcError(CALC_INPUT_MISSING, "no '<c>_emissions' inputs")
    weights = {c: abs(ctx.inp(f"{c}_emissions", "tCO2e", allow_negative=True)) for c in comps}
    wsum = sum(weights.values(), ZERO)
    if wsum == 0:
        raise CalcError(CALC_INPUT_MISSING, "all component emissions are zero")
    pds_num = ZERO
    dqr_num = {k: ZERO for k in DQR_INDICATORS}
    worst: list[str] = []
    for c in comps:
        pds = ctx.inp_opt(f"{c}_pds", "%")
        if pds is None:
            worst.append(f"{c}_pds")
            pds = ZERO
        pds_num = pds_num + weights[c] * U.convert_rat(pds, "%", "1")
        for k in DQR_INDICATORS:
            d = ctx.inp_opt(f"{c}_dqr_{k}", "1")
            if d is None:
                worst.append(f"{c}_dqr_{k}")
                d = Rat(5)
            if d < 1 or d > 5:
                raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"{c}_dqr_{k} must be within 1..5")
            dqr_num[k] = dqr_num[k] + weights[c] * d
    pds_pct = pds_num * 100 / wsum
    if worst:
        ctx.notes.append(f"unknown data quality treated as worst case (PDS 0%, DQR 5): {worst}")
    items = [Item("pds_unrounded", pds_pct, "%")]
    for k in DQR_INDICATORS:
        items.append(Item(f"dqr_{k}", dqr_num[k] / wsum, "1", digits=2))   # RND-1：DQR 2 位
    for k in DQR_INDICATORS:
        items.append(Item(f"dqr_{k}_unrounded", dqr_num[k] / wsum, "1"))
    return MethodOutput(pds_pct, "%", items)


# ---------------------------------------------------------------------------
# §4.5 供应商 PCF 校验（确定性规则，供隔离区晋升前调用）
# ---------------------------------------------------------------------------

@dataclass
class SupplierPcfCheck:
    accepted: bool
    issues: list[dict] = field(default_factory=list)
    effective_pds: str = "0"            # 计入我方 PDS 的值（缺失按 0%）
    plausibility_flag: str | None = None   # "outlier" 时不得自动核验（FR-302）


def check_supplier_pcf(
    pcf: Mapping[str, object],
    *,
    our_unit: str,
    our_reference_period_end: str,
    our_gwp_set: str,
    historical_values: list[str] | None = None,
    outlier_threshold_percent: str = "50",
) -> SupplierPcfCheck:
    """05 §4.5 的 5 条校验。``pcf`` 采用 PACT 字段名（v3.0.3）：

    ``declaredUnitOfMeasurement``（``kilogram|piece|…``）、``productMassPerDeclaredUnit``、
    ``referencePeriodEnd``（PACT 开区间，= 闭区间 end + 1 天）、``ipccCharacterizationFactors``、
    ``primaryDataShare``、``pcfIncludingBiogenicUptake``。
    """
    issues: list[dict] = []
    unit_map = {"kilogram": "kg", "piece": "piece", "ton": "t", "liter": "L", "kilowatt hour": "kWh",
                "megajoule": "MJ", "cubic meter": "m3"}
    du = unit_map.get(str(pcf.get("declaredUnitOfMeasurement", "")))
    # 1. 声明单位可换算
    if du is None:
        issues.append({"rule": "unit", "detail": "unknown declaredUnitOfMeasurement"})
    elif not U.compatible(du, our_unit):
        mass = pcf.get("productMassPerDeclaredUnit")
        if not ({du, our_unit} <= {"kg", "t", "g", "piece"} and isinstance(mass, str) and parse_dec(mass) > 0):
            issues.append({"rule": "unit", "detail": f"{du} not convertible to {our_unit} (needs productMassPerDeclaredUnit)"})
    # 2. 参考期：PACT End 为开区间 → 闭区间 end = End − 1 天；距我方参考期结束不超过 3 年
    try:
        theirs = _dt.date.fromisoformat(str(pcf.get("referencePeriodEnd"))[:10]) - _dt.timedelta(days=1)
        ours = _dt.date.fromisoformat(our_reference_period_end)
        limit = ours.replace(year=ours.year - 3) if not (ours.month == 2 and ours.day == 29) else ours.replace(year=ours.year - 3, day=28)
        if theirs < limit:
            issues.append({"rule": "validity", "detail": "reference period ends more than 3 years before ours"})
    except (TypeError, ValueError):
        issues.append({"rule": "validity", "detail": "referencePeriodEnd missing or invalid"})
    # 3. GWP 一致（不一致不混算 → CQ-METH-02）
    ipcc = pcf.get("ipccCharacterizationFactors") or []
    if [str(x).upper() for x in ipcc] != [our_gwp_set.upper()]:
        issues.append({"rule": "gwp", "detail": f"ipccCharacterizationFactors {ipcc} != [{our_gwp_set.upper()}]",
                       "decision": "CQ-METH-02"})
    # 4. PDS 缺失按 0%
    pds = pcf.get("primaryDataShare")
    effective = "0" if pds is None else str(pds)
    # 5. 合理性：偏离历史值超过阈值 → outlier
    flag = None
    val = pcf.get("pcfIncludingBiogenicUptake") or pcf.get("pcfExcludingBiogenicUptake")
    if historical_values and isinstance(val, str):
        hist = [Rat.of(parse_dec(h)) for h in historical_values]
        mean = sum(hist, ZERO) / len(hist)
        if mean != 0:
            dev = abs(Rat.of(parse_dec(val)) - mean) * 100 / abs(mean)
            if dev > Rat.of(parse_dec(outlier_threshold_percent)):
                flag = "outlier"
                issues.append({"rule": "plausibility", "detail": "deviation from historical values above threshold"})
    blocking = [i for i in issues if i["rule"] in ("unit", "validity", "gwp")]
    return SupplierPcfCheck(accepted=not blocking and flag is None, issues=issues, effective_pds=effective,
                            plausibility_flag=flag)


# ---------------------------------------------------------------------------
# §5.4 分摊决策树（纯函数；不做法律/人工判断，只把判断结果映射到路径）
# ---------------------------------------------------------------------------

@dataclass
class AllocationDecision:
    outcome: str                 # family_rule | direct_attribution | cut_off | disposal | system_expansion |
                                 # economic | physical | closed_question
    path: list[str]
    allocation: str | None = None   # params.allocation 的取值（若适用）
    closed_question: str | None = None


def decide_allocation(
    *,
    customer_rule: str | None = None,
    can_subdivide: bool | None = None,
    output_kind: str | None = None,          # recycled_waste | disposal_waste | co_product（CQ-METH-06 的人工判定）
    dominant_substitute: bool | None = None,
    economic_value_ratio: str | None = None,  # 最高价值产品作分子
    unique_physical_key: str | None = None,   # mass | machine_hours | energy_metered | volume
) -> AllocationDecision:
    """按 05 §5.4 决策树给出分摊路径；缺少人工判断时返回对应 ClosedQuestion。"""
    path = ["B: customer/PCR rule?"]
    if customer_rule:
        return AllocationDecision("family_rule", path + [f"yes → {customer_rule} (separate family, §10.2)"])
    path.append("C: process subdivision possible?")
    if can_subdivide is None:
        return AllocationDecision("closed_question", path, closed_question="CQ-METH-05")
    if can_subdivide:
        return AllocationDecision("direct_attribution", path + ["yes → E_direct,p"], allocation="direct_only")
    path.append("D: output is waste? (2008/98/EC)")
    if output_kind is None:
        return AllocationDecision("closed_question", path, closed_question="CQ-METH-06")
    if output_kind == "recycled_waste":
        return AllocationDecision("cut_off", path + ["recycled waste → cut-off zero burden"])
    if output_kind == "disposal_waste":
        return AllocationDecision("disposal", path + ["disposal waste → disposal emissions to product"])
    if output_kind != "co_product":
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"output_kind {output_kind!r}")
    path.append("E: dominant substitute product?")
    if dominant_substitute is None:
        return AllocationDecision("closed_question", path, closed_question="CQ-METH-05")
    if dominant_substitute:
        return AllocationDecision("system_expansion", path + ["yes → system expansion (substitution)"])
    path.append("F: economic value ratio > 5?")
    if economic_value_ratio is None:
        return AllocationDecision("closed_question", path, closed_question="CQ-METH-05")
    if Rat.of(parse_dec(economic_value_ratio)) > ECONOMIC_RATIO_THRESHOLD:
        return AllocationDecision("economic", path + ["yes → economic allocation"], allocation="economic")
    path.append("G: unique physical relationship?")
    if unique_physical_key:
        key = f"physical.{unique_physical_key}"
        if key not in PHYSICAL_KEYS:
            raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"physical key {unique_physical_key!r}")
        return AllocationDecision("physical", path + [f"yes → {key}"], allocation=key)
    return AllocationDecision("closed_question", path + ["no → mass / machine hours / metered energy"],
                              closed_question="CQ-METH-05")
