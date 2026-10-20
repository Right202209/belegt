# Spec: 04 §3（问卷级状态机执行器）、§4（Temporal 编排）
"""工作流：``LocalRunner``（同步/asyncio，单进程持久化）与 Temporal（``workflow.temporal``，按需导入）。"""
from __future__ import annotations

from .activities import BATCH_SIZE, NON_RETRYABLE, Activities
from .journal import Journal
from .local import CrashSimulated, LocalRunner, RetryPolicy, RunResult, Suspend, WorkflowFailed
from .logic import question_batch, request_workflow
from .states import TRANSITION_IDS, WRITER, status_reason, transition_id, writer_for

__all__ = ["LocalRunner", "RunResult", "RetryPolicy", "Suspend", "WorkflowFailed", "CrashSimulated", "Journal", "Activities",
           "BATCH_SIZE", "NON_RETRYABLE", "request_workflow", "question_batch", "TRANSITION_IDS", "WRITER", "transition_id",
           "writer_for", "status_reason"]
