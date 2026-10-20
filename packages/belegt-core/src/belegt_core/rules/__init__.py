# Spec: 03 §2.3（写入校验规则总表）、§1.5（SSOT-1…7）、§6.8（校验流程与错误码）
"""03 §2.3 写入校验规则的实现。

执行顺序（03 §6.8）：
  预检（R-NUM、R-LOC、R-EST、R-FS1 形状）→ R-SCHEMA → 守卫中止 → R-ZONE → R-PROMO → R-REF → R-STATE → 类型规则
  （R-VAL、R-UNIT、R-PERIOD、R-ONE、R-HASH、R-FS1、R-APR、R-SUBMIT、GRAPH_NUM_IN_TEXT…）。
R-IMM（expected_version 乐观锁）与幂等在 GraphStore 内执行；SSOT 规则的落点见 ``SSOT_RULES``。
"""
from __future__ import annotations

from typing import Any, Callable

from ..errors import GraphError
from ..schemas import validate as schema_validate
from ..models import SCHEMA_FILES
from .answer import ANSWER_RULES
from .base import rule_halted, rule_promo, rule_ref, rule_state, rule_zone
from .context import GraphReader, RuleContext
from .objects import TYPE_RULES
from .precheck import run_pre_schema

RULE_IDS = (
    "R-SCHEMA", "R-IMM", "R-ZONE", "R-PROMO", "R-REF", "R-STATE", "R-NUM", "R-VAL", "R-UNIT", "R-PERIOD", "R-ONE",
    "R-HASH", "R-FS1", "R-APR", "R-EST", "R-LOC", "R-SUBMIT", "GRAPH_NUM_IN_TEXT",
)

# SSOT 规则在本包中的落点（供 README/审计说明）
SSOT_RULES = {
    "SSOT-1": "工作流/检查点只保存 ObjectRef：GraphStore.write 返回 ObjectRef；本包不提供按值缓存。",
    "SSOT-2": "所有写入经 GraphStore.write/write_batch（API: POST /graph/objects[:batch]）；PostgreSQL 角色仅可 EXECUTE graph.commit_version()。",
    "SSOT-3": "边由字段派生（rules.refspec），同事务物化到 graph_edge；GraphStore.verify_edges() 做夜间一致性校验。",
    "SSOT-4": "数字只来自 Evidence.extracted_value / Calculation.result：R-NUM + R-VAL + GRAPH_NUM_IN_TEXT。",
    "SSOT-5": "answer_key 唯一生效：R-ONE + graph_effective_answer 表。",
    "SSOT-6": "导出物记录 input_hash：auditpack manifest.pack_hash；Answer.submission.export_input_hash。",
    "SSOT-7": "索引/视图可由图重建：GraphStore.rebuild_derived()（边、head、effective_answer）。",
}


def check_schema(type_name: str, body: dict[str, Any]) -> None:
    """R-SCHEMA：按 03 §5 JSON Schema（stored 模式）校验。"""
    errs = schema_validate(SCHEMA_FILES[type_name], body)
    if errs:
        raise GraphError("GRAPH_SCHEMA_INVALID", errs[0]["message"], path=errs[0]["path"],
                         details={"errors": errs[:10]})


POST_SCHEMA_GENERIC: tuple[Callable[[RuleContext], None], ...] = (rule_halted, rule_zone, rule_promo, rule_ref, rule_state)


def run_rules(ctx: RuleContext) -> None:
    """R-SCHEMA 之后的全部规则。"""
    for r in POST_SCHEMA_GENERIC:
        r(ctx)
    for r in TYPE_RULES.get(ctx.type_name, ()):
        r(ctx)
    if ctx.type_name == "Answer":
        for r in ANSWER_RULES:
            r(ctx)


__all__ = ["RULE_IDS", "SSOT_RULES", "RuleContext", "GraphReader", "run_pre_schema", "check_schema", "run_rules"]
