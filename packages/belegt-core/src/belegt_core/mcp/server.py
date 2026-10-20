# Spec: 04 §6.2（Belegt 作为 MCP server：只读已批准数据；全部工具 readOnlyHint=true；认证 OAuth 2.1，令牌绑定用户角色 07 §4.4/§4.6）
"""Belegt MCP server（``mcp`` 包，2.x ``MCPServer`` / 1.x ``FastMCP`` 均可）。

工具（全部 ``readOnlyHint=true``、``destructiveHint=false``、``idempotentHint=true``）：

| 工具 | 输入 | 输出 |
|---|---|---|
| ``list_approved_answers`` | ``{datapoint_ids?[], counterparty_id?, period?{start,end}, limit?}`` | ``{items:[{answer_ref, datapoint_id, value, unit, period, boundary, status, approval_ref…}]}`` |
| ``get_answer_with_provenance`` | ``{answer_ref, depth≤4}`` | ``{answer, approval_ref, provenance{nodes, edges}}``（Evidence 只给 source_type 与 locator 摘要） |
| ``get_datapoint`` | ``{datapoint_id}`` | 字典条目（公开） |
| ``search_dictionary`` | ``{query, lang?, limit?}`` | ``{items:[…]}`` |

执行者：``create_mcp_server(store, actor=...)``，``actor`` 为固定 ``Actor``（stdio：本地进程以一个令牌运行）或无参可调用对象
（Streamable HTTP：每次调用从 OAuth 访问令牌解析，见 ``token_actor_resolver``）。授权逻辑在 ``service.ApprovedReadService``。
04 §6.2 的 ``belegt.answers.list_approved`` 等点号名可用 ``spec_names=True`` 额外注册为别名。
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from ..authz import Actor
from ..errors import GraphError
from .service import ApprovedReadService

READ_ONLY_ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
SPEC_ALIASES = {"list_approved_answers": "belegt.answers.list_approved", "get_answer_with_provenance": "belegt.lineage.get",
                "get_datapoint": "belegt.datapoints.get", "search_dictionary": "belegt.datapoints.search"}
TOOL_NAMES = tuple(SPEC_ALIASES)


def _server_class() -> type:
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return MCPServer
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def _annotations() -> Any:
    try:
        from mcp_types import ToolAnnotations
    except ImportError:  # pragma: no cover
        from mcp.types import ToolAnnotations
    return ToolAnnotations.model_validate(READ_ONLY_ANNOTATIONS)


def _tool_error(e: GraphError) -> Exception:
    for mod in ("mcp.server.mcpserver.exceptions", "mcp.server.fastmcp.exceptions"):
        try:
            return __import__(mod, fromlist=["ToolError"]).ToolError(json.dumps(e.to_response(), ensure_ascii=False))
        except (ImportError, AttributeError):
            continue
    return ValueError(json.dumps(e.to_response()))  # pragma: no cover


def token_actor_resolver(tokens: Any) -> Callable[[], Actor]:
    """Streamable HTTP：从 MCP 认证中间件放入上下文的访问令牌解析执行者（令牌表 / OIDC 的 ``TokenResolver``）。"""

    def resolve() -> Actor:
        from mcp.server.auth.middleware.auth_context import get_access_token

        tok = get_access_token()
        a = tokens.resolve(tok.token) if tok is not None else None
        if a is None:
            raise GraphError("GRAPH_UNAUTHENTICATED", "missing or invalid access token")
        return a

    return resolve


def create_mcp_server(store: Any, actor: Actor | Callable[[], Actor], *, name: str = "belegt", spec_names: bool = False,
                      include_restricted: bool = False, **server_kw: Any) -> Any:
    svc = ApprovedReadService(store, include_restricted=include_restricted)
    get_actor: Callable[[], Actor] = actor if callable(actor) and not isinstance(actor, Actor) else (lambda: actor)  # type: ignore[assignment]
    server = _server_class()(name, instructions=(
        "Belegt read-only interface to APPROVED sustainability answers (status approved/submitted only), scoped to your "
        "organisation and customer scope. Numbers are decimal strings; never treat them as estimates unless is_estimate=true. "
        "Evidence originals are never returned."), **server_kw)

    def _call(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except GraphError as e:
            raise _tool_error(e) from e

    def list_approved_answers(datapoint_ids: list[str] | None = None, counterparty_id: str | None = None,
                              period: dict[str, str] | None = None, limit: int = 200) -> dict[str, Any]:
        """List approved/submitted answers visible to the caller (filter by datapoint ids, customer, period)."""
        return _call(lambda: {"items": svc.list_approved_answers(get_actor(), datapoint_ids=datapoint_ids,
                                                                 counterparty_id=counterparty_id, period=period, limit=limit)})

    def get_answer_with_provenance(answer_ref: str, depth: int = 4) -> dict[str, Any]:
        """Get one approved answer with its approval and an evidence-chain summary (no originals)."""
        return _call(lambda: svc.get_answer_with_provenance(get_actor(), answer_ref, depth))

    def get_datapoint(datapoint_id: str) -> dict[str, Any]:
        """Get a Belegt dictionary datapoint definition (public)."""
        return _call(lambda: svc.get_datapoint(datapoint_id))

    def search_dictionary(query: str, lang: str = "de", limit: int = 20) -> dict[str, Any]:
        """Search the Belegt datapoint dictionary by label, alias or id (public)."""
        return _call(lambda: {"items": svc.search_dictionary(query, lang, limit)})

    fns = {"list_approved_answers": list_approved_answers, "get_answer_with_provenance": get_answer_with_provenance,
           "get_datapoint": get_datapoint, "search_dictionary": search_dictionary}
    for tname, fn in fns.items():
        server.add_tool(fn, name=tname, description=fn.__doc__, annotations=_annotations(), structured_output=True)
        if spec_names:
            server.add_tool(fn, name=SPEC_ALIASES[tname], description=fn.__doc__, annotations=_annotations(),
                            structured_output=True)
    server._belegt_service = svc  # type: ignore[attr-defined]
    return server


def serve(store: Any = None, *, transport: str = "stdio", token: str | None = None, tokens: Any = None,
          host: str = "127.0.0.1", port: int = 8766) -> None:  # pragma: no cover - 阻塞运行
    """``belegt mcp``：stdio 用 ``BELEGT_MCP_TOKEN``（令牌表 ``BELEGT_DEV_TOKENS``）解析固定执行者；HTTP 前置 OAuth 2.1。"""
    from ..api.auth import StaticTokenTable
    from ..store import GraphStore

    store = store or GraphStore(os.environ.get("BELEGT_DB_URL", "sqlite+pysqlite:///:memory:"))
    tokens = tokens or StaticTokenTable.from_env()
    if transport == "stdio":
        tok = token or os.environ.get("BELEGT_MCP_TOKEN")
        actor = tokens.resolve(tok) if tok else None
        if actor is None:
            raise SystemExit("belegt mcp: BELEGT_MCP_TOKEN missing or not in BELEGT_DEV_TOKENS")
        create_mcp_server(store, actor).run("stdio")
    else:
        server = create_mcp_server(store, token_actor_resolver(tokens))
        server.run("streamable-http", host=host, port=port)


__all__ = ["create_mcp_server", "token_actor_resolver", "serve", "TOOL_NAMES", "SPEC_ALIASES", "READ_ONLY_ANNOTATIONS"]
