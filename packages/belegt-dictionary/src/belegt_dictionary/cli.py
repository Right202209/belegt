# Spec: 02 §6.2 (`belegt-dict lint` / validate), §6.5 (`belegt-dict diff`), §6.6 (贡献流程)
"""Command line: ``belegt-dict validate|lint|diff|fmt|coverage``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .dictionary import RawTree, default_root
from .yamlio import dump_yaml


def _cmd_validate(a: argparse.Namespace) -> int:
    from .validate import validate_tree
    rep = validate_tree(a.root, baseline=a.baseline, corpus=a.corpus, only=a.only)
    out = rep.to_json() if a.format == "json" else rep.to_text()
    print(out)
    return 0 if rep.ok else 1


def _cmd_diff(a: argparse.Namespace) -> int:
    from .diff import diff_trees
    res = diff_trees(a.old, a.new, release=a.release, previous=a.previous, package_version=a.package_version)
    if a.output:
        Path(a.output).write_text(res.to_yaml(), encoding="utf-8")
    if a.markdown:
        Path(a.markdown).write_text(res.to_markdown(), encoding="utf-8")
    if not a.output and not a.markdown:
        print(res.to_yaml())
        print(res.to_markdown())
    for p in res.problems:
        print(f"PROBLEM: {p}", file=sys.stderr)
    return 1 if (res.problems and a.strict) else 0


def _cmd_fmt(a: argparse.Namespace) -> int:
    root = Path(a.root) if a.root else default_root()
    tree = RawTree.read(root)
    files = list(tree.datapoints) + list(tree.mappings) + list(tree.migrations) + list(tree.enums.values())
    files += [f for f in (tree.units, tree.conversions, tree.aliases, tree.methodologies, tree.abbreviations) if f]
    changed = 0
    for f in files:
        canon = dump_yaml(f.data)
        if f.text != canon:
            changed += 1
            if a.check:
                print(f"would reformat {f.rel}")
            else:
                f.path.write_text(canon, encoding="utf-8")
                print(f"reformatted {f.rel}")
    return 1 if (a.check and changed) else 0


def _cmd_coverage(a: argparse.Namespace) -> int:
    from .validate import validate_tree
    rep = validate_tree(a.root, only=["DC-12"])
    for tid, c in rep.coverage["templates"].items():
        print(f"{tid:22} entries={c['entries']:3} datapoints={c['datapoints_mapped']:3} coverage={c['coverage_pct']}%"
              + (f"  unmapped: {', '.join(c['unmapped_tagged'])}" if c["unmapped_tagged"] else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="belegt-dict", description="Belegt 数据点字典工具（02 §6）")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "lint"):
        v = sub.add_parser(name, help="运行 DC-01…DC-12 校验")
        v.add_argument("root", nargs="?", default=None, help="字典根目录（默认：随包分发的数据）")
        v.add_argument("--baseline", help="上一发版的字典根目录（DC-02 retired 复用、DC-09 版本递增）")
        v.add_argument("--corpus", help="DC-10 模板原文语料：.txt 文件或目录（只在本地计算指纹，不入库）")
        v.add_argument("--only", nargs="*", help="只运行指定检查，如 DC-03 DC-05")
        v.add_argument("--format", choices=("text", "json"), default="text")
        v.set_defaults(fn=_cmd_validate)
    d = sub.add_parser("diff", help="生成两版本间变更与迁移说明（02 §6.5）")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("--release", required=True, help="新发版，如 v2027.2")
    d.add_argument("--previous", help="上一发版（默认读旧树 release.yaml）")
    d.add_argument("--package-version", required=True)
    d.add_argument("-o", "--output", help="迁移 YAML 输出路径（migrations/vYYYY.Q.yaml）")
    d.add_argument("--markdown", help="人读迁移说明输出路径（migrations/vYYYY.Q.md）")
    d.add_argument("--strict", action="store_true", help="存在问题（改义、删除）时返回非零")
    d.set_defaults(fn=_cmd_diff)
    f = sub.add_parser("fmt", help="把 YAML 改写为规范格式")
    f.add_argument("root", nargs="?", default=None)
    f.add_argument("--check", action="store_true")
    f.set_defaults(fn=_cmd_fmt)
    c = sub.add_parser("coverage", help="DC-12 覆盖报告")
    c.add_argument("root", nargs="?", default=None)
    c.set_defaults(fn=_cmd_coverage)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
