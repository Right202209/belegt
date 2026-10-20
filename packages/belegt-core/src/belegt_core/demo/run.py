# Spec: CLI `belegt demo`：用 examples/oem_fragebogen_de_100.xlsx 跑 收件→解析映射→取证→计算→起草→审计→待审批
#       （→ 脚本化审批 → approved → filling（filler-file 回填）→ submitted → archived），输出摘要、回填文件与审计包
"""演示驱动：上传问卷（T01 intake-triage）→ ``LocalRunner`` 推进；遇到人工节点由 ``DemoHuman``（脚本化规则，见 human.py）作答、
遇到 awaiting_supplier 由模拟供应商经门户提交（``supplier_replied`` 信号）。

- ``run_demo(db_url=None, out=None)``：端到端跑到 submitted（T17 学习回写后 archived），返回 ``DemoSummary``；``out`` 给出时写
  ``<out>/filled_<原文件名>``（回填 xlsx）、``<out>/auditpack_<req>.zip``（03 §7 审计包，已核验）、``summary.json``、``summary.txt``。
- ``run_demo(..., until="in_review")``：只跑到待审批（旧行为）。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..agents.intake_triage import IntakeInput, IntakeTriage
from .human import DemoHuman
from .setup import DemoEnv, build_env, find_example

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_BY_SUFFIX = {".xlsx": XLSX, ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12", ".csv": "text/csv",
                  ".pdf": "application/pdf",
                  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def mime_for(filename: str) -> str:
    """按扩展名取 MIME（未知扩展名按 xlsx 处理）。"""
    return MIME_BY_SUFFIX.get(Path(filename).suffix.lower(), XLSX)


def upload(env: DemoEnv, data: bytes, filename: str, *, mime: str | None = None, subject: str | None = None, body: str = "",
           sender: str = "einkauf@oem-a.example", channel: str = "email") -> str:
    mime = mime or mime_for(filename)
    st = env.store
    sha = st.put_blob(env.org_id, data, mime)
    subj = subject or "Lieferanten-Nachhaltigkeitsfragebogen 2025 – Rückmeldung bis 15.12.2026"
    out = IntakeTriage(env.svc).run(IntakeInput([sha], channel=channel, subject=subj, body=body or
                                                "Sehr geehrte Damen und Herren, anbei unser jährlicher Fragebogen.",
                                                sender=sender, message_id=f"<{sha[7:19]}@oem-a.example>",
                                                filenames=[filename]),
                                    idem_prefix=f"intake:{sha[7:31]}")
    return out.refs[0]


def simulate_supplier_reply(env: DemoEnv, sreq_ids: list[str], *, reply_text: str | None = None) -> None:
    """模拟供应商经门户提交结构化回复（``belegt-supplier-submission/1``）；``reply_text`` 给出时模拟邮件正文回复。"""
    for sid in sreq_ids:
        if reply_text is not None:
            blob = env.store.put_blob(env.org_id, reply_text.encode("utf-8"), "text/plain")
            payload = {"sreq_id": sid, "blob_sha256": blob, "kind": "email"}
        else:
            sub = {"format": "belegt-supplier-submission/1", "values": env.tenant["supplier_submission"], "sreq_id": sid}
            blob = env.store.put_blob(env.org_id, json.dumps(sub).encode("utf-8"), "application/json")
            payload = {"sreq_id": sid, "blob_sha256": blob, "kind": "submission"}
        env.runner.journal.add_signal(env.runner.workflow_id(_req_of(env, sid)), "supplier_replied", payload)


def _req_of(env: DemoEnv, sreq_id: str) -> str:
    for r in env.runner.journal.runs():
        if r["state"] == "awaiting_supplier" and sreq_id in (r.get("pending") or []):
            return r["request_id"]
    return env.runner.journal.runs()[-1]["request_id"]


def drive(env: DemoEnv, req_ref: str, *, human: DemoHuman | None = None, stop_states: tuple[str, ...] = ("in_review",),
          max_steps: int = 40) -> tuple[Any, list[dict[str, Any]]]:
    """推进工作流直到 ``stop_states``/终态，或人无法再推进（未处置的冲突 → 停在 awaiting_human）。"""
    human = human or DemoHuman(env.store, env.user("owner"), executive=env.user("exec"), clock=env.clock)
    trail: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    res = env.runner.run(req_ref)
    for _ in range(max_steps):
        trail.append({"state": res.state, "pending": len(res.pending), "status": res.progress.get("status"),
                      "t_s": round(time.perf_counter() - t0, 2)})
        if res.state in stop_states or res.state in ("completed", "failed", "cancelled", "halted"):
            break
        if res.state in ("awaiting_human", "in_review"):
            if human.resolve(res.pending) == 0:
                break  # 人按规则不处理（如 H-OTH 未处置的阻断冲突）→ 停住，摘要里如实列出
        elif res.state == "awaiting_supplier":
            simulate_supplier_reply(env, res.pending)
        elif res.state == "timer":
            env.clock.advance(hours=1)
        res = env.runner.run(req_ref)
    return res, trail


@dataclass
class DemoSummary:
    """``run_demo`` 的结果（``to_dict()`` 供 JSON；``format()`` 供 CLI）。"""
    request_id: str
    request_status: str
    workflow_state: str
    reached: list[str]                       # Request 经历过的状态（去重、按序）
    questions: dict[str, Any]
    evidence: dict[str, Any]
    calculations: list[dict[str, str]]
    scope: dict[str, str | None]             # scope1 / scope2_location / scope2_market（tCO2e）
    answers: dict[str, Any]
    approvals: dict[str, Any]
    conflicts: list[dict[str, Any]]
    closed_questions: dict[str, Any]
    supplier_requests: list[dict[str, Any]]
    emails_sent: int
    llm: dict[str, Any]
    trace_spans: int
    human_actions: dict[str, int]
    trail: list[dict[str, Any]]
    duration_s: float
    export: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), default=str))

    def __getitem__(self, k: str) -> Any:  # 兼容旧的 dict 摘要用法
        if k == "request":
            return {"id": self.request_id, "status": self.request_status, "workflow_state": self.workflow_state}
        return getattr(self, k)

    def format(self) -> str:
        return format_summary(self)


def _history_statuses(env: DemoEnv, req_id: str) -> list[str]:
    out: list[str] = []
    for v in env.store.history(req_id, org_id=env.org_id):
        s = v["status"] if isinstance(v, dict) else getattr(v, "status", None)
        if s and (not out or out[-1] != s):
            out.append(s)
    return out


def summarize(env: DemoEnv, req_ref: str, res: Any, trail: list[dict[str, Any]], human: DemoHuman | None = None,
              duration_s: float = 0.0) -> DemoSummary:
    st, org = env.store, env.org_id
    req_id = req_ref.split("#")[0]
    req = st.get(req_id, org_id=org)
    qs = st.query("Question", org_id=org, request_id=req_id)
    qids = {q["id"] for q in qs}
    maps = [m for m in st.query("Mapping", org_id=org, status="confirmed") if m["question_id"] in qids]
    methods: dict[str, int] = {}
    for m in maps:
        k = (m.get("promotion") or {}).get("rule") or m["method"]
        k = "PROMO-MAP-0.85" if k == "PROMO-MAP-0.85" else ("human/CQ" if k == "human" or m["method"] == "human" else k)
        methods[k] = methods.get(k, 0) + 1
    answers = [a for a in st.query("Answer", org_id=org) if set(a.get("question_refs") or []) & qids]
    ans_status: dict[str, int] = {}
    for a in answers:
        ans_status[a["status"]] = ans_status.get(a["status"], 0) + 1
    answered_q = {q for a in answers for q in a.get("question_refs") or []}
    apr = [a for a in st.query("Approval", org_id=org) if a.get("request_id") == req_id or a["subject_ref"].split("#")[0] in
           {x["id"] for x in answers}]
    levels: dict[str, int] = {}
    apr_status: dict[str, int] = {}
    for a in apr:
        if a["subject_ref"].startswith("ans_"):
            levels[a["risk_level"]] = levels.get(a["risk_level"], 0) + 1
        apr_status[a["status"]] = apr_status.get(a["status"], 0) + 1
    calcs = st.query("Calculation", org_id=org)
    scope: dict[str, str | None] = {"scope1": None, "scope2_location": None, "scope2_market": None}
    key = {"ghg.scope1.total": "scope1", "ghg.scope2.location_based": "scope2_location", "ghg.scope2.market_based": "scope2_market"}
    for c in calcs:
        if c["method_id"] in key:
            scope[key[c["method_id"]]] = f"{c['result']['value']} {c['result']['unit']}"
    evs = st.query("Evidence", org_id=org)
    ev_by: dict[str, int] = {}
    for e in evs:
        k = f"{e['source_type']}:{e['status']}"
        ev_by[k] = ev_by.get(k, 0) + 1
    handled = {h.get("ref", "").split("#")[0]: h for h in (human.log if human else []) if h.get("kind") == "conflict"}
    conflicts = []
    for c in sorted(st.query("Conflict", org_id=org), key=lambda c: (c["rule_id"], c["id"])):
        h = handled.get(c["id"], {})
        conflicts.append({"id": c["id"], "rule_id": c["rule_id"], "severity": c["severity"], "status": c["status"],
                          "datapoint": c.get("datapoint_id"), "handling": h.get("rule") or (
                              "H-CWRN (warn_note bei Freigabe)" if c["severity"] == "warning" else None),
                          "resolution": (c.get("resolution") or {}).get("kind"),
                          "explanation_de": (c.get("explanation") or {}).get("de")})
    cqs = [c for c in st.query("ClosedQuestion", org_id=org)]
    sreqs = st.query("SupplierRequest", org_id=org)
    costs = env.ledger.totals(req_id)
    budget = env.svc.guard.budget(req_id)
    num_answers = [a for a in answers if a["value_type"] == "number"]
    eff = [a for a in answers if a["status"] in ("submitted", "approved", "in_review", "draft")]
    return DemoSummary(
        request_id=req_id, request_status=req["status"], workflow_state=res.state, reached=_history_statuses(env, req_id),
        questions={"total": len(qs), "mapped": sum(1 for q in qs if q["status"] in ("mapped", "answered")), "mapping_by": methods,
                   "status": _count([q["status"] for q in qs])},
        evidence={"total": len(evs), "by_source_status": ev_by},
        calculations=[{"method": c["method_id"], "result": f"{c['result']['value']} {c['result']['unit']}",
                       "status": c["status"]} for c in calcs],
        scope=scope,
        answers={"total": len(answers), "effective": len(eff), "by_status": ans_status, "numbers": len(num_answers),
                 "numbers_with_ref": sum(1 for a in num_answers if a.get("evidence_refs") or a.get("calculation_ref")),
                 "questions_answered": len(answered_q & qids), "questions_pending_manual": len(qids - answered_q)},
        approvals={"by_status": apr_status, "pending": apr_status.get("pending", 0), "answer_by_level": levels},
        conflicts=conflicts,
        closed_questions={"total": len(cqs), "answered": sum(1 for c in cqs if c["status"] == "answered"),
                          "by_kind": _count([c["kind"] for c in cqs])},
        supplier_requests=[{"id": s["id"], "status": s["status"], "template": s["template_id"], "datapoints": len(s["datapoint_ids"])}
                           for s in sreqs],
        emails_sent=len(env.transport.sent),
        llm={"calls": costs["calls"], "tokens_in": costs["tokens_in"], "tokens_out": costs["tokens_out"],
             "cost_eur": str(Decimal(costs["cost_eur"]).quantize(Decimal("0.0001"))), "target_eur_p95": "3", "hard_limit_eur": "5",
             "budget_warnings": budget.warnings, "by_agent": {k: str(Decimal(v["cost_eur"]).quantize(Decimal("0.0001")))
                                                              for k, v in env.ledger.by_agent(req_id).items()}},
        trace_spans=len(env.svc.guard.recorder.for_request(req_id)),
        human_actions=human.counts() if human else {},
        trail=trail, duration_s=round(duration_s, 2),
    )


def summary(env: DemoEnv, req_ref: str, res: Any, trail: list[dict[str, Any]], human: DemoHuman | None = None) -> dict[str, Any]:
    """旧接口：dict 摘要。"""
    return summarize(env, req_ref, res, trail, human).to_dict()


def _count(xs: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def write_outputs(env: DemoEnv, s: DemoSummary, out: str | Path, source_name: str) -> dict[str, str]:
    """回填 xlsx（filler-file 的导出 blob）、审计包（核验后）、摘要。"""
    from ..auditpack import export_audit_pack, verify_audit_pack

    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    st, org = env.store, env.org_id
    export_blob = None
    for e in env.runner.journal.entries(env.runner.workflow_id(s.request_id)):
        if e["name"] == "fill_file" and e["kind"] == "activity":
            export_blob = (e["result"] or {}).get("export_blob")
            s.export = {k: v for k, v in (e["result"] or {}).items() if k != "cq_refs"}
    if export_blob:
        b = st.get_blob(org, export_blob)
        if b:
            p = d / f"filled_{source_name}"
            p.write_bytes(b[0])
            paths["filled_file"] = str(p)
    data, manifest = export_audit_pack(st, s.request_id, actor=env.user("owner"))
    p = d / f"auditpack_{s.request_id}.zip"
    p.write_bytes(data)
    paths["auditpack"] = str(p)
    ver = verify_audit_pack(data)
    s.export["auditpack_verified"] = bool(ver.get("ok"))
    s.export["auditpack_problems"] = ver.get("problems", [])[:10]
    s.export["auditpack_files"] = len(manifest.get("files") or []) if isinstance(manifest, dict) else None
    s.outputs = paths
    (d / "summary.json").write_text(json.dumps(s.to_dict(), ensure_ascii=False, indent=2), "utf-8")
    (d / "summary.txt").write_text(format_summary(s), "utf-8")
    paths["summary"] = str(d / "summary.json")
    s.outputs = paths
    return paths


def run_demo(db_url: str | None = None, out: str | Path | None = None, *, path: str | Path | None = None,
             env: DemoEnv | None = None, until: str = "submitted") -> DemoSummary:
    """端到端 demo。``until="in_review"`` 停在待审批；默认 ``"submitted"``：脚本化审批 → filler-file 回填 → 提交确认 →
    submitted（随后 T17 学习回写 → archived）。``db_url`` 例如 ``sqlite+pysqlite:////tmp/belegt-demo.db``（默认内存）。"""
    t0 = time.perf_counter()
    env = env or (build_env(url=db_url) if db_url else build_env())
    p = Path(path) if path else find_example("oem_fragebogen_de_100.xlsx")
    req_ref = upload(env, p.read_bytes(), p.name)
    full = until != "in_review"
    human = DemoHuman(env.store, env.user("owner"), executive=env.user("exec"), approve_answers=full, submit=full, clock=env.clock)
    res, trail = drive(env, req_ref, human=human, stop_states=("in_review",) if not full else ())
    s = summarize(env, req_ref, res, trail, human, duration_s=time.perf_counter() - t0)
    env.last_human = human  # type: ignore[attr-defined]  # 供测试/调试检查规则日志
    if out is not None:
        write_outputs(env, s, out, p.name)
        s.duration_s = round(time.perf_counter() - t0, 2)
        (Path(out) / "summary.json").write_text(json.dumps(s.to_dict(), ensure_ascii=False, indent=2), "utf-8")
    return s


def format_summary(s: Any) -> str:
    if isinstance(s, dict):  # 旧 dict 摘要
        s = DemoSummary(**{k: s.get(k) for k in DemoSummary.__dataclass_fields__ if k in s}) if "request_id" in s else s
    L = []
    L.append(f"Request {s.request_id}  status={s.request_status}  workflow={s.workflow_state}  ({s.duration_s} s)")
    L.append("  Status-Verlauf: " + " → ".join(s.reached))
    q = s.questions
    L.append(f"Fragen: {q['total']} (mapped {q['mapped']})  Mapping: {q['mapping_by']}")
    L.append(f"Evidenz: {s.evidence['total']}  {s.evidence['by_source_status']}")
    for c in s.calculations:
        L.append(f"  Berechnung {c['method']}: {c['result']}")
    a = s.answers
    L.append(f"Antworten: {a['total']} {a['by_status']}  Zahlen: {a['numbers']} (mit Evidenz/Berechnung: {a['numbers_with_ref']})")
    L.append(f"  Fragen beantwortet: {a['questions_answered']}  ohne Beleg (manuell): {a['questions_pending_manual']}")
    L.append(f"Freigaben: {s.approvals['by_status']}  Antworten nach Stufe: {s.approvals['answer_by_level']}")
    L.append(f"Konflikte: {len(s.conflicts)}")
    for c in s.conflicts:
        L.append(f"  {c['rule_id']} {c['severity']} → {c['status']} ({c['handling'] or '-'}) {c['datapoint'] or ''}")
    L.append(f"Geschlossene Fragen: {s.closed_questions['total']} (beantwortet {s.closed_questions['answered']})")
    for sr in s.supplier_requests:
        L.append(f"Lieferantenanfrage {sr['id']}: {sr['status']} ({sr['template']}, {sr['datapoints']} Datenpunkte)")
    L.append(f"E-Mails über egress: {s.emails_sent}")
    llm = s.llm
    L.append(f"LLM: {llm['calls']} Aufrufe, {llm['tokens_in']}+{llm['tokens_out']} Tokens, Kosten €{llm['cost_eur']} "
             f"(Ziel P95 ≤ €{llm['target_eur_p95']}, Hard-Limit €{llm['hard_limit_eur']})")
    L.append(f"Trace-Spans: {s.trace_spans}  Menschliche Aktionen (Regeln): {s.human_actions}")
    if s.export:
        L.append(f"Export: geschrieben {s.export.get('written')}  abgelehnt {len(s.export.get('rejected') or [])}  "
                 f"Auditpaket verifiziert: {s.export.get('auditpack_verified')}")
    for k, v in (s.outputs or {}).items():
        L.append(f"  {k}: {v}")
    L.append("Workflow: " + " → ".join(f"{t['state']}" for t in s.trail))
    return "\n".join(L)
