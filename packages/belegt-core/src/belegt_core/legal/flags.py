# Spec: 06 §6.2（标记写入 Question 候选的分类标签，经 guard 晋升后传给 answer-drafter 与 approval-router）、§6.3
"""Question 上的法律承诺标记约定（``tags``）。

- ``legal:LC-xx``：命中的条款类型（可多个）；``legal_commitment``：通道融合后的总标记（无类型时）。
- ``legal_cancelled``：``compliance_owner`` 在审批界面取消标记后写入（理由记审计，06 §6.2）。
"""
from __future__ import annotations

from typing import Any

LEGAL_TAG_PREFIX = "legal:"
LEGAL_TAG = "legal_commitment"
LEGAL_CANCELLED_TAG = "legal_cancelled"


def legal_types(question: dict[str, Any] | None) -> list[str]:
    tags = (question or {}).get("tags") or []
    return sorted({t[len(LEGAL_TAG_PREFIX):] for t in tags if t.startswith(LEGAL_TAG_PREFIX)})


def question_legal_flag(question: dict[str, Any] | None) -> bool:
    tags = set((question or {}).get("tags") or [])
    if LEGAL_CANCELLED_TAG in tags:
        return False
    return LEGAL_TAG in tags or any(t.startswith(LEGAL_TAG_PREFIX) for t in tags)


def legal_tags(types: list[str]) -> list[str]:
    return [LEGAL_TAG] + [f"{LEGAL_TAG_PREFIX}{t}" for t in sorted(set(types))]


__all__ = ["legal_types", "question_legal_flag", "legal_tags", "LEGAL_TAG", "LEGAL_TAG_PREFIX", "LEGAL_CANCELLED_TAG"]
