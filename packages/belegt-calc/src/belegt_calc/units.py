# Spec: 05 §1.3（单位系统）；02 §3.1–3.2（规范单位与无参数线性换算）
"""单位系统。

- 只允许**无外部参数的线性换算**（02 §3.2）；热值、Hs/Hi、密度、汇率一律是因子行。
- 换算系数以有理数表示：``GJ→MWh`` 为 ``÷3.6``（``Fraction(10, 36)``），绝不用截断系数 ``0.277778``（TV-09）。
- 复合单位写作 ``分子/分母``（如 ``tCO2e/MWh``、``kgCO2e/kg``、``EUR/tCO2e``）。
- ``tCO2`` 与 ``tCO2e`` 量纲不同、不混用（05 §1.3）。
"""
from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from .dec import Rat, parse_dec
from .errors import CALC_UNIT_INCOMPATIBLE, CalcError

# 原子单位：符号 → (量纲, 相对该量纲基准单位的有理数比例)
# 基准单位：mass=kg, co2e=kgCO2e, co2=kgCO2, carbon=kgC, energy=MWh, volume=L, time=h,
#           currency=EUR, count=piece, distance=km, 无量纲=1
_ATOMS: dict[str, tuple[str, Fraction]] = {
    # 质量
    "t": ("mass", Fraction(1000)),
    "kg": ("mass", Fraction(1)),
    "g": ("mass", Fraction(1, 1000)),
    # 排放（CO2 当量）
    "tCO2e": ("co2e", Fraction(1000)),
    "kgCO2e": ("co2e", Fraction(1)),
    "gCO2e": ("co2e", Fraction(1, 1000)),
    # 仅 CO2（不得与 CO2e 混用）
    "tCO2": ("co2", Fraction(1000)),
    "kgCO2": ("co2", Fraction(1)),
    "gCO2": ("co2", Fraction(1, 1000)),
    # 碳质量（质量平衡法、生物源碳含量）
    "tC": ("carbon", Fraction(1000)),
    "kgC": ("carbon", Fraction(1)),
    # 能量
    "GWh": ("energy", Fraction(1000)),
    "MWh": ("energy", Fraction(1)),
    "kWh": ("energy", Fraction(1, 1000)),
    "Wh": ("energy", Fraction(1, 1000000)),
    "TJ": ("energy", Fraction(10000, 36)),         # 1 TJ = 1000 GJ = 1000/3.6 MWh
    "GJ": ("energy", Fraction(10, 36)),            # 1 GJ = 1/3.6 MWh（精确）
    "MJ": ("energy", Fraction(10, 36000)),
    # 体积
    "m3": ("volume", Fraction(1000)),
    "L": ("volume", Fraction(1)),
    # 时间
    "h": ("time", Fraction(1)),
    # 货币
    "EUR": ("currency", Fraction(1)),
    # 计数
    "piece": ("count", Fraction(1)),
    # 距离
    "km": ("distance", Fraction(1)),
    # 无量纲
    "1": ("", Fraction(1)),
    "%": ("", Fraction(1, 100)),
}
CANONICAL: dict[str, str] = {
    "mass": "t",
    "co2e": "tCO2e",
    "co2": "tCO2",
    "carbon": "tC",
    "energy": "MWh",
    "volume": "L",
    "time": "h",
    "currency": "EUR",
    "count": "piece",
    "distance": "km",
    "": "1",
}


def _parse(unit: str) -> tuple[tuple[tuple[str, int], ...], Fraction]:
    """返回 (量纲签名, 比例)。量纲签名为按名排序的 (量纲, 指数) 元组。"""
    if not isinstance(unit, str) or not unit:
        raise CalcError(CALC_UNIT_INCOMPATIBLE, f"invalid unit {unit!r}")
    parts = unit.split("/")
    if len(parts) > 2:
        raise CalcError(CALC_UNIT_INCOMPATIBLE, f"unsupported compound unit {unit!r}")
    dims: dict[str, int] = {}
    scale = Fraction(1)
    for i, p in enumerate(parts):
        if p not in _ATOMS:
            raise CalcError(CALC_UNIT_INCOMPATIBLE, f"unknown unit {p!r} in {unit!r}")
        dim, s = _ATOMS[p]
        sign = 1 if i == 0 else -1
        if dim:
            dims[dim] = dims.get(dim, 0) + sign
            if dims[dim] == 0:
                del dims[dim]
        scale = scale * s if sign == 1 else scale / s
    return tuple(sorted(dims.items())), scale


def is_known(unit: str) -> bool:
    try:
        _parse(unit)
        return True
    except CalcError:
        return False


def dimension(unit: str) -> tuple[tuple[str, int], ...]:
    return _parse(unit)[0]


def compatible(a: str, b: str) -> bool:
    try:
        return _parse(a)[0] == _parse(b)[0]
    except CalcError:
        return False


def factor(from_unit: str, to_unit: str) -> Fraction:
    """``x[from] × factor = x[to]`` 的精确有理系数；量纲不兼容报 ``CALC_UNIT_INCOMPATIBLE``。"""
    da, sa = _parse(from_unit)
    db, sb = _parse(to_unit)
    if da != db:
        raise CalcError(CALC_UNIT_INCOMPATIBLE, f"cannot convert {from_unit!r} to {to_unit!r}")
    return sa / sb


def convert_rat(value: Rat, from_unit: str, to_unit: str) -> Rat:
    """精确换算（返回有理数，不截断）。"""
    if from_unit == to_unit:
        return value
    return value * Rat(factor(from_unit, to_unit))


def convert(value: Decimal | str, from_unit: str, to_unit: str) -> Decimal:
    """公开换算接口：``convert(Decimal("4444.4412"), "GJ", "MWh") == Decimal("1234.567")``。

    乘法精确；若系数含非 2/5 因子（如 ÷3.6）则执行一次 prec=34 的十进制除法。
    """
    v = Rat.of(value if isinstance(value, Decimal) else parse_dec(value, max_sig=None))
    return convert_rat(v, from_unit, to_unit).to_decimal()


def canonical_unit(unit: str) -> str:
    """单一量纲单位的规范单位（02 §3.1）；复合单位原样返回其规范写法。"""
    dims, _ = _parse(unit)
    if not dims:
        return "1"
    if len(dims) == 1 and dims[0][1] == 1:
        return CANONICAL[dims[0][0]]
    num = [CANONICAL[d] for d, e in dims if e == 1]
    den = [CANONICAL[d] for d, e in dims if e == -1]
    if len(num) == 1 and len(den) == 1 and all(abs(e) == 1 for _, e in dims):
        return f"{num[0]}/{den[0]}"
    raise CalcError(CALC_UNIT_INCOMPATIBLE, f"no canonical form for {unit!r}")


def units() -> list[str]:
    return sorted(_ATOMS)
