# Spec: 11 §2.9 防泄漏：LEAK-01（fewshot/检索库 与 test 的 MinHash 近重复，Jaccard ≥ 0.8 阻断）、
#       LEAK-02（时间切分：历史答案须早于收件日）、LEAK-03（企业级切分：同一企业不同时出现在 fewshot 与 test）；
#       §5.2 QG-07
"""防泄漏检查（确定性 MinHash，纯标准库）。"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Any, Iterable

from .synth import jaccard, shingles

NUM_PERM = 64
_MERSENNE = (1 << 61) - 1
_PERMS = [(1 + 2 * int.from_bytes(hashlib.sha256(f"a{i}".encode()).digest()[:7], "big"),
           int.from_bytes(hashlib.sha256(f"b{i}".encode()).digest()[:7], "big")) for i in range(NUM_PERM)]
TEXT_FIELDS = {"LBL-MAP": "text", "LBL-INJ": "text", "LBL-DRAFT": "text"}


def _h(s: str) -> int:
    return struct.unpack("<Q", hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest())[0]


def minhash(text: str) -> list[int]:
    hs = [_h(s) for s in shingles(text)]
    return [min(((a * h + b) % _MERSENNE) for h in hs) for a, b in _PERMS]


def est_jaccard(m1: list[int], m2: list[int]) -> float:
    return sum(1 for x, y in zip(m1, m2) if x == y) / len(m1)


@dataclass
class LeakReport:
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(c.get("ok") for c in self.checks.values())

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": self.checks}


def check_leaks(records: Iterable[dict[str, Any]], *, extra_fewshot_texts: Iterable[str] = (),
                threshold: float = 0.8, eval_partitions: tuple[str, ...] = ("test", "holdout")) -> LeakReport:
    """records：数据集原始样本（各标注类型混合）。`extra_fewshot_texts`：提示词示例 / 检索库文本（同样不得与 test 近重复）。"""
    recs = list(records)
    rep = LeakReport()
    # LEAK-01
    few = [(r["sample_id"], r[TEXT_FIELDS[r["label_type"]]]) for r in recs
           if r["partition"] == "fewshot" and r["label_type"] in TEXT_FIELDS]
    few += [(f"extra:{i}", t) for i, t in enumerate(extra_fewshot_texts)]
    evals = [(r["sample_id"], r[TEXT_FIELDS[r["label_type"]]]) for r in recs
             if r["partition"] in eval_partitions and r["label_type"] in TEXT_FIELDS]
    few_mh = [(sid, t, minhash(t), shingles(t)) for sid, t in few]
    hits = []
    for sid, t in evals:
        mh = minhash(t)
        sh = None
        for fid, ft, fmh, fsh in few_mh:
            if est_jaccard(mh, fmh) >= threshold - 0.1:  # MinHash 候选 → 精确 Jaccard 确认
                sh = sh or shingles(t)
                j = jaccard(sh, fsh)
                if j >= threshold:
                    hits.append({"eval": sid, "fewshot": fid, "jaccard": round(j, 3)})
    rep.checks["LEAK-01"] = {"ok": not hits, "pairs_checked": len(few) * len(evals), "hits": hits[:50]}
    # LEAK-02
    bad = []
    for r in recs:
        recv = r.get("received_at")
        for h in r.get("history_refs") or []:
            if recv and h.get("submitted_at") and h["submitted_at"] >= recv:
                bad.append({"sample": r["sample_id"], "ref": h.get("ref")})
    rep.checks["LEAK-02"] = {"ok": not bad, "violations": bad[:50]}
    # LEAK-03 + 簇完整性
    few_orgs = {r["org"] for r in recs if r["partition"] == "fewshot"}
    test_orgs = {r["org"] for r in recs if r["partition"] in eval_partitions}
    overlap = sorted(few_orgs & test_orgs)
    clusters: dict[str, set[str]] = {}
    for r in recs:
        clusters.setdefault(f"{r['label_type']}:{r['cluster_id']}", set()).add(r["partition"])
    split = sorted(c for c, ps in clusters.items() if len(ps) > 1)
    rep.checks["LEAK-03"] = {"ok": not overlap and not split, "org_overlap": overlap, "clusters_split": split[:50]}
    return rep
