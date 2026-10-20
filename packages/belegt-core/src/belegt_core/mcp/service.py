# Spec: 04 §6.2（Belegt 作为 MCP server：只暴露 approved/submitted；app.approved_only + app.cp_scope RLS；不暴露 draft、Evidence 原件、
#       其他客户 customer_scope 的答案；sensitivity=restricted 默认不列出）、07 §4.3（按客户授权）、03 §6.5（会话变量）
"""只读已批准数据服务（不依赖 ``mcp`` 包；MCP server 与 A2A 都调用它）。

授权：
- 租户：一律按 ``actor.org_id``（GraphStore 读过滤）。
- 只读已批准：内部把执行者强制为 ``approved_only=True``（RLS ``p_approved_only``），且再按 ``status ∈ {approved, submitted}`` 过滤。
- 客户范围：``actor.cp_scope``（RLS ``p_customer_scope``）；``customer_buyer`` 必须带 ``cp_scope``；请求的 ``counterparty_id``
  不在 ``cp_scope`` 内 → ``GRAPH_ACTOR_NOT_ALLOWED``。
- 允许的执行者：内部用户角色（compliance_owner/executive/sales/org_admin/auditor_readonly）、带 cp_scope 的 customer_buyer、
  ``agent:sales-assistant``、``service:*``；隔离区执行者与 supplier_user 拒绝。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from typing import Any

from ..authz import Actor
from ..errors import GraphError

APPROVED = ("approved", "submitted")
INTERNAL_READ_ROLES = ("compliance_owner", "executive", "sales", "org_admin", "auditor_readonly")
ANSWER_FIELDS = ("datapoint_id", "value", "value_type", "unit", "period", "boundary", "consolidation", "customer_scope",
                 "is_estimate", "status", "approval_ref", "evidence_refs", "calculation_ref", "text_lang", "valid_from",
                 "valid_to", "created_at", "answer_key")
MAX_LIST = 500


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _period_overlaps(p: dict[str, Any] | None, q: dict[str, Any] | None) -> bool:
    if not q:
        return True
    if not p:
        return False
    return p.get("start", "") <= q.get("end", "9999") and p.get("end", "9999") >= q.get("start", "")


class ApprovedReadService:
    def __init__(self, store: Any, *, registry: Any = None, include_restricted: bool = False) -> None:
        self.store = store
        self.registry = registry or getattr(store, "datapoints", None)
        self.include_restricted = include_restricted

    # ------------------------------------------------------------------ authz
    def reader(self, actor: Actor) -> Actor:
        if actor.zone == "quarantine" or (actor.is_user and actor.roles and actor.roles <= {"supplier_user"}):
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "quarantine actors may not read approved data via MCP/A2A", rule_id="AUTHZ")
        if actor.is_agent and actor.name not in ("sales-assistant",):
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"{actor.id} may not use the approved-data interface", rule_id="AUTHZ")
        if actor.is_user:
            if actor.has_role("customer_buyer") and not actor.has_role(*INTERNAL_READ_ROLES) and actor.cp_scope is None:
                raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "customer_buyer requires cp_scope", rule_id="AUTHZ")
            if not actor.has_role(*INTERNAL_READ_ROLES, "customer_buyer"):
                raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "no role permits reading approved answers", rule_id="AUTHZ")
        return replace(actor, approved_only=True)

    def _check_counterparty(self, actor: Actor, counterparty_id: str | None) -> None:
        if counterparty_id and actor.cp_scope is not None and counterparty_id not in actor.cp_scope:
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"counterparty {counterparty_id} outside cp_scope", rule_id="AUTHZ",
                             path="/counterparty_id")

    def _restricted(self, dp_ref: str) -> bool:
        info = self.registry.get(dp_ref) if self.registry is not None else None
        return bool(info and info.sensitivity == "restricted")

    # ------------------------------------------------------------------ answers
    @staticmethod
    def answer_view(a: dict[str, Any]) -> dict[str, Any]:
        out = {"answer_ref": f"{a['id']}#v{a['version']}", "answer_id": a["id"], "version": a["version"]}
        out.update({k: a[k] for k in ANSWER_FIELDS if k in a and a[k] is not None})
        return out

    def list_approved_answers(self, actor: Actor, *, datapoint_ids: list[str] | None = None, counterparty_id: str | None = None,
                              period: dict[str, Any] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        r = self.reader(actor)
        self._check_counterparty(r, counterparty_id)
        bases = {d.split("@", 1)[0] for d in datapoint_ids or []}
        items = []
        for a in self.store.query("Answer", actor=r):
            if a.get("status") not in APPROVED:
                continue
            if bases and a["datapoint_id"].split("@", 1)[0] not in bases:
                continue
            if counterparty_id and a.get("customer_scope") != "all" and counterparty_id not in (a.get("customer_scope") or []):
                continue
            if not _period_overlaps(a.get("period"), period):
                continue
            if not self.include_restricted and self._restricted(a["datapoint_id"]):
                continue
            items.append(self.answer_view(a))
        items.sort(key=lambda x: (x["datapoint_id"], (x.get("period") or {}).get("start", ""), x["answer_ref"]))
        return items[:max(1, min(limit, MAX_LIST))]

    def get_answer(self, actor: Actor, answer_ref: str) -> dict[str, Any]:
        r = self.reader(actor)
        a = self.store.get(answer_ref, actor=r) if answer_ref.startswith("ans_") else None
        if a is None or a.get("status") not in APPROVED:
            raise GraphError("GRAPH_NOT_FOUND", f"{answer_ref} not found or not approved")
        return self.answer_view(a)

    def get_answer_with_provenance(self, actor: Actor, answer_ref: str, depth: int = 4) -> dict[str, Any]:
        """Answer 只读字段 + ``approval_ref`` + 证据链摘要（Evidence 只给 ``source_type`` 与 locator 摘要，不给原件/原文）。"""
        view = self.get_answer(actor, answer_ref)
        r = self.reader(actor)
        lin = self.store.lineage(view["answer_ref"], max(1, min(depth, 4)), actor=r)
        from ..ids import type_of_id

        nodes = []
        for nref in lin.get("nodes", []):
            if nref == view["answer_ref"] or nref.startswith("dp:"):
                continue
            nid = nref.split("#", 1)[0]
            try:
                t = type_of_id(nid)
            except Exception:  # noqa: BLE001
                continue
            obj = self.store.get(nref, actor=r) or {}
            item: dict[str, Any] = {"ref": nref, "type": t}
            if t == "Evidence":
                loc = obj.get("locator") or {}
                item.update({"source_type": obj.get("source_type"), "status": obj.get("status"), "fact_key": obj.get("fact_key"),
                             "locator": {k: loc[k] for k in ("page", "sheet", "cell", "mime") if k in loc},
                             "valid_until": obj.get("valid_until")})
                if loc.get("connector"):
                    item["locator"]["connector_id"] = loc["connector"].get("connector_id")
                if loc.get("blob_sha256"):
                    item["locator"]["document"] = True  # 原件不外露（04 §6.2）
            elif t == "Calculation":
                item.update({k: obj.get(k) for k in ("method_id", "methodology_version", "factor_set_version", "engine_version",
                                                     "input_hash", "output_hash") if obj.get(k) is not None})
                if obj.get("result"):
                    item["result"] = {k: obj["result"].get(k) for k in ("value", "unit")}
            elif t == "Approval":
                item.update({k: obj.get(k) for k in ("status", "risk_level", "decided_at", "policy_version") if obj.get(k)})
            elif t == "FactorSet":
                item.update({k: obj.get(k) for k in ("factor_set_id", "version", "status") if obj.get(k)})
            else:
                continue  # Question/Request/Mapping 等内部对象不外露
            nodes.append(item)
        apr_ref = view.get("approval_ref")
        synthetic = []
        if apr_ref and apr_ref not in {n["ref"] for n in nodes}:  # approval_ref 不派生边（refspec），单独补上
            apr = self.store.get(apr_ref, actor=r) or {}
            if apr:
                nodes.append({"ref": apr_ref, "type": "Approval", **{k: apr.get(k) for k in (
                    "status", "risk_level", "decided_at", "policy_version") if apr.get(k)}})
                synthetic.append({"from": view["answer_ref"], "to": apr_ref, "kind": "approval_ref"})
        refs = {n["ref"] for n in nodes} | {view["answer_ref"]}
        edges = []
        for e in lin.get("edges", []):
            fr = f"{e['from_id']}#v{e['from_version']}"
            to = f"{e['to_id']}#v{e['to_version']}" if e.get("to_version") else e["to_id"]
            if fr in refs and to in refs:
                edges.append({"from": fr, "to": to, "kind": e.get("edge_type")})
        edges += synthetic
        return {"answer": view, "approval_ref": view.get("approval_ref"), "provenance": {"nodes": nodes, "edges": edges},
                "depth": max(1, min(depth, 4))}

    # ------------------------------------------------------------------ dictionary（公开）
    def _items(self) -> list[Any]:
        return list(getattr(self.registry, "_items", {}).values()) if self.registry is not None else []

    @staticmethod
    def datapoint_view(info: Any) -> dict[str, Any]:
        ex = info.extra or {}
        return {"datapoint_id": info.ref, "id": info.id, "semver": info.semver, "value_type": info.value_type, "unit": info.unit,
                "allowed_units": list(info.allowed_units or ()), "allowed_boundaries": list(info.allowed_boundaries or ()),
                "consolidation": list(info.consolidation or ()), "status": info.status, "sensitivity": info.sensitivity,
                "label": {k: v for k, v in (ex.get("label") or {}).items() if v}, "definition": ex.get("definition") or {},
                "tags": list(ex.get("tags") or [])}

    def get_datapoint(self, datapoint_id: str) -> dict[str, Any]:
        info = self.registry.get(datapoint_id) if "@" in datapoint_id else None
        if info is None:
            vs = sorted((v for v in self._items() if v.id == datapoint_id.split("@", 1)[0]),
                        key=lambda v: tuple(int(x) if x.isdigit() else 0 for x in v.semver.split(".")))
            info = vs[-1] if vs else None
        if info is None:
            raise GraphError("GRAPH_NOT_FOUND", f"{datapoint_id} not in dictionary")
        return self.datapoint_view(info)

    def search_dictionary(self, query: str, lang: str = "de", limit: int = 20) -> list[dict[str, Any]]:
        terms = [t for t in _norm(query).split() if t]
        if not terms:
            return []
        latest: dict[str, Any] = {}
        for v in self._items():
            cur = latest.get(v.id)
            key = tuple(int(x) if x.isdigit() else 0 for x in v.semver.split("."))
            if cur is None or key > tuple(int(x) if x.isdigit() else 0 for x in cur.semver.split(".")):
                latest[v.id] = v
        scored = []
        for v in latest.values():
            ex = v.extra or {}
            lab = ex.get("label") or {}
            primary = _norm(lab.get(lang) or "")
            hay = " ".join([_norm(v.id.replace(".", " ").replace("_", " ")), primary,
                            *(_norm(x) for x in lab.values() if x), *(_norm(a) for a in ex.get("aliases") or []),
                            _norm(" ".join(ex.get("tags") or []))])
            hits = sum(1 for t in terms if t in hay)
            if not hits:
                continue
            score = hits / len(terms) + (0.5 if all(t in primary for t in terms) else 0) + (0.25 if _norm(query) in hay else 0)
            scored.append((round(score, 4), v))
        scored.sort(key=lambda x: (-x[0], x[1].id))
        out = []
        for s, v in scored[:max(1, min(limit, 100))]:
            d = self.datapoint_view(v)
            d["score"] = s
            d["label_text"] = (d["label"].get(lang) or d["label"].get("en") or v.id)
            out.append(d)
        return out


__all__ = ["ApprovedReadService", "APPROVED", "INTERNAL_READ_ROLES"]
