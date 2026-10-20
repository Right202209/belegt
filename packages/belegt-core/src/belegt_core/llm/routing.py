# Spec: 04 §7.1（能力档、model-routing@<semver>、主/备/本地、data_residency=private 只用本地）、§5.1 GP-11、§5.5（模型不可用切备用）
"""模型路由：Agent 只声明档位（tier-S/M/L/E/V）与能力，``RoutedLLM`` 按 ``model-routing`` 配置挑选客户端与模型。

配置（dict / YAML）::

    version: model-routing@1.0.0
    clients: {cloud: {...}, local: {...}}           # 名称 → LLMClient 实例（构造时传入）
    tiers:
      tier-M:
        primary:  {client: cloud, model: mistral-large-3, dpa: true}
        fallback: {client: cloud, model: mistral-medium-3.5, dpa: true}
        local:    {client: local, model: local-vllm}

规则：``data_residency=private`` → 只允许 ``local`` 条目（GP-11）；``sensitivity=restricted`` → 只允许 ``dpa: true``
或本地条目；主模型 ``ModelUnavailable`` 时依次尝试备用、本地；全部失败抛 ``ModelUnavailable``。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .base import LLMClient, LLMRequest, LLMResponse, ModelUnavailable

DEFAULT_ROUTING: dict[str, Any] = {
    "version": "model-routing@1.0.0",
    "tiers": {
        "tier-S": {"primary": {"client": "default", "model": "mistral-small-4", "dpa": True},
                   "local": {"client": "local", "model": "local-vllm"}},
        "tier-M": {"primary": {"client": "default", "model": "mistral-large-3", "dpa": True},
                   "fallback": {"client": "default", "model": "mistral-medium-3.5", "dpa": True},
                   "local": {"client": "local", "model": "local-vllm"}},
        "tier-L": {"primary": {"client": "default", "model": "claude-sonnet-5.5-eu", "dpa": True},
                   "fallback": {"client": "default", "model": "mistral-medium-3.5", "dpa": True},
                   "local": {"client": "local", "model": "local-vllm"}},
        "tier-V": {"primary": {"client": "default", "model": "claude-sonnet-5.5-eu", "dpa": True},
                   "local": {"client": "local", "model": "local-vllm"}},
        "tier-E": {"primary": {"client": "local", "model": "embedding-local"}},
    },
}


class RoutingDenied(ModelUnavailable):
    code = "LLM_ROUTING_DENIED"


@dataclass(frozen=True)
class Route:
    tier: str
    slot: str  # primary | fallback | local
    client: str
    model: str
    dpa: bool = False


class ModelRouter:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or DEFAULT_ROUTING
        self.version = self.config.get("version", "model-routing@0.0.0")

    def routes(self, tier: str, *, data_residency: str = "eu", sensitivity: str = "customer_shared") -> list[Route]:
        t = (self.config.get("tiers") or {}).get(tier) or (self.config.get("tiers") or {}).get("tier-M") or {}
        out = []
        for slot in ("primary", "fallback", "local"):
            e = t.get(slot)
            if not e:
                continue
            r = Route(tier, slot, e["client"], e.get("model", ""), bool(e.get("dpa", slot == "local")))
            if data_residency == "private" and slot != "local":
                continue  # GP-11
            if sensitivity == "restricted" and not (r.dpa or slot == "local"):
                continue
            out.append(r)
        return out


class RoutedLLM:
    """``LLMClient`` 实现：按档位路由到具体客户端，主备切换。"""

    provider = "routed"

    def __init__(self, clients: dict[str, LLMClient], router: ModelRouter | None = None) -> None:
        self.clients = clients
        self.router = router or ModelRouter()
        self.last_route: Route | None = None

    def complete(self, req: LLMRequest) -> LLMResponse:
        routes = self.router.routes(req.tier, data_residency=req.data_residency, sensitivity=req.sensitivity)
        if not routes:
            raise RoutingDenied(f"GP-11: no admissible route for {req.tier} (residency={req.data_residency}, "
                                f"sensitivity={req.sensitivity})")
        errors = []
        for r in routes:
            client = self.clients.get(r.client)
            if client is None:
                errors.append(f"{r.slot}:{r.client} missing")
                continue
            try:
                resp = client.complete(replace(req, metadata={**req.metadata, "model": r.model, "route": r.slot}))
            except ModelUnavailable as e:
                errors.append(f"{r.slot}:{e}")
                continue
            self.last_route = r
            resp.provider = f"{resp.provider}/{r.client}:{r.slot}"
            return resp
        raise ModelUnavailable(f"all routes failed for {req.tier}: {errors}")
