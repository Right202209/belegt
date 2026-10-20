# Spec: 02 §2.4 (重命名/拆分/合并/改义), §6.4 (弃用周期), §6.5 (迁移说明格式)
"""``belegt-dict diff``: changes between two dictionary trees and the migration notes of 02 §6.5."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .dictionary import Dictionary, RawTree, build_dictionary
from .versioning import classify_change
from .yamlio import dump_yaml


@dataclass
class DiffResult:
    migration: dict[str, Any]
    other_changes: list[dict[str, Any]] = field(default_factory=list)  # PATCH/MINOR, informational
    problems: list[str] = field(default_factory=list)  # redefinitions, removals

    def to_yaml(self) -> str:
        return dump_yaml(self.migration)

    def to_markdown(self) -> str:
        m = self.migration
        L = [f"# 迁移说明 {m['release']}（包 {m['package_version']}）", "",
             f"上一版：{m.get('previous') or '—'}", ""]
        titles = {"rename": "重命名", "split": "拆分", "merge": "合并", "deprecate": "弃用", "retire": "退役",
                  "add": "新增", "major_change": "MAJOR 变更"}
        for kind, title in titles.items():
            items = [c for c in m["changes"] if c["kind"] == kind]
            if not items:
                continue
            L.append(f"## {title}（{kind}）")
            L.append("")
            for c in items:
                frm = c.get("from")
                frm_s = ", ".join(frm) if isinstance(frm, list) else (frm or "")
                to_s = ", ".join(c.get("to") or [])
                line = f"- `{frm_s}`" if frm_s else "-"
                if to_s:
                    line += f" → `{to_s}`" if frm_s else f" `{to_s}`"
                if c.get("version_from"):
                    line += f"（{c['version_from']} → {c['version_to']}）"
                line += f"；auto_migrate={'是' if c['auto_migrate'] else '否'}"
                if c.get("default_target"):
                    line += f"；默认目标 `{c['default_target']}`"
                if c.get("action_required"):
                    line += f"；需处理：{c['action_required']}"
                if c.get("note"):
                    line += f"；{c['note']}"
                L.append(line)
            L.append("")
        if not m["changes"]:
            L += ["无需迁移的结构变更。", ""]
        if self.other_changes:
            L += ["## 其他变更（PATCH/MINOR，无需迁移）", ""]
            for oc in self.other_changes:
                L.append(f"- `{oc['id']}` {oc['version_from']} → {oc['version_to']}（{oc['level']}：{', '.join(oc['reasons'])}）")
            L.append("")
        for key, title in (("mapping_files_added", "新增映射文件"), ("mapping_files_frozen", "冻结映射文件（只读保留）"),
                           ("mapping_files_removed", "删除的映射文件")):
            if m.get(key):
                L += [f"## {title}", ""] + [f"- `{x}`" for x in m[key]] + [""]
        if self.problems:
            L += ["## 需要修正的问题", ""] + [f"- {p}" for p in self.problems] + [""]
        L.append("`auto_migrate=false` 的变更，`belegt-core` 不改写任何 `Answer`，只经 `approval-router` 生成待确认任务。")
        return "\n".join(L) + "\n"


def _alias_targets(d: Dictionary) -> dict[str, str]:
    out = {a.alias: a.target for a in d.aliases.values()}
    for dp in d.datapoints.values():
        for al in dp.aliases or []:
            out.setdefault(al, dp.id)
    return out


def diff_dictionaries(old: Dictionary, new: Dictionary, *, release: str, previous: str | None,
                      package_version: str) -> DiffResult:
    changes: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    problems: list[str] = []
    O, N = old.datapoints, new.datapoints
    alias_new = _alias_targets(new)
    covered_new: set[str] = set()

    newly_deprecated = [i for i in N if N[i].status == "deprecated" and (i not in O or O[i].status not in ("deprecated", "retired"))]
    newly_retired = [i for i in N if N[i].status == "retired" and (i not in O or O[i].status != "retired")]

    # merges: several ids deprecated into the same single target
    by_target: dict[str, list[str]] = {}
    for i in newly_deprecated:
        rb = N[i].deprecation.replaced_by if N[i].deprecation else []
        if len(rb) == 1:
            by_target.setdefault(rb[0], []).append(i)
    merged: set[str] = set()
    for tgt, srcs in sorted(by_target.items()):
        if len(srcs) > 1 or tgt in O:
            # several ids into one, or one id into an already existing id → merge (02 §2.4)
            changes.append({"kind": "merge", "from": sorted(srcs), "to": [tgt], "auto_migrate": False,
                            "action_required": "若被合并的 ID 均有历史值且不相等，生成 Conflict，不自动裁决"})
            merged |= set(srcs)
            covered_new.add(tgt)
    for i in sorted(newly_deprecated):
        if i in merged:
            continue
        dep = N[i].deprecation
        rb = dep.replaced_by if dep else []
        if len(rb) == 1:
            ch: dict[str, Any] = {"kind": "rename", "from": i, "to": rb, "auto_migrate": True}
            if alias_new.get(i) != rb[0]:
                ch["note"] = "旧 ID 未写入新 ID 的 aliases（02 §2.4 要求）"
                problems.append(f"rename {i} → {rb[0]}: old id missing from aliases of the new id")
            changes.append(ch)
            covered_new |= set(rb)
        elif len(rb) > 1:
            default = alias_new.get(i) if alias_new.get(i) in rb else rb[0]
            changes.append({"kind": "split", "from": i, "to": list(rb), "auto_migrate": False, "default_target": default,
                            "action_required": "历史值不能自动拆分：作为子数据点之和的校验约束，缺失切片生成 ClosedQuestion"})
            covered_new |= set(rb)
        else:
            changes.append({"kind": "deprecate", "from": i, "auto_migrate": True,
                            "note": f"retire_after {dep.retire_after}" if dep else ""})
    for i in sorted(newly_retired):
        changes.append({"kind": "retire", "from": i, "auto_migrate": True,
                        "note": "不得用于新 Answer；历史 Answer 永久可读、可审计"})
    added = sorted(i for i in N if i not in O and i not in covered_new)
    if added:
        changes.append({"kind": "add", "to": added, "auto_migrate": True})
    for i in sorted(set(O) & set(N)):
        c = classify_change(O[i], N[i])
        if c.level == "redefine":
            problems.append(f"{i}: semantic change ({', '.join(c.reasons)}) requires a new id (02 §2.4)")
        elif c.level == "major":
            changes.append({"kind": "major_change", "from": i, "to": [i], "auto_migrate": True,
                            "version_from": O[i].version, "version_to": N[i].version,
                            "note": "; ".join(r for r in c.reasons if r.endswith(":major"))})
        elif c.level in ("minor", "patch"):
            other.append({"id": i, "level": c.level, "reasons": c.reasons, "version_from": O[i].version,
                          "version_to": N[i].version})
    for i in sorted(set(O) - set(N)):
        problems.append(f"{i}: removed from the dictionary; must be deprecated/retired instead (02 §2.4, §6.4)")

    def tm_rel(d: Dictionary) -> dict[str, str]:
        return {tm.id: f"mappings/{tm.template_id}/{tm.template_version}.yaml" for tm in d.template_mappings.values()}

    ot, nt = tm_rel(old), tm_rel(new)
    frozen = sorted(nt[t] for t, tm in new.template_mappings.items()
                    if tm.status == "frozen" and (t not in old.template_mappings or old.template_mappings[t].status != "frozen"))
    migration = {"release": release, "package_version": package_version, "previous": previous, "changes": changes,
                 "mapping_files_added": sorted(nt[t] for t in nt if t not in ot),
                 "mapping_files_frozen": frozen}
    removed = sorted(ot[t] for t in ot if t not in nt)
    if removed:
        migration["mapping_files_removed"] = removed
    return DiffResult(migration, other, problems)


def diff_trees(old_root: str | Path, new_root: str | Path, *, release: str, previous: str | None = None,
               package_version: str) -> DiffResult:
    old, p1 = build_dictionary(RawTree.read(old_root))
    new, p2 = build_dictionary(RawTree.read(new_root))
    assert old is not None and new is not None
    res = diff_dictionaries(old, new, release=release, previous=previous or old.release.get("release"),
                            package_version=package_version)
    res.problems = [f"old: {p}" for p in p1] + [f"new: {p}" for p in p2] + res.problems
    return res
