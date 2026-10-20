"""审计包 belegt-auditpack/1（03 §7）：导出、核验、篡改检测。"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from belegt_core import GraphError
from belegt_core.auditpack import export_audit_pack, verify_audit_pack

from helpers import AUDITOR, CO, FIX, ORG, SALES

REQ = FIX["request"]["id"]


def _pack(store, actor=CO, **kw):
    return export_audit_pack(store, REQ, actor=actor, **kw)


def test_export_and_verify(store, chain):
    store.put_blob(ORG, b"stadtwerke-jahresabrechnung-2025.pdf", "application/pdf")
    data, manifest = _pack(store)
    assert manifest["format"] == "belegt-auditpack/1" and manifest["filename"].startswith(f"belegt-auditpack_{REQ}_")
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    for f in ("manifest.json", "request/request.json", "questions.jsonl", "mappings.jsonl", "answers.jsonl",
              "evidence/evidence.jsonl", "calculations/calculations.jsonl", "approvals.jsonl", "conflicts.jsonl",
              "closed_questions.jsonl", "supplier_requests.jsonl", "graph/edges.jsonl", "traces.jsonl",
              "audit_events.jsonl", "audit_chain_proof.json", "README.de.md", "README.en.md"):
        assert f in names, f
    assert f"calculations/replay/{FIX['calculation']['id']}_v1.json" in names
    assert any(n.startswith("calculations/factor_rows/") for n in names)
    assert any(n.startswith("evidence/blobs/") for n in names)  # 原件按 sha256 内容寻址导出
    assert any(n.startswith("graph/lineage/") for n in names)
    answers = [json.loads(line) for line in z.read("answers.jsonl").decode().splitlines()]
    assert [a["version"] for a in answers] == [1, 2, 3]
    rep = verify_audit_pack(data)
    assert rep["ok"], rep["problems"]
    assert rep["checks"]["replay"][0]["input_hash_ok"] and rep["checks"]["numeric_answers_checked"] == 1
    # 导出本身写 export.create 审计
    assert store.audit_events(ORG)[-1]["action"] == "export.create"


def test_signed_pack(store, chain):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    data, _ = _pack(store, signing_key=key)
    assert verify_audit_pack(data, key.public_key())["ok"]
    other = Ed25519PrivateKey.generate().public_key()
    assert not verify_audit_pack(data, other)["ok"]


def test_tampered_pack_detected(store, chain):
    data, _ = _pack(store)
    zin = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zout:
        for n in zin.namelist():
            b = zin.read(n)
            if n == "answers.jsonl":
                b = b.replace(b'"value":"2027.2"', b'"value":"1027.2"')
            zout.writestr(n, b)
    rep = verify_audit_pack(out.getvalue())
    assert not rep["ok"] and any("answers.jsonl" in p for p in rep["problems"])


def test_audit_pack_roles(store, chain):
    assert _pack(store, actor=AUDITOR)[0]
    with pytest.raises(GraphError) as e:
        _pack(store, actor=SALES)
    assert e.value.code == "GRAPH_ACTOR_NOT_ALLOWED"
