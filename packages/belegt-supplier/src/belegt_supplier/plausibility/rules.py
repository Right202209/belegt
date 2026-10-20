# Spec: 08 §5.2 规则目录 PLAUS-001…027（数据）。阈值为 08 默认值；标 [需核对] 的行业区间按 08 待决问题 4 校准。
"""数据类合理性规则（确定性）。

注：08 §5.1 规定 PLAUS 规则最终归 `belegt-calc` 的 `plausibility` 模块；本包按任务要求自带一份实现，
供供应商端即时提示（08 §6.4 子集）和独立部署使用，接口保持纯函数，便于迁移。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from ..decimal_util import is_dec_str
from . import geo
from .model import PlausContext, PlausHit, PlausValue, RuleSpec

D = Decimal
SEE_DIRECT = "dp:prod.cbam.see_direct"
SEE_INDIRECT = "dp:prod.cbam.see_indirect"
ROUTE = "dp:prod.cbam.production_route"
SEC = "dp:prod.cbam.specific_electricity_consumption"
INST = "dp:prod.cbam.installation_id"
CN = "dp:prod.identity.cn_code"
CPP = "dp:prod.cbam.carbon_price_paid"
GEO = "dp:supply.eudr.geolocation"
PROD_DATE = "dp:supply.eudr.production_date"
COUNTRY = "dp:supply.eudr.country_of_production"
QTY = "dp:supply.eudr.commodity_quantity"
DDS_REF = "dp:supply.eudr.dds_reference"
DDS_VER = "dp:supply.eudr.dds_verification_number"
PCF = "dp:prod.pcf.cradle_to_gate"
PCF_EXCL = "dp:prod.pcf.cradle_to_gate.excluding_biogenic"
PCF_UNIT = "dp:prod.pcf.declared_unit"
PCF_PDS = "dp:prod.pcf.primary_data_share"
PCF_REF = "dp:prod.pcf.reference_period"
EUDR_CUTOFF = date(2020, 12, 31)

# 单位换算到规则基准单位
_TO_BASE = {
    ("tCO2e/t", "tCO2e/t"): D(1), ("kgCO2e/t", "tCO2e/t"): D("0.001"),
    ("MWh/t", "MWh/t"): D(1), ("kWh/t", "MWh/t"): D("0.001"),
    ("kg", "kg"): D(1), ("t", "kg"): D(1000),
    ("kgCO2e/kg", "kgCO2e/kg"): D(1), ("tCO2e/t", "kgCO2e/kg"): D(1),
}
_BASE_UNIT = {SEE_DIRECT: "tCO2e/t", SEE_INDIRECT: "tCO2e/t", SEC: "MWh/t", QTY: "kg", PCF: "kgCO2e/kg", PCF_EXCL: "kgCO2e/kg"}

# CBAM 附件 I（Reg. (EU) 2023/956，经 2025/2083 修订）CN 前缀近似清单 [需核对]
CBAM_ANNEX_I_PREFIXES = (
    "25070080", "252310", "252321", "252329", "252330", "252390",  # 水泥
    "27160000",  # 电力
    "28080000", "2814", "28342100", "3102", "3105",  # 化肥
    "26011200", "7201", "72021", "72024", "72026", "7203", "7205", "7206", "7207", "7208", "7209", "7210", "7211",
    "7212", "7213", "7214", "7215", "7216", "7217", "7218", "7219", "7220", "7221", "7222", "7223", "7224", "7225",
    "7226", "7227", "7228", "7229", "7301", "7302", "730300", "7304", "7305", "7306", "7307", "7308", "730900",
    "7310", "731100", "7318", "7326",  # 钢铁
    "7601", "7603", "7604", "7605", "7606", "7607", "7608", "76090000", "7610", "76110000", "7612", "76130000",
    "7614", "7616",  # 铝
    "28041000",  # 氢
)


def _num(v: PlausValue) -> Decimal | None:
    if not isinstance(v.value, str) or not is_dec_str(v.value):
        return None
    try:
        d = D(v.value)
    except InvalidOperation:
        return None
    base = _BASE_UNIT.get(v.base)
    if base and v.unit:
        f = _TO_BASE.get((v.unit, base))
        if f is None:
            return None
        d = d * f
    return d


def _by_product(values: list[PlausValue]) -> dict[str | None, dict[str, PlausValue]]:
    out: dict[str | None, dict[str, PlausValue]] = defaultdict(dict)
    for v in values:
        out[v.product_id][v.base] = v
    return out


def _fmt(d: Decimal, q: str = "0.001") -> str:
    return str(d.quantize(D(q)).normalize()) if d == d else "nan"


def _cn_of(pid: str | None, row: dict[str, PlausValue], ctx: PlausContext) -> str | None:
    if CN in row and isinstance(row[CN].value, str):
        return row[CN].value.strip()
    return ctx.requested_cn_codes.get(pid or "")


def _see_ratio(pid, row, ctx, dp, defaults) -> tuple[Decimal, Decimal] | None:
    v = row.get(dp)
    if v is None:
        return None
    x = _num(v)
    cn = _cn_of(pid, row, ctx)
    ref = defaults.get(cn or "")
    if x is None or not ref or x <= 0:
        return None
    return x / ref, ref


def _in_unit_band(r: Decimal) -> bool:
    return D(500) <= r <= D(2000) or D("0.0005") <= r <= D("0.002")


# ---------------------------------------------------------------- 001–003
def plaus_001(values, ctx):
    hits = []
    for pid, row in _by_product(values).items():
        r = _see_ratio(pid, row, ctx, SEE_DIRECT, ctx.cbam_defaults)
        if r and r[0] < D("0.30") and not _in_unit_band(r[0]):
            hits.append(PlausHit("PLAUS-001", "warn", "SEE direct far below EU default", {"ratio": _fmt(r[0], "0.01")}, SEE_DIRECT, pid))
    return hits


def plaus_002(values, ctx):
    hits = []
    for pid, row in _by_product(values).items():
        r = _see_ratio(pid, row, ctx, SEE_DIRECT, ctx.cbam_defaults)
        if r and r[0] > D("1.50") and not _in_unit_band(r[0]):
            hits.append(PlausHit("PLAUS-002", "warn", "SEE direct far above EU default", {"ratio": _fmt(r[0], "0.01")}, SEE_DIRECT, pid))
    return hits


def plaus_003(values, ctx):
    hits = []
    for pid, row in _by_product(values).items():
        for dp, defaults in ((SEE_DIRECT, ctx.cbam_defaults), (SEE_INDIRECT, ctx.cbam_defaults_indirect or ctx.cbam_defaults)):
            r = _see_ratio(pid, row, ctx, dp, defaults)
            if r and _in_unit_band(r[0]):
                hits.append(PlausHit("PLAUS-003", "block", "unit magnitude error suspected (×1000 or ÷1000)",
                                     {"ratio": _fmt(r[0], "0.0001")}, dp, pid, "unit_suspect"))
    return hits


# ---------------------------------------------------------------- 004–007
def _route(row) -> str | None:
    v = row.get(ROUTE)
    return v.value if v is not None and isinstance(v.value, str) else None


def plaus_004(values, ctx):
    hits = []
    for pid, row in _by_product(values).items():
        x = _num(row[SEE_DIRECT]) if SEE_DIRECT in row else None
        if _route(row) == "bf_bof" and x is not None and x < D("1.2"):
            hits.append(PlausHit("PLAUS-004", "warn", "BF-BOF with low direct intensity", {"see": _fmt(x)}, SEE_DIRECT, pid))
    return hits


def plaus_005(values, ctx):
    hits = []
    for pid, row in _by_product(values).items():
        x = _num(row[SEE_DIRECT]) if SEE_DIRECT in row else None
        if _route(row) == "eaf" and x is not None and x > D("0.8"):
            hits.append(PlausHit("PLAUS-005", "warn", "EAF with high direct intensity", {"see": _fmt(x)}, SEE_DIRECT, pid))
    return hits


def plaus_006(values, ctx):
    hits = []
    for pid, row in _by_product(values).items():
        if SEE_INDIRECT not in row or SEC not in row:
            continue
        ind, sec = _num(row[SEE_INDIRECT]), _num(row[SEC])
        country = ctx.installation_country.get(pid or "")
        grid = ctx.grid_factors.get(country or "")
        if ind is None or sec is None or sec <= 0 or not grid:
            continue
        implied = ind / sec
        dev = (implied - grid) / grid
        if abs(dev) > D("0.35"):
            hits.append(PlausHit("PLAUS-006", "warn", "implied electricity factor deviates from grid factor",
                                 {"implied": _fmt(implied), "grid": str(grid), "dev": _fmt(dev, "0.01"),
                                  "src": ctx.grid_factor_source}, SEE_INDIRECT, pid))
    return hits


def plaus_007(values, ctx):
    hits = []
    for pid, row in _by_product(values).items():
        sec = _num(row[SEC]) if SEC in row else None
        route = _route(row)
        if sec is None:
            continue
        if (route == "eaf" and sec < D("0.3")) or (route == "bf_bof" and sec > D("0.5")):
            hits.append(PlausHit("PLAUS-007", "warn", "specific electricity consumption inconsistent with route",
                                 {"route": route or "", "sec": _fmt(sec)}, SEC, pid))
    return hits


# ---------------------------------------------------------------- 008–014
NUMERIC_DPS = {SEE_DIRECT, SEE_INDIRECT, SEC, CPP, QTY, PCF, PCF_EXCL, PCF_PDS}


def plaus_008(values, ctx):
    hits = []
    for v in values:
        if v.base not in NUMERIC_DPS and not (v.unit and v.base not in (GEO,)):
            continue
        if v.value in (None, ""):
            continue
        if not isinstance(v.value, str) or not is_dec_str(v.value):
            hits.append(PlausHit("PLAUS-008", "block", "not a DEC-STR number", {"value": str(v.value)[:20]}, v.base, v.product_id))
            continue
        d = D(v.value)
        if d < 0 or (d == 0 and v.base not in ctx.zero_allowed and v.datapoint_id not in ctx.zero_allowed):
            hits.append(PlausHit("PLAUS-008", "block", "negative or zero value", {"value": v.value}, v.base, v.product_id))
    return hits


def plaus_009(values, ctx):
    hits = []
    for v in values:
        if v.base not in (SEE_DIRECT, SEE_INDIRECT):
            continue
        prev = ctx.previous_values.get((v.base, v.product_id))
        x = _num(v)
        if prev and x is not None and prev > 0:
            change = (x - prev) / prev
            if abs(change) > D("0.40"):
                hits.append(PlausHit("PLAUS-009", "warn", "year-on-year change > 40%", {"change": _fmt(change, "0.01")}, v.base, v.product_id))
    return hits


def plaus_010(values, ctx):
    hits = []
    for v in values:
        if v.base not in (SEE_DIRECT, SEE_INDIRECT):
            continue
        x = _num(v)
        for other in ctx.other_channel_values.get((v.base, v.product_id), []):
            if x is not None and other > 0 and abs(x - other) / other > D("0.01"):
                hits.append(PlausHit("PLAUS-010", "warn", "differs from value given via another channel/customer",
                                     {"other": str(other), "value": _fmt(x)}, v.base, v.product_id))
                break
    return hits


def plaus_011(values, ctx):
    hits = []
    for v in values:
        if v.base != INST or not isinstance(v.value, str):
            continue
        val = v.value.strip()
        if not re.match(ctx.installation_id_pattern, val):
            hits.append(PlausHit("PLAUS-011", "warn", "installation id format", {"reason": "format"}, INST, v.product_id))
        prev = ctx.previous_installation_ids.get(v.product_id) or ctx.previous_installation_ids.get(None)
        if prev and prev != val:
            hits.append(PlausHit("PLAUS-011", "warn", "installation id changed since last period", {"reason": "changed"}, INST, v.product_id))
    return hits


def plaus_012(values, ctx):
    hits = []
    for v in values:
        if v.base != CN:
            continue
        val = str(v.value or "").replace(" ", "").replace(".", "")
        reasons = []
        if not re.fullmatch(r"[0-9]{8}", val):
            reasons.append("format")
        elif not val.startswith(CBAM_ANNEX_I_PREFIXES):
            reasons.append("not_annex_i")
        req = ctx.requested_cn_codes.get(v.product_id or "")
        if req and val != req:
            reasons.append("product_mismatch")
        if reasons:
            hits.append(PlausHit("PLAUS-012", "block", "invalid CN code", {"reason": "+".join(reasons)}, CN, v.product_id))
    return hits


def plaus_013(values, ctx):
    hits = []
    for v in values:
        if v.base != CPP:
            continue
        x = _num(v)
        origin = ctx.origin_country.get(v.product_id or "")
        if x is not None and x > 0 and origin and origin.upper() not in ctx.carbon_pricing_countries:
            hits.append(PlausHit("PLAUS-013", "warn", "carbon price paid but no known pricing scheme in origin", {"country": origin}, CPP, v.product_id))
        if x is not None and x > 0 and (not v.unit or ctx.currency_given.get(v.product_id) is False):
            hits.append(PlausHit("PLAUS-013", "warn", "currency/unit missing", {"reason": "unit"}, CPP, v.product_id))
    return hits


def plaus_014(values, ctx):
    has_see = any(v.base in (SEE_DIRECT, SEE_INDIRECT) and v.value not in (None, "") for v in values)
    if has_see and not ctx.has_verification_report and not ctx.verification_expected_note:
        return [PlausHit("PLAUS-014", "info", "SEE provided without verification report or expected date")]
    return []


# ---------------------------------------------------------------- 015–021（EUDR）
def _geo_items(values):
    for v in values:
        if v.base == GEO and v.value not in (None, ""):
            try:
                obj, raw = geo.load_geojson(v.value)
            except geo.GeoError as exc:
                yield v, None, None, str(exc)
                continue
            yield v, obj, (v.raw_text or raw), None


def plaus_015(values, ctx):
    hits = []
    for v, obj, raw, err in _geo_items(values):
        problems = [err] if err else geo.validate(obj, raw)
        if problems:
            hits.append(PlausHit("PLAUS-015", "block", "; ".join(problems), {"n": str(len(problems))}, GEO, v.product_id))
    return hits


def _valid_geoms(values):
    for v, obj, raw, err in _geo_items(values):
        if err or geo.validate(obj, raw, min_decimals=0):
            continue
        for g, props in geo.geometries(obj):
            yield v, g, props


def plaus_016(values, ctx):
    if ctx.geo_oracle is None:
        return []
    hits = []
    for v, g, _ in _valid_geoms(values):
        lon, lat = geo.centroid(g)
        if ctx.geo_oracle.is_land(lon, lat) is False:
            hits.append(PlausHit("PLAUS-016", "block", "plot located in the sea", {"lon": f"{lon:.4f}", "lat": f"{lat:.4f}"}, GEO, v.product_id))
    return hits


def plaus_017(values, ctx):
    if ctx.geo_oracle is None:
        return []
    by_p = _by_product(values)
    hits = []
    for v, g, _ in _valid_geoms(values):
        cv = by_p.get(v.product_id, {}).get(COUNTRY)
        if cv is None or not isinstance(cv.value, str):
            continue
        lon, lat = geo.centroid(g)
        if ctx.geo_oracle.in_country(lon, lat, cv.value, 1000.0) is False:
            hits.append(PlausHit("PLAUS-017", "block", "plot outside declared country", {"country": cv.value}, GEO, v.product_id))
    return hits


def plaus_018(values, ctx):
    hits = []
    for v, g, props in _valid_geoms(values):
        if g.get("type") in ("Point", "MultiPoint"):
            area = geo.declared_area_ha(props)
            if area is not None and area > 4:
                hits.append(PlausHit("PLAUS-018", "block", "plot > 4 ha submitted as Point", {"area_ha": f"{area:g}"}, GEO, v.product_id))
    return hits


def plaus_019(values, ctx):
    hits = []
    total_ha = 0.0
    pid_last = None
    for v, g, _ in _valid_geoms(values):
        if g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        ha = geo.polygon_area_m2(g) / 10_000
        total_ha += ha
        pid_last = v.product_id
        if ha < 0.01 or ha > 10_000:
            hits.append(PlausHit("PLAUS-019", "warn", "polygon area out of range", {"area_ha": f"{ha:.4f}"}, GEO, v.product_id))
    if ctx.max_yield_kg_per_ha and total_ha > 0:
        qty = next((_num(v) for v in values if v.base == QTY and _num(v) is not None), None)
        if qty is not None and qty / D(str(total_ha)) > ctx.max_yield_kg_per_ha:
            hits.append(PlausHit("PLAUS-019", "warn", "quantity implausible for plot area",
                                 {"kg_per_ha": _fmt(qty / D(str(total_ha)), "1")}, GEO, pid_last))
    return hits


def _parse_dates(v: PlausValue) -> tuple[date | None, date | None, bool]:
    raw = v.value
    try:
        if isinstance(raw, dict):
            return date.fromisoformat(str(raw.get("start"))), date.fromisoformat(str(raw.get("end"))), True
        if isinstance(raw, str) and "/" in raw:
            a, b = raw.split("/", 1)
            return date.fromisoformat(a.strip()), date.fromisoformat(b.strip()), True
        if isinstance(raw, str):
            d = date.fromisoformat(raw.strip())
            return d, d, False
    except ValueError:
        return None, None, False
    return None, None, False


def plaus_020(values, ctx):
    hits = []
    today = ctx.submission_date or date.today()
    for v in values:
        if v.base != PROD_DATE or v.value in (None, ""):
            continue
        s, e, _rng = _parse_dates(v)
        if s is None or e is None:
            hits.append(PlausHit("PLAUS-020", "warn", "unparseable date", {}, PROD_DATE, v.product_id))
            continue
        if s > today or e > today:
            hits.append(PlausHit("PLAUS-020", "block", "production date in the future", {"reason": "future"}, PROD_DATE, v.product_id))
        if s > e:
            hits.append(PlausHit("PLAUS-020", "warn", "range start after end", {"reason": "order"}, PROD_DATE, v.product_id))
        if s <= EUDR_CUTOFF:
            hits.append(PlausHit("PLAUS-020", "warn", "before EUDR cut-off date, needs manual confirmation", {"reason": "pre_cutoff"}, PROD_DATE, v.product_id))
    return hits


def plaus_021(values, ctx):
    hits = []
    for pid, row in _by_product(values).items():
        ref = row.get(DDS_REF)
        ver = row.get(DDS_VER)
        rv = (ref.value or "").strip() if ref and isinstance(ref.value, str) else ""
        vv = (ver.value or "").strip() if ver and isinstance(ver.value, str) else ""
        if not rv and not vv:
            continue
        if bool(rv) != bool(vv):
            hits.append(PlausHit("PLAUS-021", "block", "DDS reference and verification number must come as a pair", {"reason": "pair"}, DDS_REF, pid))
            continue
        if not re.match(ctx.dds_reference_pattern, rv) or not re.match(ctx.dds_verification_pattern, vv):
            hits.append(PlausHit("PLAUS-021", "block", "DDS number format", {"reason": "format"}, DDS_REF, pid))
    return hits


# ---------------------------------------------------------------- 022–027（PCF 与通用）
def plaus_022(values, ctx):
    hits = []
    for pid, row in _by_product(values).items():
        if PCF not in row:
            continue
        unit = row.get(PCF_UNIT)
        if unit is None or unit.value in (None, ""):
            hits.append(PlausHit("PLAUS-022", "block", "declared unit missing", {"reason": "declared_unit"}, PCF, pid))
        x = _num(row[PCF])
        bench = ctx.pcf_benchmarks.get(ctx.product_material.get(pid or "", ""))
        if x is not None and bench and bench > 0:
            r = x / bench
            if r < D("0.2") or r > D(5):
                hits.append(PlausHit("PLAUS-022", "warn", "PCF far from industry benchmark", {"ratio": _fmt(r, "0.01")}, PCF, pid))
    return hits


def plaus_023(values, ctx):
    hits = []
    for pid, row in _by_product(values).items():
        if PCF not in row or PCF_EXCL not in row:
            continue
        inc, exc = _num(row[PCF]), _num(row[PCF_EXCL])
        if inc is None or exc is None or exc <= 0:
            continue
        diff = (inc - exc) / exc
        is_metal = (pid in ctx.metal_products) if ctx.metal_products else True
        if is_metal and abs(diff) > D("0.10"):
            hits.append(PlausHit("PLAUS-023", "warn", "incl./excl. biogenic differ > 10% for metal product", {"diff": _fmt(diff, "0.01")}, PCF, pid))
        elif not is_metal and diff < D("-0.10"):
            hits.append(PlausHit("PLAUS-023", "warn", "incl. biogenic significantly lower than excl.", {"diff": _fmt(diff, "0.01")}, PCF, pid))
    return hits


def plaus_024(values, ctx):
    hits = []
    for v in values:
        if v.base != PCF_PDS or not isinstance(v.value, str) or not is_dec_str(v.value):
            continue
        x = D(v.value)
        if x < 0 or x > 100:
            hits.append(PlausHit("PLAUS-024", "block", "primary data share outside 0–100", {"value": v.value}, PCF_PDS, v.product_id))
    return hits


def plaus_025(values, ctx):
    hits = []
    today = ctx.submission_date or date.today()
    for v in values:
        if v.base != PCF_REF or v.value in (None, ""):
            continue
        s, e, _ = _parse_dates(v)
        if s is None or e is None:
            hits.append(PlausHit("PLAUS-025", "warn", "unparseable reference period", {}, PCF_REF, v.product_id))
            continue
        if e > today:
            hits.append(PlausHit("PLAUS-025", "warn", "reference period after submission date", {"reason": "future"}, PCF_REF, v.product_id))
        rs = ctx.request_period_start
        if rs and (rs - e).days > 3 * 366:
            hits.append(PlausHit("PLAUS-025", "warn", "reference period more than 3 years from requested period", {"reason": "age"}, PCF_REF, v.product_id))
    return hits


def plaus_026(values, ctx):
    hits = []
    if not (ctx.request_period_start and ctx.request_period_end):
        return hits
    for v in values:
        if v.period_start is None or v.period_end is None:
            continue
        if not (v.period_start <= ctx.request_period_start and v.period_end >= ctx.request_period_end):
            hits.append(PlausHit("PLAUS-026", "warn", "value period does not cover requested period",
                                 {"period": f"{v.period_start}/{v.period_end}"}, v.base, v.product_id))
    return hits


def plaus_027(values, ctx):
    hits = []
    seen: dict[tuple[str | None, str], set[str]] = defaultdict(set)
    for v in values:
        if isinstance(v.value, str) and is_dec_str(v.value) and v.base in NUMERIC_DPS and D(v.value) != 0:
            seen[(v.product_id, str(D(v.value).normalize()))].add(v.base)
    for (pid, val), dps in seen.items():
        if len(dps) > 1:
            hits.append(PlausHit("PLAUS-027", "info", "identical value across different datapoints", {"value": val, "n": str(len(dps))}, None, pid))
    for pid, row in _by_product(values).items():
        if SEE_DIRECT in row:
            x = _num(row[SEE_DIRECT])
            ref = ctx.cbam_defaults.get(_cn_of(pid, row, ctx) or "")
            if x is not None and ref and x == ref:
                hits.append(PlausHit("PLAUS-027", "warn", "value equals the EU default value", {"reason": "equals_default"}, SEE_DIRECT, pid))
    return hits


def _r(rule_id, sev, applies, desc, fn, client=False) -> RuleSpec:
    return RuleSpec(rule_id, sev, applies, desc, fn, client)


DATA_RULES: list[RuleSpec] = [
    _r("PLAUS-001", "warn", SEE_DIRECT, "ratio to EU default < 0.30", plaus_001, True),
    _r("PLAUS-002", "warn", SEE_DIRECT, "ratio to EU default > 1.50", plaus_002, True),
    _r("PLAUS-003", "block", f"{SEE_DIRECT},{SEE_INDIRECT}", "×1000 / ÷1000 unit error (unit_suspect)", plaus_003, True),
    _r("PLAUS-004", "warn", f"{SEE_DIRECT}+{ROUTE}", "bf_bof and SEE direct < 1.2", plaus_004, True),
    _r("PLAUS-005", "warn", f"{SEE_DIRECT}+{ROUTE}", "eaf and SEE direct > 0.8", plaus_005, True),
    _r("PLAUS-006", "warn", f"{SEE_INDIRECT}+{SEC}", "implied electricity factor vs grid factor > ±35%", plaus_006, True),
    _r("PLAUS-007", "warn", f"{SEC}+{ROUTE}", "eaf < 0.3 MWh/t or bf_bof > 0.5 MWh/t", plaus_007, True),
    _r("PLAUS-008", "block", "all numbers", "negative, zero or non DEC-STR", plaus_008, True),
    _r("PLAUS-009", "warn", f"{SEE_DIRECT},{SEE_INDIRECT}", "change vs previous verified > ±40%", plaus_009),
    _r("PLAUS-010", "warn", "see_*", "differs > 1% across customers/channels", plaus_010),
    _r("PLAUS-011", "warn", INST, "format or changed vs previous", plaus_011),
    _r("PLAUS-012", "block", CN, "8 digits, CBAM Annex I, matches requested product", plaus_012, True),
    _r("PLAUS-013", "warn", CPP, "non-zero without known pricing scheme; currency missing", plaus_013),
    _r("PLAUS-014", "info", "verification report", "SEE without report or expected date", plaus_014),
    _r("PLAUS-015", "block", GEO, "WGS 84, ≥ 6 decimals, valid RFC 7946", plaus_015, True),
    _r("PLAUS-016", "block", GEO, "on land", plaus_016, True),
    _r("PLAUS-017", "block", f"{GEO}+{COUNTRY}", "within declared country (+1 km)", plaus_017, True),
    _r("PLAUS-018", "block", GEO, "> 4 ha must be Polygon", plaus_018, True),
    _r("PLAUS-019", "warn", GEO, "area < 0.01 ha or > 10 000 ha; yield mismatch", plaus_019),
    _r("PLAUS-020", "warn", PROD_DATE, "date logic (future = block)", plaus_020, True),
    _r("PLAUS-021", "block", f"{DDS_REF}+{DDS_VER}", "pair and format", plaus_021, True),
    _r("PLAUS-022", "warn", PCF, "benchmark ratio < 0.2 or > 5 (missing declared unit = block)", plaus_022, True),
    _r("PLAUS-023", "warn", f"{PCF} vs {PCF_EXCL}", "incl./excl. biogenic differ > 10%", plaus_023, True),
    _r("PLAUS-024", "block", PCF_PDS, "0–100", plaus_024, True),
    _r("PLAUS-025", "warn", PCF_REF, "not after submission, ≤ 3 years from request period", plaus_025),
    _r("PLAUS-026", "warn", "all", "period covers SupplierRequest.period", plaus_026),
    _r("PLAUS-027", "info", "all numbers", "copy-paste / equals default (warn)", plaus_027),
]
