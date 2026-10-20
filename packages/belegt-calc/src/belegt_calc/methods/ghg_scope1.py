# Spec: 05 §2（Scope 1：固定燃烧 §2.1、移动燃烧 §2.2、过程排放 §2.3、制冷剂逸散 §2.4、证据要求 §2.5）
"""family ``ghg-scope1@1.0.0``。

输入命名约定（name 只含 ``[a-z0-9_]``）：

固定燃烧（``ghg.scope1.stationary_combustion``）
  ``<fuel>_hi``      能量（低位热值 Hi 计）            → 直接用
  ``<fuel>_hs``      能量（高位热值 Hs 计，如德国燃气账单）→ × 因子 ``fuel.<fuel>.hi_hs_ratio``
  ``<fuel>_volume``  体积 / ``<fuel>_mass`` 质量         → × 热值因子 ``fuel.<fuel>.ncv_hi.per_l|per_kg``
  因子：``fuel.<fuel>.<gas>.<basis>``，``gas ∈ {co2, ch4, n2o}``，``basis`` 由 ``params.ef_basis``
  选择（``per_mwh_hi`` 默认 | ``per_gj_hi``）；生物质用 ``fuel.<fuel>.co2_biogenic.<basis>``，
  生物源 CO2 不计入 Scope 1，记 ``breakdown.biogenic_co2``（§2.1）。``params.gases`` 可限定计入的气体
  （默认 ``co2,ch4,n2o``；某气体因子缺失且未被排除时报 ``CALC_FACTOR_MISSING``）。
  CH4 的 GWP：燃烧源取 AR6 non-fossil（``gwp.ar6.ch4_non_fossil``），AR5 取 ``gwp.ar5.ch4``（§2.1）。

移动燃烧（``ghg.scope1.mobile_combustion``）
  ``<fuel>_volume`` → ``fuel.<fuel>.co2e.per_l``；``<fuel>_mass`` → ``fuel.<fuel>.co2e.per_kg``；
  ``<vehicle>_distance``（里程法回退）→ ``vehicle.<vehicle>.co2e.per_km``，``is_estimate=true``（§2.2）。

过程排放（``ghg.scope1.process``；``params.process_method`` 必填）
  ``emission_factor``：``<material>_consumed`` × ``process.<material>.co2e.per_t`` × 可选 ``<material>_conversion``（默认 1）
  ``mass_balance``：``44/12 × (Σ carbon_in_<x> − Σ carbon_out_<x>)``（tC，44/12 为精确有理数常量）
  ``none_confirmed``：本期无过程排放源的人工确认（值须为 0），结果 0（§2.3）。

逸散（``ghg.scope1.fugitive``）
  补充量简化法 ``refrigerant_<gas>_recharged``；质量平衡法 ``refrigerant_<gas>_inventory_decrease``、
  ``_purchased``、``_sold``、``_capacity_increase``；筛选法 ``refrigerant_<gas>_charge`` × ``leak_rate.<gas>``
  （``is_estimate``）。混合工质 GWP = Σ ``blend.<gas>.<component>`` × ``gwp.<set>.<component>``（§2.4）。

总量（``ghg.scope1.total``，两种模式）
  * 汇总模式：输入为四个子计算结果 ``stationary_combustion``、``mobile_combustion``、``process``、
    ``fugitive``（tCO2e），四项缺一报 ``CALC_INPUT_MISSING``（§2、§2.3；可用 ``fs:belegt-null@1``）。
  * 活动数据模式（05 §8.3 / TV-01 的展示形态）：按前缀 ``stationary_``/``mobile_``/``process_``/
    ``refrigerant_`` 或后缀（``_hi``/``_hs`` → 固定燃烧；``_volume``/``_mass``/``_distance`` → 移动燃烧）
    路由。breakdown 固定为 ``stationary_combustion, mobile_combustion, fugitive_refrigerant, process,
    total_unrounded``（+ 有生物质时 ``biogenic_co2``）。无过程输入时 process 记 0（TV-01 期望值；与 §2.3
    "需人工确认"的要求存在张力，见 README）。

``params.consolidation``（``operational_control|financial_control|equity_share``）必填，缺失 →
``CALC_PARAM_REQUIRES_DECISION``（CQ-METH-03）；``equity_share`` 时活动数据先乘输入 ``equity_share``（%）。
``params.gwp_set``（``ar5|ar6``）在需要 GWP 时必填，缺失 → CQ-METH-02。
"""
from __future__ import annotations

import re
from fractions import Fraction

from .. import units as U
from ..dec import ONE, ZERO, Rat
from ..engine import Ctx, Item, MethodOutput, apply_ef
from ..errors import (
    CALC_DEC_INVALID,
    CALC_FACTOR_MISSING,
    CALC_FACTOR_SET_INTEGRITY,
    CALC_INPUT_MISSING,
    CALC_METHODOLOGY_UNSUPPORTED,
    CalcError,
)
from ..registry import register
from ..types import MethodSpec

FAMILY = "ghg-scope1@1.0.0"
CONSOLIDATION = ("operational_control", "financial_control", "equity_share")
GWP_SETS = ("ar5", "ar6")
EF_BASES = ("per_mwh_hi", "per_gj_hi")
COMMON_PARAMS = ("consolidation",)
OPT_PARAMS = ("gwp_set", "country", "grid_region", "rounding_digits", "decision_ref", "delta")

_STAT_RE = re.compile(r"^(?P<fuel>[a-z0-9_]+?)_(?P<basis>hi|hs|volume|mass)$")
_MOB_RE = re.compile(r"^(?P<fuel>[a-z0-9_]+?)_(?P<basis>volume|mass|distance)$")
_REF_RE = re.compile(
    r"^refrigerant_(?P<gas>[a-z0-9_]+?)_(?P<kind>recharged|inventory_decrease|purchased|sold|capacity_increase|charge)$"
)
_PROC_EF_RE = re.compile(r"^(?P<mat>[a-z0-9_]+?)_consumed$")
_PROC_CONV_RE = re.compile(r"^(?P<mat>[a-z0-9_]+?)_conversion$")
CO2_PER_C = Fraction(44, 12)


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------

def _consolidation(ctx: Ctx) -> Rat:
    """返回活动数据乘数（股权比例法时为持股比例）。"""
    c = ctx.param("consolidation", choices=CONSOLIDATION, decision="CQ-METH-03")
    if c == "equity_share":
        return ctx.inp("equity_share", "1")
    if ctx.has("equity_share"):
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, "input equity_share only applies to consolidation=equity_share")
    return ONE


def _gwp_set(ctx: Ctx) -> str:
    return ctx.param("gwp_set", choices=GWP_SETS, decision="CQ-METH-02")  # type: ignore[return-value]


def gwp(ctx: Ctx, gas: str, *, source_kind: str) -> Rat:
    """单一气体 GWP；CH4 按 source_kind 选 fossil / non-fossil（05 §2.1）。"""
    gs = _gwp_set(ctx)
    if gas == "ch4" and gs == "ar6":
        key = "gwp.ar6.ch4_non_fossil" if source_kind == "combustion" else "gwp.ar6.ch4_fossil"
    else:
        key = f"gwp.{gs}.{gas}"
    return ctx.factor(key, "1")


def refrigerant_gwp(ctx: Ctx, gas: str) -> Rat:
    """混合工质按组分质量分数加权；纯工质直接查 GWP（05 §2.4）。"""
    prefix = f"blend.{gas}."
    comps = sorted({r.factor_key[len(prefix):] for r in ctx.fs.rows if r.factor_key.startswith(prefix)})
    if not comps:
        return gwp(ctx, gas, source_kind="fugitive")
    total = ZERO
    frac_sum = ZERO
    for c in comps:
        f = ctx.factor(prefix + c, "1")
        frac_sum = frac_sum + f
        total = total + f * gwp(ctx, c, source_kind="fugitive")
    if frac_sum != ONE:
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"blend {gas} mass fractions sum to {frac_sum.to_decimal()}, not 1")
    return total


def _strip(name: str, prefix: str) -> str:
    return name[len(prefix):] if name.startswith(prefix) else name


# ---------------------------------------------------------------------------
# 子类计算（返回 (总量 tCO2e, 生物源 CO2 tCO2e, breakdown 项, is_estimate)）
# ---------------------------------------------------------------------------

def _stationary(ctx: Ctx, names: list[tuple[str, str]], mult: Rat) -> tuple[Rat, Rat, list[Item]]:
    """names: [(输入名, 去前缀后的名)]。"""
    basis = ctx.param("ef_basis", choices=EF_BASES, default="per_mwh_hi")
    gases_param = ctx.params.get("gases", "co2,ch4,n2o")
    gases = [g.strip() for g in gases_param.split(",") if g.strip()]
    if not gases or any(g not in ("co2", "ch4", "n2o") for g in gases) or "co2" not in gases:
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"params.gases={gases_param!r} must list co2 and optionally ch4,n2o")
    e_unit = "MWh" if basis == "per_mwh_hi" else "GJ"
    by_fuel: dict[str, list[tuple[str, str]]] = {}
    for full, short in names:
        m = _STAT_RE.match(short)
        if not m:
            raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"stationary combustion input {full!r} not recognised")
        by_fuel.setdefault(m.group("fuel"), []).append((full, m.group("basis")))
    total = ZERO
    biogenic = ZERO
    items: list[Item] = []
    for fuel in sorted(by_fuel):
        energy_hi = ZERO
        for full, b in by_fuel[fuel]:
            if b == "hi":
                energy_hi = energy_hi + ctx.inp(full, e_unit) * mult
            elif b == "hs":
                hs = ctx.inp(full, e_unit) * mult
                items.append(Item(f"{fuel}_energy_hs", hs, e_unit))
                # Hs→Hi 是因子，不是单位换算；缺行必须报 CALC_FACTOR_MISSING，不得默认 1（TV-09）
                ratio = ctx.factor(f"fuel.{fuel}.hi_hs_ratio", "1")
                energy_hi = energy_hi + hs * ratio
            else:
                per = "per_l" if b == "volume" else "per_kg"
                q_unit = "L" if b == "volume" else "kg"
                ncv = ctx.factor_row(f"fuel.{fuel}.ncv_hi.{per}")
                e, e_u = apply_ef(ctx.inp(full, q_unit) * mult, q_unit, ncv)
                energy_hi = energy_hi + U.convert_rat(e, e_u, e_unit)
        items.append(Item(f"{fuel}_energy_hi", energy_hi, e_unit))
        # CO2（化石或生物源）
        fossil_key = f"fuel.{fuel}.co2.{basis}"
        bio_key = f"fuel.{fuel}.co2_biogenic.{basis}"
        if ctx.has_factor(fossil_key):
            co2, u = apply_ef(energy_hi, e_unit, ctx.factor_row(fossil_key))
            co2 = U.convert_rat(co2, u, "tCO2e")
            total = total + co2
            items.append(Item(f"{fuel}_co2", co2, "tCO2e"))
        elif ctx.has_factor(bio_key):
            r = ctx.factor_row(bio_key)
            co2, u = apply_ef(energy_hi, e_unit, r)
            # 生物源 CO2 因子单位可为 tCO2 或 tCO2e，统一记入 biogenic_co2（不计入 Scope 1）
            u_target = "tCO2e" if U.compatible(u, "tCO2e") else "tCO2"
            co2 = U.convert_rat(co2, u, u_target)
            biogenic = biogenic + co2
        else:
            raise CalcError(CALC_FACTOR_MISSING, f"{ctx.fs.id}: no CO2 factor {fossil_key!r} (or {bio_key!r})")
        for gas in ("ch4", "n2o"):
            if gas not in gases:
                continue
            row = ctx.factor_row(f"fuel.{fuel}.{gas}.{basis}")
            mass, mu = apply_ef(energy_hi, e_unit, row)
            mass_t = U.convert_rat(mass, mu, "t")
            co2e = mass_t * gwp(ctx, gas, source_kind="combustion")
            total = total + co2e
            items.append(Item(f"{fuel}_{gas}_co2e", co2e, "tCO2e"))
    return total, biogenic, items


def _mobile(ctx: Ctx, names: list[tuple[str, str]], mult: Rat) -> tuple[Rat, list[Item], bool]:
    total = ZERO
    items: list[Item] = []
    estimate = False
    for full, short in sorted(names, key=lambda x: x[1]):
        m = _MOB_RE.match(short)
        if not m:
            raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"mobile combustion input {full!r} not recognised")
        fuel, b = m.group("fuel"), m.group("basis")
        if b == "distance":
            row = ctx.factor_row(f"vehicle.{fuel}.co2e.per_km")
            q_unit = "km"
            estimate = True
            ctx.notes.append("mobile combustion via distance method (fallback, is_estimate=true, risk ≥ L2; 05 §2.2)")
        else:
            per = "per_l" if b == "volume" else "per_kg"
            q_unit = "L" if b == "volume" else "kg"
            row = ctx.factor_row(f"fuel.{fuel}.co2e.{per}")
        e, u = apply_ef(ctx.inp(full, q_unit) * mult, q_unit, row)
        e = U.convert_rat(e, u, "tCO2e")
        total = total + e
        items.append(Item(f"{fuel}_co2e", e, "tCO2e"))
    return total, items, estimate


def _fugitive(ctx: Ctx, names: list[tuple[str, str]], mult: Rat) -> tuple[Rat, list[Item], bool]:
    by_gas: dict[str, dict[str, str]] = {}
    for full, short in names:
        m = _REF_RE.match(short)
        if not m:
            raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"refrigerant input {full!r} not recognised")
        by_gas.setdefault(m.group("gas"), {})[m.group("kind")] = full
    total = ZERO
    items: list[Item] = []
    estimate = False
    for gas in sorted(by_gas):
        kinds = by_gas[gas]
        mb = {"inventory_decrease", "purchased", "sold", "capacity_increase"} & set(kinds)
        modes = [x for x in ("recharged", "charge") if x in kinds] + (["mass_balance"] if mb else [])
        if len(modes) != 1:
            raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"refrigerant {gas}: exactly one method expected, got {modes}")
        mode = modes[0]
        if mode == "recharged":
            released = ctx.inp(kinds["recharged"], "t") * mult
        elif mode == "mass_balance":
            def g(k: str) -> Rat:
                return ctx.inp(kinds[k], "t", allow_negative=(k == "inventory_decrease")) if k in kinds else ZERO
            released = (g("inventory_decrease") + g("purchased") - g("sold") - g("capacity_increase")) * mult
        else:
            rate = ctx.factor(f"leak_rate.{gas}", "1")
            released = ctx.inp(kinds["charge"], "t") * rate * mult
            estimate = True
            ctx.notes.append(f"refrigerant {gas} via screening method (is_estimate=true; 05 §2.4)")
        gw = refrigerant_gwp(ctx, gas)
        e = released * gw
        total = total + e
        items.append(Item(f"{gas}_gwp", gw, "1"))
        items.append(Item(f"{gas}_co2e", e, "tCO2e"))
    return total, items, estimate


def _process(ctx: Ctx, names: list[tuple[str, str]], mult: Rat) -> tuple[Rat, list[Item]]:
    if not names:
        raise CalcError(CALC_INPUT_MISSING, "process emissions: no inputs (a zero result needs a 'none_confirmed' human confirmation; 05 §2.3)")
    shorts = {short: full for full, short in names}
    if "none_confirmed" in shorts:
        if len(shorts) != 1:
            raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, "process 'none_confirmed' cannot be combined with process inputs")
        v = ctx.inp(shorts["none_confirmed"], "tCO2e")
        if v != 0:
            raise CalcError(CALC_DEC_INVALID, "process none_confirmed must carry value 0")
        return ZERO, [Item("process_sources_none_confirmed", ONE, "1")]
    method = ctx.param("process_method", choices=("emission_factor", "mass_balance"), decision="process_method")
    items: list[Item] = []
    total = ZERO
    if method == "emission_factor":
        convs = {}
        for short, full in shorts.items():
            mc = _PROC_CONV_RE.match(short)
            if mc:
                convs[mc.group("mat")] = full
        for short in sorted(shorts):
            full = shorts[short]
            if _PROC_CONV_RE.match(short):
                continue
            m = _PROC_EF_RE.match(short)
            if not m:
                raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"process input {full!r} not recognised for emission_factor method")
            mat = m.group("mat")
            row = ctx.factor_row(f"process.{mat}.co2e.per_t")
            e, u = apply_ef(ctx.inp(full, "t") * mult, "t", row)
            e = U.convert_rat(e, u, "tCO2e")
            if mat in convs:
                e = e * ctx.inp(convs.pop(mat), "1")
            total = total + e
            items.append(Item(f"{mat}_co2e", e, "tCO2e"))
        if convs:
            raise CalcError(CALC_INPUT_MISSING, f"conversion factor without consumption: {sorted(convs)}")
    else:
        c_in = ZERO
        c_out = ZERO
        for short in sorted(shorts):
            full = shorts[short]
            if short.startswith("carbon_in_"):
                c_in = c_in + ctx.inp(full, "tC") * mult
            elif short.startswith("carbon_out_"):
                c_out = c_out + ctx.inp(full, "tC") * mult
            else:
                raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"process input {full!r} not recognised for mass_balance method")
        items.append(Item("carbon_in", c_in, "tC"))
        items.append(Item("carbon_out", c_out, "tC"))
        total = (c_in - c_out) * Rat(CO2_PER_C)
        items.append(Item("process_co2", total, "tCO2e"))
    return total, items


# ---------------------------------------------------------------------------
# 方法注册
# ---------------------------------------------------------------------------

def _all(ctx: Ctx, prefix: str = "") -> list[tuple[str, str]]:
    return [(n, _strip(n, prefix)) for n in ctx.input_names if n != "equity_share"]


@register(MethodSpec(
    method_id="ghg.scope1.stationary_combustion", family=FAMILY,
    required_inputs=("<fuel>_hi | <fuel>_hs | <fuel>_volume | <fuel>_mass",), optional_inputs=("equity_share",),
    required_params=COMMON_PARAMS + ("gwp_set",), optional_params=OPT_PARAMS + ("ef_basis", "gases"),
    default_digits=3, result_unit="tCO2e", result_datapoints=("dp:env.ghg.scope1_total",),
    description="固定燃烧 E = Σ_f AD_f × (EF_CO2 + EF_CH4×GWP_CH4 + EF_N2O×GWP_N2O)；Hs 账单需 hi_hs_ratio 因子（05 §2.1）",
))
def stationary(ctx: Ctx) -> MethodOutput:
    mult = _consolidation(ctx)
    names = [(f, s) for f, s in _all(ctx, "stationary_")]
    if not names:
        raise CalcError(CALC_INPUT_MISSING, "no fuel inputs")
    total, bio, items = _stationary(ctx, names, mult)
    if bio:
        items.append(Item("biogenic_co2", bio, "tCO2e"))
    items.append(Item("total_unrounded", total, "tCO2e"))
    return MethodOutput(total, "tCO2e", items)


@register(MethodSpec(
    method_id="ghg.scope1.mobile_combustion", family=FAMILY,
    required_inputs=("<fuel>_volume | <fuel>_mass | <vehicle>_distance",), optional_inputs=("equity_share",),
    required_params=COMMON_PARAMS, optional_params=OPT_PARAMS,
    default_digits=3, result_unit="tCO2e", result_datapoints=("dp:env.ghg.scope1_total",),
    description="移动燃烧：燃料法 E = Σ Fuel × EF；里程法仅作回退（is_estimate）（05 §2.2）",
))
def mobile(ctx: Ctx) -> MethodOutput:
    mult = _consolidation(ctx)
    names = _all(ctx, "mobile_")
    if not names:
        raise CalcError(CALC_INPUT_MISSING, "no fuel inputs")
    total, items, est = _mobile(ctx, names, mult)
    if est:
        ctx.annotations["is_estimate"] = True
    items.append(Item("total_unrounded", total, "tCO2e"))
    return MethodOutput(total, "tCO2e", items)


@register(MethodSpec(
    method_id="ghg.scope1.process", family=FAMILY,
    required_inputs=("<material>_consumed | carbon_in_<x>/carbon_out_<x> | none_confirmed",),
    optional_inputs=("<material>_conversion", "equity_share"),
    required_params=COMMON_PARAMS + ("process_method",), optional_params=OPT_PARAMS,
    default_digits=3, result_unit="tCO2e", result_datapoints=("dp:env.ghg.scope1_total",),
    description="过程排放：排放因子法 Σ AD×EF×CF 或质量平衡法 44/12×(ΣC_in−ΣC_out)（05 §2.3）",
))
def process(ctx: Ctx) -> MethodOutput:
    mult = _consolidation(ctx)
    total, items = _process(ctx, _all(ctx, "process_"), mult)
    items.append(Item("total_unrounded", total, "tCO2e"))
    return MethodOutput(total, "tCO2e", items)


@register(MethodSpec(
    method_id="ghg.scope1.fugitive", family=FAMILY,
    required_inputs=("refrigerant_<gas>_recharged | refrigerant_<gas>_{inventory_decrease,purchased,sold,capacity_increase} | refrigerant_<gas>_charge",),
    optional_inputs=("equity_share",),
    required_params=COMMON_PARAMS + ("gwp_set",), optional_params=OPT_PARAMS,
    default_digits=3, result_unit="tCO2e", result_datapoints=("dp:env.ghg.scope1_total",),
    description="制冷剂逸散：质量平衡法 / 补充量简化法 / 筛选法（is_estimate）；混合工质 GWP 按组分加权（05 §2.4）",
))
def fugitive(ctx: Ctx) -> MethodOutput:
    mult = _consolidation(ctx)
    names = _all(ctx)
    if not names:
        raise CalcError(CALC_INPUT_MISSING, "no refrigerant inputs")
    total, items, est = _fugitive(ctx, names, mult)
    if est:
        ctx.annotations["is_estimate"] = True
    items.append(Item("total_unrounded", total, "tCO2e"))
    return MethodOutput(total, "tCO2e", items)


_AGG = ("stationary_combustion", "mobile_combustion", "process", "fugitive")


@register(MethodSpec(
    method_id="ghg.scope1.total", family=FAMILY,
    required_inputs=("汇总模式: stationary_combustion, mobile_combustion, process, fugitive (tCO2e)",
                     "或活动数据模式: 见模块文档"),
    optional_inputs=("equity_share",),
    required_params=COMMON_PARAMS, optional_params=OPT_PARAMS + ("ef_basis", "gases", "process_method"),
    default_digits=3, result_unit="tCO2e", result_datapoints=("dp:env.ghg.scope1_total",),
    description="Scope 1 总量 = 固定燃烧 + 移动燃烧 + 过程 + 逸散（05 §2；汇总模式用 fs:belegt-null@1）",
))
def total(ctx: Ctx) -> MethodOutput:
    names = ctx.input_names
    if any(n in _AGG for n in names):
        # 汇总模式
        ctx.param("consolidation", choices=CONSOLIDATION, decision="CQ-METH-03")
        missing = [n for n in _AGG if n not in names]
        if missing:
            raise CalcError(CALC_INPUT_MISSING, f"scope 1 total needs all four sub-results; missing {missing} (05 §2.3)")
        vals = {n: ctx.inp(n, "tCO2e") for n in _AGG}
        tot = vals["stationary_combustion"] + vals["mobile_combustion"] + vals["fugitive"] + vals["process"]
        items = [
            Item("stationary_combustion", vals["stationary_combustion"], "tCO2e"),
            Item("mobile_combustion", vals["mobile_combustion"], "tCO2e"),
            Item("fugitive_refrigerant", vals["fugitive"], "tCO2e"),
            Item("process", vals["process"], "tCO2e"),
            Item("total_unrounded", tot, "tCO2e"),
        ]
        return MethodOutput(tot, "tCO2e", items)

    # 活动数据模式
    mult = _consolidation(ctx)
    groups: dict[str, list[tuple[str, str]]] = {"stationary": [], "mobile": [], "process": [], "fugitive": []}
    for n in names:
        if n == "equity_share":
            continue
        if n.startswith("stationary_"):
            groups["stationary"].append((n, n[len("stationary_"):]))
        elif n.startswith("mobile_"):
            groups["mobile"].append((n, n[len("mobile_"):]))
        elif n.startswith("process_"):
            groups["process"].append((n, n[len("process_"):]))
        elif n.startswith("refrigerant_"):
            groups["fugitive"].append((n, n))
        elif re.search(r"_(hi|hs)$", n):
            groups["stationary"].append((n, n))
        elif re.search(r"_(volume|mass|distance)$", n):
            groups["mobile"].append((n, n))
        else:
            raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"scope 1 input {n!r} cannot be routed to a source category")
    stat = mob = fug = proc = bio = ZERO
    est = False
    if groups["stationary"]:
        stat, bio, _ = _stationary(ctx, groups["stationary"], mult)
    if groups["mobile"]:
        mob, _, e1 = _mobile(ctx, groups["mobile"], mult)
        est = est or e1
    if groups["fugitive"]:
        fug, _, e2 = _fugitive(ctx, groups["fugitive"], mult)
        est = est or e2
    if groups["process"]:
        proc, _ = _process(ctx, groups["process"], mult)
    else:
        ctx.notes.append("no process inputs: process recorded as 0 (05 §8.3 display form; §2.3 requires a human confirmation in sub-calculation chains)")
    tot = stat + mob + fug + proc
    if est:
        ctx.annotations["is_estimate"] = True
    items = [
        Item("stationary_combustion", stat, "tCO2e"),
        Item("mobile_combustion", mob, "tCO2e"),
        Item("fugitive_refrigerant", fug, "tCO2e"),
        Item("process", proc, "tCO2e"),
        Item("total_unrounded", tot, "tCO2e"),
    ]
    if bio:
        items.append(Item("biogenic_co2", bio, "tCO2e"))
    return MethodOutput(tot, "tCO2e", items)
