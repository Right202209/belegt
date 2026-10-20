# Spec: 05 §8.2（CLI）；03 §7（verify-pack）
"""``belegt-calc`` 命令行。

退出码：0 成功/一致；1 计算错误或校验失败；2 用法错误；3 漂移（replay / verify-pack）；4 input_hash 不一致（replay）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import factors as F
from ._version import ENGINE_VERSION
from .engine import calculate, replay
from .errors import CalcError
from .hashing import input_hash
from .registry import list_methods

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_DRIFT, EXIT_INPUT_HASH = 0, 1, 2, 3, 4


def _out(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _err(e: CalcError) -> int:
    sys.stderr.write(json.dumps({"error": e.to_dict()}, ensure_ascii=False) + "\n")
    return EXIT_FAIL


def _load_request(path: str) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if "request" in doc and "inputs" not in doc:   # 也接受单情形向量文件
        doc = doc["request"]
    return doc


def _req(args: argparse.Namespace) -> tuple[dict, Any]:
    req = _load_request(args.input)
    if getattr(args, "method", None):
        req["method_id"] = args.method
    if getattr(args, "mv", None):
        req["methodology_version"] = args.mv
    for k in ("method_id", "methodology_version", "inputs", "boundary", "period"):
        if k not in req:
            raise SystemExit(f"belegt-calc: request is missing {k!r} (or pass --method/--mv)")
    fs = F.load_factor_set(args.factors)
    if req.get("factor_set_version") not in (None, fs.id):
        raise CalcError("CALC_FACTOR_MISSING", f"request references {req['factor_set_version']}, --factors is {fs.id}")
    return req, fs


def cmd_run(args: argparse.Namespace) -> int:
    try:
        req, fs = _req(args)
        res = calculate(req["method_id"], req["methodology_version"], fs, req["inputs"], req.get("params") or {},
                        req["boundary"], req["period"], req.get("customer_scope", "all"))
    except CalcError as e:
        return _err(e)
    out = res.to_dict()
    if args.annotations:
        out = {"calculation": out, "annotations": res.annotations}
    _out(out)
    return EXIT_OK


def cmd_hash(args: argparse.Namespace) -> int:
    try:
        req, fs = _req(args)
        h = input_hash({**req, "factor_set_version": fs.id, "factor_set_rows_hash": fs.rows_hash})
    except CalcError as e:
        return _err(e)
    sys.stdout.write(h + "\n")
    return EXIT_OK


def cmd_replay(args: argparse.Namespace) -> int:
    try:
        calc = json.loads(Path(args.calculation).read_text(encoding="utf-8"))
        if "calculation" in calc and "method_id" not in calc:
            calc = calc["calculation"]
        fs = F.load_factor_set(args.factors)
    except CalcError as e:
        return _err(e)
    rep = replay(calc, fs)
    _out(rep.to_dict())
    if rep.error:
        return EXIT_FAIL
    if not rep.input_hash_match:
        return EXIT_INPUT_HASH
    if rep.drift:
        return EXIT_DRIFT
    return EXIT_OK


def cmd_vectors(args: argparse.Namespace) -> int:
    from .vectors import verify_dir

    d = Path(args.directory)
    if not d.exists():
        sys.stderr.write(f"belegt-calc: vector directory {d} not found\n")
        return EXIT_USAGE
    results = verify_dir(d)
    failed = [r for r in results if not r.ok]
    for r in results:
        line = f"{'PASS' if r.ok else 'FAIL'}  {r.case_id:28s} ({r.file})"
        sys.stdout.write(line + "\n")
        for f in r.failures:
            sys.stdout.write(f"      - {f}\n")
    sys.stdout.write(f"\n{len(results) - len(failed)}/{len(results)} cases passed ({ENGINE_VERSION})\n")
    return EXIT_OK if not failed else EXIT_FAIL


def _write_fs(fs, out: str | None) -> None:
    text = F.dump_factor_set(fs)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        sys.stderr.write(f"wrote {out}\n")
    else:
        sys.stdout.write(text)


def cmd_factors(args: argparse.Namespace) -> int:
    try:
        if args.action == "import":
            rows = F.import_rows_csv(Path(args.csv).read_text(encoding="utf-8"))
            upstream = None
            if args.upstream_url:
                upstream = {"title": args.upstream_title or "", "url": args.upstream_url,
                            "retrieved_at": args.retrieved_at or ""}
            fs = F.make_draft(args.id, rows, license=args.license, published_at=args.published_at,
                              valid_from=args.valid_from, valid_until=args.valid_until, upstream=upstream)
            _write_fs(fs, args.out)
        elif args.action == "freeze":
            fs = F.freeze(F.load_factor_set(args.file, allow_draft=True))
            _write_fs(fs, args.out or args.file)
            sys.stdout.write(json.dumps({"id": fs.id, "row_count": len(fs.rows), "rows_hash": fs.rows_hash}) + "\n")
        elif args.action == "deprecate":
            fs = F.deprecate(F.load_factor_set(args.file), reason=args.reason, deprecated_at=args.date)
            _write_fs(fs, args.out or args.file)
        elif args.action == "diff":
            _out(F.diff(F.load_factor_set(args.old, allow_draft=True), F.load_factor_set(args.new, allow_draft=True)))
        elif args.action == "list":
            _out([{"id": i, "rows_hash": F.load_factor_set(i).rows_hash} for i in F.list_bundled()])
        elif args.action == "show":
            fs = F.load_factor_set(args.file, allow_draft=True)
            _out({"id": fs.id, "status": fs.status, "row_count": len(fs.rows), "rows_hash": fs.rows_hash,
                  "warnings": fs.warnings})
    except CalcError as e:
        return _err(e)
    return EXIT_OK


def cmd_verify_pack(args: argparse.Namespace) -> int:
    from .pack import verify_pack

    key = None
    if args.public_key:
        raw = Path(args.public_key).read_bytes()
        key = raw if len(raw) == 32 else bytes.fromhex(raw.decode().strip())
    rep = verify_pack(args.pack, factors_dir=args.factors_dir, public_key=key)
    _out(rep)
    if rep["drift"]:
        return EXIT_DRIFT
    return EXIT_OK if rep["ok"] else EXIT_FAIL


def cmd_methods(args: argparse.Namespace) -> int:
    _out([m.to_dict() for m in list_methods()])
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="belegt-calc", description="Belegt 确定性计算库（05 计算方法论）")
    p.add_argument("--version", action="version", version=ENGINE_VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="执行计算，输出 Calculation JSON")
    r.add_argument("--method")
    r.add_argument("--mv", help="methodology_version，如 ghg-scope1@1.0.0")
    r.add_argument("--factors", required=True, help="因子库 YAML 路径或内置 ID（fs:...）")
    r.add_argument("--input", required=True, help="请求 JSON（calculate 的参数）")
    r.add_argument("--annotations", action="store_true", help="同时输出 annotations（不属于 Calculation 字段）")
    r.set_defaults(func=cmd_run)

    h = sub.add_parser("hash", help="只计算 input_hash（03 §4.5）")
    h.add_argument("--method")
    h.add_argument("--mv")
    h.add_argument("--factors", required=True)
    h.add_argument("--input", required=True)
    h.set_defaults(func=cmd_hash)

    rp = sub.add_parser("replay", help="重放 Calculation（退出码 0=一致 3=漂移）")
    rp.add_argument("calculation")
    rp.add_argument("--factors", required=True)
    rp.set_defaults(func=cmd_replay)

    v = sub.add_parser("vectors", help="测试向量")
    vs = v.add_subparsers(dest="vcmd", required=True)
    vv = vs.add_parser("verify", help="逐位校验测试向量（FR-209）")
    vv.add_argument("directory", nargs="?", default="tests/vectors")
    vv.set_defaults(func=cmd_vectors)

    f = sub.add_parser("factors", help="因子库 import|diff|freeze|deprecate|list|show（05 §7.3）")
    fs = f.add_subparsers(dest="action", required=True)
    fi = fs.add_parser("import", help="CSV → 未冻结草稿（status: draft）")
    fi.add_argument("--id", required=True, help="fs:<source>@<version>")
    fi.add_argument("--csv", required=True)
    fi.add_argument("--license", required=True)
    fi.add_argument("--published-at", required=True)
    fi.add_argument("--valid-from", required=True)
    fi.add_argument("--valid-until")
    fi.add_argument("--upstream-title")
    fi.add_argument("--upstream-url")
    fi.add_argument("--retrieved-at")
    fi.add_argument("--out")
    ff = fs.add_parser("freeze", help="计算 rows_hash，status=active")
    ff.add_argument("file")
    ff.add_argument("--out")
    fd = fs.add_parser("deprecate", help="active → deprecated（不可逆）")
    fd.add_argument("file")
    fd.add_argument("--reason", required=True)
    fd.add_argument("--date", required=True, help="弃用日期 YYYY-MM-DD（库本身不读时钟）")
    fd.add_argument("--out")
    fdf = fs.add_parser("diff", help="两版因子库差异报告")
    fdf.add_argument("old")
    fdf.add_argument("new")
    fs.add_parser("list", help="列出随包分发的因子库")
    fsh = fs.add_parser("show", help="显示因子库摘要并校验 rows_hash")
    fsh.add_argument("file")
    f.set_defaults(func=cmd_factors)

    vp = sub.add_parser("verify-pack", help="校验审计包（03 §7）")
    vp.add_argument("pack")
    vp.add_argument("--factors-dir")
    vp.add_argument("--public-key", help="Ed25519 公钥（32 字节原始或 hex 文本）；需要可选依赖 cryptography")
    vp.set_defaults(func=cmd_verify_pack)

    m = sub.add_parser("methods", help="列出全部方法（list_methods）")
    m.set_defaults(func=cmd_methods)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
