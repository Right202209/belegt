#!/usr/bin/env python3
# Spec: 01 UI-04（上传问卷 → 推进）、04 §3.2（T18/T19、T07/T08）、06 §4.4（首次外发审批）——冒烟前置：只经 HTTP API
"""把一份短问卷经 ``belegt serve --dev-auth`` 的 HTTP API 推进到 ``in_review``（收件箱里有待审批答案）。

规则与 demo 的"脚本化的人"一致：single_choice 选第一项；yes_no / numeric_confirm 答"是"；首次外发由 compliance_owner 批准；
awaiting_supplier 时以 ``belegt-supplier-submission/1`` 结构化提交模拟供应商门户回复。

    python prepare_backend.py http://127.0.0.1:8765 path/to/oem_fragebogen_kurz.csv  → 打印 JSON {request_id, states}
"""
from __future__ import annotations

import json
import sys
import time

import httpx

H = {"Authorization": "Bearer demo-owner"}
SUBMISSION = [{"datapoint_id": "dp:prod.cbam.see_direct", "value": "1.85", "unit": "tCO2e/t"},
              {"datapoint_id": "dp:prod.cbam.see_indirect", "value": "0.32", "unit": "tCO2e/t"},
              {"datapoint_id": "dp:prod.cbam.specific_electricity_consumption", "value": "0.55", "unit": "MWh/t"},
              {"datapoint_id": "dp:prod.cbam.carbon_price_paid", "value": "0", "unit": "EUR"}]


def main(base: str, csv_path: str) -> dict:
    c = httpx.Client(base_url=base, headers=H, timeout=60, trust_env=False)
    with open(csv_path, "rb") as f:
        r = c.post("/requests", files={"file": ("lieferantenfragebogen.csv", f.read(), "application/vnd.ms-excel")},
                   data={"subject": "Lieferanten-Fragebogen 2025 – Rückmeldung bis 15.12.2026", "sender": "einkauf@oem-a.example"})
    r.raise_for_status()
    req_id = r.json()["request"]["id"]
    states: list[str] = []
    for _ in range(60):
        d = c.get(f"/requests/{req_id}").json()
        wf = d["workflow"]
        st = wf.get("state")
        if st in (None, "scheduled", "running"):
            time.sleep(0.3)
            continue
        states.append(st)
        if st in ("in_review", "completed", "failed", "halted", "cancelled"):
            break
        body: dict = {}
        if st == "awaiting_supplier":
            sid = wf["pending"][0]
            blob = c.post("/blobs", content=json.dumps({"format": "belegt-supplier-submission/1", "sreq_id": sid,
                                                        "values": SUBMISSION}).encode(),
                          headers={"Content-Type": "application/json", "X-Filename": "submission.json"}).json()["blob_sha256"]
            body = {"signal": "supplier_responded", "payload": {"sreq_id": sid, "blob_sha256": blob, "kind": "submission"}}
        else:
            for ref in wf.get("pending") or []:
                oid = ref.split("#")[0]
                if oid.startswith("cq_"):
                    cq = c.get(f"/graph/objects/{oid}").json()
                    cq = cq.get("object", cq)
                    if cq["status"] != "open":
                        continue
                    ans = {"choice": cq["options"][0]["key"]} if cq["kind"] == "single_choice" else {"bool": True}
                    c.post(f"/closed-questions/{oid}/answer", json=ans).raise_for_status()
                elif oid.startswith("apr_"):
                    apr = c.get(f"/graph/objects/{oid}").json()
                    apr = apr.get("object", apr)
                    if apr["subject_ref"].startswith("sreq_") and apr["status"] == "pending":
                        c.post("/approvals", json={"subject_id": apr["subject_ref"].split("#")[0], "decision": "approved",
                                                   "kind": "supplier_request_first_send"}).raise_for_status()
        c.post(f"/requests/{req_id}/advance", json=body).raise_for_status()
    return {"request_id": req_id, "states": states}


if __name__ == "__main__":
    out = main(sys.argv[1], sys.argv[2])
    print(json.dumps(out))
    sys.exit(0 if out["states"] and out["states"][-1] == "in_review" else 1)
