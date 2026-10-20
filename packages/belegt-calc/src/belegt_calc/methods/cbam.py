# Spec: 05 §6（CBAM：§6.2 SEE 简单/复杂货物、§6.3 默认值与加价、§6.4 SEFA 与 CBAM factor、
#       §6.5 原产国已付碳价、§6.6 证书数与成本差（修正公式）、§6.7 价格口径、§6.8 50 t 门槛）
"""families ``cbam-see@1.0.0``、``cbam-cost-gap@1.0.0``、``cbam-threshold@1.0.0``。

``cbam.see_simple`` / ``cbam.see_complex``（实际值，§6.2）::

    SEE_dir = (AttrEm_dir + Σ_i M_i × SEE_i,dir) / AL
    SEE_ind = (E_el × EF_el + Σ_i M_i × SEE_i,ind) / AL
    SEC     = E_el / AL

  输入：``attributed_emissions_direct``（tCO2e）、``activity_level``（t）、``electricity_consumed``（MWh）与
  ``electricity_emission_factor``（tCO2e/MWh，运营者提供；或直接给 ``attributed_emissions_indirect``）；
  复杂货物另有前体 ``precursor_<x>_mass``（t）、``precursor_<x>_see_direct``、``precursor_<x>_see_indirect``
  （tCO2e/t，前体装置核查值；只输入来自非豁免第三国的 Annex I 前体）。
  ``params.component ∈ {direct (默认), indirect, specific_electricity_consumption}`` 决定 result；三者都写入 breakdown。
  钢铁、铝、氢只计直接排放进入证书数（Annex II）。

``cbam.see_default``（§6.3）：``SEE_def,y = DV_total × (1 + μ_y)``；``DV_total`` 查 ``cbam.default.total.<cn>.<country>``
  （CN 从 8 位逐级回退到 6、4 位；国家未列出时用 ``.OTHER`` 行），``μ_y`` 查 ``cbam.markup.<sector>.<y>``
  （若无当年键，取不晚于 y 的最近年份键，体现"2028 年起 30%"）。输入 ``import_year``（须与计算期间年份一致）。
  FactorSet 只存原始默认值与加价规则，不存含加价默认值。

``cbam.sefa_default``（§6.4，Guidance 4 Eq. 6）：``SEFA_def = CBAM_y × CSCF_y × BM_g``，
  ``BM_g`` 查 ``cbam.bm_default.<cn>.<route>``（``params.route``；只有一条路线时可省略）。

``cbam.cost_gap``（§6.6，修正公式）::

    N_def = max(0, M × (SEE_def,y − SEFA_def))
    N_act = max(0, M × (SEE_act − SEFA_act) − D_cpp),   D_cpp = CPP_EUR / P_y   （Art. 9(1)，仅实际值路径）
    cost_gap = (N_def − N_act) × P_y

  价格 ``cbam.price.<YYYYQn>``（2026 季度均价，Art. 21(1a)）或 ``cbam.price.<YYYY>W<ww>``（2027 起周均价）；
  ``params.quarter``/``params.week`` 必须与计算期间一致，否则 ``CALC_PERIOD_MISMATCH``。

``cbam.threshold_check``（§6.8）：``m_cum = Σ lots``（排除 CN 2716 电力与 2804 10 00 氢）；``exempt = m_cum ≤ 门槛``
  （门槛取因子行 ``cbam.threshold.net_mass``）。输入 ``lot_<id>``（t），每批 CN 编码由 ``params.cn_lot_<id>`` 给出。
"""
from __future__ import annotations

import datetime as _dt
import re

from ..dec import ONE, ZERO, Rat
from ..engine import Ctx, Item, MethodOutput
from ..errors import (
    CALC_FACTOR_MISSING,
    CALC_INPUT_MISSING,
    CALC_METHODOLOGY_UNSUPPORTED,
    CALC_PARAM_REQUIRES_DECISION,
    CALC_PERIOD_MISMATCH,
    CalcError,
)
from ..registry import register
from ..types import MethodSpec

SECTORS = ("iron_steel", "aluminium", "hydrogen", "cement", "fertilisers", "electricity")
DIRECT_ONLY_SECTORS = ("iron_steel", "aluminium", "hydrogen")   # 2023/956 Annex II
THRESHOLD_EXCLUDED_CN_PREFIXES = ("2716", "28041000")             # Art. 2a：电力与氢不计入
_PRECURSOR_RE = re.compile(r"^precursor_(?P<x>[a-z0-9_]+?)_(?P<kind>mass|see_direct|see_indirect)$")
_CN_RE = re.compile(r"^[0-9]{4,8}$")
_QUARTER_RE = re.compile(r"^(?P<y>[0-9]{4})Q(?P<q>[1-4])$")
_WEEK_RE = re.compile(r"^(?P<y>[0-9]{4})W(?P<w>[0-9]{2})$")


def _cn(ctx: Ctx) -> str:
    cn = ctx.param("cn_code")
    if not _CN_RE.match(cn or ""):
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"params.cn_code {cn!r} must be 4–8 digits")
    return cn  # type: ignore[return-value]


def _cn_candidates(cn: str) -> list[str]:
    out = []
    for n in (8, 6, 4):
        if len(cn) >= n:
            c = cn[:n]
            if c not in out:
                out.append(c)
    return out


def _import_year(ctx: Ctx) -> int:
    y = ctx.raw_value("import_year") if ctx.has("import_year") else None
    if y is None:
        raise CalcError(CALC_INPUT_MISSING, "input import_year is required")
    if ctx.raw_unit("import_year") != "1" or not re.fullmatch(r"[0-9]{4}", y):
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, "import_year must be a 4-digit year with unit '1'")
    year = int(y)
    if year != ctx.period.year:
        raise CalcError(CALC_PERIOD_MISMATCH, f"import_year {year} does not match calculation period {ctx.period.start}..{ctx.period.end}")
    return year


# ---------------------------------------------------------------------------
# SEE 实际值
# ---------------------------------------------------------------------------

def _see_actual(ctx: Ctx, *, complex_goods: bool) -> MethodOutput:
    component = ctx.param("component", choices=("direct", "indirect", "specific_electricity_consumption"), default="direct")
    sector = ctx.param("sector", choices=SECTORS, required=False)
    al = ctx.inp("activity_level", "t")
    if al <= 0:
        raise CalcError(CALC_INPUT_MISSING, "activity_level must be > 0")
    attr_dir = ctx.inp("attributed_emissions_direct", "tCO2e")
    e_el = ctx.inp_opt("electricity_consumed", "MWh")
    if ctx.has("attributed_emissions_indirect"):
        if ctx.has("electricity_emission_factor"):
            raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, "give either attributed_emissions_indirect or electricity_emission_factor")
        attr_ind = ctx.inp("attributed_emissions_indirect", "tCO2e")
    elif e_el is not None:
        attr_ind = e_el * ctx.inp("electricity_emission_factor", "tCO2e/MWh")
    else:
        attr_ind = None
    pre: dict[str, dict[str, str]] = {}
    for n in ctx.input_names:
        m = _PRECURSOR_RE.match(n)
        if m:
            pre.setdefault(m.group("x"), {})[m.group("kind")] = n
    if pre and not complex_goods:
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, "precursors given: use cbam.see_complex (simple goods use zero-embedded precursors only)")
    if complex_goods and not pre:
        raise CalcError(CALC_INPUT_MISSING, "cbam.see_complex requires at least one precursor_<x>_mass")
    ee_dir = ZERO
    ee_ind = ZERO
    for x in sorted(pre):
        k = pre[x]
        if "mass" not in k:
            raise CalcError(CALC_INPUT_MISSING, f"precursor_{x}_mass missing")
        mass = ctx.inp(k["mass"], "t")
        ee_dir = ee_dir + mass * ctx.inp(f"precursor_{x}_see_direct", "tCO2e/t")
        if "see_indirect" in k:
            ee_ind = ee_ind + mass * ctx.inp(k["see_indirect"], "tCO2e/t")
        elif attr_ind is not None:
            raise CalcError(CALC_INPUT_MISSING, f"precursor_{x}_see_indirect missing")
    items: list[Item] = [Item("attributed_emissions_direct", attr_dir, "tCO2e")]
    if complex_goods:
        items.append(Item("ee_inp_mat_direct", ee_dir, "tCO2e"))
    see_dir = (attr_dir + ee_dir) / al
    items.append(Item("see_direct", see_dir, "tCO2e/t"))
    see_ind = None
    if attr_ind is not None:
        items.append(Item("attributed_emissions_indirect", attr_ind, "tCO2e"))
        if complex_goods:
            items.append(Item("ee_inp_mat_indirect", ee_ind, "tCO2e"))
        see_ind = (attr_ind + ee_ind) / al
        items.append(Item("see_indirect", see_ind, "tCO2e/t"))
    sec = None
    if e_el is not None:
        sec = e_el / al
        items.append(Item("specific_electricity_consumption", sec, "MWh/t"))
    if sector in DIRECT_ONLY_SECTORS:
        items.append(Item("see_for_certificates", see_dir, "tCO2e/t"))
        ctx.notes.append("Reg. (EU) 2023/956 Annex II: only direct emissions count for certificates (iron/steel, aluminium, hydrogen)")
    elif sector is not None and see_ind is not None:
        items.append(Item("see_for_certificates", see_dir + see_ind, "tCO2e/t"))
    if component == "direct":
        return MethodOutput(see_dir, "tCO2e/t", items)
    if component == "indirect":
        if see_ind is None:
            raise CalcError(CALC_INPUT_MISSING, "indirect SEE needs electricity_consumed + electricity_emission_factor")
        return MethodOutput(see_ind, "tCO2e/t", items)
    if sec is None:
        raise CalcError(CALC_INPUT_MISSING, "SEC needs electricity_consumed")
    return MethodOutput(sec, "MWh/t", items)


_SEE_PARAMS = ("component", "sector", "cn_code", "country", "rounding_digits", "decision_ref")


@register(MethodSpec(
    method_id="cbam.see_simple", family="cbam-see@1.0.0",
    required_inputs=("attributed_emissions_direct", "activity_level"),
    optional_inputs=("electricity_consumed", "electricity_emission_factor", "attributed_emissions_indirect"),
    required_params=(), optional_params=_SEE_PARAMS,
    default_digits=6, result_unit="tCO2e/t",
    result_datapoints=("dp:prod.cbam.see_direct", "dp:prod.cbam.see_indirect", "dp:prod.cbam.specific_electricity_consumption"),
    description="简单货物 SEE_dir = AttrEm_dir / AL；SEE_ind = E_el × EF_el / AL；SEC = E_el / AL（05 §6.2）",
))
def see_simple(ctx: Ctx) -> MethodOutput:
    return _see_actual(ctx, complex_goods=False)


@register(MethodSpec(
    method_id="cbam.see_complex", family="cbam-see@1.0.0",
    required_inputs=("attributed_emissions_direct", "activity_level", "precursor_<x>_mass", "precursor_<x>_see_direct"),
    optional_inputs=("electricity_consumed", "electricity_emission_factor", "attributed_emissions_indirect",
                     "precursor_<x>_see_indirect"),
    required_params=(), optional_params=_SEE_PARAMS,
    default_digits=6, result_unit="tCO2e/t",
    result_datapoints=("dp:prod.cbam.see_direct", "dp:prod.cbam.see_indirect", "dp:prod.cbam.specific_electricity_consumption"),
    description="复杂货物 SEE = (AttrEm + Σ M_i × SEE_i) / AL，直接、间接各算一遍（05 §6.2）",
))
def see_complex(ctx: Ctx) -> MethodOutput:
    return _see_actual(ctx, complex_goods=True)


# ---------------------------------------------------------------------------
# 默认值、加价、SEFA
# ---------------------------------------------------------------------------

def markup_key(ctx: Ctx, sector: str, year: int) -> str:
    exact = f"cbam.markup.{sector}.{year}"
    if ctx.has_factor(exact):
        return exact
    prefix = f"cbam.markup.{sector}."
    years = sorted(int(r.factor_key[len(prefix):]) for r in ctx.fs.rows
                   if r.factor_key.startswith(prefix) and r.factor_key[len(prefix):].isdigit())
    earlier = [y for y in years if y <= year]
    if not earlier:
        raise CalcError(CALC_FACTOR_MISSING, f"{ctx.fs.id}: no markup rule for {sector} in or before {year}")
    return f"{prefix}{earlier[-1]}"


@register(MethodSpec(
    method_id="cbam.see_default", family="cbam-see@1.0.0",
    required_inputs=("import_year",), optional_inputs=(),
    required_params=("cn_code", "country", "sector"), optional_params=("rounding_digits", "decision_ref"),
    default_digits=6, result_unit="tCO2e/t", result_datapoints=(),
    description="SEE_def,y = DV_total × (1 + μ_y)；证书计算一律取 total 列再加价（05 §6.3）",
))
def see_default(ctx: Ctx) -> MethodOutput:
    cn = _cn(ctx)
    country = ctx.param("country")
    sector = ctx.param("sector", choices=SECTORS)
    year = _import_year(ctx)
    key = None
    for c in _cn_candidates(cn):
        for geo in (country, "OTHER"):
            k = f"cbam.default.total.{c}.{geo}"
            if ctx.has_factor(k):
                key = k
                break
        if key:
            break
    if key is None:
        raise CalcError(CALC_FACTOR_MISSING, f"{ctx.fs.id}: no default value for CN {cn} / {country} (nor Other countries)")
    if key.endswith(".OTHER"):
        ctx.notes.append("country not listed: 'Other countries and territories' default value used (05 §6.3)")
    dv = ctx.factor(key, "tCO2e/t", geography=False)
    mu = ctx.factor(markup_key(ctx, sector, year), "1", geography=False)
    see = dv * (ONE + mu)
    ctx.notes.append("mark-up included; CBAM Registry rounding rule not published [unverified] (05 §6.3)")
    items = [
        Item("default_value_total", dv, "tCO2e/t"),
        Item("markup", mu, "1"),
        Item("see_default_unrounded", see, "tCO2e/t"),
    ]
    return MethodOutput(see, "tCO2e/t", items)


@register(MethodSpec(
    method_id="cbam.sefa_default", family="cbam-see@1.0.0",
    required_inputs=("import_year",), optional_inputs=(),
    required_params=("cn_code",), optional_params=("route", "sector", "rounding_digits", "decision_ref"),
    default_digits=6, result_unit="tCO2e/t", result_datapoints=(),
    description="SEFA_def = CBAM_y × CSCF_y × BM_g（Guidance 4 Eq. 6；BM_g 为 2025/2620 Column B）（05 §6.4）",
))
def sefa_default(ctx: Ctx) -> MethodOutput:
    cn = _cn(ctx)
    year = _import_year(ctx)
    route = ctx.params.get("route")
    key = None
    for c in _cn_candidates(cn):
        prefix = f"cbam.bm_default.{c}."
        routes = sorted({r.factor_key[len(prefix):] for r in ctx.fs.rows if r.factor_key.startswith(prefix)})
        if not routes:
            continue
        if route is None:
            if len(routes) > 1:
                raise CalcError(CALC_PARAM_REQUIRES_DECISION, f"several benchmark routes for CN {c}: {routes}; set params.route")
            key = prefix + routes[0]
        elif route in routes:
            key = prefix + route
        else:
            raise CalcError(CALC_FACTOR_MISSING, f"{ctx.fs.id}: no benchmark for CN {c} route {route!r}")
        break
    if key is None:
        raise CalcError(CALC_FACTOR_MISSING, f"{ctx.fs.id}: no default-value benchmark for CN {cn}")
    cf = ctx.factor(f"cbam.factor.{year}", "1", geography=False)
    cscf = ctx.factor(f"cbam.cscf.{year}", "1", geography=False)
    bm = ctx.factor(key, "tCO2e/t", geography=False)
    sefa = cf * cscf * bm
    items = [
        Item("cbam_factor", cf, "1"),
        Item("cscf", cscf, "1"),
        Item("benchmark", bm, "tCO2e/t"),
        Item("sefa_default_unrounded", sefa, "tCO2e/t"),
    ]
    return MethodOutput(sefa, "tCO2e/t", items)


# ---------------------------------------------------------------------------
# 成本差
# ---------------------------------------------------------------------------

def _price_period(ctx: Ctx) -> tuple[str, _dt.date, _dt.date]:
    q = ctx.params.get("quarter")
    w = ctx.params.get("week")
    if (q is None) == (w is None):
        raise CalcError(CALC_INPUT_MISSING, "exactly one of params.quarter (2026) or params.week (2027+) is required")
    if q is not None:
        m = _QUARTER_RE.match(q)
        if not m:
            raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"params.quarter {q!r} must be YYYYQn")
        y, n = int(m.group("y")), int(m.group("q"))
        start = _dt.date(y, 3 * n - 2, 1)
        end = (_dt.date(y + 1, 1, 1) if n == 4 else _dt.date(y, 3 * n + 1, 1)) - _dt.timedelta(days=1)
        return f"cbam.price.{q}", start, end
    m = _WEEK_RE.match(w or "")
    if not m:
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"params.week {w!r} must be YYYYWww")
    y, n = int(m.group("y")), int(m.group("w"))
    try:
        start = _dt.date.fromisocalendar(y, n, 1)
    except ValueError as e:
        raise CalcError(CALC_METHODOLOGY_UNSUPPORTED, f"invalid ISO week {w}") from e
    return f"cbam.price.{w}", start, start + _dt.timedelta(days=6)


@register(MethodSpec(
    method_id="cbam.cost_gap", family="cbam-cost-gap@1.0.0",
    required_inputs=("see_default", "sefa_default", "see_actual", "sefa_actual", "import_mass"),
    optional_inputs=("carbon_price_paid",),
    required_params=("quarter|week",), optional_params=("cn_code", "country", "sector", "rounding_digits", "decision_ref"),
    default_digits=2, result_unit="EUR", result_datapoints=(),
    description="cost_gap = (N_def − N_act) × P_y，N_act 扣除原产国已付碳价 D_cpp = CPP/P（Art. 9(1)）（05 §6.6）",
))
def cost_gap(ctx: Ctx) -> MethodOutput:
    key, start, end = _price_period(ctx)
    if ctx.period.start_date != start or ctx.period.end_date != end:
        raise CalcError(CALC_PERIOD_MISMATCH, f"price period {start}..{end} does not equal calculation period {ctx.period.start}..{ctx.period.end}")
    sector = ctx.param("sector", choices=SECTORS, required=False)
    m = ctx.inp("import_mass", "t")
    see_def = ctx.inp("see_default", "tCO2e/t")
    sefa_def = ctx.inp("sefa_default", "tCO2e/t")
    see_act = ctx.inp("see_actual", "tCO2e/t")
    sefa_act = ctx.inp("sefa_actual", "tCO2e/t")
    cpp = ctx.inp_opt("carbon_price_paid", "EUR", ZERO)
    price = ctx.factor(key, "EUR/tCO2e", geography=False)
    if price <= 0:
        raise CalcError(CALC_FACTOR_MISSING, f"{key} must be > 0")
    if sector in DIRECT_ONLY_SECTORS:
        ctx.notes.append("SEE_act for iron/steel/aluminium/hydrogen must be direct emissions only (Annex II)")
    n_def = m * (see_def - sefa_def)
    if n_def < 0:
        n_def = ZERO
    n_act_gross = m * (see_act - sefa_act)
    d_cpp = cpp / price
    n_act = n_act_gross - d_cpp
    if n_act < 0:
        n_act = ZERO
    cost_def = n_def * price
    cost_act = n_act * price
    gap = cost_def - cost_act
    items = [
        Item("certificates_default", n_def, "tCO2e"),
        Item("certificates_actual_gross", n_act_gross, "tCO2e"),
        Item("carbon_price_deduction_certificates", d_cpp, "tCO2e"),
        Item("certificates_actual", n_act, "tCO2e"),
        Item("cost_default", cost_def, "EUR"),
        Item("cost_actual", cost_act, "EUR"),
        Item("cost_gap_per_tonne", gap / m if m > 0 else ZERO, "EUR/t"),
        Item("cost_gap_unrounded", gap, "EUR"),
    ]
    return MethodOutput(gap, "EUR", items)


# ---------------------------------------------------------------------------
# 50 t 门槛
# ---------------------------------------------------------------------------

@register(MethodSpec(
    method_id="cbam.threshold_check", family="cbam-threshold@1.0.0",
    required_inputs=("lot_<id> (t)",), optional_inputs=(),
    required_params=("cn_lot_<id>",), optional_params=("rounding_digits", "decision_ref"),
    default_digits=3, result_unit="t", result_datapoints=("dp:prod.cbam.import_net_mass_annual",),
    description="m_cum = Σ m_net（排除 CN 2716、2804 10 00）；exempt = m_cum ≤ 50 t（门槛为因子行）（05 §6.8）",
))
def threshold_check(ctx: Ctx) -> MethodOutput:
    p = ctx.period
    if not (p.start_date.month == 1 and p.start_date.day == 1 and p.end_date.month == 12 and p.end_date.day == 31
            and p.start_date.year == p.end_date.year):
        raise CalcError(CALC_PERIOD_MISMATCH, "threshold is assessed per calendar year (Art. 2a)")
    lots = [n for n in ctx.input_names if n.startswith("lot_")]
    if not lots:
        raise CalcError(CALC_INPUT_MISSING, "no lot_<id> inputs")
    cum = ZERO
    excluded = ZERO
    for n in lots:
        cn = ctx.params.get(f"cn_{n}")
        if cn is None or not _CN_RE.match(cn):
            raise CalcError(CALC_INPUT_MISSING, f"params.cn_{n} (CN code of {n}) is required")
        mass = ctx.inp(n, "t")
        if any(cn.startswith(pfx) for pfx in THRESHOLD_EXCLUDED_CN_PREFIXES):
            excluded = excluded + mass
        else:
            cum = cum + mass
    thr = ctx.factor("cbam.threshold.net_mass", "t")
    exempt = cum <= thr
    util = cum * 100 / thr
    items = [
        Item("threshold", thr, "t"),
        Item("excluded_mass", excluded, "t"),
        Item("exempt", ONE if exempt else ZERO, "1"),
        Item("threshold_utilisation_percent", util, "%"),
        Item("approaching_threshold", ONE if (exempt and util >= 80) else ZERO, "1"),
    ]
    ctx.annotations["exempt"] = exempt
    ctx.notes.append("hint, not legal advice: '基于您提供的数据，系统提示可能……' (risk_level ≥ L2; 05 §6.8)")
    return MethodOutput(cum, "t", items)
