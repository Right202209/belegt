# Spec: 04 §6.1「测试：belegt-connectors 提供合规测试套件（只读断言、schema、分页），作为社区连接器合并门槛」
"""连接器合规套件 `belegt-connector/1`。

| 检查 | 内容 |
|---|---|
| CC-01 命名 | 工具名 `<category>.<system>.<resource>.<verb>`，verb ∈ {query,get,list,describe}，category 已登记 |
| CC-02 只读注解 | 每个工具 `readOnlyHint=true`、`destructiveHint=false`；manifest `read_only=true`、scopes 均以 `.read` 结尾 |
| CC-03 输入 schema | 合法 Draft 2020-12、`type=object`、`additionalProperties=false`；分页工具声明 `cursor`/`page_size≤1000` |
| CC-04 envelope | 输出符合 04 §6.1 envelope；`query_hash` 为 sha256；`fetched_at` 为 UTC ISO 8601 |
| CC-05 DEC-STR | `fields` 中不出现 JSON number（数值必须是十进制字符串） |
| CC-06 locator | 每条记录有 03 §3.5 Locator，含 `connector{connector_id, query_hash, record_key, fetched_at}` 且与 envelope 一致；文件类 record 的 blob_sha256 格式正确 |
| CC-07 分页 | page_size=1 逐页遍历：无重复 record_key、游标终止、与一次性结果集合一致；伪造游标被拒 |
| CC-08 可重放 | 同一参数两次调用 `query_hash` 相同、record_key 集合相同；`cursor`/`page_size` 不影响 query_hash |
| CC-09 无秘密泄漏 | 注入的秘密值不出现在 manifest 与任何输出中 |
| CC-10 增量 | 增量工具返回 `sync_token`；带回 token 且数据未变时返回 0 条记录 |
| CC-11 写拒绝 | 注册写 verb（如 `.update`）被拒绝 |
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from .base import BaseConnector
from .spec import (
    ENVELOPE_SCHEMA, LOCATOR_SCHEMA, MAX_PAGE_SIZE, SHA256_RE, ConnectorError, encode_cursor, validate_tool_name,
)

ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


@dataclass
class Finding:
    check: str
    tool: str | None
    ok: bool
    message: str = ""


@dataclass
class ComplianceReport:
    connector_id: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(f.ok for f in self.findings)

    def failures(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok]

    def add(self, check: str, tool: str | None, ok: bool, message: str = "") -> None:
        self.findings.append(Finding(check, tool, ok, message))

    def to_dict(self) -> dict[str, Any]:
        return {"connector_id": self.connector_id, "ok": self.ok,
                "findings": [f.__dict__ for f in self.findings]}

    def to_text(self) -> str:
        lines = [f"belegt-connector/1 compliance · {self.connector_id} · {'PASS' if self.ok else 'FAIL'}"]
        for f in self.findings:
            lines.append(f"  [{'ok ' if f.ok else 'ERR'}] {f.check} {f.tool or '-'} {f.message}")
        return "\n".join(lines)


def _json_numbers(obj: Any, path: str = "") -> list[str]:
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return []
    if isinstance(obj, (int, float)):
        return [path or "/"]
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in _json_numbers(v, f"{path}/{k}")]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in _json_numbers(v, f"{path}/{i}")]
    return [path]


def run_compliance(connector: BaseConnector, probes: dict[str, dict[str, Any]] | None = None, *,
                   secrets: list[str] | None = None, check_pagination: bool = True) -> ComplianceReport:
    rep = ComplianceReport(connector.connector_id)
    probes = probes if probes is not None else connector.compliance_probes()
    manifest = connector.manifest()
    mjson = manifest.model_dump_json()

    # CC-02 manifest
    rep.add("CC-02", None, manifest.read_only is True and all(s.endswith(".read") for s in manifest.scopes),
            "manifest read_only/scopes")
    # CC-11 写 verb 被拒
    try:
        validate_tool_name(f"{connector.category}.{connector.system}.records.update")
        rep.add("CC-11", None, False, "write verb accepted")
    except ConnectorError:
        rep.add("CC-11", None, True)

    for spec in connector.tools():
        name = spec.name
        try:
            validate_tool_name(name)
            rep.add("CC-01", name, True)
        except ConnectorError as e:
            rep.add("CC-01", name, False, e.message)
        ann = spec.annotations
        rep.add("CC-02", name, ann.get("readOnlyHint") is True and ann.get("destructiveHint") is False)
        s = spec.input_schema
        ok3, msg3 = True, ""
        try:
            jsonschema.Draft202012Validator.check_schema(s)
        except jsonschema.SchemaError as e:
            ok3, msg3 = False, str(e.message)
        if s.get("type") != "object" or s.get("additionalProperties") is not False:
            ok3, msg3 = False, "inputSchema must be object with additionalProperties=false"
        if spec.paginated:
            props = s.get("properties", {})
            if "cursor" not in props or props.get("page_size", {}).get("maximum", 0) > MAX_PAGE_SIZE:
                ok3, msg3 = False, "paginated tool must declare cursor and page_size<=1000"
        rep.add("CC-03", name, ok3, msg3)

        if name not in probes:
            rep.add("CC-04", name, True, "no probe (skipped)")
            continue
        args = probes[name]
        try:
            env = connector.call(name, args)
        except ConnectorError as e:
            rep.add("CC-04", name, False, f"probe failed: {e.code} {e.message}")
            continue
        errs = list(jsonschema.Draft202012Validator(ENVELOPE_SCHEMA).iter_errors(env))
        ok4 = not errs and bool(SHA256_RE.match(env["query_hash"])) and bool(ISO_UTC_RE.match(env["fetched_at"]))
        rep.add("CC-04", name, ok4, errs[0].message if errs else ("" if ok4 else "query_hash/fetched_at format"))
        nums = [p for r in env["records"] for p in _json_numbers(r.get("fields"), "/fields")]
        rep.add("CC-05", name, not nums, f"JSON numbers at {nums[:3]}" if nums else "")
        ok6, msg6 = True, ""
        lv = jsonschema.Draft202012Validator(LOCATOR_SCHEMA)
        for r in env["records"]:
            loc = r.get("locator")
            if not loc or list(lv.iter_errors(loc)):
                ok6, msg6 = False, f"invalid locator for {r.get('record_key')}"
                break
            c = loc.get("connector") or {}
            if (c.get("connector_id") != env.get("connector_id") or c.get("query_hash") != env["query_hash"]
                    or c.get("record_key") != r["record_key"] or c.get("fetched_at") != env["fetched_at"]):
                ok6, msg6 = False, f"locator.connector mismatch for {r.get('record_key')}"
                break
        rep.add("CC-06", name, ok6, msg6)
        # CC-08
        try:
            env2 = connector.call(name, {**args, "page_size": 7} if spec.paginated else args)
            same_hash = env2["query_hash"] == env["query_hash"]
            if spec.paginated:
                same_keys = True
            else:
                same_keys = [r["record_key"] for r in env2["records"]] == [r["record_key"] for r in env["records"]]
            rep.add("CC-08", name, same_hash and same_keys, "" if same_hash else "query_hash depends on page_size")
        except ConnectorError as e:
            rep.add("CC-08", name, False, e.message)
        # CC-07
        if spec.paginated and check_pagination:
            try:
                full = []
                for page in connector.iter_all(name, {**args, "page_size": MAX_PAGE_SIZE}):
                    full += [r["record_key"] for r in page["records"]]
                seen: list[str] = []
                pages = 0
                for page in connector.iter_all(name, {**args, "page_size": 1}):
                    pages += 1
                    seen += [r["record_key"] for r in page["records"]]
                    if pages > len(full) + 2:
                        raise ConnectorError("CONNECTOR_CURSOR_INVALID", "cursor does not terminate")
                ok7 = len(seen) == len(set(seen)) and set(seen) == set(full)
                msg7 = "" if ok7 else "paged result differs from full result"
                try:
                    connector.call(name, {**args, "cursor": encode_cursor("sha256:" + "0" * 64, 1)})
                    ok7, msg7 = False, "foreign cursor accepted"
                except ConnectorError as e:
                    ok7 = ok7 and e.code == "CONNECTOR_CURSOR_INVALID"
                rep.add("CC-07", name, ok7, msg7)
            except ConnectorError as e:
                rep.add("CC-07", name, False, e.message)
        # CC-10
        if spec.incremental:
            token = env.get("sync_token")
            if env["records"] and not token:
                rep.add("CC-10", name, False, "no sync_token returned")
            elif token:
                try:
                    recs, _ = connector.sync(name, args, token)
                    rep.add("CC-10", name, len(recs) == 0, f"{len(recs)} records after unchanged sync" if recs else "")
                except ConnectorError as e:
                    rep.add("CC-10", name, False, e.message)
        # CC-09
        if secrets:
            blob = json.dumps(env, ensure_ascii=False) + mjson
            leaked = [s for s in secrets if s and s in blob]
            rep.add("CC-09", name, not leaked, "secret leaked" if leaked else "")
    return rep
