# Spec: 04 §2.14、§5（策略检查点 GP-01…GP-11、预算熔断、循环检测、Trace）、07 §2.3、§3
"""守卫（确定性横切）：以 ``GraphStore(policy_hook=guard.policy_hook)`` 接入证据图写入。"""
from __future__ import annotations

from .budget import AGENT_BUDGETS, CALL_LIMITS, REQUEST_BUDGET, Budget, BudgetExceeded, BudgetState
from .injection import InjectionReport, contains_canary, instruction_hits, make_canary, scan, spotlight, SPOTLIGHT_SYSTEM_NOTE
from .loops import LoopDetected, LoopDetector
from .policy import POLICY_VERSION, TOOL_WHITELIST, Guard, GuardConfig, GuardedLLM, Halted, allowed_numbers_deep
from .promotion import (
    DEFAULT_THRESHOLDS, PromotionResult, new_cq, promote_mappings, promote_questions, promote_request,
    promote_request_after_confirm, promote_supplier_evidence, readback_questionnaire,
)
from .trace import Span, TraceRecorder

__all__ = [
    "Guard", "GuardConfig", "GuardedLLM", "Halted", "POLICY_VERSION", "TOOL_WHITELIST", "allowed_numbers_deep",
    "Budget", "BudgetState", "BudgetExceeded", "REQUEST_BUDGET", "AGENT_BUDGETS", "CALL_LIMITS",
    "LoopDetector", "LoopDetected", "TraceRecorder", "Span", "InjectionReport", "scan", "spotlight", "make_canary",
    "contains_canary", "instruction_hits", "SPOTLIGHT_SYSTEM_NOTE", "PromotionResult", "DEFAULT_THRESHOLDS",
    "promote_request", "promote_request_after_confirm", "promote_questions", "promote_mappings",
    "promote_supplier_evidence", "new_cq", "readback_questionnaire",
]
