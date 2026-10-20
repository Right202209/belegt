# Spec: 04 §2.11 filler（filler-file：按 Question.locator 与 Mapping.transform 把 approved 答案确定性回填原格式；导出物登记
#       export_input_hash；Answer → submitted；filler-portal：沙箱浏览器、域名白名单、动作白名单、提交前人确认令牌、每次填入后
#       DOM 回读逐位比对；门户失败降级为导出文件）、§4.4 PortalGraph、§5.1 GP-08、07 §2.2、§3.4 RT-PI-06/07
"""填报。

- ``FillerFile``：确定性，无 LLM（``belegt_io.fill_back``）；
- ``FillerPortal``：浏览器 Agent 接口（``PortalDriver`` 协议）+ ``SandboxPortalStub``（内存表单，测试/开发用）；
  步骤计划由脚本给出（``script``），模型只做"定位元素"（``portal_locate``），每步 GP-08 检查，``submit`` 需人工令牌。
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
from decimal import Decimal
from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from ..errors import GraphError
from ..guard.loops import LoopDetector
from ..guard.policy import Halted
from ..guard.promotion import MIME_SUFFIX, readback_questionnaire
from ..llm.base import LLMOutputInvalid, ModelUnavailable
from ..store import WriteItem
from .base import AgentBase, AgentOutput


def _approved_answers(svc: Any, req_id: str) -> list[dict[str, Any]]:
    st, org = svc.store, svc.org_id
    qids = {q["id"] for q in st.query("Question", org_id=org, request_id=req_id)}
    return [a for a in st.query("Answer", org_id=org, status="approved") if set(a.get("question_refs") or []) & qids]


def export_value(svc: Any, a: dict[str, Any], q: dict[str, Any], mapping: dict[str, Any] | None) -> Any:
    """Answer 值 → 原格式单元格值（Mapping.transform：unit_convert；bool/enum 由 fill_back 适配选项）。"""
    v = a["value"]
    if a["value_type"] == "number":
        d = Decimal(str(v))
        tr = (mapping or {}).get("transform") or {}
        want = q.get("unit_requested")
        if tr.get("kind") == "unit_convert":
            d = svc.store.units.convert(d, tr["params"]["from"], tr["params"]["to"])
        elif want and a.get("unit") and want != a["unit"] and svc.store.units.compatible(a["unit"], want):
            d = svc.store.units.convert(d, a["unit"], want)
        return d
    tr = (mapping or {}).get("transform") or {}
    if tr.get("kind") == "enum_map" and isinstance(v, str):  # 03 §3.4：枚举码 → 本模板的选项文本
        m = (tr.get("params") or {}).get("map") or {}
        return m.get(v, v)
    if a["value_type"] == "date" and q.get("answer_format") == "number" and isinstance(v, str) and len(v) >= 4 and v[:4].isdigit():
        return Decimal(v[:4])  # "Zieljahr"类问题：日期型数据点 → 年份（确定性）
    return v


class FillerFile(AgentBase):
    agent_id = "filler"

    def run(self, request_ref: str, *, idem_prefix: str) -> AgentOutput:
        from belegt_io import Locator as IOLocator, fill_back

        st, org = self.svc.store, self.svc.org_id
        req_id = request_ref.split("#")[0]
        req = st.get(req_id, org_id=org)
        with self.context(req_id, idem_prefix, activity="fill_file", input_obj={"request": request_ref}) as ctx:
            pq = readback_questionnaire(st, org, req)
            if pq is None:
                return AgentOutput(outcome="no_file", details={"portal": True})
            qs = {q["id"]: q for q in st.query("Question", org_id=org, request_id=req_id)}
            maps = {m["question_id"]: m for m in st.query("Mapping", org_id=org, status="confirmed")}
            answers: dict[str, Any] = {}
            used: list[str] = []
            for a in _approved_answers(self.svc, req_id):  # 只读 approved（RLS approved_only，03 §6.5）
                for qid in a.get("question_refs") or []:
                    q = qs.get(qid)
                    if not q:
                        continue
                    loc = str(IOLocator(**{k: v for k, v in q["locator"].items() if k in ("blob_sha256", "mime", "sheet", "cell", "page")}))
                    answers[loc] = export_value(self.svc, a, q, maps.get(qid))
                used.append(f"{a['id']}#v{a['version']}")
            sha = (req.get("source_ref") or {}).get("blob_sha256", [None])[0]
            blob = st.get_blob(org, sha)
            suffix = MIME_SUFFIX.get(blob[1], ".xlsx")
            tmpdir = tempfile.mkdtemp()
            src, dst = os.path.join(tmpdir, "src" + suffix), os.path.join(tmpdir, "out" + suffix)
            with open(src, "wb") as f:
                f.write(blob[0])
            rep = fill_back(src, answers, dst, parsed=pq)
            with open(dst, "rb") as f:
                out_sha = st.put_blob(org, f.read(), blob[1])
            return AgentOutput(refs=used, outcome="exported" if rep.ok else "export_with_rejections", details={
                "export_blob": out_sha, "export_input_hash": rep.export_input_hash, "written": len(rep.written),
                "rejected": [{"cell": c.cell, "code": c.code} for c in rep.rejected], "report_md": rep.to_markdown()[:20000]})

    def mark_submitted(self, request_ref: str, export_input_hash: str, *, idem_prefix: str, channel: str = "file_export") -> AgentOutput:
        """人确认后：approved → submitted（R-SUBMIT 在写入时强制），Request filling → submitted（T16）。"""
        st, org = self.svc.store, self.svc.org_id
        req_id = request_ref.split("#")[0]
        now = st.peek_now()[:19] + "Z"
        with self.context(req_id, idem_prefix, activity="mark_submitted") as ctx:
            items = [WriteItem("Answer", {"id": a["id"], "status": "submitted",
                                          "submission": {"request_id": req_id, "channel": channel, "submitted_at": now,
                                                         "export_input_hash": export_input_hash}},
                               expected_version=a["version"], patch_mode="merge_from_head", idempotency_key=f"submit:{a['id']}")
                     for a in _approved_answers(self.svc, req_id)]
            refs = ctx.write_batch(items)
            r = st.get(req_id, org_id=org)
            rref = ctx.write("Request", {"id": req_id, "status": "submitted"}, key="request-submitted",
                             expected_version=r["version"], patch_mode="merge_from_head")
            return AgentOutput(refs=refs + [rref], outcome="submitted")


# ---------------------------------------------------------------------------- filler-portal
class PortalDriver(Protocol):
    """沙箱浏览器驱动（生产：Playwright 于一次性 microVM，仅经 egress 代理；07 §2.2）。"""

    def navigate(self, url: str) -> None: ...
    def elements(self) -> list[dict[str, Any]]: ...
    def fill(self, element_id: str, value: str) -> None: ...
    def select(self, element_id: str, value: str) -> None: ...
    def click(self, element_id: str) -> None: ...
    def read(self, element_id: str) -> str | None: ...
    def screenshot(self) -> bytes: ...
    def submit(self) -> str: ...
    @property
    def current_url(self) -> str: ...


class SandboxPortalStub:
    """内存门户：``fields={label: element_id}``；``hidden_text`` 模拟页面中的注入文本（RT-PI-06）。"""

    def __init__(self, base_url: str, fields: dict[str, str], hidden_text: str = "") -> None:
        self.base_url, self.fields, self.hidden_text = base_url, fields, hidden_text
        self.values: dict[str, str] = {}
        self.url = base_url
        self.submitted = False
        self.actions: list[tuple[str, str]] = []

    @property
    def current_url(self) -> str:
        return self.url

    def navigate(self, url: str) -> None:
        self.url = url
        self.actions.append(("navigate", url))

    def elements(self) -> list[dict[str, Any]]:
        els = [{"id": eid, "label": label, "type": "input"} for label, eid in self.fields.items()]
        if self.hidden_text:
            els.append({"id": "hidden", "label": self.hidden_text, "type": "aria-hidden"})
        return els

    def fill(self, element_id: str, value: str) -> None:
        self.values[element_id] = value
        self.actions.append(("fill", element_id))

    def select(self, element_id: str, value: str) -> None:
        self.fill(element_id, value)

    def click(self, element_id: str) -> None:
        self.actions.append(("click", element_id))

    def read(self, element_id: str) -> str | None:
        return self.values.get(element_id)

    def screenshot(self) -> bytes:
        return repr(sorted(self.values.items())).encode("utf-8")

    def submit(self) -> str:
        self.submitted = True
        self.actions.append(("submit", ""))
        return "PORTAL-RECEIPT-0001"


class PortalState(TypedDict, total=False):
    steps: list[dict[str, Any]]
    i: int
    filled: list[dict[str, Any]]
    mismatches: list[str]
    last: dict[str, Any] | None


class FillerPortal(AgentBase):
    agent_id = "filler"
    recursion_limit = 150  # 04 §4.4 / §2.11：≤ 150 步

    def run(self, request_ref: str, portal_url: str, allowed_domains: list[str], fields: dict[str, Any], *, idem_prefix: str,
            driver: PortalDriver | None = None) -> AgentOutput:
        """``fields={label: value}``（来自 approved 答案）。只填写、回读比对，不点击提交；返回差异表供人确认。"""
        drv = driver or self.svc.portal_driver
        req_id = request_ref.split("#")[0]
        g = self.svc.guard
        with self.context(req_id, idem_prefix, activity="fill_portal") as ctx:
            g.check_portal_action("navigate", portal_url, allowed_domains)
            drv.navigate(portal_url)
            steps = [{"label": k, "value": str(v)} for k, v in fields.items()]
            graph = StateGraph(PortalState)

            def act(s: PortalState) -> PortalState:
                step = s["steps"][s["i"]]
                els = drv.elements()
                try:
                    loc = ctx.ask("portal_locate", {"target_label": step["label"], "elements": els})
                    eid = loc.get("element_id")
                except (ModelUnavailable, LLMOutputInvalid):
                    eid = None
                if eid is None or eid == "hidden" or eid not in {e["id"] for e in els if e.get("type") == "input"}:
                    return {"last": None, "mismatches": s.get("mismatches", []) + [f"not_found:{step['label']}"]}
                g.check_portal_action("fill", drv.current_url, allowed_domains)
                drv.fill(eid, step["value"])
                last = {"label": step["label"], "element": eid, "value": step["value"]}
                return {"filled": s.get("filled", []) + [last], "last": last}

            def readback_compare(s: PortalState) -> PortalState:  # 截图 + DOM 回读，与 Answer 值逐位比对
                last = s.get("last")
                mm = list(s.get("mismatches", []))
                if last and drv.read(last["element"]) != last["value"]:
                    mm.append(f"mismatch:{last['label']}")
                drv.screenshot()
                return {"i": s["i"] + 1, "mismatches": mm}

            def nxt(s: PortalState) -> str:
                return "act" if s["i"] < len(s["steps"]) else END

            graph.add_node("act", act)
            graph.add_node("readback_compare", readback_compare)
            graph.add_conditional_edges(START, nxt, {"act": "act", END: END})
            graph.add_edge("act", "readback_compare")
            graph.add_conditional_edges("readback_compare", nxt, {"act": "act", END: END})
            try:
                st = graph.compile(checkpointer=InMemorySaver()).invoke(
                    {"steps": steps, "i": 0, "filled": [], "mismatches": []},
                    {"configurable": {"thread_id": idem_prefix}, "recursion_limit": self.recursion_limit})
            except GraphRecursionError as e:
                raise LoopDetector.from_recursion(e, "PortalGraph") from e
            return AgentOutput(outcome="filled" if not st["mismatches"] else "mismatch",
                               details={"filled": st["filled"], "mismatches": st["mismatches"], "submitted": False})

    def submit(self, portal_url: str, allowed_domains: list[str], *, confirm_token: str | None, expected_token: str,
               driver: PortalDriver | None = None) -> str:
        """最终"提交"：GP-08 要求人工确认令牌（ClosedQuestion yes_no 作答后由工作流签发）。"""
        drv = driver or self.svc.portal_driver
        self.svc.guard.check_portal_action("submit", drv.current_url, allowed_domains, confirm_token=confirm_token,
                                           expected_token=expected_token)
        return drv.submit()
