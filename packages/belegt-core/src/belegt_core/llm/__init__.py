# Spec: CONTRACTS（LLM 统一经 belegt_core.llm.LLMClient）、04 §2.0、§7（模型路由、缓存与成本）、10 §4
"""LLM 层：``LLMClient`` 协议、``OpenAICompatibleClient``（httpx）、确定性 ``MockLLM``、模型路由、缓存与成本记账。"""
from __future__ import annotations

from .base import TIERS, LLMClient, LLMError, LLMOutputInvalid, LLMRequest, LLMResponse, ModelUnavailable, estimate_tokens
from .cache import CachedLLM, RefCache
from .cost import HARD_LIMIT_EUR, TARGET_EUR_P95, CostEntry, CostLedger, PriceTable, percentile
from .embedding import HashingEmbedder, cosine
from .mock import GLOSSARY, MockLLM, instruction_hits, score_candidate
from .openai_compat import OpenAICompatibleClient
from .routing import DEFAULT_ROUTING, ModelRouter, RoutedLLM, Route, RoutingDenied
from .structured import complete_json, parse_json, schema_errors


def default_llm() -> LLMClient:
    """有 ``BELEGT_LLM_BASE_URL`` 时返回 ``OpenAICompatibleClient``，否则返回 ``MockLLM``（离线 demo/测试）。"""
    import os

    if os.environ.get("BELEGT_LLM_BASE_URL"):
        return OpenAICompatibleClient.from_env()
    return MockLLM()


__all__ = [
    "TIERS", "LLMClient", "LLMRequest", "LLMResponse", "LLMError", "LLMOutputInvalid", "ModelUnavailable",
    "estimate_tokens", "OpenAICompatibleClient", "MockLLM", "GLOSSARY", "score_candidate", "instruction_hits",
    "ModelRouter", "RoutedLLM", "Route", "RoutingDenied", "DEFAULT_ROUTING", "RefCache", "CachedLLM", "PriceTable",
    "CostLedger", "CostEntry", "TARGET_EUR_P95", "HARD_LIMIT_EUR", "percentile", "HashingEmbedder", "cosine",
    "complete_json", "parse_json", "schema_errors", "default_llm",
]
