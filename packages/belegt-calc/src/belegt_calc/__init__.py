# Spec: 05 §8.1（Python API）
"""belegt-calc：Belegt 确定性计算库（Apache-2.0）。

公开 API（05 §8.1）::

    calculate(method_id, methodology_version, factor_set, inputs, params, boundary, period, customer_scope="all") -> CalcResult
    input_hash(m: dict) -> str
    output_hash(result: dict, breakdown: list[dict]) -> str
    replay(calculation: dict, factor_set: FactorSet) -> ReplayReport
    list_methods() -> list[MethodSpec]
    CalcError(code, detail)

运行期只依赖标准库与 PyYAML（读因子文件）。不读时钟、网络、随机数或环境变量（05 §1.1）。
"""
from __future__ import annotations

from ._version import ENGINE_VERSION, __version__
from .engine import calculate, replay
from .errors import ERROR_CODES, CalcError
from .factors import load_factor_set
from .hashing import input_hash, output_hash, rows_hash
from .registry import FAMILIES, list_methods
from .types import CalcResult, FactorRow, FactorSet, InputValue, MethodSpec, Period, ReplayReport
from .units import convert

__all__ = [
    "CalcError",
    "CalcResult",
    "ENGINE_VERSION",
    "ERROR_CODES",
    "FAMILIES",
    "FactorRow",
    "FactorSet",
    "InputValue",
    "MethodSpec",
    "Period",
    "ReplayReport",
    "__version__",
    "calculate",
    "convert",
    "input_hash",
    "list_methods",
    "load_factor_set",
    "output_hash",
    "replay",
    "rows_hash",
]
