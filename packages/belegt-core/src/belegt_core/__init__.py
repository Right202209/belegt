# Spec: 03（证据图）、04 §3（Request 状态机）、07 §2/§4/§7（信任分区、权限、审计）
"""belegt_core —— Belegt 核心证据图层（AGPL-3.0-only）。

公开 API（后续子任务只应依赖这里导出的名字）：

- 模型：``belegt_core.models``（03 全部对象）、``SCHEMA_VERSION``、``GRAPH_TYPES``
- 标识：``new_id``、``new_trace_id``、``ObjectRef``、``ref``、``type_of_id``
- 规范化/哈希：``canonicalize``（JCS）、``content_hash``、``hash_obj``、``normalize_dec``、``input_hash``、``answer_key``
- 错误：``GraphError``、``ERROR_CODES``
- 权限：``Actor``
- 状态机：``STATE_MACHINES``、``check_transition``
- 存储：``GraphStore``、``WriteItem``、``HeadInfo``
- 审计：``verify_events``、``compute_event_hash``；审计包：``export_audit_pack``、``verify_audit_pack``
- API：``create_app``（FastAPI，延迟导入）
"""
from __future__ import annotations

from typing import Any

from .authz import Actor
from .canonical import canonicalize, content_hash, dec_str, hash_obj, normalize_dec
from .datapoints import DataPointInfo, DataPointRegistry
from .errors import ERROR_CODES, GraphError
from .hashing import answer_key, conflict_key, input_hash, output_hash
from .ids import ORG_GLOBAL, ObjectRef, new_id, new_trace_id, ref, type_of_id
from .models import GRAPH_TYPES, SCHEMA_VERSION
from .statemachines import STATE_MACHINES, check_transition
from .store import GraphStore, HeadInfo, WriteItem
from .audit import compute_event_hash, verify_events
from .units import default_units

__version__ = "0.1.0"


def __getattr__(name: str) -> Any:  # 延迟导入，避免 import belegt_core 就拉起 FastAPI
    if name in ("create_app", "app"):
        from . import api

        return getattr(api, name)
    if name in ("export_audit_pack", "verify_audit_pack"):
        from . import auditpack

        return getattr(auditpack, name)
    raise AttributeError(name)


__all__ = [
    "__version__", "Actor", "GraphError", "ERROR_CODES", "GraphStore", "WriteItem", "HeadInfo", "ObjectRef", "ref",
    "new_id", "new_trace_id", "type_of_id", "ORG_GLOBAL", "SCHEMA_VERSION", "GRAPH_TYPES", "canonicalize",
    "content_hash", "hash_obj", "normalize_dec", "dec_str", "input_hash", "output_hash", "answer_key", "conflict_key",
    "STATE_MACHINES", "check_transition", "DataPointRegistry", "DataPointInfo", "default_units",
    "compute_event_hash", "verify_events", "create_app", "export_audit_pack", "verify_audit_pack",
]

# ---- 06 子任务：一致性审计 / 审批路由 / 法律承诺 / 有效期（延迟导出；子包也可直接导入）
_REVIEW_EXPORTS = {
    "ConsistencyEngine": "consistency", "AuditContext": "consistency", "resolve_conflict": "consistency",
    "ApprovalRouter": "approval", "BatchStager": "approval", "load_policy": "approval",
    "detect_legal_clauses": "legal", "scan_expiry": "expiry", "apply_expiry": "expiry",
}
_core_getattr = __getattr__


def __getattr__(name: str) -> Any:  # noqa: F811
    mod = _REVIEW_EXPORTS.get(name)
    if mod is not None:
        import importlib

        return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
    return _core_getattr(name)


__all__ += list(_REVIEW_EXPORTS)

# ---- 集成子任务：工作流（Local/Temporal）、HTTP 组装、MCP、A2A（延迟导出；temporalio / mcp 只在访问时导入）
_INTEGRATION_EXPORTS = {
    "LocalRunner": "workflow.local", "RunResult": "workflow.local",
    "RequestWorkflow": "workflow.temporal", "QuestionBatchWorkflow": "workflow.temporal", "TemporalEngine": "workflow.temporal",
    "WorkflowOptions": "workflow.temporal", "create_worker": "workflow.temporal_worker", "build_activities": "workflow.temporal_worker",
    "create_full_app": "app_factory", "Settings": "app_factory",
    "workflow_router": "api.routes_workflow", "configure_workflow": "api.routes_workflow",
    "create_mcp_server": "mcp.server", "ApprovedReadService": "mcp.service", "MCPConnector": "mcp.client",
    "EvidenceCandidate": "mcp.client", "envelope_to_candidates": "mcp.client",
    "a2a_router": "a2a.routes", "configure_a2a": "a2a.routes", "SubscriptionService": "a2a.subscriptions",
    "verify_signature": "a2a.subscriptions",
}
_review_getattr = __getattr__


def __getattr__(name: str) -> Any:  # noqa: F811
    mod = _INTEGRATION_EXPORTS.get(name)
    if mod is not None:
        import importlib

        return getattr(importlib.import_module(f"{__name__}.{mod}"), name)
    return _review_getattr(name)


__all__ += list(_INTEGRATION_EXPORTS)
