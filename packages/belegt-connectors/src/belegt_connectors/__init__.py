# Spec: 04 §6.1（belegt-connector/1）；FR-005、FR-101、FR-203
"""belegt-connectors：Belegt 只读 MCP 连接器（Apache-2.0）。

公开 API：
- 规范：`SPEC_VERSION`、`ToolSpec`、`ConnectorManifest`、`AuthSpec`、`ENVELOPE_SCHEMA`、`LOCATOR_SCHEMA`、
  `ConnectorError`、`query_hash`、`dec_str`、`validate_tool_name`、`mcp_safe_name`
- 基类：`BaseConnector`、`CallContext`、`Page`、`SecretResolver`
- 连接器：`FileFolderConnector`、`SqlReadOnlyConnector`、`MockErpConnector`、`MailboxConnector`
  （`LocalMailBackend`、`ImapBackend`）、`UtilityBillPdfConnector`
- 合规：`run_compliance` → `ComplianceReport`
- 证据：`record_to_evidence`
- MCP：`build_mcp_server`、`serve`（需要 mcp 包，延迟导入）
"""
from .base import BaseConnector, CallContext, Page, SecretResolver
from .compliance import ComplianceReport, Finding, run_compliance
from .connectors.erp_mock import MockErpConnector
from .connectors.files import FileFolderConnector
from .connectors.mailbox import ImapBackend, LocalMailBackend, MailboxConnector, due_date_candidates
from .connectors.sql import SqlReadOnlyConnector, is_readonly_sql
from .connectors.utility_bills import UtilityBillPdfConnector
from .evidence import record_to_evidence
from .spec import (
    ENVELOPE_SCHEMA, LOCATOR_SCHEMA, SPEC_VERSION, AuthSpec, ConnectorError, ConnectorManifest, ToolSpec, dec_str,
    mcp_safe_name, query_hash, validate_tool_name,
)

__version__ = "0.1.0"


def build_mcp_server(connector: BaseConnector, **kw):  # noqa: ANN201
    from .mcp_server import build_mcp_server as _b
    return _b(connector, **kw)


def serve(connector: BaseConnector, **kw) -> None:
    from .mcp_server import serve as _s
    _s(connector, **kw)


__all__ = [
    "SPEC_VERSION", "ToolSpec", "ConnectorManifest", "AuthSpec", "ENVELOPE_SCHEMA", "LOCATOR_SCHEMA",
    "ConnectorError", "query_hash", "dec_str", "validate_tool_name", "mcp_safe_name",
    "BaseConnector", "CallContext", "Page", "SecretResolver",
    "FileFolderConnector", "SqlReadOnlyConnector", "is_readonly_sql", "MockErpConnector", "MailboxConnector",
    "LocalMailBackend", "ImapBackend", "due_date_candidates", "UtilityBillPdfConnector",
    "run_compliance", "ComplianceReport", "Finding", "record_to_evidence", "build_mcp_server", "serve",
]
