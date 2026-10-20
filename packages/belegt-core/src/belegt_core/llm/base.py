# Spec: 04 §2.0（LLM 输出一律受 JSON Schema 约束；提示词版本 prompt:<agent>@<semver>）、§7.1（能力档、OpenAI 兼容适配层）
"""LLM 客户端协议与请求/响应数据类。

所有 Agent 只经 ``LLMClient.complete(LLMRequest) -> LLMResponse`` 调用模型；``LLMRequest`` 同时携带
- 给真实模型的 ``system`` / ``user`` 文本（外部文本已按 07 §3.2 D3 放进 ``<<EXT:nonce>>`` 数据块）；
- 给确定性 ``MockLLM`` 的 ``task`` 与结构化 ``data``（与 user 文本同源，MockLLM 不解析自然语言）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

TIERS = ("tier-S", "tier-M", "tier-L", "tier-E", "tier-V")


class LLMError(Exception):
    code = "LLM_ERROR"


class ModelUnavailable(LLMError):
    """模型服务不可用（04 §5.5：切备用模型；都不可用时 Activity 退避重试）。"""

    code = "LLM_MODEL_UNAVAILABLE"


class LLMOutputInvalid(LLMError):
    """结构化输出 JSON 解析或 schema 校验失败（重试后仍失败）。"""

    code = "LLM_OUTPUT_INVALID"

    def __init__(self, message: str, errors: list[str] | None = None, raw: str | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []
        self.raw = raw


@dataclass
class LLMRequest:
    agent: str                       # agent id，如 question-parser
    task: str                        # 任务名（MockLLM 路由键），如 map_questions
    tier: str = "tier-M"
    system: str = ""
    user: str = ""
    schema: dict[str, Any] | None = None
    schema_name: str = "output"
    prompt_version: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    max_tokens_out: int = 2000
    temperature: float = 0.0
    capabilities: tuple[str, ...] = ("json_schema",)
    data_residency: str = "eu"       # eu | private（GP-11）
    sensitivity: str = "customer_shared"
    metadata: dict[str, Any] = field(default_factory=dict)

    def messages(self) -> list[dict[str, str]]:
        out = []
        if self.system:
            out.append({"role": "system", "content": self.system})
        out.append({"role": "user", "content": self.user or json.dumps(self.data, ensure_ascii=False, default=str)})
        return out


@dataclass
class LLMResponse:
    content: str
    parsed: Any = None
    model: str = ""
    provider: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_eur: Decimal = Decimal("0")
    cached: bool = False
    attempts: int = 1
    latency_ms: int = 0
    raw: dict[str, Any] | None = None


@runtime_checkable
class LLMClient(Protocol):
    def complete(self, req: LLMRequest) -> LLMResponse: ...


def estimate_tokens(text: str) -> int:
    """粗估 token 数（≈ 4 字符/ token，CJK 1 字符 ≈ 1 token）。用于 MockLLM 与预算预检。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return max(1, (len(text) - cjk) // 4 + cjk)
