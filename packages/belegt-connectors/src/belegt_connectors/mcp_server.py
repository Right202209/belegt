# Spec: 04 §6.1（连接器以本地进程 stdio 或 Streamable HTTP 运行；工具注解 readOnlyHint/destructiveHint；
#       不支持点号的客户端映射为 `__`）
"""把任一 `BaseConnector` 暴露为 MCP server。兼容 mcp 2.x（`mcp.server.mcpserver.MCPServer`）与 1.x（`FastMCP`）。

- 工具的 `inputSchema` 原样使用连接器作者手写的 JSON Schema（而非由函数签名推导），`outputSchema` 为 envelope。
- 每次调用返回 envelope（结构化输出）；`ConnectorError` 以工具错误返回（`{error:{code,message}}` 文本）。
- `dot_names=False` 时工具名中的 `.` 映射为 `__`。
"""
from __future__ import annotations

import inspect
import json
from typing import Any

from .base import BaseConnector
from .spec import ENVELOPE_SCHEMA, ConnectorError, mcp_safe_name

_PY_TYPES: dict[str, Any] = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list,
                             "object": dict}


def _server_class() -> tuple[type, str]:
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return MCPServer, "mcp2"
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP, "mcp1"
    except ImportError as e:
        raise ImportError("MCP 形态需要安装 mcp 包：pip install 'belegt-connectors[mcp]'") from e


def _annotations(spec_ann: dict[str, Any]) -> Any:
    try:
        from mcp_types import ToolAnnotations  # mcp 2
    except ImportError:
        from mcp.types import ToolAnnotations  # mcp 1
    return ToolAnnotations.model_validate(spec_ann)


def _py_type(prop: dict[str, Any]) -> Any:
    t = prop.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t is None and "enum" in prop:
        return Any
    return _PY_TYPES.get(t, Any)


def _tool_error_class() -> type[Exception]:
    for mod in ("mcp.server.mcpserver.exceptions", "mcp.server.fastmcp.exceptions"):
        try:
            return __import__(mod, fromlist=["ToolError"]).ToolError
        except (ImportError, AttributeError):
            continue
    return ValueError  # pragma: no cover


def _make_handler(connector: BaseConnector, tool_name: str, schema: dict[str, Any]) -> Any:
    tool_error = _tool_error_class()

    def handler(**kwargs: Any) -> dict[str, Any]:
        args = {k: v for k, v in kwargs.items() if v is not None}
        try:
            return connector.call(tool_name, args)
        except ConnectorError as e:
            # 预期内错误 → MCP 工具错误（isError=true），文本为 {"error":{code,message,details}}
            raise tool_error(json.dumps(e.to_dict(), ensure_ascii=False)) from e

    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    params = []
    for pname, prop in props.items():
        ann = _py_type(prop)
        if pname in required:
            params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, annotation=ann))
        else:
            params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, default=None,
                                            annotation=ann | None if ann is not Any else Any))
    handler.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    handler.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict[str, Any]}
    handler.__name__ = mcp_safe_name(tool_name)
    return handler


def build_mcp_server(connector: BaseConnector, *, name: str | None = None, dot_names: bool = True) -> Any:
    """返回已注册全部只读工具的 MCP server 对象（未启动）。"""
    cls, flavor = _server_class()
    server = cls(name or f"belegt-{connector.kind}",
                 instructions=(f"Belegt read-only connector {connector.connector_id} (belegt-connector/1). "
                               "All tools are read-only; numeric values are decimal strings; every record carries "
                               "a locator usable as Evidence.locator."))
    for spec in connector.tools():
        exposed = spec.name if dot_names else mcp_safe_name(spec.name)
        handler = _make_handler(connector, spec.name, spec.input_schema)
        server.add_tool(handler, name=exposed, description=spec.description,
                        annotations=_annotations(spec.annotations), structured_output=True)
        tool = server._tool_manager.get_tool(exposed)  # noqa: SLF001 - 用手写 schema 覆盖签名推导的 schema
        if tool is not None:
            tool.parameters = spec.input_schema
            try:
                tool.__dict__["output_schema"] = ENVELOPE_SCHEMA  # mcp2: cached_property
            except Exception:  # pragma: no cover
                pass
            if flavor == "mcp1" and hasattr(tool, "fn_metadata"):  # pragma: no cover - 依 mcp 版本
                try:
                    tool.fn_metadata.output_schema = ENVELOPE_SCHEMA
                except Exception:
                    pass
    server._belegt_connector = connector  # type: ignore[attr-defined]
    return server


def serve(connector: BaseConnector, *, transport: str = "stdio", dot_names: bool = True, host: str = "127.0.0.1",
          port: int = 8765) -> None:
    """启动 MCP server（阻塞）。stdio：本地进程；streamable-http：需在前面放 OAuth 2.1 / mTLS 反向代理（04 §6.1）。"""
    server = build_mcp_server(connector, dot_names=dot_names)
    if transport == "stdio":
        server.run("stdio")
    elif transport in ("streamable-http", "http"):
        try:
            server.run("streamable-http", host=host, port=port)
        except TypeError:  # mcp 1.x：host/port 在 settings
            server.settings.host, server.settings.port = host, port
            server.run("streamable-http")
    else:
        raise ValueError(f"unsupported transport {transport!r}")
