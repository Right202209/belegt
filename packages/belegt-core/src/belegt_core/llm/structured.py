# Spec: 04 §2.0（结构化输出 + 自检）、§7.1（不支持结构化输出时退化为 "JSON 模式 + 校验 + 重试 1 次"）、07 §3.2 D4
"""结构化 JSON 输出：解析 + JSON Schema 校验 + 带错误反馈的重试。"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

import jsonschema

from .base import LLMClient, LLMOutputInvalid, LLMRequest, LLMResponse

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def parse_json(text: str) -> Any:
    t = (text or "").strip()
    m = _FENCE.match(t)
    if m:
        t = m.group(1)
    return json.loads(t)


def schema_errors(schema: dict[str, Any] | None, obj: Any) -> list[str]:
    if not schema:
        return []
    v = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in
            sorted(v.iter_errors(obj), key=lambda e: list(e.absolute_path))][:10]


def complete_json(client: LLMClient, req: LLMRequest, retries: int = 1) -> LLMResponse:
    """调用模型并返回 ``parsed`` 已通过 ``req.schema`` 的响应；失败把错误回灌重试 ``retries`` 次。

    返回的 ``tokens_in/tokens_out/cost_eur`` 为所有尝试之和（重试成本计入预算）。"""
    total_in = total_out = 0
    cost = None
    last_err: list[str] = []
    raw = None
    cur = req
    for attempt in range(1, retries + 2):
        resp = client.complete(cur)
        total_in += resp.tokens_in
        total_out += resp.tokens_out
        cost = resp.cost_eur if cost is None else cost + resp.cost_eur
        raw = resp.content
        try:
            obj = resp.parsed if resp.parsed is not None else parse_json(resp.content)
        except (ValueError, TypeError) as e:
            last_err = [f"invalid JSON: {e}"]
        else:
            last_err = schema_errors(req.schema, obj)
            if not last_err:
                resp.parsed = obj
                resp.tokens_in, resp.tokens_out, resp.cost_eur, resp.attempts = total_in, total_out, cost, attempt
                return resp
        fb = ("Your previous output was rejected by the JSON schema validator: " + "; ".join(last_err)
              + ". Return only JSON that satisfies the schema.")
        cur = replace(req, user=(req.user + "\n\n" + fb) if req.user else fb,
                      metadata={**req.metadata, "retry": attempt, "errors": last_err})
    err = LLMOutputInvalid(f"{req.agent}/{req.task}: structured output invalid after {retries + 1} attempts",
                           errors=last_err, raw=raw)
    err.tokens_in, err.tokens_out, err.cost_eur = total_in, total_out, cost  # type: ignore[attr-defined]
    raise err
