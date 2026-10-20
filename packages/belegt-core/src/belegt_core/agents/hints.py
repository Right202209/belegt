# Spec: 04 §2.3（hints_ref：可信区确定性检索预先算好的候选集，只含 dp: ID、TemplateMapping 条目 ID、相似度，不含任何答案值）、
#       §4.3 Activity build_hints、§7.2 C1/C2
"""映射候选检索（trusted，确定性）：嵌入相似度 + 字典标签词元覆盖，每题 Top-K DataPoint。

结果以 JSON blob 登记（``hints_ref = sha256:…``），question-parser（隔离区）只读该 blob，从而只能在候选集中选择
（与 schema enum 约束一起防止编造 ID）。
"""
from __future__ import annotations

import json
from typing import Any

from ..llm.embedding import cosine, fuzzy_cover, tokens
from .base import Services, dp_label

TOP_K = 8
FUZZY_POOL = 40


def dp_aliases(info: Any) -> list[str]:
    """别名索引：字典别名表 + 租户扩展的 ``extra.aliases`` + 内置术语表（``llm.mock.GLOSSARY``）。"""
    from ..llm.mock import GLOSSARY

    return list((info.extra or {}).get("aliases") or []) + list(GLOSSARY.get(info.id, []))


def _dp_text(info: Any) -> str:
    lab = (info.extra or {}).get("label") or {}
    parts = [lab.get("de"), lab.get("en"), lab.get("zh"), info.id.split(":", 1)[1].replace(".", " ").replace("_", " ")] + \
        dp_aliases(info)
    return " ".join(x for x in parts if x)


def _label_texts(info: Any) -> list[str]:
    lab = (info.extra or {}).get("label") or {}
    return [x for x in (lab.get("de"), lab.get("en"), lab.get("zh")) if x] + dp_aliases(info)


def build_hints(svc: Services, questions: list[dict[str, Any]], *, top_k: int = TOP_K) -> dict[str, Any]:
    reg = svc.registry
    latest: dict[str, Any] = {}
    for ref in list(reg._items):  # noqa: SLF001 - 注册表遍历
        info = reg.get(ref)
        if info is None or info.status not in ("active",):
            continue
        cur = latest.get(info.id)
        if cur is None or tuple(map(int, info.semver.split("."))) > tuple(map(int, cur.semver.split("."))):
            latest[info.id] = info
    dps = [i for i in latest.values() if (i.extra or {}).get("label")]
    emb = svc.embedder
    dp_vecs = {i.id: emb.embed(_dp_text(i)) for i in dps}
    dp_toks = {i.id: set(tokens(_dp_text(i))) for i in dps}
    dp_label_toks = {i.id: [s for s in (set(tokens(x)) for x in _label_texts(i)) if s] for i in dps}
    dp_alias_lists = {i.id: dp_aliases(i) for i in dps}
    from ..llm.mock import alias_hit
    out: dict[str, Any] = {"space_id": emb.space_id, "candidates": {}}
    for q in questions:
        text = " ".join(q["text"].values()) if isinstance(q["text"], dict) else str(q["text"])
        qv = emb.embed(text)
        qt = set(tokens(text))
        scored = []
        for i in dps:
            sim = cosine(qv, dp_vecs[i.id])
            ov = len(qt & dp_toks[i.id]) / max(1, len(dp_toks[i.id]))
            # 按语言分别算标签覆盖（中文题只与中文标签比，避免被德/英词元稀释）
            ov = max([ov] + [len(qt & s) / len(s) for s in dp_label_toks[i.id]])
            # 别名/术语表短语命中：保证进入 Top-K（检索只决定候选集，最终置信度由 rank 给出）
            boost = 0.5 if dp_alias_lists[i.id] and alias_hit(text, dp_alias_lists[i.id]) else 0.0
            scored.append((max(sim, 0.0) * 0.6 + ov * 0.4 + boost, sim, i, ov, boost))
        scored.sort(key=lambda x: (-x[0], x[2].id))
        # 错别字容忍：对预筛的前 FUZZY_POOL 个候选用近似词元覆盖重算（"Scpoe 1 emissoins"）
        pool = []
        for score, sim, i, ov, boost in scored[:FUZZY_POOL]:
            fov = max([ov] + [fuzzy_cover(qt, s) for s in dp_label_toks[i.id]])
            pool.append((max(sim, 0.0) * 0.6 + fov * 0.4 + boost, sim, i, fov, boost))
        scored = sorted(pool, key=lambda x: (-x[0], x[2].id)) + scored[FUZZY_POOL:]
        out["candidates"][q["qid"]] = [
            {"datapoint_id": i.id, "semver": i.semver, "label_de": dp_label(i, "de"), "label_en": dp_label(i, "en"),
             "label_zh": ((i.extra or {}).get("label") or {}).get("zh") or "",
             "value_type": i.value_type, "unit": i.unit, "boundary": list(i.allowed_boundaries), "similarity": round(sim, 4),
             "aliases": list((i.extra or {}).get("aliases") or [])}
            for _, sim, i, _ov, _b in scored[:top_k]]
    return out


def put_hints(svc: Services, hints: dict[str, Any]) -> str:
    return svc.store.put_blob(svc.org_id, json.dumps(hints, sort_keys=True, ensure_ascii=False).encode("utf-8"),
                              "application/vnd.belegt.hints+json")


def load_hints(svc: Services, hints_ref: str) -> dict[str, Any]:
    b = svc.store.get_blob(svc.org_id, hints_ref)
    return json.loads(b[0]) if b else {"candidates": {}}
