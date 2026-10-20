# Spec: 04 §2.2–§2.13（全部 Agent 契约）、§2.0（统一约定）
"""Agent 实现。每个 Agent 是 ``AgentBase`` 子类，``run(...) -> AgentOutput``（只含 ObjectRef 与 outcome）。"""
from __future__ import annotations

from .answer_drafter import AnswerDrafter
from .approval_router import ApprovalRouterAgent, FallbackApprovalRouter
from .base import AgentBase, AgentOutput, OrgConfig, Prompt, PromptLibrary, RunContext, Services, build_registry, det_id, latest_ref
from .calc_service import CalcService, ensure_factor_set
from .connectors import DocumentIndex, LocalERPConnector
from .consistency_auditor import ConsistencyAuditorAgent
from .evidence_miner import EvidenceMiner
from .facts import CALC_RECIPES, FACTS
from .filler import FillerFile, FillerPortal, PortalDriver, SandboxPortalStub
from .hints import build_hints, load_hints, put_hints
from .intake_triage import IntakeInput, IntakeTriage
from .question_parser import QuestionParser
from .reg_radar import RegRadar
from .sales_assistant import SalesAssistant
from .supplier_reply_reviewer import SupplierReplyReviewer
from .upstream_chaser import UpstreamChaser

AGENT_CLASSES = {
    "intake-triage": IntakeTriage, "question-parser": QuestionParser, "evidence-miner": EvidenceMiner,
    "calc-service": CalcService, "consistency-auditor": ConsistencyAuditorAgent, "answer-drafter": AnswerDrafter,
    "approval-router": ApprovalRouterAgent, "upstream-chaser": UpstreamChaser, "supplier-reply-reviewer": SupplierReplyReviewer,
    "filler": FillerFile, "reg-radar": RegRadar, "sales-assistant": SalesAssistant,
}

__all__ = [
    "AgentBase", "AgentOutput", "RunContext", "Services", "OrgConfig", "Prompt", "PromptLibrary", "build_registry", "det_id",
    "latest_ref", "IntakeTriage", "IntakeInput", "QuestionParser", "EvidenceMiner", "CalcService", "ensure_factor_set",
    "AnswerDrafter", "ApprovalRouterAgent", "FallbackApprovalRouter", "ConsistencyAuditorAgent", "UpstreamChaser",
    "SupplierReplyReviewer", "FillerFile", "FillerPortal", "PortalDriver", "SandboxPortalStub", "RegRadar", "SalesAssistant",
    "LocalERPConnector", "DocumentIndex", "build_hints", "put_hints", "load_hints", "FACTS", "CALC_RECIPES", "AGENT_CLASSES",
]
