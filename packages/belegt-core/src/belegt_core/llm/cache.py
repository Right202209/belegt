# Spec: 04 §7.2（缓存层 C1…C5：只返回 ObjectRef，命中写 Trace.cache，缓存不是事实源 SSOT-7）
"""缓存。

- ``RefCache``：C1 模板直映射 / C2 历史题匹配 / C3 文档抽取 / C5 计算，值一律是 ``ObjectRef``（或 dp: ID），
  命中返回 ``(ref, layer)``；可随时清空，由证据图重建（SSOT-7）。
- ``CachedLLM``：同一（模型路由、提示词版本、系统提示、用户内容、schema）的结构化响应缓存，用于 Activity 重试与
  影子重跑时避免重复计费（相当于 provider 侧 C4 前缀缓存之外的完全命中）；命中 ``cached=True``、成本 0。
"""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from decimal import Decimal
from typing import Any

from ..canonical import hash_obj
from .base import LLMClient, LLMRequest, LLMResponse

LAYERS = ("C1", "C2", "C3", "C4", "C5")


class RefCache:
    def __init__(self) -> None:
        self._d: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self.hits: dict[str, int] = {k: 0 for k in LAYERS}
        self.misses: dict[str, int] = {k: 0 for k in LAYERS}

    @staticmethod
    def key(*parts: Any) -> str:
        return hash_obj([p if isinstance(p, (str, int, float, bool, type(None), list, dict)) else str(p) for p in parts])

    def get(self, layer: str, key: str) -> str | None:
        with self._lock:
            v = self._d.get((layer, key))
            (self.hits if v is not None else self.misses)[layer] += 1
            return v

    def put(self, layer: str, key: str, ref: str) -> None:
        with self._lock:
            self._d[(layer, key)] = ref

    def invalidate(self, layer: str | None = None) -> None:
        with self._lock:
            self._d = {k: v for k, v in self._d.items() if layer is not None and k[0] != layer}


class CachedLLM:
    provider = "cached"

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self._d: dict[str, LLMResponse] = {}
        self._lock = threading.Lock()
        self.hits = 0

    @staticmethod
    def key(req: LLMRequest) -> str:
        return hash_obj({"agent": req.agent, "task": req.task, "tier": req.tier, "pv": req.prompt_version,
                         "system": req.system, "user": req.user, "schema": req.schema,
                         "data": json.loads(json.dumps(req.data, default=str, sort_keys=True)),
                         "residency": req.data_residency})

    def complete(self, req: LLMRequest) -> LLMResponse:
        k = self.key(req)
        with self._lock:
            hit = self._d.get(k)
        if hit is not None:
            self.hits += 1
            return replace(hit, cached=True, cost_eur=Decimal("0"), tokens_in=0, tokens_out=0)
        resp = self.inner.complete(req)
        with self._lock:
            self._d[k] = resp
        return resp
