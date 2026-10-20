# Spec: 04 §2.14（guard：策略检查点、晋升、预算熔断、循环检测、注入扫描、Trace；fail-closed）、§5.1 GP-01…GP-11、
#       §5.2、§5.3、§5.5、07 §2.2（运行时隔离）、§3.3（金丝雀）、03 §6.8（GRAPH_HALTED）
"""``Guard``：确定性横切守卫。

接入点：
- ``GraphStore(policy_hook=guard.policy_hook)``：图写入前（GP-01 身份、GP-02 区隔离预检、守卫中止冻结、限速）；
- ``guard.llm(client, ...)`` 返回 ``GuardedLLM``：每次模型调用前后（GP-04 注入/金丝雀、GP-09 预算、GP-11 路由驻留、
  成本记账与 Trace 子 span）；
- ``guard.check_tool``（GP-03）、``guard.check_output_numbers``（GP-05）、``guard.check_portal_action``（GP-08）、
  ``guard.enter``（GP-10）；晋升（GP-06）在 ``guard.promotion``；外发（GP-07）由 ``egress`` 调用 ``guard.egress_halted``。
- ``guard.halt(request_id, reason, return_to)``：写 Request ``halted``（``agent:guard``，T20）+ ``AuditEvent policy.halt``。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Callable, Iterable

from ..authz import QUARANTINE_AGENTS, QUARANTINE_READ, Actor, validate_actor
from ..errors import GraphError
from ..llm.base import LLMClient, LLMRequest, LLMResponse, estimate_tokens
from ..llm.cost import CostEntry, CostLedger
from ..rules.numtext import _readings, add_date, add_text_tokens, all_tokens, numeric_tokens  # noqa: F401
from ..canonical import normalize_dec
from .budget import BudgetExceeded, BudgetState
from .injection import contains_canary, make_canary, scan
from .loops import LoopDetected, LoopDetector
from .trace import Span, TraceRecorder

POLICY_VERSION = "guard-policy@1.0.0"

# GP-03：每个 Agent 的工具白名单（部署清单静态声明，07 §3.2 D6）
TOOL_WHITELIST: dict[str, frozenset[str]] = {
    "intake-triage": frozenset(),
    "question-parser": frozenset(),
    "supplier-reply-reviewer": frozenset(),
    "evidence-miner": frozenset({"docs.search", "docs.get_page", "erp.*.query", "erp.*.get", "erp.*.list",
                                 "file.excel.range.get"}),
    "calc-service": frozenset(),
    "answer-drafter": frozenset(),
    "approval-router": frozenset(),
    "upstream-chaser": frozenset({"egress.send_templated"}),
    "filler": frozenset({"portal.navigate", "portal.fill", "portal.select", "portal.upload", "portal.click",
                         "portal.submit"}),
    "reg-radar": frozenset({"web.fetch"}),
    "sales-assistant": frozenset(),
    "consistency-auditor": frozenset(),
}


class Halted(Exception):
    """策略违规导致中止（halted:policy）。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _tool_allowed(agent: str, tool: str) -> bool:
    for pat in TOOL_WHITELIST.get(agent, frozenset()):
        if pat == tool or (("*" in pat) and re.fullmatch(pat.replace(".", r"\.").replace("*", r"[a-z0-9_]+(?:\.[a-z0-9_]+)*"), tool)):
            return True
    return False


@dataclass
class GuardConfig:
    data_residency: str = "eu"            # eu | private（GP-11）
    fail_closed: bool = False             # guard 不可用 → 拒绝所有 LLM 调用与外发（04 §2.14）
    write_rate_per_min: int = 20_000      # 每执行者每分钟写入上限（GRAPH_RATE_LIMITED）
    canaries: tuple[str, ...] = field(default_factory=lambda: (make_canary(),))


class Guard:
    actor_id = "agent:guard"

    def __init__(self, store: Any, *, ledger: CostLedger | None = None, recorder: TraceRecorder | None = None,
                 loops: LoopDetector | None = None, config: GuardConfig | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self.ledger = ledger or CostLedger()
        self.recorder = recorder or TraceRecorder(store)
        self.loops = loops or LoopDetector()
        self.config = config or GuardConfig()
        self.budgets: dict[str, BudgetState] = {}
        self.halted_requests: set[str] = set()
        self.egress_halted = False
        self.decisions: list[dict[str, Any]] = []  # belegt_guard_decisions_total 的进程内替身
        self._writes: dict[str, list[float]] = {}
        self._clock = clock
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def _decide(self, gp: str, result: str, span: Span | None = None, **extra: Any) -> None:
        with self._lock:
            self.decisions.append({"check": gp, "result": result, **{k: str(v) for k, v in extra.items()}})
        if span is not None:
            span.check(gp, result, **extra)

    def budget(self, request_id: str | None) -> BudgetState:
        key = request_id or "_global"
        with self._lock:
            if key not in self.budgets:
                self.budgets[key] = BudgetState(key)
            return self.budgets[key]

    def actor(self, org_id: str) -> Actor:
        return Actor.of(self.actor_id, org_id)

    # ------------------------------------------------------------------ GP-01/02 + 中止 + 限速：图写入前
    def policy_hook(self, actor: Actor, type_name: str, obj: dict[str, Any]) -> None:
        try:
            validate_actor(actor)  # GP-01：身份在 _baseline §C
        except GraphError:
            self._decide("GP-01", "deny", actor=actor.id)
            raise
        if actor.is_agent and actor.name in QUARANTINE_AGENTS and obj.get("trust_zone") == "trusted":
            self._decide("GP-02", "deny", actor=actor.id, type=type_name)
            raise GraphError("GRAPH_TRUST_ZONE_VIOLATION", f"GP-02: quarantine agent {actor.id} may not write trusted "
                             f"{type_name}", rule_id="GP-02", path="/trust_zone")
        req_id = obj.get("id") if type_name == "Request" else obj.get("request_id")
        if actor.is_agent and actor.id != self.actor_id and isinstance(req_id, str) and req_id in self.halted_requests:
            self._decide("GP-09", "deny", reason="halted")
            raise GraphError("GRAPH_HALTED", f"request {req_id} is halted by guard", details={"request_id": req_id})
        now = self._clock()
        with self._lock:
            w = [t for t in self._writes.get(actor.id, []) if now - t < 60.0]
            w.append(now)
            self._writes[actor.id] = w
        if len(w) > self.config.write_rate_per_min:
            self._decide("GP-09", "deny", reason="rate")
            raise GraphError("GRAPH_RATE_LIMITED", f"{actor.id}: more than {self.config.write_rate_per_min} writes/min",
                             rule_id="GUARD")

    # ------------------------------------------------------------------ GP-02：读隔离
    def check_read(self, actor: Actor, type_name: str) -> None:
        if actor.is_agent and actor.name in QUARANTINE_AGENTS and type_name not in QUARANTINE_READ.get(actor.name, frozenset()):
            self._decide("GP-02", "deny", actor=actor.id, type=type_name)
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"GP-02: {actor.id} may not read {type_name}", rule_id="GP-02")

    # ------------------------------------------------------------------ GP-03：工具白名单（MCP readOnlyHint）
    def check_tool(self, agent: str, tool: str, annotations: dict[str, Any] | None = None, *, span: Span | None = None,
                   activity_scope: str | None = None, args: Any = None) -> str | None:
        ok = _tool_allowed(agent, tool)
        ann = annotations or {}
        zone_trusted = agent not in QUARANTINE_AGENTS
        if ok and zone_trusted and tool.split(".")[0] in ("erp", "docs", "file", "web") and ann.get("readOnlyHint") is not True:
            ok = False
        if not ok:
            self._decide("GP-03", "deny", span, agent=agent, tool=tool)
            if activity_scope:
                self.loops.call(activity_scope, f"agent:{agent}", "denied:" + tool, args)
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"GP-03: tool {tool} not whitelisted for {agent}", rule_id="GP-03")
        self._decide("GP-03", "allow", span, tool=tool)
        if activity_scope:
            sig = self.loops.call(activity_scope, f"agent:{agent}", tool, args)  # GP-10 / LD-01
            if span is not None:
                span.loop_sig = sig
            return sig
        return None

    # ------------------------------------------------------------------ GP-04：注入扫描
    def scan_external(self, text: str, *, zone: str = "quarantine", span: Span | None = None) -> list[str]:
        rep = scan(text, self.config.canaries)
        flags = rep.flags
        if flags:
            self._decide("GP-04", "flag", span, zone=zone)
            if span is not None:
                span.flags.extend(f for f in flags if f not in span.flags)
            if zone == "trusted":
                raise Halted("policy", "GP-04: instruction-like text inside trusted context")
        else:
            self._decide("GP-04", "allow", span)
        return flags

    # ------------------------------------------------------------------ GP-05：输出数字扫描
    def check_output_numbers(self, texts: Iterable[str], sources: Iterable[Any], *, span: Span | None = None,
                             path: str = "/value") -> None:
        allowed = allowed_numbers_deep(sources)
        bad = []
        for t in texts:
            for tok in all_tokens(t or ""):
                if not (_readings(tok) & allowed):
                    bad.append(tok.split("date:", 1)[-1])
        if bad:
            self._decide("GP-05", "deny", span, tokens=len(bad))
            raise GraphError("GRAPH_NUM_IN_TEXT", f"GP-05: unsourced numbers in generated text: {bad}", path=path,
                             rule_id="GP-05", details={"tokens": bad})
        self._decide("GP-05", "allow", span)

    # ------------------------------------------------------------------ GP-08：门户动作
    def check_portal_action(self, action: str, url_or_domain: str, allowed_domains: Iterable[str], *,
                            confirm_token: str | None = None, expected_token: str | None = None) -> None:
        host = re.sub(r"^https?://", "", url_or_domain or "", flags=re.I).split("/")[0].split(":")[0].lower()
        if action not in ("navigate", "fill", "select", "upload", "click", "submit"):
            self._decide("GP-08", "deny", action=action)
            raise Halted("policy", f"GP-08: portal action {action!r} not in whitelist")
        if not any(host == d or host.endswith("." + d) for d in allowed_domains):
            self._decide("GP-08", "deny", host=host)
            raise Halted("policy", f"GP-08: domain {host} not whitelisted for this request")
        if action == "submit" and (not confirm_token or confirm_token != expected_token):
            self._decide("GP-08", "deny", action="submit")
            raise Halted("policy", "GP-08: submit requires a human confirmation token")
        self._decide("GP-08", "allow", action=action)

    # ------------------------------------------------------------------ GP-10：进入 Activity/节点
    def enter(self, request_id: str | None, activity: str, args: Any = None, *, span: Span | None = None) -> None:
        if request_id and request_id in self.halted_requests:
            raise Halted("policy", f"request {request_id} is halted")
        self._decide("GP-10", "allow", span, activity=activity)

    def record_status(self, request_id: str, status: str) -> None:
        self.loops.status(request_id, status)  # LD-03

    def record_rejection(self, object_id: str | None, code: str) -> None:
        if object_id:
            self.loops.rejection(object_id, code)  # LD-02

    # ------------------------------------------------------------------ 中止 / 恢复
    def halt(self, org_id: str, request_id: str, reason: str, return_to: str, *, detail: str = "") -> str | None:
        """T20：Request → halted（agent:guard），冻结该 Request 的 Agent 写入与外发，写 AuditEvent policy.halt。"""
        self.halted_requests.add(request_id)
        self.store.record_audit(self.actor(org_id), "policy.halt", object_ref=request_id,
                                details={"reason": reason, "return_to": return_to, "detail": detail[:500],
                                         "policy_version": POLICY_VERSION})
        h = self.store.head(request_id, org_id=org_id)
        if h is None or h.status in ("halted", "submitted", "archived", "cancelled"):
            return None
        if h.trust_zone == "quarantine":
            # 晋升前（Request v1 quarantine，schema 只允许 received）不能落 halted 版本：只冻结（内存 + 审计），
            # 晋升/Agent 写入与外发照样被 GRAPH_HALTED / EGRESS_DENIED_HALTED 拦下
            return None
        reason_slug = reason if reason in ("budget_tokens", "budget_cost", "budget_steps", "budget_time", "loop", "policy") else "policy"
        if h.status == "awaiting_human":  # 侧路之间：return_to 取原 return_to
            from ..statemachines import parse_status_reason

            cur = self.store.get(request_id, org_id=org_id)
            info = parse_status_reason(cur.get("status_reason")) if cur else None
            return_to = info["return_to"] if info else return_to
        return self.store.write("Request", {"id": request_id, "status": "halted",
                                            "status_reason": f"halted:{reason_slug};return_to={return_to}"},
                                expected_version=h.version, actor=self.actor(org_id), patch_mode="merge_from_head")

    def release(self, request_id: str, budget_patch: dict[str, Any] | None = None) -> None:
        self.halted_requests.discard(request_id)
        self.budget(request_id).apply_patch(budget_patch)

    # ------------------------------------------------------------------ GP-09/GP-11 + 成本：模型调用包装
    def llm(self, client: LLMClient, *, agent: str, org_id: str, request_id: str | None = None,
            parent: Span | None = None) -> "GuardedLLM":
        return GuardedLLM(self, client, agent=agent, org_id=org_id, request_id=request_id, parent=parent)


class GuardedLLM:
    """``LLMClient`` 包装：pre-call（fail-closed、GP-11 驻留、GP-09 预算预检、GP-04 金丝雀）→ 调用 → post-output
    （金丝雀泄露扫描、记账、预算扣减）。每次调用一个 ``kind=llm`` 子 span。"""

    def __init__(self, guard: Guard, inner: LLMClient, *, agent: str, org_id: str, request_id: str | None,
                 parent: Span | None) -> None:
        self.guard, self.inner, self.agent, self.org_id, self.request_id, self.parent = guard, inner, agent, org_id, request_id, parent

    def complete(self, req: LLMRequest) -> LLMResponse:
        g = self.guard
        with g.recorder.span(f"agent:{self.agent}", self.org_id, parent=self.parent, request_id=self.request_id,
                             kind="llm", input_obj={"task": req.task, "pv": req.prompt_version}) as sp:
            sp.prompt_version = req.prompt_version
            if g.config.fail_closed:
                g._decide("GP-09", "deny", sp, reason="fail_closed")
                raise Halted("policy", "guard fail-closed: LLM calls are denied")
            if g.config.data_residency == "private" and req.data_residency != "private":
                req = replace(req, data_residency="private")  # GP-11：私有化租户强制本地路由
            g._decide("GP-11", "allow", sp, residency=req.data_residency)
            est = estimate_tokens(req.system) + estimate_tokens(req.user or str(req.data))
            st = g.budget(self.request_id)
            try:
                st.precheck_call(self.agent, est, req.max_tokens_out)
            except BudgetExceeded:
                g._decide("GP-09", "deny", sp, reason="precheck")
                raise
            t0 = time.monotonic()
            resp = self.inner.complete(req)
            dt_s = time.monotonic() - t0
            sp.model, sp.model_provider = resp.model, resp.provider
            sp.tokens_in, sp.tokens_out, sp.cost_eur = resp.tokens_in, resp.tokens_out, resp.cost_eur
            sp.cache = {"layer": "C4" if resp.cached else "none", "key": None}
            g.ledger.record(CostEntry(self.request_id, self.agent, req.tier, resp.model, resp.tokens_in, resp.tokens_out,
                                      resp.cost_eur, resp.cached))
            if contains_canary(resp.content, g.config.canaries):
                g._decide("GP-04", "deny", sp, reason="canary")
                raise Halted("policy", "canary string leaked into model output (LLM07)")
            ratios = st.charge(self.agent, tokens_in=resp.tokens_in, tokens_out=resp.tokens_out, cost_eur=resp.cost_eur,
                               steps=1, time_s=dt_s)
            sp.budget = {"scope": "request", "tokens_used": st.total.tokens, "tokens_limit": st.request_budget.tokens,
                         "cost_used": str(st.total.cost_eur), "cost_limit": str(st.request_budget.cost_eur)}
            g._decide("GP-09", "allow", sp, used_ratio=ratios.get("tokens", "0"))
            return resp


_NUMERIC_KEYS = ("value", "extracted_value", "corrected_value", "delta", "delta_ratio", "threshold")


def allowed_numbers_deep(sources: Iterable[Any]) -> set[str]:
    """GP-05 允许集合：来源对象中的数值字段、期间日期分量，以及来源文本字段中出现的数字 token（数字必须有来源）。"""
    out: set[str] = set()

    def add_tok(tok: str) -> None:
        out.update(_readings(tok))

    def walk(o: Any, key: str | None = None) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, bool) or o is None:
            return
        elif isinstance(o, (int, float, Decimal)):
            n = normalize_dec(o) if not isinstance(o, float) else str(o)
            if isinstance(n, str):
                out.add(n)
                out.add(n.lstrip("-"))
        elif isinstance(o, str):
            if key in ("start", "end", "valid_from", "valid_until", "due_date"):
                add_date(out, o)  # 整体日期 + 年份分量（月/日分量不单独放行）
            elif key in ("id", "ref", "content_hash", "blob_sha256", "provenance", "supersedes") or o.startswith(("sha256:", "dp:")):
                return
            else:
                add_text_tokens(out, o)
    for s in sources:
        walk(s)
    return out
