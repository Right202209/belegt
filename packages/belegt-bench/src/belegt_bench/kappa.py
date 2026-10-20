# Spec: 11 §2.5（双人标注一致性：Cohen's κ，门槛 κ ≥ 0.8；值型按“值+单位+期间”成类；定位先 IoU ≥ 0.5 判一致）、
#       §4.2（judge 校准：量表用加权 κ ≥ 0.7）
"""标注一致性计算。纯标准库，确定性。

κ = (p_o − p_e) / (1 − p_e)，p_e = Σ_k p_{A,k}·p_{B,k}（两人各自边际分布）。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Hashable, Iterable, Sequence

KAPPA_THRESHOLD = 0.8
JUDGE_KAPPA_THRESHOLD = 0.7


@dataclass
class KappaResult:
    kappa: float
    p_o: float
    p_e: float
    n: int
    threshold: float = KAPPA_THRESHOLD
    disagreements: list[int] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.n > 0 and self.kappa >= self.threshold

    def to_dict(self) -> dict[str, Any]:
        return {"kappa": round(self.kappa, 4), "p_o": round(self.p_o, 4), "p_e": round(self.p_e, 4), "n": self.n,
                "threshold": self.threshold, "pass": self.passed, "disagreements": self.disagreements}


def cohen_kappa(a: Sequence[Hashable], b: Sequence[Hashable], *, threshold: float = KAPPA_THRESHOLD) -> KappaResult:
    if len(a) != len(b):
        raise ValueError("annotator label lists differ in length")
    n = len(a)
    if n == 0:
        return KappaResult(0.0, 0.0, 0.0, 0, threshold)
    agree = [i for i in range(n) if a[i] == b[i]]
    p_o = len(agree) / n
    ca, cb = Counter(a), Counter(b)
    p_e = sum((ca[k] / n) * (cb[k] / n) for k in set(ca) | set(cb))
    if p_e >= 1.0:  # 两人都只用了同一个类别
        kappa = 1.0 if p_o == 1.0 else 0.0
    else:
        kappa = (p_o - p_e) / (1 - p_e)
    return KappaResult(kappa, p_o, p_e, n, threshold, [i for i in range(n) if a[i] != b[i]])


def weighted_kappa(a: Sequence[int], b: Sequence[int], categories: Sequence[int] | None = None, *,
                   weights: str = "quadratic", threshold: float = JUDGE_KAPPA_THRESHOLD) -> KappaResult:
    """有序量表（如 1–5 分）的加权 κ：κ_w = 1 − Σw·o / Σw·e。"""
    if len(a) != len(b):
        raise ValueError("annotator label lists differ in length")
    n = len(a)
    cats = sorted(set(categories or []) | set(a) | set(b))
    k = len(cats)
    if n == 0 or k < 2:
        return KappaResult(1.0 if n and list(a) == list(b) else 0.0, 1.0, 1.0, n, threshold)
    idx = {c: i for i, c in enumerate(cats)}

    def w(i: int, j: int) -> float:
        d = abs(i - j) / (k - 1)
        return d * d if weights == "quadratic" else d

    obs = [[0.0] * k for _ in range(k)]
    for x, y in zip(a, b):
        obs[idx[x]][idx[y]] += 1
    ra = [sum(row) for row in obs]
    rb = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    num = sum(w(i, j) * obs[i][j] for i in range(k) for j in range(k))
    den = sum(w(i, j) * ra[i] * rb[j] / n for i in range(k) for j in range(k))
    kappa = 1.0 - num / den if den else 1.0
    p_o = sum(obs[i][i] for i in range(k)) / n
    return KappaResult(kappa, p_o, 0.0, n, threshold, [i for i, (x, y) in enumerate(zip(a, b)) if x != y])


def _norm_dec(v: Any) -> str:
    try:
        d = Decimal(str(v))
        return format(d.normalize(), "f") if d != d.to_integral_value() else str(d.to_integral_value())
    except (InvalidOperation, ValueError):
        return str(v).strip()


def value_category(label: dict[str, Any] | None) -> Hashable:
    """值型标注（LBL-EVID / LBL-VALID）：值 + 单位 + 期间 全部一致才算同一类别。"""
    if label is None:
        return ("null",)
    period = label.get("period") or {}
    return (_norm_dec(label.get("value", label.get("extracted_value"))), label.get("unit"),
            period.get("start"), period.get("end"), label.get("valid_until"))


def iou(b1: Sequence[float], b2: Sequence[float]) -> float:
    x0, y0 = max(b1[0], b2[0]), max(b1[1], b2[1])
    x1, y1 = min(b1[2], b2[2]), min(b1[3], b2[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    a1 = max(0.0, b1[2] - b1[0]) * max(0.0, b1[3] - b1[1])
    a2 = max(0.0, b2[2] - b2[0]) * max(0.0, b2[3] - b2[1])
    union = a1 + a2 - inter
    return inter / union if union > 0 else 0.0


def locators_agree(l1: dict[str, Any], l2: dict[str, Any], *, iou_threshold: float = 0.5) -> bool:
    """定位一致：同页且 IoU ≥ 0.5，或同工作表同单元格（11 §2.5、M-EVI-02）。"""
    if l1.get("cell") or l2.get("cell"):
        return (l1.get("sheet"), l1.get("cell")) == (l2.get("sheet"), l2.get("cell"))
    if l1.get("page") != l2.get("page"):
        return False
    if l1.get("bbox") and l2.get("bbox"):
        return iou(l1["bbox"], l2["bbox"]) >= iou_threshold
    return l1.get("bbox") is None and l2.get("bbox") is None


def location_kappa(a: Sequence[dict[str, Any]], b: Sequence[dict[str, Any]]) -> KappaResult:
    """LBL-LOC：先用 IoU ≥ 0.5 判定一致，再以“页码”为类别计算 κ——一致时 B 取 A 的类别，不一致时 B 记为
    `<页码>|disagree`，因此 p_o 等于 IoU 一致率，p_e 由页码边际分布给出。"""
    ca: list[Hashable] = []
    cb: list[Hashable] = []
    for x, y in zip(a, b):
        key = x.get("cell") or f"p{x.get('page')}"
        ca.append(key)
        cb.append(key if locators_agree(x, y) else f"{y.get('cell') or 'p' + str(y.get('page'))}|disagree")
    return cohen_kappa(ca, cb)


def label_key(label_type: str, label: Any) -> Hashable:
    """把一份原始标注规约为可比较类别。"""
    if label_type in ("LBL-EVID", "LBL-VALID"):
        return value_category(label)
    if label_type == "LBL-MAP":
        if isinstance(label, dict):
            return "OOD" if label.get("ood") else (label.get("top1") or tuple(sorted(label.get("acceptable", []))))
        return label
    if isinstance(label, dict):
        return tuple(sorted((k, str(v)) for k, v in label.items()))
    if isinstance(label, list):
        return tuple(label)
    return label


def batch_kappa(samples: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按标注类型分别计算 κ（样本须有恰好两份 `annotations`）。返回 {label_type: KappaResult.to_dict() + status}。"""
    by_type: dict[str, tuple[list[Hashable], list[Hashable], list[str]]] = {}
    loc_pairs: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for s in samples:
        anns = s.get("annotations") or []
        if len(anns) != 2:
            continue
        lt = s["label_type"]
        if lt == "LBL-LOC" or (isinstance(anns[0]["label"], dict) and "bbox" in anns[0]["label"]
                               and "value" not in anns[0]["label"]):
            pa, pb = loc_pairs.setdefault("LBL-LOC", ([], []))
            pa.append(anns[0]["label"])
            pb.append(anns[1]["label"])
            continue
        a, b, ids = by_type.setdefault(lt, ([], [], []))
        a.append(label_key(lt, anns[0]["label"]))
        b.append(label_key(lt, anns[1]["label"]))
        ids.append(s["sample_id"])
    out: dict[str, dict[str, Any]] = {}
    for lt, (a, b, ids) in by_type.items():
        r = cohen_kappa(a, b)
        d = r.to_dict()
        d["disagreement_ids"] = [ids[i] for i in r.disagreements]
        d["status"] = "pass" if r.passed else "rejected_batch"  # 11 §2.5：低于门槛整批退回重标
        out[lt] = d
    for lt, (a, b) in loc_pairs.items():
        r = location_kappa(a, b)
        out[lt] = {**r.to_dict(), "status": "pass" if r.passed else "rejected_batch"}
    return out
