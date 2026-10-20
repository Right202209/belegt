# Spec: 02 §6.5 迁移说明（belegt-dict diff）、§6.2 CLI（belegt-dict validate）
import subprocess
import sys

from belegt_dictionary.cli import main
from belegt_dictionary.diff import diff_trees
from belegt_dictionary.yamlio import load_yaml_text
from conftest import minimal_dp


def _dep(replaced_by):
    return {"since": "v2027.2", "reason": "RFC test", "replaced_by": replaced_by, "retire_after": "v2027.4"}


def test_diff_no_changes(make_tree):
    a, b = make_tree("a"), make_tree("b")
    res = diff_trees(a.root, b.root, release="v2027.2", package_version="0.2.0")
    assert res.migration["changes"] == [] and res.migration["previous"] == "v2027.1"
    assert not res.problems


def test_diff_rename_split_merge_add_major_retire(make_tree):
    a, b = make_tree("a"), make_tree("b")
    # rename: renewable_share → renewable_energy_share (old id in new aliases)
    old = b.dp("dp:energy.consumption.renewable_share")
    new = dict(old, id="dp:energy.consumption.renewable_energy_share",
               aliases=["dp:energy.consumption.renewable_share"])
    b.add_dp(new)
    b.edit_dp("dp:energy.consumption.renewable_share", lambda d: d.update(status="deprecated", aliases=None,
                                                                          deprecation=_dep([new["id"]])) or d.pop("aliases"))
    al = b.read("aliases/aliases.yaml")
    al["aliases"] = [x for x in al["aliases"] if x["alias"] != "dp:energy.consumption.green_power_share"]
    b.write("aliases/aliases.yaml", al)
    # split: scope3_total → c01 + c04 (existing) — modelled as split
    b.edit_dp("dp:env.ghg.scope3_total", lambda d: d.update(status="deprecated", deprecation=_dep(
        ["dp:env.ghg.scope3_category.c01", "dp:env.ghg.scope3_category.c04"])))
    # merge: two cert expiry ids into iso14001
    for i in ("dp:cert.iso45001.valid_until", "dp:cert.iatf16949.valid_until"):
        b.edit_dp(i, lambda d: d.update(status="deprecated", deprecation=_dep(["dp:cert.iso14001.valid_until"])))
    # add
    b.add_dp(minimal_dp("dp:mat.rohs.exemption_listed"))
    # major: remove allowed unit
    b.edit_dp("dp:env.ghg.scope1_total", lambda d: d.update(allowed_units=["tCO2e"], version="2.0.0"))
    # minor
    b.edit_dp("dp:env.ghg.scope2_market_based", lambda d: d["label"].update(en="Scope 2 GHG (market-based)") or
              d.update(version="1.0.1"))
    # retire
    b.edit_dp("dp:mat.rohs.compliant", lambda d: d.update(status="retired", deprecation=_dep([])))
    res = diff_trees(a.root, b.root, release="v2027.2", previous="v2027.1", package_version="0.2.0")
    kinds = {c["kind"]: c for c in res.migration["changes"]}
    assert kinds["rename"]["from"] == "dp:energy.consumption.renewable_share"
    assert kinds["rename"]["to"] == ["dp:energy.consumption.renewable_energy_share"] and kinds["rename"]["auto_migrate"]
    assert kinds["split"]["auto_migrate"] is False and kinds["split"]["default_target"] == "dp:env.ghg.scope3_category.c01"
    assert "action_required" in kinds["split"]
    assert kinds["merge"]["from"] == ["dp:cert.iatf16949.valid_until", "dp:cert.iso45001.valid_until"]
    assert kinds["merge"]["to"] == ["dp:cert.iso14001.valid_until"] and kinds["merge"]["auto_migrate"] is False
    assert kinds["add"]["to"] == ["dp:mat.rohs.exemption_listed"]   # rename target not double-listed
    assert kinds["major_change"]["from"] == "dp:env.ghg.scope1_total" and kinds["major_change"]["version_to"] == "2.0.0"
    assert kinds["retire"]["from"] == "dp:mat.rohs.compliant"
    assert any(o["id"] == "dp:env.ghg.scope2_market_based" and o["level"] == "patch" for o in res.other_changes)
    # machine-readable output validates against migration.schema.json
    b.write("migrations/v2027.2.yaml", load_yaml_text(res.to_yaml()))
    rep = b.validate(only=["DC-01"])
    assert not [f for f in rep.errors if "migrations/" in f.path], [str(f) for f in rep.errors]
    md = res.to_markdown()
    assert "# 迁移说明 v2027.2" in md and "拆分" in md and "合并" in md and "auto_migrate=否" in md


def test_diff_reports_problems(make_tree):
    a, b = make_tree("a"), make_tree("b")
    b.edit_dp("dp:env.ghg.scope1_total", lambda d: d.update(unit="kgCO2e"))
    b.path(b.dp_rel("dp:mat.rohs.compliant")).unlink()
    res = diff_trees(a.root, b.root, release="v2027.2", package_version="0.2.0")
    assert any("requires a new id" in p for p in res.problems)
    assert any("removed" in p for p in res.problems)
    assert "需要修正的问题" in res.to_markdown()


def test_diff_mapping_files(make_tree):
    a, b = make_tree("a"), make_tree("b")
    m = b.read("mappings/cdp_climate/2025.yaml")
    m.update(template_mapping="tm:cdp_climate@2026", template_version="2026")
    b.write("mappings/cdp_climate/2026.yaml", m)
    b.edit_mapping("cdp_climate", "2025", lambda x: x.update(status="frozen"))
    res = diff_trees(a.root, b.root, release="v2027.2", package_version="0.2.0")
    assert res.migration["mapping_files_added"] == ["mappings/cdp_climate/2026.yaml"]
    assert res.migration["mapping_files_frozen"] == ["mappings/cdp_climate/2025.yaml"]


def test_cli_validate_ok(capsys):
    assert main(["validate"]) == 0
    out = capsys.readouterr().out
    assert "0 error(s)" in out and "DC-12" in out


def test_cli_validate_json_and_failure(tree, capsys):
    tree.edit_dp("dp:env.ghg.scope1_total", lambda d: d["label"].pop("de"))
    assert main(["lint", str(tree.root), "--format", "json"]) == 1
    assert '"ok": false' in capsys.readouterr().out


def test_cli_diff_writes_files(make_tree, tmp_path):
    a, b = make_tree("a"), make_tree("b")
    b.add_dp(minimal_dp("dp:mat.rohs.exemption_listed"))
    y, md = tmp_path / "v2027.2.yaml", tmp_path / "v2027.2.md"
    assert main(["diff", str(a.root), str(b.root), "--release", "v2027.2", "--package-version", "0.2.0",
                 "-o", str(y), "--markdown", str(md)]) == 0
    assert "kind: add" in y.read_text(encoding="utf-8") and "新增" in md.read_text(encoding="utf-8")


def test_cli_fmt_check(tree):
    assert main(["fmt", str(tree.root), "--check"]) == 0
    p = tree.path("units/units.yaml")
    p.write_text(p.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert main(["fmt", str(tree.root), "--check"]) == 1
    assert main(["fmt", str(tree.root)]) == 0
    assert main(["fmt", str(tree.root), "--check"]) == 0


def test_cli_coverage(capsys):
    assert main(["coverage"]) == 0
    assert "vsme" in capsys.readouterr().out


def test_module_entrypoint():
    r = subprocess.run([sys.executable, "-m", "belegt_dictionary.cli", "validate", "--only", "DC-03"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
