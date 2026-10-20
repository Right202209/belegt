# Spec: 07 §4.4（API 从身份推出 created_by，Agent 不能自报身份）、§4.6（认证）；03 §6.5（会话变量 app.org_id/app.actor/app.zone/app.cp_scope）
"""开发用静态 Bearer 令牌表 → ``Actor``。

生产应替换为 OIDC/SPIFFE（07 §4.4/§4.6）：实现 ``TokenResolver`` 协议并传给 ``create_app(token_resolver=...)``。
令牌表格式（dict / JSON / YAML）::

    {"<token>": {"actor": "user:u_compliance01", "org_id": "org_…", "roles": ["compliance_owner"],
                 "cp_scope": ["cp_…"] | null, "approved_only": false}}

环境变量 ``BELEGT_DEV_TOKENS`` 可指向 JSON/YAML 文件。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from ..authz import Actor
from ..errors import GraphError


class TokenResolver(Protocol):
    def resolve(self, token: str) -> Actor | None: ...


class StaticTokenTable:
    def __init__(self, table: dict[str, dict[str, Any]] | None = None) -> None:
        self.table = dict(table or {})

    @classmethod
    def from_env(cls) -> "StaticTokenTable":
        path = os.environ.get("BELEGT_DEV_TOKENS")
        if not path:
            return cls()
        text = Path(path).read_text("utf-8")
        if path.endswith((".yaml", ".yml")):
            import yaml

            return cls(yaml.safe_load(text) or {})
        return cls(json.loads(text))

    def add(self, token: str, actor: str, org_id: str, roles: list[str] | tuple[str, ...] = (), **kw: Any) -> None:
        self.table[token] = {"actor": actor, "org_id": org_id, "roles": list(roles), **kw}

    def resolve(self, token: str) -> Actor | None:
        e = self.table.get(token)
        if not e:
            return None
        cp = e.get("cp_scope")
        return Actor(id=e["actor"], org_id=e["org_id"], roles=frozenset(e.get("roles") or ()),
                     cp_scope=tuple(cp) if cp else None, approved_only=bool(e.get("approved_only", False)))


def actor_from_header(resolver: TokenResolver, authorization: str | None, ip: str | None = None,
                      user_agent: str | None = None) -> Actor:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise GraphError("GRAPH_UNAUTHENTICATED", "missing bearer token")
    a = resolver.resolve(authorization.split(" ", 1)[1].strip())
    if a is None:
        raise GraphError("GRAPH_UNAUTHENTICATED", "invalid token")
    if a.is_user:
        a = Actor(id=a.id, org_id=a.org_id, roles=a.roles, cp_scope=a.cp_scope, approved_only=a.approved_only,
                  ip=ip, user_agent=user_agent)
    return a
