# Spec: 05 §1.2（舍入）、§1.3（单位换算）；03 §4.5（input_hash 规范化）
"""属性测试（标准库 random，固定种子，可复现）：单位换算往返、舍入边界、input_hash 对键序不敏感。"""
from __future__ import annotations

import random
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext

import pytest

from belegt_calc import CalcError, calculate, convert, input_hash
from belegt_calc import units as U
from belegt_calc.dec import Rat, calc_context, norm, round_str
from belegt_calc.hashing import jcs

from calc_testutil import Y2025, inp

SEED = 20261019
N = 400


def _rand_dec(rng: random.Random, max_int_digits=9, max_frac=6, signed=False) -> Decimal:
    i = rng.randrange(0, 10 ** rng.randint(1, max_int_digits))
    f = rng.randint(0, max_frac)
    v = Decimal(i).scaleb(-f)
    if signed and rng.random() < 0.3:
        v = -v
    return v


# ---------------- 单位换算往返 ----------------

EXACT_PAIRS = [("MWh", "kWh"), ("MWh", "Wh"), ("GWh", "MWh"), ("t", "kg"), ("kg", "g"), ("tCO2e", "kgCO2e"),
               ("m3", "L"), ("%", "1"), ("tC", "kgC"), ("MWh", "GJ"), ("MWh", "MJ"), ("kWh", "GJ"),
               ("tCO2e/MWh", "kgCO2e/kWh"), ("kgCO2e/kg", "tCO2e/t"), ("EUR/tCO2e", "EUR/kgCO2e")]


@pytest.mark.parametrize("a,b", EXACT_PAIRS)
def test_roundtrip_coarse_fine_coarse_exact(a, b):
    """先乘后除方向（a→b→a）在 Rat 中逐位精确往返；含 3.6 的能量换算也不例外。"""
    rng = random.Random(SEED + hash((a, b)) % 1000)
    for _ in range(N):
        x = _rand_dec(rng)
        y = U.convert_rat(Rat.of(x), a, b)
        assert U.convert_rat(y, b, a) == Rat.of(x)
        # 公开接口（十进制，经一次 prec=34 除法）往返：a→b 精确时往返逐位相等
        xb = convert(x, a, b)
        if Rat.of(xb) == y:
            assert convert(xb, b, a) == x or norm(convert(xb, b, a)) == norm(x)


def test_gj_mwh_division_not_truncated_coefficient():
    """05 §1.3 / TV-09：÷3.6 精确；截断系数 ×0.277778 不可逆。"""
    assert norm(convert("4444.4412", "GJ", "MWh")) == "1234.567"
    assert norm(convert("3600000", "GJ", "MWh")) == "1000000"
    assert norm(Decimal("3600000") * Decimal("0.277778")) == "1000000.8"
    assert norm(Decimal("4444.4412") * Decimal("0.277778")) == "1234.5679876536"
    assert U.factor("GJ", "MWh") == U.factor("MWh", "GJ") ** -1


def test_decimal_roundtrip_within_precision():
    """GJ→MWh 需一次十进制除法（34 位）；把结果按 DEC-STR 上限 18 位有效数字存储后再换回，相对误差 < 1e-17。
    把 34 位的除法结果直接再乘 3.6 需要 35 位，按 05 §1.2 报 CALC_PRECISION_OVERFLOW（乘法必须精确）。"""
    rng = random.Random(SEED)
    for _ in range(N):
        x = _rand_dec(rng) + Decimal("0.000001")
        y = convert(x, "GJ", "MWh")
        with localcontext(calc_context()) as c:
            c.prec = 18
            y18 = +y
        z = convert(y18, "MWh", "GJ")
        assert abs(z - x) <= abs(x) * Decimal("1e-17")
    with pytest.raises(CalcError) as e:
        convert(convert("1", "GJ", "MWh"), "MWh", "GJ")
    assert e.value.code == "CALC_PRECISION_OVERFLOW"


def test_incompatible_units_rejected():
    for a, b in [("t", "tCO2e"), ("tCO2", "tCO2e"), ("MWh", "t"), ("h", "MWh"), ("kgCO2e/kg", "kgCO2e/piece")]:
        with pytest.raises(CalcError) as e:
            U.factor(a, b)
        assert e.value.code == "CALC_UNIT_INCOMPATIBLE"
    with pytest.raises(CalcError):
        U.factor("parsec", "km")


# ---------------- 舍入边界 ----------------

def test_half_even_ties_against_decimal_reference():
    rng = random.Random(SEED)
    differs_from_half_up = 0
    for _ in range(N):
        digits = rng.randint(0, 6)
        base = _rand_dec(rng, max_frac=digits, signed=True)
        tie = base + Decimal(5).scaleb(-(digits + 1)) * (1 if base >= 0 else -1)   # 恰好在中点
        with localcontext(calc_context()):
            ref = tie.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN)
            up = tie.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
        assert round_str(Rat.of(tie), digits) == norm(ref)
        # 中点舍入后末位必为偶数
        assert int(abs(ref).scaleb(digits)) % 2 == 0
        differs_from_half_up += norm(ref) != norm(up)
    assert differs_from_half_up > N // 4


def test_round_half_even_specific_boundaries():
    cases = {("2.0125", 3): "2.012", ("2.0135", 3): "2.014", ("-2.0125", 3): "-2.012", ("0.0005", 3): "0",
             ("0.0015", 3): "0.002", ("2.5", 0): "2", ("3.5", 0): "4", ("-0.0004", 3): "0", ("1658.0800", 3): "1658.08",
             ("99315.288", 2): "99315.29", ("0", 3): "0", ("2027.200", 1): "2027.2"}
    for (v, d), exp in cases.items():
        assert round_str(Rat.of(v), d) == exp, (v, d)


def test_single_division_then_round():
    """三个 1/3 相加后舍入得 1；各自舍入再加得 0.999（同一计算内不做中间舍入）。"""
    a = Rat(1) / 3
    assert round_str(a + a + a, 3) == "1"
    assert norm(Decimal(round_str(a, 3)) * 3) == "0.999"


def test_inexact_multiplication_overflow():
    with pytest.raises(CalcError) as e:
        Rat.of("123456789012345678") * Rat.of("1234567890123456.78")
    assert e.value.code == "CALC_PRECISION_OVERFLOW"


def test_result_rounding_through_engine(fs_example):
    rng = random.Random(SEED)
    params = {"consolidation": "operational_control", "grid_region": "DE", "grid_factor_key": "test.tie_factor"}
    for _ in range(100):
        x = _rand_dec(rng, max_int_digits=6, max_frac=4)
        res = calculate("ghg.scope2.location_based", "ghg-scope2-location@1.0.0", fs_example,
                        [inp("electricity_purchased", norm(x), "MWh")], params, "organization", Y2025)
        with localcontext(calc_context()):
            ref = (x * Decimal("0.805")).quantize(Decimal("0.001"), rounding=ROUND_HALF_EVEN)
        assert res.value == norm(ref)


# ---------------- input_hash 对键序不敏感 ----------------

def _shuffle_dict(d, rng):
    if isinstance(d, dict):
        items = list(d.items())
        rng.shuffle(items)
        return {k: _shuffle_dict(v, rng) for k, v in items}
    if isinstance(d, list):
        return [_shuffle_dict(x, rng) for x in d]
    return d


BASE_M = {
    "method_id": "ghg.scope1.total",
    "methodology_version": "ghg-scope1@1.0.0",
    "factor_set_version": "fs:belegt-example@2026.10",
    "factor_set_rows_hash": "sha256:25cd7633e6907491a142f0c6cf90224f4403b25fb49957bacb67987bb71faaf9",
    "boundary": "organization",
    "period": {"start": "2025-01-01", "end": "2025-12-31"},
    "params": {"consolidation": "operational_control", "gwp_set": "ar6"},
    "inputs": [
        {"name": "diesel_volume", "ref": "ev_01JE0TV01DS000000000000001#v1", "value": "18000", "unit": "L"},
        {"name": "natural_gas_hi", "ref": "ev_01JE0TV01NG000000000000001#v1", "value": "2400", "unit": "MWh"},
        {"name": "refrigerant_r_410a_recharged", "ref": "ev_01JE0TV01RF000000000000001#v1", "value": "12.5", "unit": "kg"},
    ],
}
TV01_HASH = "sha256:0952b50523eb3b8eaef1a5e3f481141494fcad7a8912c19fab4bf9558fa3728c"


def test_input_hash_key_order_insensitive():
    rng = random.Random(SEED)
    assert input_hash(BASE_M) == TV01_HASH
    for _ in range(200):
        m = _shuffle_dict(BASE_M, rng)
        rng.shuffle(m["inputs"])                         # inputs 也按 name 排序
        assert input_hash(m) == TV01_HASH


def test_input_hash_normalisation_and_extra_keys():
    m = _shuffle_dict(BASE_M, random.Random(1))
    m["inputs"] = [dict(i) for i in m["inputs"]]
    m["inputs"][0]["value"] = {"18000": "18000.000", "2400": "2400.0", "12.5": "12.50"}[m["inputs"][0]["value"]]
    m["engine_version"] = "belegt-calc@9.9.9"            # 不纳入 input_hash（03 §4.5-4）
    m["customer_scope"] = ["cp_01JC2B3C4D5E6F7G8H9J0K1M2N"]
    m["output_hash"] = None
    assert input_hash(m) == TV01_HASH


def test_input_hash_sensitive_to_content():
    m = dict(BASE_M, params={"consolidation": "operational_control", "gwp_set": "ar5"})
    assert input_hash(m) != TV01_HASH
    m = dict(BASE_M, params={**BASE_M["params"], "rounding_digits": "1"})
    assert input_hash(m) != TV01_HASH


def test_jcs_utf16_key_order_and_nfc():
    # RFC 8785 §3.2.3 示例：按 UTF-16 码元排序（U+1F600 的代理对 D83D 排在 U+20AC 之后）
    obj = {"\u20ac": 1, "\r": 2, "1": 4, "\U0001f600": 5, "\u0080": 6, "\u00f6": 7}
    assert jcs(obj) == '{"\\r":2,"1":4,"\u0080":6,"\u00f6":7,"\u20ac":1,"\U0001f600":5}'
    # 03 §4.5-3 要求 NFC：U+FB33 是组合排除字符，NFC 后为 U+05D3 U+05BC，因此排在 U+00F6 与 U+20AC 之间
    assert jcs({"\u20ac": 1, "\ufb33": 3}) == '{"\u05d3\u05bc":3,"\u20ac":1}'
    # NFC：分解形式与组合形式哈希相同
    assert jcs({"a": "Mu\u0308nchen"}) == jcs({"a": "M\u00fcnchen"})
    with pytest.raises(CalcError):
        jcs({"x": 1.5})
