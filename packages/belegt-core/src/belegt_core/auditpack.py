# Spec: 03 §7（审计包 belegt-auditpack/1）、07 §7.4（导出）、NFR-04、FR-602
"""审计包导出与核验。

``export_audit_pack(store, req_id, actor=...)`` → ``(zip_bytes, manifest)``；同时写 ``AuditEvent action=export.create``。
``verify_audit_pack(zip_bytes, public_key=None)`` 按 03 §7 核验步骤 1–4 执行（签名/文件哈希、input/output_hash 重算、
R-NUM/R-VAL、审计片段哈希链）。商业许可因子库（``license=proprietary-byol``）只导出行 ID 与行哈希（03 待决 4）。
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import html
import io
import json
import zipfile
from decimal import Decimal
from typing import Any, Iterable

from .authz import AUDIT_PACK_ROLES, Actor
from .audit import verify_events
from .canonical import canonicalize, hash_obj, sha256_tag
from .errors import GraphError
from .hashing import build_input_m, input_hash, output_hash

FORMAT = "belegt-auditpack/1"
_MIME_EXT = {"application/pdf": "pdf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
             "text/csv": "csv", "image/png": "png", "application/json": "json"}


def _jsonl(objs: Iterable[dict[str, Any]]) -> bytes:
    return "".join(canonicalize(o, strip_nulls=False) + "\n" for o in objs).encode("utf-8")


def _ext(mime: str | None) -> str:
    return _MIME_EXT.get(mime or "", "bin")


def _ids_from(refs: Iterable[str]) -> set[str]:
    return {r.split("#", 1)[0] for r in refs if isinstance(r, str)}


def _redact_sreq(s: dict[str, Any]) -> dict[str, Any]:
    s = dict(s)
    if "portal_token_hash" in s:
        s["portal_token_hash"] = "redacted"
    return s


def export_audit_pack(store: Any, req_id: str, *, actor: Actor, as_of_tx: str | None = None,
                      signing_key: Any = None, excerpt_only: bool = False) -> tuple[bytes, dict[str, Any]]:
    if actor.is_user and not (actor.roles & AUDIT_PACK_ROLES):
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "audit pack export requires compliance_owner/auditor_readonly/org_admin",
                         rule_id="AUTHZ")
    org = actor.org_id
    req_versions = [v for v in store.history(req_id, org_id=org)]
    if not req_versions:
        raise GraphError("GRAPH_REF_NOT_FOUND", f"{req_id} not found")
    now = store.peek_now()
    kw = {"org_id": org, "include_history": True}
    if as_of_tx:
        kw["as_of_tx"] = as_of_tx
        req_versions = [v for v in req_versions if v["created_at"] <= store_ts(as_of_tx)]

    def hist(type_name: str, pred) -> list[dict[str, Any]]:
        return [b for b in store.query(type_name, **kw) if pred(b)]

    questions = hist("Question", lambda b: b.get("request_id") == req_id)
    qids = {q["id"] for q in questions}
    mappings = hist("Mapping", lambda b: b.get("question_id") in qids)
    answers = hist("Answer", lambda b: bool(set(b.get("question_refs") or []) & qids)
                   or (b.get("submission") or {}).get("request_id") == req_id)
    aids = {a["id"] for a in answers}
    # 谱系闭包：Calculation → Evidence / Calculation
    calc_ids = _ids_from(a.get("calculation_ref") for a in answers)
    ev_ids = _ids_from(r for a in answers for r in a.get("evidence_refs") or [])
    calcs: dict[str, list[dict[str, Any]]] = {}
    frontier = set(calc_ids)
    while frontier:
        cid = frontier.pop()
        vs = [v for v in store.history(cid, org_id=org) if not as_of_tx or v["created_at"] <= store_ts(as_of_tx)]
        calcs[cid] = vs
        for v in vs:
            for inp in v.get("inputs") or []:
                tid = inp["ref"].split("#", 1)[0]
                if tid.startswith("ev_"):
                    ev_ids.add(tid)
                elif tid not in calcs:
                    frontier.add(tid)
    evidence = [v for eid in sorted(ev_ids) for v in store.history(eid, org_id=org)
                if not as_of_tx or v["created_at"] <= store_ts(as_of_tx)]
    calculations = [v for cid in sorted(calcs) for v in calcs[cid]]
    approvals = hist("Approval", lambda b: b.get("request_id") == req_id or b.get("subject_ref", "").split("#")[0] in aids)
    conflicts = hist("Conflict", lambda b: b.get("request_id") == req_id or bool(_ids_from(b.get("subject_refs") or []) & aids))
    targets = qids | aids | ev_ids | {m["id"] for m in mappings} | set(calcs)
    cqs = hist("ClosedQuestion", lambda b: b.get("request_id") == req_id or b.get("target_ref", "").split("#")[0] in targets)
    sreqs = [_redact_sreq(s) for s in hist("SupplierRequest", lambda b: (b.get("trigger_ref") or "").split("#")[0] in targets)]

    files: dict[str, bytes] = {}
    files["request/request.json"] = json.dumps(req_versions, ensure_ascii=False, indent=2).encode("utf-8")
    for sha in sorted({s for v in req_versions for s in (v.get("source_ref") or {}).get("blob_sha256", [])}):
        b = store.get_blob(org, sha)
        if b:
            files[f"request/source/{sha.split(':')[1]}.{_ext(b[1])}"] = b[0]
    files["questions.jsonl"] = _jsonl(questions)
    files["mappings.jsonl"] = _jsonl(mappings)
    files["answers.jsonl"] = _jsonl(answers)
    files["evidence/evidence.jsonl"] = _jsonl(evidence)
    for sha in sorted({(v.get("locator") or {}).get("blob_sha256") for v in evidence} - {None}):
        b = store.get_blob(org, sha)
        if b:
            files[f"evidence/blobs/{sha.split(':')[1]}.{_ext(b[1])}"] = b[0]
    files["calculations/calculations.jsonl"] = _jsonl(calculations)
    factor_sets: dict[str, dict[str, Any]] = {}
    for c in calculations:
        fs = store.get(c["factor_set_version"], org_id=org)
        if fs is None:
            continue
        factor_sets[fs["id"]] = fs
        m = build_input_m(c, fs["rows_hash"])
        files[f"calculations/replay/{c['id']}_v{c['version']}.json"] = json.dumps(
            {"calc_ref": f"{c['id']}#v{c['version']}", "m": m, "expected_input_hash": c["input_hash"],
             "expected_output_hash": c["output_hash"], "engine_version": c["engine_version"]},
            ensure_ascii=False, indent=2).encode("utf-8")
    for fs_id, fs in factor_sets.items():
        used = {f["source_row_id"] for c in calculations if c["factor_set_version"] == fs_id for f in c.get("factors_used") or []}
        rows = [r for r in store.factor_rows(fs_id) if r["source_row_id"] in used]
        buf = io.StringIO()
        restricted = fs.get("license") == "proprietary-byol"
        cols = ["source_row_id", "row_hash"] if restricted else ["source_row_id", "factor_key", "geography", "year", "value",
                                                                 "unit", "row_hash"]
        w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in sorted(rows, key=lambda r: r["source_row_id"]):
            row = {k: r[k] for k in ("factor_key", "geography", "year", "value", "unit", "source_row_id")}
            out = {"source_row_id": r["source_row_id"], "row_hash": hash_obj(row)}
            if not restricted:
                out.update(row)
            w.writerow(out)
        files[f"calculations/factor_rows/{fs_id.replace(':', '_')}.csv"] = buf.getvalue().encode("utf-8")
    files["approvals.jsonl"] = _jsonl(approvals)
    files["conflicts.jsonl"] = _jsonl(conflicts)
    files["closed_questions.jsonl"] = _jsonl(cqs)
    files["supplier_requests.jsonl"] = _jsonl(sreqs)

    included = {f"{o['id']}#v{o['version']}" for o in req_versions + questions + mappings + answers + evidence
                + calculations + approvals + conflicts + cqs + sreqs}
    included_ids = _ids_from(included)
    edges = [e for oid in sorted(included_ids) for e in store.edges(org_id=org, from_id=oid)
             if f"{e['from_id']}#v{e['from_version']}" in included]
    files["graph/edges.jsonl"] = _jsonl(edges)
    for a in answers:
        if a.get("status") in ("approved", "submitted"):
            lin = store.lineage(f"{a['id']}#v{a['version']}", org_id=org)
            items = "".join(f"<li><code>{html.escape(n)}</code></li>" for n in lin["nodes"])
            files[f"graph/lineage/{a['id']}_v{a['version']}.html"] = (
                f"<!doctype html><meta charset='utf-8'><title>Lineage {a['id']}</title><h1>{html.escape(a['id'])}"
                f"#v{a['version']}</h1><p>value: {html.escape(str(a.get('value')))} {html.escape(str(a.get('unit') or ''))}"
                f"</p><ul>{items}</ul>").encode("utf-8")
    trace_ids = {o["provenance"] for o in req_versions + questions + mappings + answers + evidence + calculations}
    traces = [{k: v for k, v in t.items() if k != "payload_blob"} for t in store.traces(org, trace_ids)]
    files["traces.jsonl"] = _jsonl(traces)

    # 审计片段：覆盖全部相关对象版本的连续区间
    all_events = store.audit_events(org)
    rel = [e["seq"] for e in all_events if e.get("object_ref") in included]
    segment = [e for e in all_events if rel and min(rel) <= e["seq"] <= max(rel)]
    files["audit_events.jsonl"] = _jsonl(segment)
    files["audit_chain_proof.json"] = json.dumps({
        "org_id": org, "first_seq": segment[0]["seq"] if segment else None,
        "last_seq": segment[-1]["seq"] if segment else None,
        "first_prev_hash": segment[0]["prev_hash"] if segment else None,
        "last_event_hash": segment[-1]["event_hash"] if segment else None,
        "anchor": None, "anchor_note": "daily WORM/RFC 3161 anchor by service:audit-anchor (07 §7.2); not part of core",
    }, indent=2).encode("utf-8")
    files["README.en.md"] = _README_EN.format(req_id=req_id).encode("utf-8")
    files["README.de.md"] = _README_DE.format(req_id=req_id).encode("utf-8")

    entries = [{"path": p, "sha256": sha256_tag(b), "bytes": len(b)} for p, b in sorted(files.items())]
    dictionary_release = None
    try:
        import belegt_dictionary  # type: ignore

        dictionary_release = getattr(belegt_dictionary, "__version__", None)
    except Exception:
        pass
    manifest = {
        "format": FORMAT, "req_id": req_id, "org_id": org, "as_of_tx": as_of_tx or now, "generated_at": now,
        "generated_by": actor.id, "dictionary_release": dictionary_release,
        "engine_versions": sorted({c["engine_version"] for c in calculations}),
        "factor_sets": sorted(factor_sets), "files": entries, "pack_hash": hash_obj(entries),
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    stamp = dt.datetime.strptime(now, "%Y-%m-%dT%H:%M:%S.%fZ").strftime("%Y%m%dT%H%M%SZ")
    manifest["filename"] = f"belegt-auditpack_{req_id}_{stamp}.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", manifest_bytes)
        if signing_key is not None:
            z.writestr("manifest.sig", base64.b64encode(_sign(signing_key, manifest_bytes)))
        for p, b in sorted(files.items()):
            z.writestr(p, b)
    data = buf.getvalue()
    store.record_audit(actor, "export.create", object_ref=f"{req_id}", content_hash=manifest["pack_hash"],
                       details={"format": FORMAT, "files": len(entries), "filename": manifest["filename"]})
    return data, manifest


def store_ts(s: str) -> str:
    from .store.graph_store import parse_ts

    return parse_ts(s)


def _sign(key: Any, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if isinstance(key, (bytes, bytearray)):
        key = Ed25519PrivateKey.from_private_bytes(bytes(key))
    return key.sign(data)


def verify_audit_pack(data: bytes, public_key: Any = None) -> dict[str, Any]:
    """03 §7 核验步骤 1–4。返回 ``{ok, problems, checks}``。"""
    problems: list[str] = []
    checks: dict[str, Any] = {}
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    manifest_bytes = z.read("manifest.json")
    manifest = json.loads(manifest_bytes)
    if manifest.get("format") != FORMAT:
        problems.append(f"unknown format {manifest.get('format')}")
    # 1 签名与文件哈希
    if public_key is not None:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            pk = Ed25519PublicKey.from_public_bytes(public_key) if isinstance(public_key, (bytes, bytearray)) else public_key
            pk.verify(base64.b64decode(z.read("manifest.sig")), manifest_bytes)
            checks["signature"] = "ok"
        except Exception as e:
            problems.append(f"manifest signature invalid: {type(e).__name__}")
    listed = set()
    for f in manifest["files"]:
        listed.add(f["path"])
        if f["path"] not in names:
            problems.append(f"missing file {f['path']}")
            continue
        b = z.read(f["path"])
        if sha256_tag(b) != f["sha256"] or len(b) != f["bytes"]:
            problems.append(f"hash mismatch {f['path']}")
    extra = names - listed - {"manifest.json", "manifest.sig"}
    if extra:
        problems.append(f"unlisted files: {sorted(extra)}")
    if hash_obj(manifest["files"]) != manifest.get("pack_hash"):
        problems.append("pack_hash mismatch")
    checks["files"] = len(listed)

    def jl(p: str) -> list[dict[str, Any]]:
        return [json.loads(line) for line in z.read(p).decode("utf-8").splitlines() if line.strip()] if p in names else []

    # 2 重放 input_hash / output_hash
    calcs = {f"{c['id']}#v{c['version']}": c for c in jl("calculations/calculations.jsonl")}
    replays = []
    for p in sorted(n for n in names if n.startswith("calculations/replay/")):
        r = json.loads(z.read(p))
        c = calcs.get(r["calc_ref"])
        ok_in = input_hash(r["m"]) == r["expected_input_hash"]
        ok_out = c is not None and output_hash(c["result"], c.get("breakdown")) == r["expected_output_hash"]
        if not (ok_in and ok_out):
            problems.append(f"replay mismatch {r['calc_ref']}")
        replays.append({"calc_ref": r["calc_ref"], "input_hash_ok": ok_in, "output_hash_ok": ok_out})
    checks["replay"] = replays
    # 3 R-NUM / R-VAL
    evs = {f"{e['id']}#v{e['version']}": e for e in jl("evidence/evidence.jsonl")}
    nchecked = 0
    for a in jl("answers.jsonl"):
        if a.get("status") not in ("approved", "submitted") or a.get("value_type") != "number":
            continue
        nchecked += 1
        if not (a.get("evidence_refs") or a.get("calculation_ref")):
            problems.append(f"R-NUM violated {a['id']}#v{a['version']}")
            continue
        if a.get("calculation_ref"):
            c = calcs.get(a["calculation_ref"])
            src = (c or {}).get("result", {}).get("value")
            src_unit = (c or {}).get("result", {}).get("unit")
        else:
            nums = [evs[r] for r in a.get("evidence_refs") or [] if r in evs and evs[r].get("value_type") == "number"]
            src = nums[0]["extracted_value"] if len(nums) == 1 else None
            src_unit = nums[0].get("unit") if len(nums) == 1 else None
        if src is not None and src_unit and a.get("unit") and src_unit != a["unit"]:  # R-VAL：确定性单位换算后逐位比较
            try:
                from .units import default_units

                src = default_units().convert(Decimal(str(src)), src_unit, a["unit"])
            except Exception:  # noqa: BLE001 - 不可换算 → 视为不一致
                src = None
        if src is None or Decimal(str(src)).normalize() != Decimal(str(a["value"])).normalize():
            problems.append(f"R-VAL violated {a['id']}#v{a['version']}")
    checks["numeric_answers_checked"] = nchecked
    # 4 审计片段哈希链
    proof = json.loads(z.read("audit_chain_proof.json")) if "audit_chain_proof.json" in names else {}
    seg = jl("audit_events.jsonl")
    rep = verify_events(seg, org_id=proof.get("org_id", ""), expected_prev=proof.get("first_prev_hash"))
    if seg and rep.head_hash != proof.get("last_event_hash"):
        rep.problems.append("segment tail does not match proof")
    if rep.problems:
        problems.extend(f"audit chain: {p}" for p in rep.problems)
    checks["audit_chain"] = {"events": len(seg), "ok": not rep.problems}
    return {"ok": not problems, "problems": problems, "checks": checks, "manifest": {k: manifest[k] for k in
            ("format", "req_id", "org_id", "as_of_tx", "pack_hash")}}


_README_EN = """# Belegt audit pack ({req_id})

Format `belegt-auditpack/1` (Belegt design doc 03 §7). Verification:

1. `belegt-calc verify-pack <zip>` – checks the manifest signature (`manifest.sig`, Ed25519) and every file hash; `pack_hash = sha256(JCS(files))`.
2. For each `calculations/replay/*.json`, recompute `input_hash` from `m` (RFC 8785 JCS + SHA-256) and `output_hash` from the calculation's `result` + `breakdown`.
3. Every `approved`/`submitted` number answer must cite evidence or a calculation (R-NUM) and equal the cited value (R-VAL).
4. `audit_chain_proof.json` + `audit_events.jsonl`: `event_hash = sha256(prev_hash || JCS(event without event_hash))`, consecutive `seq`.
"""
_README_DE = """# Belegt-Prüfpaket ({req_id})

Format `belegt-auditpack/1` (Designdokument 03 §7). Prüfschritte:

1. `belegt-calc verify-pack <zip>` – Signatur von `manifest.json` (`manifest.sig`, Ed25519) und alle Datei-Hashes prüfen; `pack_hash = sha256(JCS(files))`.
2. Für jede Datei `calculations/replay/*.json` `input_hash` aus `m` (RFC 8785 JCS + SHA-256) und `output_hash` aus `result` + `breakdown` neu berechnen.
3. Jede freigegebene/übermittelte Zahlenantwort muss Evidenz oder eine Berechnung referenzieren (R-NUM) und deren Wert exakt tragen (R-VAL).
4. `audit_chain_proof.json` + `audit_events.jsonl`: `event_hash = sha256(prev_hash || JCS(Ereignis ohne event_hash))`, lückenlose `seq`.
"""
