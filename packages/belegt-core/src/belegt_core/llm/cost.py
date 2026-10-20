# Spec: 10 §4.1–§4.4（价格基准、单份 100 题估算、建议取值：P95 ≤ €3 目标、€5 硬熔断）、04 §5.2、§7.3
"""成本记账。

- ``PriceTable``：模型 → (输入, 输出) 每百万 token 价格（USD，10 §4.1 合同口径：EU 区域端点已含 +10%），按
  ``usd_eur`` 汇率换算为 EUR（默认 0.92，工程假设，可配置）。未知模型按其档位默认价计。
- ``CostLedger``：按 Request / Agent / 模型 / 档位聚合 token 与 ``cost_eur``（Decimal），供预算熔断与 DSH-03 看板；
  ``p95()`` 计算多份问卷的 P95 单份成本，``over_target()`` 判断是否超过 10 §4.4 的目标值 €3。
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Iterable

TARGET_EUR_P95 = Decimal("3")   # 10 §4.4：单份问卷（100 题）LLM 成本 P95 ≤ €3
HARD_LIMIT_EUR = Decimal("5")   # 04 §5.2 / 10 §4.4：守卫硬熔断 €5/份

# USD / 1M token（10 §4.1；EU 区域端点 +10% 已计入 Anthropic/OpenAI 行）
DEFAULT_PRICES_USD: dict[str, tuple[Decimal, Decimal]] = {
    "claude-sonnet-5.5-eu": (Decimal("2.20"), Decimal("11.00")),
    "claude-haiku-5.5-eu": (Decimal("0.11"), Decimal("0.55")),
    "claude-opus-5.5-eu": (Decimal("4.40"), Decimal("22.00")),
    "gpt-6.1-sol-eu": (Decimal("2.20"), Decimal("11.00")),
    "gpt-6-luna-eu": (Decimal("0.11"), Decimal("0.55")),
    "mistral-medium-3.5": (Decimal("1.50"), Decimal("7.50")),
    "mistral-large-3": (Decimal("0.50"), Decimal("1.50")),
    "mistral-small-4": (Decimal("0.15"), Decimal("0.60")),
    "local-vllm": (Decimal("0"), Decimal("0")),
    "embedding-local": (Decimal("0"), Decimal("0")),
}
# 档位默认价（10 §4.3 方案 B：S=Mistral Small 4，M=Mistral Large 3，L/V=Sonnet 5.5 级 EU）
TIER_DEFAULT_MODEL = {
    "tier-S": "mistral-small-4", "tier-M": "mistral-large-3", "tier-L": "claude-sonnet-5.5-eu",
    "tier-V": "claude-sonnet-5.5-eu", "tier-E": "embedding-local",
}


@dataclass
class PriceTable:
    prices_usd: dict[str, tuple[Decimal, Decimal]] = field(default_factory=lambda: dict(DEFAULT_PRICES_USD))
    usd_eur: Decimal = Decimal("0.92")

    def cost_eur(self, model: str, tokens_in: int, tokens_out: int, tier: str | None = None,
                 cached_ratio: Decimal = Decimal("0")) -> Decimal:
        p = self.prices_usd.get(model)
        if p is None:
            base = model.removeprefix("mock-")
            p = self.prices_usd.get(base) or self.prices_usd.get(TIER_DEFAULT_MODEL.get(tier or "", ""), (Decimal(0), Decimal(0)))
        pin, pout = p
        tin = Decimal(tokens_in) * (1 - cached_ratio) + Decimal(tokens_in) * cached_ratio * Decimal("0.05")
        usd = (tin * pin + Decimal(tokens_out) * pout) / Decimal(1_000_000)
        return (usd * self.usd_eur).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


@dataclass
class CostEntry:
    request_id: str | None
    agent: str
    tier: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_eur: Decimal
    cached: bool = False


class CostLedger:
    """线程安全的成本流水（进程内；生产由 Trace 表聚合，10 §5.1）。"""

    def __init__(self) -> None:
        self._entries: list[CostEntry] = []
        self._lock = threading.Lock()

    def record(self, entry: CostEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def entries(self, request_id: str | None = None) -> list[CostEntry]:
        with self._lock:
            return [e for e in self._entries if request_id is None or e.request_id == request_id]

    def totals(self, request_id: str | None = None, agent: str | None = None) -> dict[str, object]:
        es = [e for e in self.entries(request_id) if agent is None or e.agent == agent]
        return {
            "calls": len(es),
            "tokens_in": sum(e.tokens_in for e in es),
            "tokens_out": sum(e.tokens_out for e in es),
            "cost_eur": sum((e.cost_eur for e in es), Decimal("0")),
            "cache_hits": sum(1 for e in es if e.cached),
        }

    def by_agent(self, request_id: str | None = None) -> dict[str, dict[str, object]]:
        agents = sorted({e.agent for e in self.entries(request_id)})
        return {a: self.totals(request_id, a) for a in agents}

    def by_model(self, request_id: str | None = None) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for e in self.entries(request_id):
            d = out.setdefault(e.model, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_eur": Decimal("0")})
            d["calls"] += 1  # type: ignore[operator]
            d["tokens_in"] += e.tokens_in  # type: ignore[operator]
            d["tokens_out"] += e.tokens_out  # type: ignore[operator]
            d["cost_eur"] += e.cost_eur  # type: ignore[operator]
        return out

    def request_costs(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for e in self.entries():
            if e.request_id:
                out[e.request_id] = out.get(e.request_id, Decimal("0")) + e.cost_eur
        return out

    def p95(self, request_ids: Iterable[str] | None = None) -> Decimal:
        costs = self.request_costs()
        vals = sorted(costs[r] for r in (request_ids or costs.keys()) if r in costs)
        return percentile(vals, 95)

    def over_target(self, request_id: str, target: Decimal = TARGET_EUR_P95) -> bool:
        return self.totals(request_id)["cost_eur"] > target  # type: ignore[operator]


def percentile(sorted_vals: list[Decimal], pct: int) -> Decimal:
    """最近秩法（nearest-rank）。"""
    if not sorted_vals:
        return Decimal("0")
    k = max(1, math.ceil(pct / 100 * len(sorted_vals)))
    return sorted_vals[k - 1]
