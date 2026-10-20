# Spec: 04 §3.1（状态图）、§3.2（转移表 T01…T23 与执行组件）、03 §3.2（写入合法性摘要：各转移的 created_by）
"""Request 状态转移 → 写入执行者（只依赖标准库，Temporal 工作流沙箱内也可导入）。"""
from __future__ import annotations

TRANSITION_IDS: dict[tuple[str, str], str] = {
    ("received", "triaged"): "T02", ("received", "cancelled"): "T03", ("triaged", "parsed"): "T04",
    ("parsed", "mapped"): "T05", ("mapped", "evidence_gathering"): "T06", ("evidence_gathering", "awaiting_supplier"): "T07",
    ("awaiting_supplier", "evidence_gathering"): "T08", ("evidence_gathering", "drafting"): "T09",
    ("drafting", "auditing"): "T10", ("auditing", "in_review"): "T11", ("in_review", "auditing"): "T12",
    ("in_review", "drafting"): "T13", ("in_review", "approved"): "T14", ("approved", "filling"): "T15",
    ("filling", "submitted"): "T16", ("submitted", "archived"): "T17",
}
WRITER: dict[str, str] = {
    "T04": "service:workflow", "T05": "agent:guard", "T06": "service:workflow", "T07": "agent:upstream-chaser",
    "T08": "service:workflow", "T09": "service:workflow", "T10": "agent:answer-drafter", "T11": "agent:approval-router",
    "T12": "agent:approval-router", "T13": "agent:approval-router", "T14": "agent:approval-router", "T15": "agent:filler",
    "T16": "agent:filler", "T17": "service:workflow", "T18": "service:workflow", "T19": "service:workflow", "T20": "agent:guard",
}
SIDE = ("awaiting_human", "halted")
TERMINAL = ("archived", "cancelled")


def transition_id(src: str | None, dst: str) -> str | None:
    if dst == "awaiting_human":
        return "T18"
    if src == "awaiting_human":
        return "T19"
    if dst == "halted":
        return "T20"
    if src == "halted":
        return "T21"
    if dst == "cancelled":
        return "T23" if src == "in_review" else "T22" if src != "received" else "T03"
    return TRANSITION_IDS.get((src or "", dst))


def writer_for(src: str | None, dst: str) -> str | None:
    t = transition_id(src, dst)
    return WRITER.get(t or "")


def status_reason(kind: str, reason: str, return_to: str) -> str:
    return f"{kind}:{reason};return_to={return_to}"
