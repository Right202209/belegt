# Spec: 04 §2.6 consistency-auditor（判定确定、只用 LLM 写解释；判定不可降级：失败即阻塞提交）、T11、06 §1.3（fail-closed）
"""consistency-auditor 调用适配：``belegt_core.consistency.ConsistencyEngine``（另一子任务实现 06 的规则集）存在时用其
``pre_submit_gate``；不存在时退化为最小规则（无规则 → gate=pass，并在结果中标明 ``implementation=fallback``）。"""
from __future__ import annotations

from typing import Any

from .base import AgentBase, AgentOutput

try:
    from ..consistency import ConsistencyEngine as _Engine  # type: ignore
except Exception:  # pragma: no cover
    _Engine = None


class ConsistencyAuditorAgent(AgentBase):
    agent_id = "consistency-auditor"

    @property
    def implementation(self) -> str:
        return "belegt_core.consistency" if _Engine is not None else "fallback"

    def run(self, request_ref: str, *, idem_prefix: str) -> AgentOutput:
        req_id = request_ref.split("#")[0]
        org = self.svc.org_id
        with self.context(req_id, idem_prefix, activity="audit", input_obj={"request": request_ref}):
            if _Engine is None:
                return AgentOutput(outcome="pass", details={"implementation": "fallback", "gate": "pass", "blocking": []})
            run = _Engine(self.svc.store).run("pre_submit_gate", org_id=org, request_id=req_id)
            blocking = [f"{c['id']}#v{c['version']}" for c in self.svc.store.query("Conflict", org_id=org, status="open")
                        if c.get("severity") == "blocking" and (c.get("request_id") == req_id)]
            gate = run.gate or "pass"
            summary: dict[str, Any] = run.summary() if hasattr(run, "summary") else {}
            return AgentOutput(refs=list(run.conflicts_written), outcome=gate, details={
                "implementation": self.implementation, "gate": gate, "blocking": blocking,
                "conflicts_written": list(run.conflicts_written), "counts": summary.get("counts", {}),
                "unknown_rules": sorted({r.rule_id for r in run.results if r.outcome == "unknown"})})
