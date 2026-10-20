# Spec: 11 §2.1（belegt-bench 公开集）、§2.2(3)（bench-synth 确定性生成）、§2.6–§2.9（规模、分层、版本化、防泄漏）、
#       §7.1（开源内容：合成问卷、单据、冲突答案集、注入样本；金丝雀 GUID）
"""`belegt-bench build`：确定性构建黄金集 `bench-v2027.1`（同一代码 → 逐字节相同的数据文件与 manifest）。

分区规划（按问卷/文档/答案集为簇，簇不跨分区）：
- `fewshot`：2 份只含字典标签直译（easy）的问卷，虚拟企业 org-F1/F2（LEAK-03：不出现在 test）；
- `dev` / `test` / `holdout`：其余问卷；模板 `private_oem_c` 只出现在 test（模板级留出，11 §2.9）；
- 手写难例按 9 个伪问卷簇分布（dev 2、test 6、holdout 1）。
"""
from __future__ import annotations

import json
import random
import uuid
from pathlib import Path
from typing import Any

from .build_evidence import doc_specs, render
from .build_sets import CONFLICT_SETS, DRAFT_SAMPLES, INJECTION_SAMPLES
from .dataset import DATA_FILES, DEFAULT_VERSION, canonical, sample_hash, sha256_hex, write_jsonl
from .schema import ANNOTATION_VERSION, json_schemas, parse_sample
from .synth import shingles, synthesize_questionnaire
from .synth_lexicon import HARD_CASES

SEED = 20271
GENERATOR = "belegt_bench.build@0.1.0"
BUILD_DATE = "2026-10-19"  # 固定（可复现）；发版时更新

#: (簇 ID, 模板, 语言, 格式, 虚拟企业, 分区, 题数, 收件日)
QUESTIONNAIRES: list[tuple[str, str, str, str, str, str, int, str]] = [
    ("QN-F01", "vsme", "de", "xlsx", "org-F1", "fewshot", 8, "2026-01-12"),
    ("QN-F02", "cdp_climate", "en", "xlsx", "org-F2", "fewshot", 8, "2026-01-20"),
    ("QN-D01", "vsme", "en", "xlsx", "org-A", "dev", 9, "2026-02-03"),
    ("QN-D02", "cbam_communication", "de", "xlsx", "org-A", "dev", 7, "2026-02-10"),
    ("QN-D03", "private_oem_a", "de", "xlsx", "org-B", "dev", 10, "2026-02-17"),
    ("QN-D04", "pact_pcf", "en", "json", "org-B", "dev", 7, "2026-02-24"),
    ("QN-D05", "reach_scip", "de", "pdf_text", "org-B", "dev", 6, "2026-03-03"),
    ("QN-D06", "cmrt", "en", "xlsx", "org-A", "dev", 6, "2026-03-10"),
    ("QN-T01", "vsme", "de", "xlsx", "org-C", "test", 10, "2026-03-17"),
    ("QN-T02", "vsme", "de", "pdf_text", "org-D", "test", 10, "2026-03-24"),
    ("QN-T03", "vsme", "en", "docx", "org-E", "test", 9, "2026-03-31"),
    ("QN-T04", "cdp_climate", "en", "xlsx", "org-C", "test", 10, "2026-04-07"),
    ("QN-T05", "cdp_climate", "de", "xlsx", "org-D", "test", 10, "2026-04-14"),
    ("QN-T06", "cbam_communication", "en", "xlsx", "org-E", "test", 8, "2026-04-21"),
    ("QN-T07", "cbam_communication", "de", "xlsx", "org-G", "test", 8, "2026-04-28"),
    ("QN-T08", "cmrt", "en", "xlsx", "org-C", "test", 6, "2026-05-05"),
    ("QN-T09", "pact_pcf", "en", "json", "org-D", "test", 7, "2026-05-12"),
    ("QN-T10", "catenax_pcf", "en", "json", "org-E", "test", 6, "2026-05-19"),
    ("QN-T11", "eudr_dds", "de", "pdf_text", "org-G", "test", 7, "2026-05-26"),
    ("QN-T12", "eudr_dds", "en", "email_body", "org-C", "test", 6, "2026-06-02"),
    ("QN-T13", "reach_scip", "en", "xlsx", "org-D", "test", 7, "2026-06-09"),
    ("QN-T14", "eu_battery_passport", "en", "pdf_text", "org-E", "test", 8, "2026-06-16"),
    ("QN-T15", "private_oem_a", "de", "xlsx", "org-G", "test", 10, "2026-06-23"),
    ("QN-T16", "private_oem_a", "en", "xlsx", "org-C", "test", 10, "2026-06-30"),
    ("QN-T17", "private_oem_b", "de", "docx", "org-D", "test", 10, "2026-07-07"),
    ("QN-T18", "private_oem_b", "de", "email_body", "org-E", "test", 8, "2026-07-14"),
    ("QN-T19", "private_oem_c", "de", "xlsx", "org-G", "test", 10, "2026-07-21"),
    ("QN-T20", "private_oem_c", "en", "pdf_text", "org-C", "test", 10, "2026-07-28"),
    ("QN-T21", "private_oem_a", "de", "csv", "org-D", "test", 10, "2026-08-04"),
    ("QN-T22", "vsme", "de", "csv", "org-E", "test", 9, "2026-08-11"),
    ("QN-T23", "private_oem_b", "en", "xlsx", "org-G", "test", 10, "2026-08-18"),
    ("QN-T24", "cdp_climate", "en", "pdf_text", "org-E", "test", 9, "2026-08-25"),
    ("QN-T25", "vsme", "zh", "xlsx", "org-C", "test", 8, "2026-08-27"),
    ("QN-O01", "vsme", "de", "xlsx", "org-H", "holdout", 9, "2026-09-01"),
    ("QN-O02", "private_oem_a", "de", "xlsx", "org-H", "holdout", 10, "2026-09-08"),
    ("QN-O03", "cbam_communication", "en", "xlsx", "org-H", "holdout", 7, "2026-09-15"),
    ("QN-O04", "cdp_climate", "en", "xlsx", "org-H", "holdout", 9, "2026-09-22"),
    ("QN-O05", "private_oem_b", "de", "pdf_text", "org-H", "holdout", 8, "2026-09-29"),
    ("QN-O06", "cdp_climate", "zh", "xlsx", "org-H", "holdout", 6, "2026-10-06"),
]
HARD_CLUSTERS = [("QN-H01", "dev", "org-A"), ("QN-H02", "dev", "org-B"), ("QN-H03", "test", "org-C"),
                 ("QN-H04", "test", "org-D"), ("QN-H05", "test", "org-E"), ("QN-H06", "test", "org-G"),
                 ("QN-H07", "test", "org-C"), ("QN-H08", "test", "org-D"), ("QN-H09", "holdout", "org-H")]
HELD_OUT_TEMPLATES = ["private_oem_c"]


def _canary(version: str, rel: str) -> str:
    return f"belegt-bench-canary:{uuid.uuid5(uuid.NAMESPACE_URL, f'https://belegt.org/bench/{version}/{rel}')}"


def build_mapping(dictionary: Any) -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    out: list[dict[str, Any]] = []
    fewshot_sh: list[set[str]] = []
    for cid, tpl, lang, fmt, org, part, n, recv in QUESTIONNAIRES:
        items = synthesize_questionnaire(dictionary, qn_id=cid, template=tpl, lang=lang, n=n, rng=rng,
                                         avoid=None if part == "fewshot" else fewshot_sh,
                                         easy_only=(part == "fewshot"))
        if part == "fewshot":
            fewshot_sh.extend(shingles(it.text) for it in items)
        for it in items:
            out.append({
                "sample_id": f"map:{it.question_id}", "label_type": "LBL-MAP", "partition": part,
                "strata": {"template": tpl, "lang": lang, "format": fmt}, "cluster_id": cid, "org": org,
                "synthetic": True, "flags": ["synthetic"] + (["hard"] if it.difficulty == "hard" else []),
                "difficulty": it.difficulty, "received_at": recv, "question_id": it.question_id,
                "native_id": it.native_id, "text": it.text, "lang": it.lang, "answer_format": it.answer_format,
                "acceptable": it.acceptable, "ood": False, "unit_hint": it.unit_hint, "period_hint": it.period_hint,
                "boundary_hint": it.boundary_hint, "template_mapping_ref": it.template_mapping_ref,
                "adjudicated_by": "synth:generator", "notes": f"source={it.source}",
            })
    # 手写难例：确定性分簇
    hard = list(HARD_CASES)
    per = -(-len(hard) // len(HARD_CLUSTERS))
    for ci, (cid, part, org) in enumerate(HARD_CLUSTERS):
        for j, h in enumerate(hard[ci * per:(ci + 1) * per], start=1):
            sh = shingles(h["text"])
            if part != "fewshot" and any(len(sh & a) / len(sh | a) >= 0.7 for a in fewshot_sh):
                continue
            flags = ["hard"] + list(h.get("flags", []))
            ood = bool(h.get("ood"))
            fmt = ["xlsx", "email_body", "pdf_text"][(ci + j) % 3]
            out.append({
                "sample_id": f"map:{cid}-q{j:02d}", "label_type": "LBL-MAP", "partition": part,
                "strata": {"template": "private_oem_hard", "lang": h["lang"], "format": fmt}, "cluster_id": cid,
                "org": org, "synthetic": False, "flags": flags, "difficulty": "hard",
                "received_at": "2026-06-01", "question_id": f"{cid}-q{j:02d}", "native_id": str(j),
                "text": h["text"], "lang": h["lang"], "answer_format": None, "acceptable": h["acceptable"],
                "ood": ood, "unit_hint": h.get("unit_hint"), "period_hint": None, "boundary_hint": None,
                "hard_kind": h["kind"], "adjudicated_by": "maintainers:pending-double-annotation",
            })
    return out


def _evid_partition(i: int) -> str:
    return ["dev", "test", "test", "test", "holdout", "test", "dev", "test"][i % 8]


def build_evidence(out_dir: Path, version: str) -> list[dict[str, Any]]:
    recs = []
    for i, spec in enumerate(doc_specs()):
        rel = f"evidence/docs/{spec.doc_id}.pdf"
        data, facts = render(spec, _canary(version, rel))
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        sha = sha256_hex(data)
        for f in facts:
            for loc in f["locators"]:
                loc.update({"blob_sha256": sha, "mime": "application/pdf"})
        flags = ["synthetic"]
        if any("internal_inconsistency" in f["flags"] for f in facts):
            flags.append("internal_inconsistency")
        if spec.validity and spec.validity.get("derived"):
            flags.append("derived")
        recs.append({
            "sample_id": f"evid:{spec.doc_id}", "label_type": "LBL-EVID", "partition": _evid_partition(i),
            "strata": {"template": "none", "lang": spec.lang, "format": "pdf_text"}, "cluster_id": spec.doc_id,
            "org": "org-" + "ACDEG"[i % 5], "synthetic": True, "flags": flags,
            "difficulty": "hard" if spec.notes else "medium", "doc_path": rel, "doc_sha256": sha,
            "mime": "application/pdf", "doc_kind": spec.kind,
            "facts": [{**f, "boundary": "organization"} for f in facts],
            "validity": spec.validity, "distractor_values": spec.distractors, "notes": spec.notes,
            "adjudicated_by": "synth:generator",
        })
    return recs


def build_conflicts() -> list[dict[str, Any]]:
    recs = []
    for i, cs in enumerate(CONFLICT_SETS):
        answers = []
        for a in cs["answers"]:
            a = dict(a)
            a.setdefault("boundary", "organization" if not a.get("boundary") else a["boundary"])
            if a.get("value") is not None and not isinstance(a["value"], (bool, str)):
                a["value"] = str(a["value"])
            answers.append({k: v for k, v in a.items() if v is not None})
        recs.append({
            "sample_id": f"conf:{cs['id']}", "label_type": "LBL-CONFLICT",
            "partition": ["dev", "test", "test", "test", "holdout"][i % 5],
            "strata": {"template": "none", "lang": "de", "format": "json"}, "cluster_id": cs["id"],
            "org": "org-" + "ACDEG"[i % 5], "synthetic": True, "flags": ["synthetic"],
            "difficulty": "medium", "answers": answers, "evidence": cs.get("evidence", []),
            "context": cs.get("context", {}), "expected": cs["expected"],
            "negative_reason": cs.get("negative_reason"), "adjudicated_by": "synth:generator",
        })
    return recs


def build_injections() -> list[dict[str, Any]]:
    recs = []
    for i, s in enumerate(INJECTION_SAMPLES):
        is_inj = s["intent"] != "none"
        part = "fewshot" if s["id"] in ("INJ-01", "INJ-N1") else ["dev", "test", "test", "test", "holdout"][i % 5]
        recs.append({
            "sample_id": f"inj:{s['id']}", "label_type": "LBL-INJ", "partition": part,
            "strata": {"template": "none", "lang": s["lang"],
                       "format": "email_body" if s["channel"] in ("email_body", "supplier_reply") else "pdf_text"},
            "cluster_id": s["id"], "org": "org-F1" if part == "fewshot" else "org-" + "ACDEG"[i % 5],
            "synthetic": False, "flags": [], "difficulty": "hard", "channel": s["channel"], "text": s["text"],
            "lang": s["lang"], "is_injection": is_inj, "intent": s["intent"], "technique": s.get("technique"),
            "forbidden_actions": s.get("forbidden_actions", []), "adjudicated_by": "maintainers:red-team",
        })
    return recs


def build_drafts() -> list[dict[str, Any]]:
    recs = []
    for i, s in enumerate(DRAFT_SAMPLES):
        recs.append({
            "sample_id": f"draft:{s['id']}", "label_type": "LBL-DRAFT",
            "partition": ["dev", "test", "test", "holdout"][i % 4],
            "strata": {"template": "none", "lang": s["lang"], "format": "json"}, "cluster_id": s["id"],
            "org": "org-" + "ACDEG"[i % 5], "synthetic": True, "flags": ["synthetic"], "difficulty": "medium",
            "text": s["text"], "lang": s["lang"], "cited": s["cited"],
            "expected_new_numbers": s["expected_new_numbers"], "adjudicated_by": "maintainers",
        })
    return recs


def build(out_dir: str | Path, *, version: str = DEFAULT_VERSION) -> dict[str, Any]:
    from belegt_dictionary import load_dictionary

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    d = load_dictionary()
    sets = {
        "LBL-MAP": build_mapping(d),
        "LBL-EVID": build_evidence(out, version),
        "LBL-CONFLICT": build_conflicts(),
        "LBL-INJ": build_injections(),
        "LBL-DRAFT": build_drafts(),
    }
    files = []
    samples = []
    for lt, recs in sets.items():
        rel = DATA_FILES[lt]
        can = _canary(version, rel)
        for r in recs:
            r["_canary"] = can
            parse_sample(r)  # schema 校验（失败即构建失败）
            samples.append({"sample_id": r["sample_id"], "label_type": lt, "partition": r["partition"],
                            "cell": f"{r['strata']['template']}×{r['strata']['lang']}×{r['strata']['format']}",
                            "cluster_id": r["cluster_id"], "org": r["org"], "synthetic": r["synthetic"],
                            "sha256": sample_hash(r)})
        write_jsonl(out / rel, recs)
        files.append({"path": rel, "sha256": sha256_hex((out / rel).read_bytes()), "records": len(recs),
                      "canary": can})
    for p in sorted((out / "evidence" / "docs").glob("*.pdf")):
        rel = p.relative_to(out).as_posix()
        files.append({"path": rel, "sha256": sha256_hex(p.read_bytes()), "records": 1, "canary": _canary(version, rel)})
    schema_dir = out / "schema"
    schema_dir.mkdir(exist_ok=True)
    for lt, sch in json_schemas().items():
        sp = schema_dir / f"{lt.lower()}.schema.json"
        sp.write_text(json.dumps(sch, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        files.append({"path": sp.relative_to(out).as_posix(), "sha256": sha256_hex(sp.read_bytes()), "records": 1,
                      "canary": None})
    mp = sets["LBL-MAP"]
    counts = {lt: len(recs) for lt, recs in sets.items()}
    counts["LBL-EVID.facts"] = sum(len(r["facts"]) for r in sets["LBL-EVID"])
    counts["LBL-CONFLICT.planted"] = sum(len(r["expected"]) for r in sets["LBL-CONFLICT"])
    counts["LBL-INJ.injections"] = sum(1 for r in sets["LBL-INJ"] if r["is_injection"])
    counts["LBL-MAP.hard"] = sum(1 for r in mp if r["difficulty"] == "hard")
    counts["LBL-MAP.ood"] = sum(1 for r in mp if r["ood"])
    counts["LBL-MAP.by_lang"] = dict(sorted(_count(r["lang"] for r in mp).items()))
    counts["by_partition"] = dict(sorted(_count(s["partition"] for s in samples).items()))
    manifest = {
        "dataset": "belegt-bench", "version": version, "frozen": True, "build_date": BUILD_DATE,
        "generator": GENERATOR, "seed": SEED, "annotation_version": ANNOTATION_VERSION,
        "license": {"data": "CC-BY-4.0", "code": "Apache-2.0"},
        "attribution": "belegt-bench, © Belegt contributors, CC BY 4.0. Synthetic data; no customer data.",
        "dictionary_version": _dict_version(d), "held_out_templates": HELD_OUT_TEMPLATES,
        "counts": counts, "files": files, "samples": samples,
        "kappa_report": {
            "LBL-MAP": {"status": "not_applicable_synthetic",
                        "note": "合成题的金标准由生成器构造（值已知）；手写难例 "
                                f"{counts['LBL-MAP.hard']} 条中的 {sum(1 for r in mp if not r['synthetic'])} 条"
                                "（synthetic=false）待双人标注（adjudicated_by=maintainers:pending-double-annotation），"
                                "κ 报告随 bench-v2027.1.1 补丁发布"},
            "LBL-EVID": {"status": "not_applicable_synthetic", "note": "单据由生成器渲染，事实与定位由同一份规格写出"},
            "LBL-CONFLICT": {"status": "not_applicable_synthetic", "note": "种植冲突由作者按 06 规则构造"},
            "LBL-INJ": {"status": "pending_double_annotation", "note": "红队作者单人标注"},
            "LBL-DRAFT": {"status": "pending_double_annotation", "note": "作者单人标注"},
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "LICENSE-DATA.txt").write_text(
        "The belegt-bench dataset (all files in this directory) is licensed under Creative Commons Attribution 4.0 "
        "International (CC BY 4.0): https://creativecommons.org/licenses/by/4.0/legalcode\n"
        "Attribution: \"belegt-bench, (c) Belegt contributors, CC BY 4.0\". All data is synthetic or written by the "
        "contributors; it contains no customer data and no copyrighted questionnaire text.\n", encoding="utf-8")
    return manifest


def _count(it: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out


def _dict_version(d: Any) -> str:
    import belegt_dictionary

    rel = getattr(d, "release", None)
    rel = rel.get("release") if isinstance(rel, dict) else getattr(rel, "release", rel)
    return f"belegt-dictionary {getattr(belegt_dictionary, '__version__', '?')} ({rel})"


def canonical_manifest(m: dict[str, Any]) -> str:
    return canonical(m)
