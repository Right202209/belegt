# Spec: 05 §8.1（CalcError）、05「本文新增标识 · 错误码」
"""belegt-calc 错误类型与错误码。

错误码沿用 05 文末「本文新增标识」；``CALC_FACTOR_SET_INTEGRITY`` 为本实现补充
（因子库 rows_hash 与行内容不一致、未冻结的草稿库），05 未定义对应标识，见 README「与文档的差异」。
"""
from __future__ import annotations

CALC_UNKNOWN_METHOD = "CALC_UNKNOWN_METHOD"
CALC_METHODOLOGY_UNSUPPORTED = "CALC_METHODOLOGY_UNSUPPORTED"
CALC_INPUT_MISSING = "CALC_INPUT_MISSING"
CALC_PARAM_REQUIRES_DECISION = "CALC_PARAM_REQUIRES_DECISION"
CALC_FACTOR_MISSING = "CALC_FACTOR_MISSING"
CALC_FACTOR_SET_DEPRECATED = "CALC_FACTOR_SET_DEPRECATED"
CALC_UNIT_INCOMPATIBLE = "CALC_UNIT_INCOMPATIBLE"
CALC_DEC_INVALID = "CALC_DEC_INVALID"
CALC_PRECISION_OVERFLOW = "CALC_PRECISION_OVERFLOW"
CALC_ALLOCATION_IMBALANCE = "CALC_ALLOCATION_IMBALANCE"
CALC_PERIOD_MISMATCH = "CALC_PERIOD_MISMATCH"
# 本实现补充（05 未定义）：
CALC_FACTOR_SET_INTEGRITY = "CALC_FACTOR_SET_INTEGRITY"

ERROR_CODES: tuple[str, ...] = (
    CALC_UNKNOWN_METHOD,
    CALC_METHODOLOGY_UNSUPPORTED,
    CALC_INPUT_MISSING,
    CALC_PARAM_REQUIRES_DECISION,
    CALC_FACTOR_MISSING,
    CALC_FACTOR_SET_DEPRECATED,
    CALC_UNIT_INCOMPATIBLE,
    CALC_DEC_INVALID,
    CALC_PRECISION_OVERFLOW,
    CALC_ALLOCATION_IMBALANCE,
    CALC_PERIOD_MISMATCH,
    CALC_FACTOR_SET_INTEGRITY,
)


class CalcError(Exception):
    """计算错误：``CalcError(code, detail)``（05 §8.1）。

    ``decision`` 可携带 ClosedQuestion 模板号（如 ``CQ-METH-05``），供 calc-service 转为 ClosedQuestion。
    """

    def __init__(self, code: str, detail: str = "", *, decision: str | None = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown CALC error code: {code}")
        self.code = code
        self.detail = detail
        self.decision = decision
        super().__init__(f"{code}: {detail}" if detail else code)

    def to_dict(self) -> dict:
        d = {"code": self.code, "detail": self.detail}
        if self.decision:
            d["decision"] = self.decision
        return d
