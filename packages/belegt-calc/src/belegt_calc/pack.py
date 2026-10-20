# Spec: 03 §7（审计包导出格式与核验步骤 1–2）；05 §8.2（verify-pack）
"""审计包核验：``belegt-calc verify-pack <zip>``。

做的事（03 §7 核验步骤 1–2）：
1. ``manifest.json``：逐个文件校验 ``sha256`` 与 ``bytes``，并重算 ``pack_hash = sha256(JCS(files))``；
   ``manifest.sig``（Ed25519）只有在安装了 ``cryptography`` 且给出公钥时才校验，否则记为 ``skipped``
   （belegt-calc 运行期只依赖标准库 + PyYAML）。
2. ``calculations/replay/<calc_id>.json``：由原文 M 重算 ``input_hash``，与记录值比较。
3. ``calculations/calculations.jsonl``：每个 Calculation（取每个 id 的最高版本）用包内
   ``calculations/factor_rows/<fs_id>.csv`` 重建因子库（rows_hash 必须与 M 一致），或回退到 ``--factors-dir`` /
   随包因子库，执行 ``replay``：input_hash 与 output_hash 都一致才算通过；output_hash 不同即漂移。
   受许可限制、只导出行 ID 与哈希的因子库无法重算数值，记 ``input_hash_only``。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

from .engine import replay
from .errors import CalcError
from .factors import factor_set_from_dict, load_factor_set
from .hashing import input_hash, jcs, rows_hash
from .types import FactorSet


def _hex(v: str) -> str:
    return v.split(":", 1)[1] if isinstance(v, str) and v.startswith("sha256:") else v


def _rows_from_csv(text: str) -> list[dict]:
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        row: dict[str, Any] = {}
        for k, v in rec.items():
            if k is None or v is None or v == "":
                continue
            if "." in k:
                a, b = k.split(".", 1)
                row.setdefault(a, {})[b] = v
            else:
                row[k] = v
        rows.append(row)
    return rows


def _fs_from_pack(z: zipfile.ZipFile, fs_id: str, expected_rows_hash: str | None) -> FactorSet | None:
    names = set(z.namelist())
    for cand in (f"calculations/factor_rows/{fs_id}.csv", f"calculations/factor_rows/{fs_id.replace(':', '_')}.csv"):
        if cand in names:
            rows = _rows_from_csv(z.read(cand).decode("utf-8"))
            if not rows or "value" not in rows[0]:
                return None  # 受许可限制：只有行 ID 与哈希
            h = rows_hash(rows)
            if expected_rows_hash and h != expected_rows_hash:
                return None
            source = fs_id.split(":", 1)[1].split("@", 1)[0]
            doc = {"id": fs_id, "source": source, "license": "unknown", "published_at": "", "valid_from": "",
                   "rows": rows, "rows_hash": h, "status": "active"}
            return factor_set_from_dict(doc, origin=cand)
    return None


def _fs_external(fs_id: str, factors_dir: Path | None) -> FactorSet | None:
    if factors_dir is not None:
        p = factors_dir / f"{fs_id[3:]}.yaml"
        if p.exists():
            return load_factor_set(p)
    try:
        return load_factor_set(fs_id)
    except CalcError:
        return None


def verify_pack(path: str | Path, *, factors_dir: str | Path | None = None, public_key: bytes | None = None) -> dict:
    fdir = Path(factors_dir) if factors_dir else None
    report: dict[str, Any] = {"pack": str(path), "files": {"checked": 0, "failed": []}, "pack_hash": None,
                              "signature": "absent", "replay_preimages": [], "calculations": []}
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        # 1. manifest
        if "manifest.json" in names:
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            files = manifest.get("files") or []
            for f in files:
                report["files"]["checked"] += 1
                p = f.get("path")
                if p not in names:
                    report["files"]["failed"].append({"path": p, "reason": "missing"})
                    continue
                data = z.read(p)
                if hashlib.sha256(data).hexdigest() != _hex(f.get("sha256", "")):
                    report["files"]["failed"].append({"path": p, "reason": "sha256 mismatch"})
                elif f.get("bytes") is not None and int(f["bytes"]) != len(data):
                    report["files"]["failed"].append({"path": p, "reason": "size mismatch"})
            if "pack_hash" in manifest:
                calc = hashlib.sha256(jcs(files).encode("utf-8")).hexdigest()
                report["pack_hash"] = "ok" if calc == _hex(manifest["pack_hash"]) else "mismatch"
            if "manifest.sig" in names:
                report["signature"] = _verify_sig(z.read("manifest.json"), z.read("manifest.sig"), public_key)
        else:
            report["files"]["failed"].append({"path": "manifest.json", "reason": "missing"})

        # 2. replay 原文
        for n in sorted(x for x in names if x.startswith("calculations/replay/") and x.endswith(".json")):
            doc = json.loads(z.read(n).decode("utf-8"))
            m = doc.get("input_hash_preimage") or doc.get("M") or doc.get("m")
            entry = {"file": n, "ok": False}
            try:
                got = input_hash(m)
                entry["recomputed_input_hash"] = got
                entry["ok"] = got == doc.get("input_hash")
            except (CalcError, TypeError, KeyError) as e:
                entry["error"] = str(e)
            report["replay_preimages"].append(entry)

        # 3. Calculation 重放
        calcs: dict[str, dict] = {}
        if "calculations/calculations.jsonl" in names:
            for line in z.read("calculations/calculations.jsonl").decode("utf-8").splitlines():
                if line.strip():
                    c = json.loads(line)
                    cid = c.get("id", f"#{len(calcs)}")
                    if cid not in calcs or c.get("version", 0) > calcs[cid].get("version", 0):
                        calcs[cid] = c
        preimages: dict[str, dict] = {}
        for n in names:
            if n.startswith("calculations/replay/") and n.endswith(".json"):
                d = json.loads(z.read(n).decode("utf-8"))
                preimages[Path(n).stem] = d.get("input_hash_preimage") or d.get("M") or {}
        for cid in sorted(calcs):
            c = calcs[cid]
            fs_id = c.get("factor_set_version", "")
            rh = (preimages.get(cid) or {}).get("factor_set_rows_hash")
            fs = _fs_from_pack(z, fs_id, rh) or _fs_external(fs_id, fdir)
            entry: dict[str, Any] = {"id": cid, "version": c.get("version"), "factor_set_version": fs_id}
            if fs is None or (rh and fs.rows_hash != rh):
                entry["status"] = "input_hash_only"
                if cid in preimages:
                    try:
                        entry["input_hash_ok"] = input_hash(preimages[cid]) == c.get("input_hash")
                    except CalcError as e:
                        entry["input_hash_ok"] = False
                        entry["error"] = str(e)
                report["calculations"].append(entry)
                continue
            rep = replay(c, fs)
            entry.update({"status": "ok" if rep.ok else ("drift" if rep.drift else "failed"),
                          "input_hash_ok": rep.input_hash_match, "output_hash_ok": rep.output_hash_match})
            if rep.error:
                entry["error"] = rep.error
            report["calculations"].append(entry)

    failed = bool(report["files"]["failed"]) or report["pack_hash"] == "mismatch" or report["signature"] == "invalid"
    failed = failed or any(not e["ok"] for e in report["replay_preimages"])
    failed = failed or any(e.get("status") == "failed" or e.get("input_hash_ok") is False for e in report["calculations"])
    drift = any(e.get("status") == "drift" for e in report["calculations"])
    report["ok"] = not failed and not drift
    report["drift"] = drift
    return report


def _verify_sig(message: bytes, sig: bytes, public_key: bytes | None) -> str:
    if public_key is None:
        return "skipped (no public key)"
    try:  # 可选依赖
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # type: ignore
    except Exception:  # noqa: BLE001
        return "skipped (cryptography not installed)"
    try:
        raw = sig
        try:
            import base64

            raw = base64.b64decode(sig, validate=True)
        except Exception:  # noqa: BLE001
            pass
        Ed25519PublicKey.from_public_bytes(public_key).verify(raw, message)
        return "valid"
    except Exception:  # noqa: BLE001
        return "invalid"
