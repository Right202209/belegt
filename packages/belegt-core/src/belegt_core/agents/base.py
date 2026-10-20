# Spec: 04 §2.0（统一约定：run(input)->output；输入只含 ObjectRef/request_id/trace_id/budget；输出只含 ObjectRef[] 与 outcome；
#       幂等键 wf:<req_id>:act:<activity>:<seq>；LLM 输出先 /graph/validate 再写入；提示词版本 prompt:<agent>@<semver>；
#       外部文本放 <external_data>/spotlight 数据块）、07 §2（信任分区）
"""Agent 运行时基础设施。

- ``Services``：一个租户的依赖束（GraphStore、LLM、Guard、egress、连接器、提示词库、嵌入、缓存、组织配置）。
- ``AgentBase``：每个 Agent 的基类（``agent_id``、``zone``、``prompt``），``context()`` 产生 ``RunContext``。
- ``RunContext``：一次 Activity 调用：Trace span、受守卫包装的 LLM、确定性 ID、幂等写入（先查幂等表再写，
  Activity 重试/断点续跑时不重复写）、``validate``（/graph/validate 自检）、``ask``（结构化 LLM 调用）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Iterable

import yaml
from sqlalchemy import select
from ulid import ULID

from ..authz import Actor
from ..datapoints import DataPointInfo, DataPointRegistry
from ..errors import GraphError
from ..guard import Guard, Span
from ..guard.injection import SPOTLIGHT_SYSTEM_NOTE, spotlight
from ..ids import TYPE_PREFIX
from ..llm.base import LLMClient, LLMRequest
from ..llm.cache import RefCache
from ..llm.embedding import HashingEmbedder
from ..llm.structured import complete_json
from ..store import GraphStore, WriteItem
from ..store import tables as T


# ---------------------------------------------------------------------------- 确定性 ID
def det_id(type_name: str, key: str) -> str:
    """由幂等键派生确定性 ULID（同一 Activity 重试得到同一对象 ID，配合幂等键防重复写）。"""
    prefix = TYPE_PREFIX[type_name]
    return prefix + str(ULID.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:16]))


def det_trace(key: str) -> str:
    return det_id("Trace", "trace:" + key)


# ---------------------------------------------------------------------------- 提示词库
@dataclass(frozen=True)
class Prompt:
    id: str              # prompt:<agent>@<semver>
    agent: str
    task: str
    tier: str
    system: str
    user_template: str
    schema: dict[str, Any] | None
    max_tokens_out: int = 2000

    @property
    def version(self) -> str:
        return self.id


class PromptLibrary:
    """``agents/prompts/<agent>.yaml``（版本化：每个文件含若干 task，``id: prompt:<agent>@<semver>``）。"""

    def __init__(self, extra: dict[str, Prompt] | None = None) -> None:
        self._p: dict[tuple[str, str], Prompt] = {}
        root = resources.files("belegt_core.agents").joinpath("prompts")
        for f in sorted(root.iterdir(), key=lambda p: p.name):
            if not f.name.endswith(".yaml"):
                continue
            doc = yaml.safe_load(f.read_text("utf-8"))
            for task, t in (doc.get("tasks") or {}).items():
                p = Prompt(id=doc["id"], agent=doc["agent"], task=task, tier=t.get("tier", "tier-M"),
                           system=t.get("system", ""), user_template=t.get("user", "{data}"), schema=t.get("schema"),
                           max_tokens_out=int(t.get("max_tokens_out", 2000)))
                self._p[(doc["agent"], task)] = p
        for k, p in (extra or {}).items():
            self._p[(p.agent, p.task)] = p

    def get(self, agent: str, task: str) -> Prompt:
        return self._p[(agent, task)]

    def all(self) -> list[Prompt]:
        return list(self._p.values())


# ---------------------------------------------------------------------------- DataPoint 注册表（带标签）
def build_registry(*, draft_as_active: bool = True) -> DataPointRegistry:
    """字典包 + 内置集合；附 ``extra={label, definition, evidence_requirements, enum_ref, validity_rule}``。

    字典 v2027.1 全部条目 ``status: draft``（随发版转 active，见 belegt-dictionary README），而 R-REF 要求 Mapping 目标为
    ``active``。``draft_as_active=True``（默认，用于 demo/试点）把 draft 视为 active，并在 ``extra['dictionary_status']`` 保留原状态。
    """
    reg = DataPointRegistry.default()
    try:
        import belegt_dictionary  # type: ignore

        d = belegt_dictionary.load_dictionary()
        for dp_id, dp in d.datapoints.items():
            ref = f"{dp_id}@{dp.version}"
            info = reg.get(ref)
            if info is None:
                continue
            er = dp.evidence_requirements
            vr = dp.validity_rule
            extra = {
                "label": {"de": dp.label.de, "en": dp.label.en, "zh": getattr(dp.label, "zh", None)},
                "definition": {"en": dp.definition.en} if getattr(dp, "definition", None) else {},
                "accepted_source_types": [getattr(s, "value", s) for s in (er.accepted_source_types if er else [])],
                "requires_calculation": bool(er.requires_calculation) if er else False,
                "max_age_days": er.max_age_days if er else None,
                "human_confirmation_allowed": bool(er.human_confirmation_allowed) if er else False,
                "enum_ref": dp.enum_ref, "dictionary_status": info.status,
                "validity": {"min": getattr(vr, "min", None), "max": getattr(vr, "max", None)} if vr else {},
                "tags": list(dp.tags or []),
            }
            status = "active" if (draft_as_active and info.status == "draft") else info.status
            reg.register(DataPointInfo(**{**{k: getattr(info, k) for k in (
                "id", "semver", "value_type", "unit", "dimension", "allowed_boundaries", "consolidation",
                "requires_consolidation", "sensitivity", "allowed_units")}, "status": status, "extra": extra}))
    except Exception:  # pragma: no cover - 字典包缺失时用内置集合
        pass
    # 内置集合里比字典包更新的版本（如 scope2_*@1.1.0，_baseline §M）没有标签：继承同一 DataPoint 最近一个带标签版本的
    # 标签/定义/证据要求（语义不随 MINOR 变化）。否则 hints 检索会把它们整个漏掉，Scope 2 题只能落到 Scope 1 上。
    for ref in list(reg._items):  # noqa: SLF001
        info = reg.get(ref)
        if info is None or (info.extra or {}).get("label"):
            continue
        donors = [v for v in reg.versions(info.id) if (v.extra or {}).get("label")]
        if donors:
            donor = max(donors, key=lambda v: tuple(int(x) for x in v.semver.split(".")))
            reg.register(DataPointInfo(**{**{k: getattr(info, k) for k in (
                "id", "semver", "value_type", "unit", "dimension", "allowed_boundaries", "consolidation",
                "requires_consolidation", "status", "sensitivity", "allowed_units")},
                "extra": {**donor.extra, "label_inherited_from": donor.ref}}))
    return reg


def latest_ref(reg: DataPointRegistry, base_id: str) -> str | None:
    vs = sorted(reg.versions(base_id), key=lambda v: tuple(int(x) for x in v.semver.split(".")))
    act = [v for v in vs if v.status == "active"]
    return (act or vs)[-1].ref if vs else None


def dp_label(info: DataPointInfo | None, lang: str = "de") -> str:
    if info is None:
        return ""
    lab = (info.extra or {}).get("label") or {}
    return lab.get(lang) or lab.get("en") or info.id


# ---------------------------------------------------------------------------- 组织配置
@dataclass
class OrgConfig:
    """租户上下文（主数据引用 + 默认值）。"""

    org_id: str
    customer_scope_default: str | list[str] = "all"
    default_product_id: str | None = None
    default_site_id: str | None = None
    consolidation: str = "operational_control"
    grid_region: str = "DE"
    country: str = "DE"
    sender_name: str = "Compliance"
    sender_role: str = "Nachhaltigkeit"
    company_name: str = ""
    portal_base_url: str = "https://supplier.belegt.example"
    privacy_note_link: str = "https://supplier.belegt.example/privacy"
    data_residency: str = "eu"
    supplier_for_datapoints: dict[str, str] = field(default_factory=dict)  # dp base id → supplier cp_id
    factor_sets: dict[str, str] = field(default_factory=lambda: {"default": "fs:belegt-example@2026.10"})
    thresholds: dict[str, float] = field(default_factory=dict)


@dataclass
class Services:
    store: GraphStore
    llm: LLMClient
    guard: Guard
    org: OrgConfig
    egress: Any = None
    connectors: dict[str, Any] = field(default_factory=dict)
    prompts: PromptLibrary = field(default_factory=PromptLibrary)
    embedder: HashingEmbedder = field(default_factory=HashingEmbedder)
    ref_cache: RefCache = field(default_factory=RefCache)
    portal_driver: Any = None
    supplier_templates: Any = None

    @property
    def org_id(self) -> str:
        return self.org.org_id

    @property
    def registry(self) -> DataPointRegistry:
        return self.store.datapoints


@dataclass
class AgentOutput:
    refs: list[str] = field(default_factory=list)
    outcome: str = "ok"
    cq_refs: list[str] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"refs": self.refs, "outcome": self.outcome, "cq_refs": self.cq_refs, "gaps": self.gaps,
                "details": self.details}


class RunContext:
    def __init__(self, agent: "AgentBase", request_id: str | None, idem_prefix: str, span: Span) -> None:
        self.agent, self.svc, self.request_id, self.idem_prefix, self.span = agent, agent.svc, request_id, idem_prefix, span
        self.llm = agent.svc.guard.llm(agent.svc.llm, agent=agent.agent_id, org_id=agent.svc.org_id,
                                       request_id=request_id, parent=span)

    @property
    def store(self) -> GraphStore:
        return self.svc.store

    def key(self, suffix: str) -> str:
        if suffix.startswith("org:"):  # 租户级幂等键：同一来源的证据跨 Activity/Request 只写一次（03 §3.4 证据共享）
            return f"org:{self.svc.org_id}:{suffix[4:]}"
        return f"{self.idem_prefix}:{suffix}"

    def det_id(self, type_name: str, suffix: str) -> str:
        return det_id(type_name, self.key(suffix))

    # ---------------------------------------------------------------- 幂等写入
    def existing(self, key: str) -> str | None:
        from ..guard.promotion import idempotent_ref

        return idempotent_ref(self.store, self.svc.org_id, key)

    def write(self, type_name: str, body: dict[str, Any], *, key: str, expected_version: int | None = None,
              patch_mode: str = "replace", actor: Actor | None = None) -> str:
        full = self.key(key)
        prev = self.existing(full)
        if prev:
            return prev
        body = dict(body)
        if type_name != "FactorSet":
            body.setdefault("provenance", self.span.trace_id)
        try:
            return self.store.write(type_name, body, expected_version=expected_version, idempotency_key=full,
                                    actor=actor or self.agent.actor(), patch_mode=patch_mode)
        except GraphError as e:
            self.svc.guard.record_rejection(body.get("id"), e.code)  # LD-02
            raise

    def write_batch(self, items: list[WriteItem], *, actor: Actor | None = None) -> list[str]:
        out: list[str] = []
        todo: list[WriteItem] = []
        idx: list[int] = []
        for it in items:
            full = self.key(it.idempotency_key or "")
            prev = self.existing(full)
            if prev:
                out.append(prev)
            else:
                it.idempotency_key = full
                if isinstance(it.object, dict) and it.type != "FactorSet":
                    it.object.setdefault("provenance", self.span.trace_id)
                idx.append(len(out))
                out.append("")
                todo.append(it)
        for i in range(0, len(todo), 400):
            chunk = todo[i:i + 400]
            refs = self.store.write_batch(chunk, actor=actor or self.agent.actor())
            for j, r in enumerate(refs):
                out[idx[i + j]] = r
        return out

    def validate(self, type_name: str, body: dict[str, Any]) -> GraphError | None:
        """04 §2.0：LLM 产物先过 ``/graph/validate``（不写入）。返回错误或 None。"""
        try:
            self.store.validate(WriteItem(type_name, dict(body)), actor=self.agent.actor())
            return None
        except GraphError as e:
            return e

    # ---------------------------------------------------------------- LLM
    def ask(self, task: str, data: dict[str, Any], *, external: dict[str, str] | None = None, tier: str | None = None,
            schema: dict[str, Any] | None = None, retries: int = 1) -> dict[str, Any]:
        p = self.svc.prompts.get(self.agent.agent_id, task)
        system = p.system
        user_parts = [p.user_template.replace("{data}", json.dumps(data, ensure_ascii=False, default=str))]
        for name, text in (external or {}).items():
            block, nonce = spotlight(text)
            system = system + "\n" + SPOTLIGHT_SYSTEM_NOTE.replace("{nonce}", nonce)
            user_parts.append(f"<external_data name=\"{name}\">\n{block}\n</external_data>")
            data = {**data, name: text}
        req = LLMRequest(agent=self.agent.agent_id, task=task, tier=tier or p.tier, system=system,
                         user="\n\n".join(user_parts), schema=schema or p.schema, schema_name=task, prompt_version=p.id,
                         data=data, max_tokens_out=p.max_tokens_out, data_residency=self.svc.org.data_residency)
        self.span.prompt_version = p.id
        return complete_json(self.llm, req, retries=retries).parsed


class AgentBase:
    agent_id: str = ""
    zone: str = "trusted"

    def __init__(self, svc: Services) -> None:
        self.svc = svc

    def actor(self) -> Actor:
        return Actor.of(f"agent:{self.agent_id}", self.svc.org_id)

    def prompt_version(self) -> str | None:
        try:
            return next(p.id for p in self.svc.prompts.all() if p.agent == self.agent_id)
        except StopIteration:
            return None

    def context(self, request_id: str | None, idem_prefix: str, *, activity: str | None = None, attempt: int = 1,
                input_obj: Any = None):
        """返回上下文管理器：``with agent.context(...) as ctx:``（一个 Agent span）。"""
        agent = self

        class _CM:
            def __enter__(self_inner) -> RunContext:
                self_inner._cm = agent.svc.guard.recorder.span(
                    f"agent:{agent.agent_id}", agent.svc.org_id, trace_id=det_trace(idem_prefix), request_id=request_id,
                    activity=activity or agent.agent_id, attempt=attempt, input_obj=input_obj)
                sp = self_inner._cm.__enter__()
                sp.prompt_version = agent.prompt_version()
                agent.svc.guard.enter(request_id, activity or agent.agent_id, span=sp)
                return RunContext(agent, request_id, idem_prefix, sp)

            def __exit__(self_inner, *exc: Any) -> Any:
                return self_inner._cm.__exit__(*exc)

        return _CM()


def chunks(seq: list[Any], n: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
