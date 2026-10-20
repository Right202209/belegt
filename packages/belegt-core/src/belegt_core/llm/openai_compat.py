# Spec: 04 §7.1（OpenAI 兼容适配层：vLLM/Ollama/云端；结构化输出不支持时退化为 JSON 模式 + 校验 + 重试）、CONTRACTS（环境变量）
"""``OpenAICompatibleClient``：httpx 实现的 ``/chat/completions`` 客户端。

环境变量：``BELEGT_LLM_BASE_URL``（如 ``http://localhost:8000/v1``）、``BELEGT_LLM_API_KEY``、``BELEGT_LLM_MODEL``。
- 首选 ``response_format={"type":"json_schema", "json_schema":{name, schema, strict:true}}``（guided decoding）；
  服务端返回 400 且提示不支持时自动退化为 ``{"type":"json_object"}``，并在系统提示中附 schema。
- 429 / 5xx / 网络错误：指数退避重试（``max_retries``）；最终失败抛 ``ModelUnavailable``。
- 结构化校验与回灌重试在 ``structured.complete_json`` 中完成（本类只做传输）。
测试通过 ``transport=httpx.MockTransport(...)`` 注入，绝不访问网络。
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from typing import Any

import httpx

from .base import LLMRequest, LLMResponse, ModelUnavailable
from .cost import PriceTable


class OpenAICompatibleClient:
    provider = "openai-compatible"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None, *,
                 timeout: float = 60.0, max_retries: int = 3, backoff_s: float = 1.0,
                 transport: httpx.BaseTransport | None = None, prices: PriceTable | None = None,
                 provider: str | None = None, sleep=time.sleep) -> None:
        self.base_url = (base_url or os.environ.get("BELEGT_LLM_BASE_URL") or "http://localhost:8000/v1").rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("BELEGT_LLM_API_KEY", "")
        self.model = model or os.environ.get("BELEGT_LLM_MODEL") or "local-vllm"
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.prices = prices or PriceTable()
        self.provider = provider or self.provider
        self._sleep = sleep
        self._json_schema_supported = True
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, headers=headers, transport=transport)

    @classmethod
    def from_env(cls, **kw: Any) -> "OpenAICompatibleClient":
        return cls(**kw)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------
    def _payload(self, req: LLMRequest, use_schema: bool) -> dict[str, Any]:
        msgs = req.messages()
        body: dict[str, Any] = {"model": req.metadata.get("model") or self.model, "messages": msgs, "temperature": req.temperature,
                                "max_tokens": req.max_tokens_out}
        if req.schema is not None:
            if use_schema:
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": req.schema_name, "schema": req.schema, "strict": True}}
            else:
                body["response_format"] = {"type": "json_object"}
                hint = "Respond with a single JSON object that validates against this JSON Schema:\n" + \
                    json.dumps(req.schema, ensure_ascii=False)
                if msgs and msgs[0]["role"] == "system":
                    msgs[0] = {"role": "system", "content": msgs[0]["content"] + "\n\n" + hint}
                else:
                    msgs.insert(0, {"role": "system", "content": hint})
        return body

    def complete(self, req: LLMRequest) -> LLMResponse:
        attempt = 0
        t0 = time.monotonic()
        last: Exception | None = None
        while attempt <= self.max_retries:
            attempt += 1
            body = self._payload(req, self._json_schema_supported)
            try:
                r = self._http.post("/chat/completions", json=body)
            except httpx.HTTPError as e:
                last = e
                self._sleep(self.backoff_s * 2 ** (attempt - 1))
                continue
            if r.status_code == 400 and req.schema is not None and self._json_schema_supported and \
                    ("response_format" in r.text or "json_schema" in r.text):
                self._json_schema_supported = False  # 退化为 JSON 模式（04 §7.1）
                attempt -= 1
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = ModelUnavailable(f"HTTP {r.status_code}")
                ra = r.headers.get("retry-after")
                self._sleep(float(ra) if ra and ra.replace(".", "", 1).isdigit() else self.backoff_s * 2 ** (attempt - 1))
                continue
            if r.status_code >= 400:
                raise ModelUnavailable(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            usage = data.get("usage") or {}
            tin, tout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            model = data.get("model") or self.model
            cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
            ratio = Decimal(cached) / Decimal(tin) if tin else Decimal(0)
            return LLMResponse(content=content, model=model, provider=self.provider, tokens_in=tin, tokens_out=tout,
                               cost_eur=self.prices.cost_eur(model, tin, tout, req.tier, ratio), attempts=attempt,
                               latency_ms=int((time.monotonic() - t0) * 1000), raw=data)
        raise ModelUnavailable(f"model endpoint unavailable after {self.max_retries + 1} attempts: {last}")
