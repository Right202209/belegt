# Spec: 04 §6.3（A2A：Agent Card、客户订阅已批准数据点 + webhook 推送）
"""A2A（最小可用）：``a2a_router``（Agent Card ``/.well-known/agent.json``、JSON-RPC ``/a2a``、``/a2a/subscriptions``）与
``SubscriptionService``（订阅存储、同意检查、HMAC 签名 webhook 推送）。延迟导入。"""
from __future__ import annotations

from typing import Any

_EXPORTS = {
    "a2a_router": "routes", "A2AServices": "routes", "configure_a2a": "routes", "SKILLS": "routes",
    "SubscriptionService": "subscriptions", "sign": "subscriptions", "verify_signature": "subscriptions",
    "grant_allows": "subscriptions",
}


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(name)
    import importlib

    return getattr(importlib.import_module(f"{__name__}.{mod}"), name)


__all__ = list(_EXPORTS)
