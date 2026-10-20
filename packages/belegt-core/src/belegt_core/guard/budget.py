# Spec: 04 §5.2（三级预算：调用级/Agent 级/Request 级，80% 预警、100% BudgetExceeded → halted）、§2 各 Agent 预算、10 §4.4
"""预算熔断。

默认值（工程假设，租户可调且只可收紧，10 §4.4-5）：
- Request 级：100 题 ≤ 1.5M token、≤ €5（硬熔断）、≤ 60 min 机器时间（不含人/上游等待）；目标值 €3（P95）只预警。
- Agent 级（每 Request）与调用级：按 04 §2 各 Agent 的"预算"行。
确定性服务（calc-service、approval-router、filler-file、egress-gateway）不计 token，只计时长。
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from ..llm.cost import HARD_LIMIT_EUR, TARGET_EUR_P95


class BudgetExceeded(Exception):
    """达到 100% 预算；``reason`` ∈ budget_tokens|budget_cost|budget_steps|budget_time（04 §5.2 status_reason）。"""

    def __init__(self, reason: str, scope: str, used: Any, limit: Any, agent: str | None = None) -> None:
        super().__init__(f"{reason} exceeded at {scope} level ({agent or '-'}): {used} >= {limit}")
        self.reason, self.scope, self.used, self.limit, self.agent = reason, scope, used, limit, agent


@dataclass
class Budget:
    tokens: int | None = None
    cost_eur: Decimal | None = None
    steps: int | None = None
    time_s: float | None = None

    def tightened(self, other: "Budget") -> "Budget":
        """租户只可收紧：逐项取较小值。"""
        def mn(a, b):
            return b if a is None else a if b is None else min(a, b)
        return Budget(mn(self.tokens, other.tokens), mn(self.cost_eur, other.cost_eur), mn(self.steps, other.steps),
                      mn(self.time_s, other.time_s))


@dataclass(frozen=True)
class CallLimit:
    tokens_in: int
    tokens_out: int
    time_s: float = 120.0


REQUEST_BUDGET = Budget(tokens=1_500_000, cost_eur=HARD_LIMIT_EUR, time_s=3600.0)
# 04 §2 各 Agent 预算（每 Request）
AGENT_BUDGETS: dict[str, Budget] = {
    "intake-triage": Budget(tokens=10_000, steps=3, time_s=30 * 3),
    "question-parser": Budget(tokens=250_000, time_s=600),
    "evidence-miner": Budget(tokens=400_000, time_s=900),
    "answer-drafter": Budget(tokens=300_000, time_s=900),
    "upstream-chaser": Budget(tokens=60_000),
    "supplier-reply-reviewer": Budget(tokens=300_000),
    "consistency-auditor": Budget(tokens=50_000, time_s=300),
    "filler": Budget(tokens=200_000, steps=150, time_s=45 * 60),
    "sales-assistant": Budget(tokens=10_000),
    "reg-radar": Budget(tokens=2_000_000),
    "calc-service": Budget(time_s=500 * 2.0),
    "approval-router": Budget(time_s=60),
}
CALL_LIMITS: dict[str, CallLimit] = {
    "intake-triage": CallLimit(8_000, 500, 30),
    "question-parser": CallLimit(12_000, 2_000),
    "evidence-miner": CallLimit(20_000, 1_000),
    "answer-drafter": CallLimit(8_000, 1_000),
    "upstream-chaser": CallLimit(3_000, 1_000),
    "supplier-reply-reviewer": CallLimit(30_000, 2_000),
    "consistency-auditor": CallLimit(1_000, 1_000),
    "sales-assistant": CallLimit(10_000, 1_000),
    "reg-radar": CallLimit(60_000, 2_000),
    "filler": CallLimit(20_000, 1_000),
}
DETERMINISTIC = frozenset({"calc-service", "approval-router", "egress-gateway", "guard"})
WARN_RATIO = Decimal("0.8")


@dataclass
class Usage:
    tokens: int = 0
    cost_eur: Decimal = Decimal("0")
    steps: int = 0
    time_s: float = 0.0


@dataclass
class BudgetState:
    """一个 Request 的预算使用（可序列化，随工作流日志保存以便断点续跑）。"""

    request_id: str
    request_budget: Budget = field(default_factory=lambda: Budget(**asdict(REQUEST_BUDGET)))
    agent_budgets: dict[str, Budget] = field(default_factory=lambda: {k: Budget(**asdict(v)) for k, v in AGENT_BUDGETS.items()})
    total: Usage = field(default_factory=Usage)
    per_agent: dict[str, Usage] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # ---------------------------------------------------------------- checks
    def precheck_call(self, agent: str, tokens_in: int, max_out: int) -> None:
        lim = CALL_LIMITS.get(agent)
        if lim and tokens_in > lim.tokens_in:
            raise BudgetExceeded("budget_tokens", "call", tokens_in, lim.tokens_in, agent)
        self._check(agent, extra_tokens=tokens_in + min(max_out, lim.tokens_out if lim else max_out), soft=True)

    def charge(self, agent: str, *, tokens_in: int = 0, tokens_out: int = 0, cost_eur: Decimal = Decimal("0"),
               steps: int = 0, time_s: float = 0.0) -> dict[str, str]:
        with self._lock:
            u = self.per_agent.setdefault(agent, Usage())
            if agent not in DETERMINISTIC:
                u.tokens += tokens_in + tokens_out
                self.total.tokens += tokens_in + tokens_out
                u.cost_eur += cost_eur
                self.total.cost_eur += cost_eur
            u.steps += steps
            self.total.steps += steps
            u.time_s += time_s
            self.total.time_s += time_s
        self._check(agent)
        return self.ratios(agent)

    def _check(self, agent: str, extra_tokens: int = 0, soft: bool = False) -> None:
        rb, u, au = self.request_budget, self.total, self.per_agent.get(agent, Usage())
        ab = self.agent_budgets.get(agent, Budget())
        checks = [
            ("budget_tokens", "request", u.tokens + extra_tokens, rb.tokens),
            ("budget_cost", "request", u.cost_eur, rb.cost_eur),
            ("budget_time", "request", u.time_s, rb.time_s),
            ("budget_tokens", "agent", au.tokens + extra_tokens, ab.tokens),
            ("budget_steps", "agent", au.steps, ab.steps),
            ("budget_time", "agent", au.time_s, ab.time_s),
        ]
        for reason, scope, used, limit in checks:
            if limit is None or (agent in DETERMINISTIC and reason in ("budget_tokens", "budget_cost")):
                continue
            if soft and reason != "budget_tokens":
                continue
            u_d, l_d = Decimal(str(used)), Decimal(str(limit))
            if (u_d > l_d) if soft else (u_d >= l_d):
                raise BudgetExceeded(reason, scope, used, limit, agent)
            if not soft and u_d >= WARN_RATIO * l_d:
                w = f"{scope}:{agent if scope == 'agent' else '*'}:{reason}:80%"
                if w not in self.warnings:
                    self.warnings.append(w)
        if not soft and u.cost_eur > TARGET_EUR_P95 and "request:*:cost_over_target" not in self.warnings:
            self.warnings.append("request:*:cost_over_target")

    def ratios(self, agent: str | None = None) -> dict[str, str]:
        rb = self.request_budget
        out = {}
        if rb.tokens:
            out["tokens"] = str(round(self.total.tokens / rb.tokens, 4))
        if rb.cost_eur:
            out["cost"] = str((self.total.cost_eur / rb.cost_eur).quantize(Decimal("0.0001")))
        return out

    def apply_patch(self, patch: dict[str, Any] | None) -> None:
        """``resume`` 信号的 ``budget_patch``：人追加预算（如 ``{"request": {"cost_eur": "8"}}``）。"""
        for scope, vals in (patch or {}).items():
            tgt = self.request_budget if scope == "request" else self.agent_budgets.setdefault(scope, Budget())
            for k, v in (vals or {}).items():
                setattr(tgt, k, Decimal(str(v)) if k == "cost_eur" else (int(v) if k in ("tokens", "steps") else float(v)))

    # ---------------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, Any]:
        def b(x: Budget) -> dict[str, Any]:
            return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(x).items()}
        def us(x: Usage) -> dict[str, Any]:
            return {"tokens": x.tokens, "cost_eur": str(x.cost_eur), "steps": x.steps, "time_s": x.time_s}
        return {"request_id": self.request_id, "request_budget": b(self.request_budget),
                "agent_budgets": {k: b(v) for k, v in self.agent_budgets.items()}, "total": us(self.total),
                "per_agent": {k: us(v) for k, v in self.per_agent.items()}, "warnings": list(self.warnings)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BudgetState":
        def b(x: dict[str, Any]) -> Budget:
            return Budget(tokens=x.get("tokens"), cost_eur=Decimal(x["cost_eur"]) if x.get("cost_eur") is not None else None,
                          steps=x.get("steps"), time_s=x.get("time_s"))
        def us(x: dict[str, Any]) -> Usage:
            return Usage(int(x["tokens"]), Decimal(x["cost_eur"]), int(x["steps"]), float(x["time_s"]))
        return cls(request_id=d["request_id"], request_budget=b(d["request_budget"]),
                   agent_budgets={k: b(v) for k, v in d["agent_budgets"].items()}, total=us(d["total"]),
                   per_agent={k: us(v) for k, v in d["per_agent"].items()}, warnings=list(d.get("warnings") or []))
