# Spec: 04 §2.15（egress-gateway，全系统唯一一套 EGRESS_DENIED_* 错误码）
"""出口网关：唯一出站通道（邮件、供应商端链接、门户 HTTP、A2A 响应、对外分享链接）。"""
from __future__ import annotations

from .gateway import (
    CALLER_HANDLING, EGRESS_CODES, EGRESS_DENIED_APPROVAL_MISSING, EGRESS_DENIED_DLP, EGRESS_DENIED_HALTED,
    EGRESS_DENIED_QUIET_PERIOD, EGRESS_DENIED_RATE_LIMITED, EGRESS_DENIED_RECIPIENT_NOT_ALLOWED,
    EGRESS_DENIED_TEMPLATE_UNREGISTERED, EGRESS_DENIED_TEMPLATE_UNREVIEWED, BusinessCalendar, CoreEgressGateway,
    EgressDenied, EgressMessage, EgressReceipt, Ledger, MemoryTransport, PayloadItem, StaticTemplateRegistry,
    recipient_hash,
)


def supplier_templates():
    """``belegt_supplier.TemplateRegistry.load_builtin()``（已安装时），否则 None。"""
    try:
        from belegt_supplier import TemplateRegistry  # type: ignore

        return TemplateRegistry.load_builtin()
    except Exception:
        return None


__all__ = [
    "CoreEgressGateway", "EgressDenied", "EgressMessage", "EgressReceipt", "PayloadItem", "Ledger", "MemoryTransport",
    "BusinessCalendar", "StaticTemplateRegistry", "EGRESS_CODES", "CALLER_HANDLING", "recipient_hash",
    "EGRESS_DENIED_TEMPLATE_UNREGISTERED", "EGRESS_DENIED_TEMPLATE_UNREVIEWED", "EGRESS_DENIED_RECIPIENT_NOT_ALLOWED",
    "EGRESS_DENIED_RATE_LIMITED", "EGRESS_DENIED_QUIET_PERIOD", "EGRESS_DENIED_APPROVAL_MISSING", "EGRESS_DENIED_DLP",
    "EGRESS_DENIED_HALTED", "supplier_templates",
]
