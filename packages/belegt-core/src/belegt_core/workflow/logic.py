# Spec: 04 §3（问卷级状态机：T02…T17 主路径、T18/T19 awaiting_human、T07/T08 awaiting_supplier、T20/T21 halted 及 return_to、
#       题级并行与"最慢关口"、预取）、§4.3 RequestWorkflow 伪代码
"""问卷级工作流逻辑（LocalRunner 与 Temporal ``RequestWorkflow`` 共用；只依赖标准库，确定性）。

``wf`` 提供：``act(name, *args)``（Activity，结果只含 ObjectRef/计数）、``child(kind, *args)``（每 25 题一个批次：LocalRunner 中
顺序执行，Temporal 中为 ``QuestionBatchWorkflow`` 子工作流并行）、``set_status``、``wait_human``（T18/T19）、``wait_approvals``、
``wait_supplier``（T07/T08 + SupplierChaseWorkflow）、``sleep_until``（timer）、``halt``（T20，等 resume 信号）、
``progress``（query）。
题级并行：映射与起草按批扇出；"最慢关口"——请求级状态只在全部批次越过关口后推进；等人时先做不依赖人的预取（evidence）。
"""
from __future__ import annotations

from typing import Any

MAX_EVIDENCE_ROUNDS = 8  # 每轮至多一次人工等待；供应商回复后的核验与重算还需要一轮（LD-02 兜底）
MAX_REVIEW_ROUNDS = 4


async def request_workflow(wf: Any, req_ref: str) -> dict[str, Any]:
    req_id = req_ref.split("#")[0]
    wf.progress(phase="promote", request_id=req_id)
    # T02：PROMO-REQ-0.90（或人确认）
    p = await wf.act("promote_request", req_ref)
    if not p["promoted"]:
        await wf.wait_human("received", p["cq_refs"], "confirm")
        p = await wf.act("promote_request_confirmed", req_ref, p["cq_refs"])
        if p.get("cancelled"):
            return {"status": "cancelled"}
    # T04：hints（trusted 检索）→ question-parser（隔离区）→ PROMO-Q-STRUCT
    wf.progress(phase="parse")
    hints = await wf.act("build_hints", req_ref)
    parsed = await wf.act("parse_questions", req_ref, hints["hints_ref"])
    qp = await wf.act("promote_questions", req_ref)
    if qp["failed"]:
        await wf.wait_human("triaged", qp["cq_refs"], "confirm")
        await wf.act("promote_questions", req_ref)
    await wf.set_status("parsed")
    # 映射晋升：每 25 题一个批次（QuestionBatchWorkflow.map）
    wf.progress(phase="map", questions=parsed["questions"])
    batches = (await wf.act("question_batches", req_ref))["batches"]
    blocking: list[str] = []
    for b in await wf.fan_out("map", req_ref, batches):
        blocking += b["cq_refs"] + b["unmapped_refs"]
    if blocking:
        await wf.act("prefetch_evidence", req_ref)  # 等人时先预取已确认 DataPoint 的证据（不依赖人的工作先做）
        await wf.wait_human("parsed", blocking, "cq")
        await wf.act("finalize_mappings", req_ref)
    await wf.set_status("mapped")             # T05（guard：全部 Question mapped）
    await wf.set_status("evidence_gathering")  # T06
    # 取证 + 计算（数字通道）；封闭式确认/方法论决策/缺口定性
    wf.progress(phase="evidence")
    supplier_done = False
    for _round in range(MAX_EVIDENCE_ROUNDS):
        g = await wf.act("gather_evidence", req_ref, supplier_done)
        wf.progress(evidence=g["counts"])
        if g["cq_refs"]:
            await wf.wait_human("evidence_gathering", g["cq_refs"], "cq")
            continue
        if g["supplier_gaps"] and not supplier_done:
            ch = await wf.act("chase_plan", req_ref, g["supplier_gaps"])
            if ch["sreq_refs"]:
                await wf.wait_human("evidence_gathering", ch["approval_refs"], "confirm")  # 首次外发人确认
                sent = await wf.act("chase_send", req_ref, ch["sreq_refs"])
                while sent["deferred"]:
                    await wf.sleep_until(sent["retry_after"])  # EGRESS_DENIED_QUIET_PERIOD / RATE_LIMITED：按 retry_after 重提
                    sent = await wf.act("chase_send", req_ref, sent["deferred"])
                if sent["sent"]:
                    await wf.set_status("awaiting_supplier")  # T07
                    rep = await wf.wait_supplier(sent["sent"], ch["due_date"])
                    await wf.act("review_replies", req_ref, rep)
                    await wf.set_status("evidence_gathering")  # T08
            supplier_done = True
            continue
        if g["internal_gaps"] and not g.get("gaps_confirmed"):
            cq = await wf.act("confirm_gaps", req_ref, g["internal_gaps"])
            await wf.wait_human("evidence_gathering", cq["cq_refs"], "confirm")
            continue
        break
    await wf.set_status("drafting")  # T09
    # 起草 → 审计 → 审批（T10…T14，含 T12/T13 回退）
    for _round in range(MAX_REVIEW_ROUNDS):
        wf.progress(phase="draft")
        drafted = await wf.fan_out("draft", req_ref, batches)
        wf.progress(drafted=sum(d["answers"] for d in drafted), pending_manual=sum(d["pending_manual"] for d in drafted))
        await wf.set_status("auditing")  # T10
        wf.progress(phase="audit")
        while True:
            a = await wf.act("audit", req_ref)
            if a["blocking"]:
                await wf.wait_human("auditing", a["blocking"], "conflict")
                continue
            if a["gate"] == "unknown":
                await wf.halt("policy", "auditing", "consistency gate unknown (fail-closed, 06 §1.3)")
                continue
            break
        r = await wf.act("route_approvals", req_ref)
        await wf.set_status("in_review")  # T11
        wf.progress(phase="review", approvals=len(r["approval_refs"]), levels=r["levels"])
        outcome = await wf.wait_approvals(r["approval_refs"])
        if outcome == "approved":
            break
        if outcome == "modified":
            await wf.set_status("auditing")  # T12
            continue
        await wf.set_status("drafting")      # T13
    await wf.set_status("approved")  # T14
    await wf.set_status("filling")   # T15
    wf.progress(phase="fill")
    f = await wf.act("fill_file", req_ref)
    await wf.wait_human("filling", f["cq_refs"], "confirm")
    await wf.act("mark_submitted", req_ref, f["export_input_hash"])  # T16
    await wf.act("archive_learn", req_ref)
    await wf.set_status("archived")  # T17
    wf.progress(phase="archived")
    return {"status": "archived", "export_blob": f["export_blob"]}


async def question_batch(wf: Any, kind: str, req_ref: str, question_ids: list[str]) -> dict[str, Any]:
    """QuestionBatchWorkflow：每 25 题一个批次（map: PROMO-MAP-0.85 / CQ-MAP-BAND；draft: answer-drafter）。"""
    if kind == "map":
        return await wf.act("promote_mappings_batch", req_ref, question_ids)
    return await wf.act("draft_answers_batch", req_ref, question_ids)
