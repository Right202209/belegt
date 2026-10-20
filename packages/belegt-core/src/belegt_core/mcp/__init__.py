# Spec: 04 §6.1（MCP client 连接器）、§6.2（Belegt 作为 MCP server，只读已批准数据）
"""MCP 集成（延迟导入；``service`` 不依赖 ``mcp`` 包）。

- ``ApprovedReadService``：只读已批准答案/字典的授权与视图（MCP server 与 A2A 共用）。
- ``create_mcp_server(store, actor)``：Belegt MCP server（工具 list_approved_answers、get_answer_with_provenance、get_datapoint、
  search_dictionary）。
- ``MCPConnector``：调用外部只读连接器 MCP server，envelope → ``EvidenceCandidate``。
"""
from __future__ import annotations

from typing import Any

_EXPORTS = {
    "ApprovedReadService": "service",
    "create_mcp_server": "server", "token_actor_resolver": "server", "serve": "server", "TOOL_NAMES": "server",
    "MCPConnector": "client", "EvidenceCandidate": "client", "envelope_to_candidates": "client", "check_tool": "client",
    "ConnectorPolicyError": "client", "ConnectorCallError": "client",
}


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(name)
    import importlib

    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)


__all__ = list(_EXPORTS)
