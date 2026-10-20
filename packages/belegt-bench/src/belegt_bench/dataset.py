# Spec: 11 §2.8（版本化：manifest.json 记录样本 ID、分层标签、sha256、标注版本、字典版本、kappa 报告；发布即冻结）、
#       §2.9（分区）、§5.2 QG-07（manifest 校验）
"""数据集加载与 manifest 校验。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from .schema import SampleBase, parse_sample

DEFAULT_VERSION = "bench-v2027.1"
DATA_FILES = {
    "LBL-MAP": "mapping/questions.jsonl",
    "LBL-EVID": "evidence/samples.jsonl",
    "LBL-CONFLICT": "conflict/answer_sets.jsonl",
    "LBL-INJ": "injection/samples.jsonl",
    "LBL-DRAFT": "drafting/rnum_samples.jsonl",
}


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sample_hash(rec: dict[str, Any]) -> str:
    return sha256_hex(canonical(rec).encode("utf-8"))


def packaged_root(version: str = DEFAULT_VERSION) -> Path:
    return Path(str(resources.files("belegt_bench") / "data" / version))


def dataset_root(version_or_path: str | Path | None = None) -> Path:
    if version_or_path is None:
        return packaged_root()
    p = Path(version_or_path)
    if p.exists():
        return p
    return packaged_root(str(version_or_path))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(canonical(r) + "\n" for r in records), encoding="utf-8")


@dataclass
class Dataset:
    root: Path
    manifest: dict[str, Any]
    raw: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def version(self) -> str:
        return self.manifest.get("version", "unknown")

    def records(self, label_type: str, partition: str | Iterable[str] | None = None) -> list[dict[str, Any]]:
        recs = self.raw.get(label_type, [])
        if partition is None or partition == "all":
            return list(recs)
        parts = {partition} if isinstance(partition, str) else set(partition)
        return [r for r in recs if r["partition"] in parts]

    def samples(self, label_type: str, partition: str | Iterable[str] | None = None) -> list[SampleBase]:
        return [parse_sample(r) for r in self.records(label_type, partition)]

    def doc_path(self, rel: str) -> Path:
        return self.root / rel


def load_dataset(version_or_path: str | Path | None = None) -> Dataset:
    root = dataset_root(version_or_path)
    mpath = root / "manifest.json"
    if not mpath.exists():
        raise FileNotFoundError(f"manifest.json not found under {root}")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    raw = {lt: read_jsonl(root / rel) for lt, rel in DATA_FILES.items() if (root / rel).exists()}
    return Dataset(root, manifest, raw)


def verify_manifest(version_or_path: str | Path | None = None) -> list[str]:
    """QG-07 的 manifest 部分：文件哈希、样本哈希、ID 唯一、分区合法、schema 合法、文档哈希。返回错误列表（空 = 通过）。"""
    errors: list[str] = []
    root = dataset_root(version_or_path)
    try:
        ds = load_dataset(root)
    except Exception as e:  # noqa: BLE001
        return [f"MANIFEST_UNREADABLE: {e}"]
    m = ds.manifest
    for f in m.get("files", []):
        p = root / f["path"]
        if not p.exists():
            errors.append(f"FILE_MISSING: {f['path']}")
            continue
        if sha256_hex(p.read_bytes()) != f["sha256"]:
            errors.append(f"FILE_HASH_MISMATCH: {f['path']}")
    listed = {s["sample_id"]: s for s in m.get("samples", [])}
    seen: set[str] = set()
    for lt, recs in ds.raw.items():
        for r in recs:
            sid = r.get("sample_id")
            if sid in seen:
                errors.append(f"DUPLICATE_SAMPLE_ID: {sid}")
            seen.add(sid)
            ent = listed.get(sid)
            if ent is None:
                errors.append(f"SAMPLE_NOT_IN_MANIFEST: {sid}")
                continue
            if ent["sha256"] != sample_hash(r):
                errors.append(f"SAMPLE_HASH_MISMATCH: {sid}")
            if ent["partition"] != r.get("partition"):
                errors.append(f"PARTITION_MISMATCH: {sid}")
            try:
                parse_sample(r)
            except Exception as e:  # noqa: BLE001
                errors.append(f"SCHEMA_INVALID: {sid}: {str(e).splitlines()[0]}")
            if lt == "LBL-EVID":
                dp = root / r["doc_path"]
                if not dp.exists() or sha256_hex(dp.read_bytes()) != r["doc_sha256"]:
                    errors.append(f"DOC_HASH_MISMATCH: {sid}")
    for sid in set(listed) - seen:
        errors.append(f"SAMPLE_MISSING: {sid}")
    kr = m.get("kappa_report", {})
    for lt, rep in kr.items():
        if isinstance(rep, dict) and rep.get("status") == "rejected_batch":
            errors.append(f"KAPPA_BELOW_THRESHOLD: {lt}")
    return errors
