# Spec: 03 §3.11（ClosedQuestion 作答 → 派生写入）、06 §3.2（冲突处置 A/B/C）、§4.3（单条裁决的确认项）、§5（批量 + 撤销窗口）、
#       04 §3.2 T18/T19、§2.1（unmapped 由人映射）
"""演示用"脚本化的人"：按**明文规则**处理待处理的人工项（只在 demo/测试中使用，真实系统由人在 UI 中操作）。

规则（每条都写进 ``log``，demo 摘要按规则汇总）：

H-CQ   ClosedQuestion：single_choice 选第一项（映射 Top-1 / 方法论第一选项）；yes_no / numeric_confirm 答"是"。
H-MAP  unmapped Question：登记租户自定义 DataPoint（``dp:custom.q.n<ordinal>``）并由人确认 Mapping。
H-SREQ SupplierRequest 首次外发：compliance_owner 经 ``ApprovalRouter.decide_supplier_first_send`` 批准。
H-C032 CR-032（枚举硬约束）阻断冲突：若答案值能按枚举标签/人工同义词表唯一对应到一个枚举码 → 处置 A（统一口径）：
       人写更正版本（``origin=human``，去掉 ``reused_from``）后 ``resolve_conflict(kind="corrected")``；否则不处理（摘要列为未处置）。
H-CWRN warning 冲突（如 CR-004/CR-013 证书即将到期）不在冲突层处置；审批时必须带 ``warn_note``（06 §4.3），写明续证计划。
H-OTH  其它 blocking 冲突：**不**自动驳回或接受差异（不掩盖），记为未处置，工作流停在 awaiting_human。
H-APR  答案审批（``approve_answers=True`` 时）：先按 06 §5.1 检查批量条件，满足者经 ``BatchStager`` 暂存并在撤销窗口结束后提交；
       其余单条 ``ApprovalRouter.decide``，确认项按规则给出：估算 → ``estimate_ack``；有 warning 冲突 → ``warn_note``；
       L3 → 策略要求的全部确认项（step-up/条款确认），角色要求只含 executive 时由 executive 裁决。
H-SUB  提交确认（``submit-confirm`` 卡）：``submit=True`` 时答"是"。
"""
from __future__ import annotations

from typing import Any

from ..authz import Actor
from ..datapoints import DataPointInfo
from ..store import GraphStore, WriteItem

# 人工同义词表（演示租户的"人"的领域知识）：德文生产路线名称 → CBAM 枚举码
ENUM_SYNONYMS: dict[str, dict[str, str]] = {
    "dp:prod.cbam.production_route": {"elektrolichtbogenofen": "eaf", "lichtbogenofen": "eaf", "hochofen": "bf_bof",
                                      "hochofen konverter": "bf_bof", "direktreduktion": "dri_eaf"},
}
WARN_NOTE = ("Zertifikat läuft vor Ende des geforderten Zeitraums ab; Rezertifizierungsaudit ist beauftragt, "
             "Nachweis wird nach Erneuerung nachgereicht.")


def _norm(s: Any) -> str:
    from ..llm.embedding import normalize

    return normalize(str(s or ""))


class DemoHuman:
    def __init__(self, store: GraphStore, user: Actor, *, approve_answers: bool = False, executive: Actor | None = None,
                 submit: bool = False, clock: Any = None, use_batches: bool = True) -> None:
        self.store, self.user, self.approve_answers, self.executive = store, user, approve_answers, executive
        self.submit, self.clock, self.use_batches = submit, clock, use_batches
        self.log: list[dict[str, Any]] = []
        self._router = None
        self._stager = None

    @property
    def org(self) -> str:
        return self.user.org_id

    def now(self) -> str:
        return self.store.peek_now()[:19] + "Z"

    @property
    def router(self) -> Any:
        if self._router is None:
            from ..approval import ApprovalRouter

            self._router = ApprovalRouter(self.store)
        return self._router

    @property
    def stager(self) -> Any:
        if self._stager is None:
            from ..approval import BatchStager
            from ..consistency import ConsistencyEngine

            self._stager = BatchStager(self.store, self.router, ConsistencyEngine(self.store), clock=self.clock)
        return self._stager

    # ------------------------------------------------------------------ 入口
    def resolve(self, refs: list[str]) -> int:
        n = 0
        apr_answers = []
        for r in refs:
            oid = r.split("#")[0]
            o = self.store.get(oid, org_id=self.org)
            if o is None:
                continue
            if oid.startswith("cq_") and o["status"] == "open":
                if "submit-confirm" in (o.get("tags") or []) and not self.submit:
                    self.log.append({"kind": "submit_held", "ref": r})
                    continue
                self.answer_cq(o)
                n += 1
            elif oid.startswith("q_") and o["status"] == "unmapped":
                self.map_question(o)
                n += 1
            elif oid.startswith("apr_") and o["status"] == "pending":
                if o["subject_ref"].startswith("sreq_"):
                    self.approve_first_send(o)
                    n += 1
                elif self.approve_answers and o["subject_ref"].startswith("ans_"):
                    apr_answers.append(o)
            elif oid.startswith("cfl_") and o["status"] == "open":
                n += self.handle_conflict(o)
        if apr_answers:
            n += self.approve_all(apr_answers)
        return n

    # ------------------------------------------------------------------ H-CQ / H-MAP / H-SREQ
    def answer_cq(self, cq: dict[str, Any]) -> str:
        ans: dict[str, Any] = {"answered_by": self.user.id, "answered_at": self.now()}
        if cq["kind"] == "single_choice":
            ans["choice"] = cq["options"][0]["key"]
        elif cq["kind"] == "multi_choice":
            ans["choices"] = [cq["options"][0]["key"]]
        else:  # yes_no / numeric_confirm / 其它
            ans["bool"] = True
        ref = self.store.write("ClosedQuestion", {"id": cq["id"], "status": "answered", "answer": ans}, expected_version=cq["version"],
                               actor=self.user, patch_mode="merge_from_head")
        rule = "H-SUB" if "submit-confirm" in (cq.get("tags") or []) else "H-CQ"
        self.log.append({"kind": "cq", "rule": rule, "cq_kind": cq["kind"], "ref": ref, "tags": cq.get("tags") or []})
        return ref

    def map_question(self, q: dict[str, Any]) -> list[str]:
        reg = self.store.datapoints
        vt = {"number": "number", "bool": "bool", "enum": "enum", "date": "date", "file": "file"}.get(q["answer_format"], "text")
        unit = q.get("unit_requested") if vt == "number" else None
        if vt == "number" and not unit:
            unit = "1"
        base = f"dp:custom.q.n{q['ordinal']:03d}"
        if not reg.versions(base):
            reg.register(DataPointInfo(id=base, semver="1.0.0", value_type=vt, unit=unit, dimension=None,
                                       allowed_boundaries=("organization",), consolidation=None, requires_consolidation=False,
                                       sensitivity="customer_shared", allowed_units=(unit,) if unit else (), status="active",
                                       extra={"label": q["text"], "tenant_custom": True,
                                              "accepted_source_types": ["document", "erp", "historical_answer", "human_confirmation"]}))
        m: dict[str, Any] = {"question_id": q["id"], "datapoint_id": f"{base}@1.0.0", "confidence": 1.0, "rank": 1, "method": "human",
                             "status": "confirmed", "boundary_hint": "organization"}
        if q.get("period_requested"):
            m["period_hint"] = q["period_requested"]
        refs = self.store.write_batch([
            WriteItem("Mapping", m),
            WriteItem("Question", {"id": q["id"], "status": "mapped"}, expected_version=q["version"], patch_mode="merge_from_head"),
        ], actor=self.user)
        self.log.append({"kind": "map", "rule": "H-MAP", "question": q["id"], "datapoint": base})
        return refs

    def approve_first_send(self, apr: dict[str, Any]) -> None:
        sid = apr["subject_ref"].split("#")[0]
        try:
            self.router.decide_supplier_first_send(sid, actor=self.user)
        except Exception:  # noqa: BLE001 - 退回到直接落地（与旧行为一致）
            self.decide(apr, "approved")
        self.log.append({"kind": "approval", "rule": "H-SREQ", "subject": "sreq", "decision": "approved", "level": apr["risk_level"]})

    # ------------------------------------------------------------------ 冲突（H-C032 / H-CWRN / H-OTH）
    def handle_conflict(self, c: dict[str, Any]) -> int:
        if c.get("severity") != "blocking":
            self.log.append({"kind": "conflict", "rule": "H-CWRN", "rule_id": c["rule_id"], "ref": f"{c['id']}#v{c['version']}",
                             "action": "warn_note_at_approval"})
            return 0
        if c["rule_id"] == "CR-032" and self._correct_enum(c):
            return 1
        if c["rule_id"] == "CR-007" and self._unify_encoding(c):
            return 1
        self.log.append({"kind": "conflict", "rule": "H-OTH", "rule_id": c["rule_id"], "ref": f"{c['id']}#v{c['version']}",
                         "action": "unresolved"})
        return 0

    def _correct_enum(self, c: dict[str, Any]) -> bool:
        from ..consistency import resolve_conflict
        from ..consistency.meta import MetaProvider

        ans_refs = [s for s in c.get("subject_refs") or [] if s.startswith("ans_")]
        if len(ans_refs) != 1:
            return False
        a = self.store.get(ans_refs[0].split("#")[0], org_id=self.org)
        if a is None or a.get("value_type") != "enum":
            return False
        base = a["datapoint_id"].split("@")[0]
        meta = MetaProvider.default().get(base)
        codes = list(meta.enum_values or ()) if meta else []
        code = self._enum_code(base, a["value"], codes)
        if code is None:
            return False
        if a["value"] != code:
            patch = {"id": a["id"], "value": code, "origin": "human", "reused_from": None, "drafting_note": None,
                     "evidence_refs": None}
            self.store.write("Answer", patch, expected_version=a["version"], actor=self.user, patch_mode="merge_from_head")
        res = resolve_conflict(self.store, c["id"], actor=self.user, kind="corrected",
                               note=f"Freitext '{a['value']}' auf Enumerationscode '{code}' vereinheitlicht (06 §3.2 A).")
        self._set_enum_map(a, code)
        self.log.append({"kind": "conflict", "rule": "H-C032", "rule_id": c["rule_id"], "ref": res["conflict_ref"],
                         "action": "corrected", "from": a["value"], "to": code})
        return True

    def _set_enum_map(self, a: dict[str, Any], code: str) -> None:
        """客户模板以文本选项作答（如下拉"Elektrolichtbogenofen"）：在该题已确认的 Mapping 上登记 ``transform=enum_map``，
        回填时枚举码 → 模板选项（03 §3.4），答案本身保持字典枚举码。"""
        syn = {v: k for k, v in ENUM_SYNONYMS.get(a["datapoint_id"].split("@")[0], {}).items()}
        for qid in a.get("question_refs") or []:
            q = self.store.get(qid, org_id=self.org)
            opts = (q or {}).get("options") or []
            label = next((o for o in opts if _norm(o) == _norm(a["value"])), None) or \
                next((o for o in opts if syn.get(code) and _norm(o) == syn[code]), None)
            if not label:
                continue
            for m in self.store.query("Mapping", org_id=self.org, status="confirmed"):
                if m["question_id"] == qid:
                    # confirmed Mapping 不可原地修改（03 状态机）：拒绝旧映射 + 人工确认一条带 transform 的新映射（同一事务）
                    new = {k: m[k] for k in ("question_id", "datapoint_id", "rank", "boundary_hint", "period_hint") if m.get(k)}
                    new.update(confidence=1.0, method="human", status="confirmed",
                               transform={"kind": "enum_map", "params": {"map": {code: label}}})
                    self.store.write_batch([
                        WriteItem("Mapping", {"id": m["id"], "status": "rejected"}, expected_version=m["version"],
                                  patch_mode="merge_from_head"),
                        WriteItem("Mapping", new)], actor=self.user)
                    self.log.append({"kind": "mapping_transform", "rule": "H-C032-MAP", "question": qid, "map": {code: label}})

    def _unify_encoding(self, c: dict[str, Any]) -> bool:
        """H-C007：跨客户枚举答案只是编码不同（自由文本标签 vs 枚举码，按 H-C032 同一张同义词表对应到同一个码）→ 处置 A，
        以已是枚举码的一侧为基准，为另一侧（通常是已提交给其他客户的历史答案）生成更正答案；对外更正由销售另行确认。"""
        from ..consistency import resolve_conflict
        from ..consistency.meta import MetaProvider

        ans_refs = [s for s in c.get("subject_refs") or [] if s.startswith("ans_")]
        if len(ans_refs) != 2:
            return False
        sides = [self.store.get(r, org_id=self.org) for r in ans_refs]
        if any(s is None or s.get("value_type") != "enum" for s in sides):
            return False
        base = sides[0]["datapoint_id"].split("@")[0]
        meta = MetaProvider.default().get(base)
        codes = list(meta.enum_values or ()) if meta else []
        mapped = [self._enum_code(base, s["value"], codes) for s in sides]
        if None in mapped or mapped[0] != mapped[1]:
            return False  # 真正的语义分歧：不由脚本处理
        base_side = 0 if sides[0]["value"] in codes else 1
        res = resolve_conflict(self.store, c["id"], actor=self.user, kind="corrected", base_side=base_side,
                               note=f"Gleiche Produktionsroute, nur unterschiedlich kodiert ('{sides[1 - base_side]['value']}' = "
                                    f"'{mapped[0]}'); Altantwort wird auf den Enumerationscode korrigiert (06 §3.2 A).")
        self.log.append({"kind": "conflict", "rule": "H-C007", "rule_id": c["rule_id"], "ref": res["conflict_ref"],
                         "action": "corrected", "followups": [f["type"] for f in res.get("followups") or []]})
        return True

    @staticmethod
    def _enum_code(base: str, value: Any, codes: list[str]) -> str | None:
        v = _norm(value)
        if value in codes:
            return value
        syn = ENUM_SYNONYMS.get(base, {})
        if v in syn and syn[v] in codes:
            return syn[v]
        hits = [c for c in codes if _norm(c) == v]
        return hits[0] if len(hits) == 1 else None

    # ------------------------------------------------------------------ 审批（H-APR）
    def approve_all(self, aprs: list[dict[str, Any]]) -> int:
        pending = [a for a in aprs if a["status"] == "pending"]
        done: set[str] = set()
        if self.use_batches and self.clock is not None:
            # 06 §5.1：L0/L1 可批量（批次号在暂存时由服务端生成，路由时不带 batch_id）；L2/L3 单条
            batchable = [a for a in pending if a["risk_level"] in ("L0", "L1")]
            by_batch: dict[str, list[dict[str, Any]]] = {}
            for a in batchable:
                by_batch.setdefault(a["risk_level"], []).append(a)
            for bid, items in sorted(by_batch.items()):
                lvl = items[0]["risk_level"]
                size = self.stager.policy.max_items(lvl) or len(items)
                for i in range(0, len(items), size):
                    chunk = items[i:i + size]
                    chk = self.stager.check(self.user, [a["id"] for a in chunk])
                    ok_ids = [it.approval_id for it in chk.items if it.ok]
                    if not ok_ids:
                        self.log.append({"kind": "batch_rejected", "batch_id": bid, "reasons": chk.reasons[:5]})
                        continue
                    b = self.stager.stage(self.user, ok_ids)
                    self.clock.advance(minutes=self.stager.policy.undo_window_minutes + 1)  # 撤销窗口结束
                    self.stager.settle()
                    committed = [x for x in ok_ids if (self.store.get(x, org_id=self.org) or {}).get("status") == "approved"]
                    done |= set(committed)
                    self.log.append({"kind": "approval", "rule": "H-APR", "mode": "batch", "batch_id": b.batch_id,
                                     "count": len(committed), "level": lvl})
        for a in pending:
            if a["id"] in done:
                continue
            cur = self.store.get(a["id"], org_id=self.org)
            if cur is None or cur["status"] != "pending":
                continue
            self.decide_single(cur)
        return len(pending)

    def decide_single(self, apr: dict[str, Any]) -> str:
        from ..consistency import AuditContext

        ans = self.store.get(apr["subject_ref"].split("#")[0], org_id=self.org)
        ctx = AuditContext.from_store(self.store, self.org)
        open_c = ctx.conflicts_for(ans["id"], "open")
        conf: dict[str, Any] = {}
        reasons = []
        if ans.get("is_estimate"):
            conf["estimate_ack"] = True
            reasons.append("estimate_ack")
        if any(c.get("severity") == "warning" for c in open_c):
            conf["warn_note"] = WARN_NOTE
            reasons.append("warn_note")
        if apr["risk_level"] == "L3":
            for k in self.router.policy.confirmations("L3"):
                conf[k] = True
            reasons.append("l3_confirmations")
        need = set(apr.get("required_role") or [])
        actor = self.user if (self.user.roles & need) or self.executive is None else self.executive
        if "compliance_owner" not in need and self.executive is not None:
            actor = self.executive
        # 职责分离（07 §4.1 / R-APR）：改过该答案的人不得裁决 → 换另一位具备所需角色的审批人
        editors = {v.get("created_by") for v in self.store.history(ans["id"], org_id=self.org)}
        if actor.id in editors and self.executive is not None and actor.id != self.executive.id and self.executive.roles & need:
            actor = self.executive
            reasons.append("segregation_of_duties")
        res = self.router.decide(apr["id"], actor=actor, decision="approved", confirmations=conf,
                                 comment="; ".join(reasons) or None)
        self.log.append({"kind": "approval", "rule": "H-APR", "mode": "single", "level": apr["risk_level"], "by": actor.id,
                         "confirmations": reasons})
        return res["approval_ref"]

    def decide(self, apr: dict[str, Any], decision: str) -> str:
        """直接落地一个裁决（只用于没有专门入口的主体；答案审批走 ``decide_single``）。"""
        actor = self.user if "executive" not in apr.get("required_role", []) or self.executive is None else self.executive
        body = {"id": apr["id"], "status": decision, "decision": decision, "decided_by": actor.id, "decided_at": self.now()}
        items = [WriteItem("Approval", body, expected_version=apr["version"], patch_mode="merge_from_head", actor=actor)]
        subj = apr["subject_ref"]
        if subj.startswith("ans_") and decision == "approved":
            a = self.store.get(subj.split("#")[0], org_id=self.org)
            items.append(WriteItem("Answer", {"id": a["id"], "status": "approved", "approval_ref": f"{apr['id']}#v{apr['version'] + 1}"},
                                   expected_version=a["version"], patch_mode="merge_from_head",
                                   actor=Actor.of("agent:approval-router", self.org)))
        refs = self.store.write_batch(items, actor=actor)
        return refs[0]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for h in self.log:
            k = h.get("rule") or h["kind"]
            if h.get("mode") == "batch":
                out["H-APR(batch items)"] = out.get("H-APR(batch items)", 0) + h["count"]
                continue
            out[k] = out.get(k, 0) + 1
        return out
