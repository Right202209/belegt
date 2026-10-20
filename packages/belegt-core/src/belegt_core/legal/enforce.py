# Spec: 06 §6.2（标记写入 Question 分类标签；取消只能由 compliance_owner 在审批界面执行并记录理由，样本进评测集）、
#       §6.3（强制处理：legal_commitment=true、risk_level=L3、不可 L0 复用；answer-drafter 只给"建议立场 + 风险摘要"）
from __future__ import annotations

from typing import Any

from ..authz import Actor
from ..errors import GraphError
from .detector import LegalDetection, notice_text
from .flags import LEGAL_CANCELLED_TAG, LEGAL_TAG, LEGAL_TAG_PREFIX, legal_tags, legal_types, question_legal_flag

MIN_CANCEL_REASON = 10


def flag_question(question: dict[str, Any], detection: LegalDetection) -> dict[str, Any]:
    """返回带法律标记的 Question 体（question-parser 写 quarantine 候选；guard 晋升时原样保留 tags）。"""
    if not detection.flagged:
        return question
    tags = [t for t in (question.get("tags") or []) if t != LEGAL_TAG and not t.startswith(LEGAL_TAG_PREFIX)]
    return {**question, "tags": (tags + legal_tags(detection.types))[:20]}


def enforce_on_answer(answer: dict[str, Any], questions: list[dict[str, Any]]) -> dict[str, Any]:
    """06 §6.3：被标记题目对应的 Answer 一律 legal_commitment=true；定级 L3、不可复用（由 approval-router 执行）。

    返回 ``{"patch": {...}, "risk_level": "L3"|None, "drafting": {...}, "notice": I18nText|None}``。
    """
    flagged = [q for q in questions if question_legal_flag(q)]
    if not flagged:
        return {"patch": {}, "risk_level": None, "drafting": None, "notice": None}
    types = sorted({t for q in flagged for t in legal_types(q)})
    patch: dict[str, Any] = {}
    if not answer.get("legal_commitment"):
        patch["legal_commitment"] = True
    return {
        "patch": patch,
        "risk_level": "L3",
        "no_reuse": True,
        "drafting": {"mode": "position_and_risk_summary", "default_value": None,
                     "rule": "answer-drafter must not propose a default 'yes/ja' for legal clauses (06 §6.3)"},
        "notice": notice_text(types),
        "legal_types": types,
    }


def cancel_legal_flag(store: Any, question_id: str, *, actor: Actor, reason: str) -> dict[str, Any]:
    """取消法律标记（仅 compliance_owner，理由必填），写 Question 新版本 + 审计；返回进入评测集的样本记录。"""
    if not actor.is_user or not actor.has_role("compliance_owner"):
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "only compliance_owner may cancel a legal flag (06 §6.2)",
                         details={"required_role": ["compliance_owner"]})
    if not reason or len(reason.strip()) < MIN_CANCEL_REASON:
        raise GraphError("GRAPH_SCHEMA_INVALID", f"a reason of at least {MIN_CANCEL_REASON} characters is required",
                         path="/reason")
    q = store.get(question_id, actor=actor)
    if q is None:
        raise GraphError("GRAPH_NOT_FOUND", f"{question_id} not found")
    types = legal_types(q)
    if not question_legal_flag(q):
        raise GraphError("GRAPH_ILLEGAL_TRANSITION", "question has no active legal flag", path="/tags")
    tags = [t for t in (q.get("tags") or []) if t != LEGAL_TAG and not t.startswith(LEGAL_TAG_PREFIX)]
    tags.append(LEGAL_CANCELLED_TAG)
    ref = store.write("Question", {"id": q["id"], "tags": tags[:20]}, expected_version=q["version"], actor=actor,
                      patch_mode="merge_from_head")
    sample = {"kind": "legal_flag_cancelled", "question_ref": ref, "previous_ref": f"{q['id']}#v{q['version']}",
              "types": types, "reason": reason.strip(), "cancelled_by": actor.id, "eval_set": "FR-406"}
    store.record_audit(actor, "authz.change", object_ref=ref,
                       details={"event": "legal.flag.cancel", "types": types, "reason": reason.strip()})
    return sample


__all__ = ["flag_question", "enforce_on_answer", "cancel_legal_flag"]
