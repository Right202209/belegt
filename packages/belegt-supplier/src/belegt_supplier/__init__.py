# Spec: 08（上游追数与供应商端）
"""belegt-supplier：可自托管、多语言的供应商端（Apache-2.0）。

公开 API（稳定）：
- 模板：`TemplateRegistry`、`RenderedMessage`、`TemplateError`
- 催办：`build_schedule`、`next_action`、`compute_due_date`、`ChaseState`、`BusinessCalendar`
- 合理性校验：`run_plausibility`、`PlausValue`、`PlausContext`、`PlausReport`、`scan_file`、`scan_text_fields`
- 状态机：`transition`、`TRANSITIONS`、`TransitionError`
- 出口：`EgressGateway`、`DefaultEgressGateway`、`EgressDenied`、`OutboundMessage`、`OutboxTransport`、`SmtpTransport`
- 与核心交互：`CoreClient`、`HttpCoreClient`、`InMemoryCoreClient`；签名 `webhook_signature`、`verify_webhook`、`jws_sign`、`jws_verify`
- 应用：`SupplierService`、`create_app`、`Settings`
"""
__version__ = "0.1.0"

from .chase import POLICY_ID, ChaseAction, ChaseSchedule, ChaseState, build_schedule, compute_due_date, next_action  # noqa: E402
from .config import Settings  # noqa: E402
from .core_client import CoreClient, HttpCoreClient, InMemoryCoreClient  # noqa: E402
from .egress import (  # noqa: E402
    ALL_CODES, DefaultEgressGateway, EgressDenied, EgressGateway, MemoryTransport, OutboundMessage, OutboxTransport,
    SmtpTransport,
)
from .messages import RenderedMessage, TemplateError, TemplateRegistry  # noqa: E402
from .models import RequestManifest, SubmissionPackage, SupplierRequest  # noqa: E402
from .plausibility import (  # noqa: E402
    ALL_RULE_IDS, PlausContext, PlausReport, PlausValue, run_plausibility, scan_file, scan_text_fields,
)
from .signing import jws_sign, jws_verify, verify_webhook, webhook_signature  # noqa: E402
from .statemachine import TRANSITIONS, TransitionError, transition  # noqa: E402
from .workcal import BusinessCalendar  # noqa: E402


def create_app(*args, **kwargs):  # 延迟导入，避免纯库使用者加载 FastAPI
    from .web.app import create_app as _create

    return _create(*args, **kwargs)


def __getattr__(name: str):
    if name == "SupplierService":
        from .service import SupplierService

        return SupplierService
    raise AttributeError(name)


__all__ = [
    "ALL_CODES", "ALL_RULE_IDS", "BusinessCalendar", "ChaseAction", "ChaseSchedule", "ChaseState", "CoreClient",
    "DefaultEgressGateway", "EgressDenied", "EgressGateway", "HttpCoreClient", "InMemoryCoreClient", "MemoryTransport",
    "OutboundMessage", "OutboxTransport", "POLICY_ID", "PlausContext", "PlausReport", "PlausValue", "RenderedMessage",
    "RequestManifest", "Settings", "SmtpTransport", "SubmissionPackage", "SupplierRequest", "SupplierService",
    "TRANSITIONS", "TemplateError", "TemplateRegistry", "TransitionError", "build_schedule", "compute_due_date",
    "create_app", "jws_sign", "jws_verify", "next_action", "run_plausibility", "scan_file", "scan_text_fields",
    "transition", "verify_webhook", "webhook_signature",
]
