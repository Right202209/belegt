# Spec: 11 §4（bench-runner）、§5（CI 门禁）、§2.5（κ）、§2.8（manifest）、§2.9（LEAK-01…03）、§2.2（bench-synth）
"""`belegt-bench` 命令行。

    belegt-bench run --agent question-parser [--predictor auto|core|baseline] [--entrypoint mod:fn]
                     [--dataset DIR|VERSION] [--partition test|dev|holdout|fewshot|all] [--smoke] [--out DIR]
    belegt-bench gate --results DIR [--baseline DIR] [--thresholds FILE] [--stage pr|merge|release] [--fail-on-warn]
    belegt-bench build --out DIR            # 重建黄金集（确定性）
    belegt-bench validate [--dataset ...]   # manifest + schema + 防泄漏
    belegt-bench kappa FILE.jsonl           # 双人标注 κ（每行一条样本，含两份 annotations）
    belegt-bench synth --template vsme --lang de --n 10 [--seed 1] [--csv OUT]   # 合成问卷
    belegt-bench stats [--dataset ...]      # 黄金集规模
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from .runner import AGENTS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="belegt-bench", description="Belegt evaluation benchmark")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="运行评测")
    p.add_argument("--agent", required=True, choices=AGENTS)
    p.add_argument("--predictor", default="auto", choices=["auto", "core", "baseline"])
    p.add_argument("--entrypoint")
    p.add_argument("--dataset")
    p.add_argument("--partition", default="test", choices=["test", "dev", "holdout", "fewshot", "all"])
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--out", default="bench-results")
    p.add_argument("--bootstrap", type=int, default=2000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--role", default="owner", choices=["owner", "prompt_author"])
    p.add_argument("--strict", action="store_true", help="被跳过时退出 3（默认 0）")

    g = sub.add_parser("gate", help="CI 门禁 QG-01…QG-07")
    g.add_argument("--results", required=True, help="候选结果目录（eval-report.*.json）或单个报告")
    g.add_argument("--baseline", help="基线结果目录；`builtin` = 随包分发的参考基线（bench-v2027.1/test）")
    g.add_argument("--thresholds")
    g.add_argument("--stage", default="pr", choices=["pr", "merge", "release"])
    g.add_argument("--bootstrap", type=int)
    g.add_argument("--fail-on-warn", action="store_true")
    g.add_argument("--json", dest="json_out", help="把门禁结论写入 JSON 文件")

    b = sub.add_parser("build", help="确定性重建黄金集")
    b.add_argument("--out", required=True)

    v = sub.add_parser("validate", help="manifest + schema + 防泄漏")
    v.add_argument("--dataset")

    k = sub.add_parser("kappa", help="双人标注一致性")
    k.add_argument("file")

    s = sub.add_parser("synth", help="合成问卷（bench-synth）")
    s.add_argument("--template", default="vsme")
    s.add_argument("--lang", default="de")
    s.add_argument("--n", type=int, default=10)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--csv")

    st = sub.add_parser("stats", help="黄金集规模")
    st.add_argument("--dataset")

    ns = ap.parse_args(argv)

    if ns.cmd == "run":
        from .runner import run
        rep = run(ns.agent, dataset=ns.dataset, partition=ns.partition, smoke=ns.smoke, predictor=ns.predictor,
                  entrypoint=ns.entrypoint, out_dir=ns.out, bootstrap=ns.bootstrap, seed=ns.seed, role=ns.role,
                  progress=lambda m: print(m, file=sys.stderr))
        if rep["status"] != "ok":
            print(f"[belegt-bench] {ns.agent}: {rep.get('skip_reason')}")
            return 3 if ns.strict else 0
        print(f"[belegt-bench] {ns.agent} · {rep['config']['predictor']} · {rep['dataset_version']}/{rep['partition']}"
              f"{' smoke' if rep['smoke'] else ''} · n={rep['n_samples']}")
        for mid, m in rep["metrics"].items():
            ci = f" CI95 [{m['ci95'][0]:.4f}, {m['ci95'][1]:.4f}]" if m.get("ci95") else ""
            print(f"  {mid:<10} {m['name']:<28} {('—' if m['value'] is None else f'{m['value']:.4f}'):>8} (n={m['n']}){ci}")
        if rep.get("worst_cell"):
            w = rep["worst_cell"]
            print(f"  worst cell {w['cell']}: {w['metrics'][w['metric']]['value']} (n={w['n']})")
        print(f"  report → {Path(ns.out) / f'eval-report.{ns.agent}.json'}")
        return 0

    if ns.cmd == "gate":
        from .gate import evaluate, load_report_dir, load_thresholds, render_text
        try:
            cand, cs = load_report_dir(ns.results)
            from .gate import builtin_baseline_dir
            bdir = builtin_baseline_dir() if ns.baseline == "builtin" else ns.baseline
            base, bs = load_report_dir(bdir) if bdir else ({}, {})
            th = load_thresholds(ns.thresholds)
        except (OSError, json.JSONDecodeError, KeyError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if not cand:
            print("error: no eval-report*.json found", file=sys.stderr)
            return 2
        res = evaluate(list(cand.values()), base=base, cand_samples=cs, base_samples=bs, thresholds=th,
                       stage=ns.stage, bootstrap_b=ns.bootstrap)
        print(render_text(res, list(cand.values())))
        if ns.json_out:
            Path(ns.json_out).write_text(json.dumps(res.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        if res.verdict == "block" or (ns.fail_on_warn and res.verdict == "warn"):
            return 1
        return 0

    if ns.cmd == "build":
        from .build import build
        m = build(ns.out)
        print(json.dumps(m["counts"], ensure_ascii=False, indent=1))
        return 0

    if ns.cmd == "validate":
        from .dataset import load_dataset, verify_manifest
        from .leak import check_leaks
        errs = verify_manifest(ns.dataset)
        ds = load_dataset(ns.dataset)
        leak = check_leaks([r for recs in ds.raw.values() for r in recs])
        print(json.dumps({"manifest_errors": errs, "leak": leak.to_dict()}, ensure_ascii=False, indent=1))
        return 0 if not errs and leak.ok else 1

    if ns.cmd == "kappa":
        from .kappa import batch_kappa
        recs = [json.loads(ln) for ln in Path(ns.file).read_text(encoding="utf-8").splitlines() if ln.strip()]
        rep = batch_kappa(recs)
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep and all(r["status"] == "pass" for r in rep.values()) else 1

    if ns.cmd == "synth":
        from belegt_dictionary import load_dictionary

        from .synth import render_csv, synthesize_questionnaire
        items = synthesize_questionnaire(load_dictionary(), qn_id="SYN", template=ns.template, lang=ns.lang, n=ns.n,
                                         rng=random.Random(ns.seed))
        if ns.csv:
            Path(ns.csv).write_bytes(render_csv(items))
        for it in items:
            print(json.dumps({"question_id": it.question_id, "native_id": it.native_id, "text": it.text,
                              "acceptable": it.acceptable, "unit_hint": it.unit_hint}, ensure_ascii=False))
        return 0

    if ns.cmd == "stats":
        from .dataset import load_dataset
        ds = load_dataset(ns.dataset)
        print(json.dumps({"version": ds.version, **ds.manifest["counts"]}, ensure_ascii=False, indent=1))
        return 0
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
