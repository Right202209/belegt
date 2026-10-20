# Spec: 03 §6.8（错误码表）、§2.3（规则 → 错误码）
"""GraphError 与错误码表。

响应体结构：``{"error": {"code", "message", "rule_id", "path", "details"}}``。
"""
from __future__ import annotations

from typing import Any

# code -> (HTTP 状态, 默认 rule_id)
ERROR_CODES: dict[str, tuple[int, str | None]] = {
    "GRAPH_SCHEMA_INVALID": (400, "R-SCHEMA"),
    "GRAPH_UNIT_INCOMPATIBLE": (400, "R-UNIT"),
    "GRAPH_TRUST_ZONE_VIOLATION": (403, "R-ZONE"),
    "GRAPH_PROMOTION_DENIED": (403, "R-PROMO"),
    "GRAPH_ACTOR_NOT_ALLOWED": (403, "AUTHZ"),
    "GRAPH_REF_NOT_FOUND": (404, "R-REF"),
    "GRAPH_REF_CROSS_TENANT": (403, "R-REF"),
    "GRAPH_VERSION_CONFLICT": (409, "R-IMM"),
    "GRAPH_ILLEGAL_TRANSITION": (409, "R-STATE"),
    "GRAPH_DUPLICATE_EFFECTIVE_ANSWER": (409, "R-ONE"),
    "GRAPH_IDEMPOTENCY_MISMATCH": (409, None),
    "GRAPH_NUM_UNREFERENCED": (422, "R-NUM"),
    "GRAPH_NUM_VALUE_MISMATCH": (422, "R-VAL"),
    "GRAPH_NUM_IN_TEXT": (422, "GRAPH_NUM_IN_TEXT"),
    "GRAPH_PERIOD_UNCOVERED": (422, "R-PERIOD"),
    "GRAPH_HASH_MISMATCH": (422, "R-HASH"),
    "GRAPH_LOCATOR_MISSING": (422, "R-LOC"),
    "GRAPH_APPROVAL_REQUIRED": (422, "R-APR"),
    "GRAPH_SUBMIT_BLOCKED": (422, "R-SUBMIT"),
    "GRAPH_DATAPOINT_DEPRECATED": (422, "DP-DEPRECATED"),
    "GRAPH_RATE_LIMITED": (429, "GUARD"),
    "GRAPH_HALTED": (423, "GUARD"),
    # 本包新增（API 层，不在 03 §6.8 表内）
    "GRAPH_UNAUTHENTICATED": (401, "AUTH"),
    "GRAPH_NOT_FOUND": (404, None),
    "GRAPH_BATCH_TOO_LARGE": (400, None),
}


class GraphError(Exception):
    """证据图写入/读取错误。字段与 03 §6.8 响应体一致。"""

    def __init__(
        self,
        code: str,
        message: str = "",
        rule_id: str | None = None,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown graph error code {code}")
        self.code = code
        self.message = message or code
        self.rule_id = rule_id if rule_id is not None else ERROR_CODES[code][1]
        self.path = path
        self.details = details or {}
        super().__init__(f"{code}[{self.rule_id}] {self.path or ''}: {self.message}")

    @property
    def http_status(self) -> int:
        return ERROR_CODES[self.code][0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "rule_id": self.rule_id,
            "path": self.path,
            "details": self.details,
        }

    def to_response(self) -> dict[str, Any]:
        return {"error": self.to_dict()}
