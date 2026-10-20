# Spec: 04 §6.1（`file.excel.range.get` 示例、只读、分页、增量）；03 §3.5 Locator（blob_sha256 + sheet + cell）
"""CSV / Excel 文件夹连接器（共享盘、生产台账、计量表）。

工具：
- `file.folder.files.list`      列出根目录下的 csv/xlsx/xlsm（增量：按 mtime + 内容哈希水位）
- `file.folder.files.describe`  单个文件的工作表、尺寸、表头
- `file.excel.range.get`        04 §6.1 示例：`{blob_sha256|path, sheet, range}` → 逐单元格记录（locator 精确到 cell）
- `file.excel.rows.query`       把工作表当表格读（表头行 + 数据行），逐行记录，locator 为行区域
- `file.csv.rows.query`         CSV 行查询（自动识别 `;`/`,`/制表符与编码）

所有记录的 `locator` 都含 `blob_sha256` + `mime` + 单元格，可直接作为 `source_type=document` 的 Evidence 定位。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..base import BaseConnector, CallContext, safe_join
from ..spec import CURSOR_PROPS, SYNC_PROPS, ConnectorError, dec_str, object_schema, sha256_bytes

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSM_MIME = "application/vnd.ms-excel.sheet.macroEnabled.12"
CSV_MIME = "text/csv"
SUFFIX_MIME = {".xlsx": XLSX_MIME, ".xlsm": XLSM_MIME, ".csv": CSV_MIME}
MAX_CELLS = 100_000


def col_letter(idx: int) -> str:
    """1 → A，27 → AA。"""
    s = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def _cell_value(v: Any) -> tuple[str | None, str | None, str]:
    """返回 (value_text, value_dec, data_type)。"""
    if v is None:
        return None, None, "empty"
    if isinstance(v, bool):
        return ("TRUE" if v else "FALSE"), None, "bool"
    if isinstance(v, (int, float)):
        d = dec_str(v)
        return d, d, "number"
    if isinstance(v, datetime):
        return v.date().isoformat() if (v.hour, v.minute, v.second) == (0, 0, 0) else v.isoformat(), None, "date"
    if hasattr(v, "isoformat"):
        return v.isoformat(), None, "date"
    return str(v), None, "text"


def read_csv_rows(data: bytes) -> list[list[str]]:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover
        raise ConnectorError("CONNECTOR_SOURCE_ERROR", "cannot decode CSV")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delim = dialect.delimiter
    except csv.Error:
        delim = ";" if sample.count(";") > sample.count(",") else ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delim)]


class FileFolderConnector(BaseConnector):
    kind = "files"
    category = "file"
    system = "folder"
    title = "CSV/Excel folder (read-only)"
    source_system = "file-share"
    attested = False

    def __init__(self, root: str | Path, connector_id: str | None = None, **kw: Any) -> None:
        super().__init__(connector_id or f"conn:files:{Path(root).name or 'root'}", **kw)
        self.root = Path(root)
        if not self.root.is_dir():
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"root folder {root!s} does not exist")
        file_ref = {
            "path": {"type": "string", "description": "相对连接器根目录的路径"},
            "blob_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        }
        self.register(
            "file.folder.files.list", "列出根目录（递归）下的 CSV/XLSX/XLSM 文件，含内容哈希；支持增量同步",
            object_schema({"glob": {"type": "string", "default": "**/*"}, **CURSOR_PROPS, **SYNC_PROPS}),
            self._files_list, paginated=True, incremental=True)
        self.register(
            "file.folder.files.describe", "描述单个文件：工作表、尺寸、首行表头",
            object_schema(dict(file_ref)), self._files_describe)
        self.register(
            "file.excel.range.get", "读取 Excel 区域的单元格（04 §6.1 示例）；每个单元格一条记录，locator 精确到 cell",
            object_schema({**file_ref, "sheet": {"type": "string"},
                           "range": {"type": "string", "pattern": "^[A-Z]{1,3}[1-9][0-9]*(:[A-Z]{1,3}[1-9][0-9]*)?$"},
                           "include_empty": {"type": "boolean", "default": False}, **CURSOR_PROPS},
                          ["sheet", "range"]),
            self._range_get, paginated=True, evidence_source_type="document")
        self.register(
            "file.excel.rows.query", "把工作表当表格读：表头行 + 数据行；可按列等值过滤",
            object_schema({**file_ref, "sheet": {"type": "string"},
                           "header_row": {"type": "integer", "minimum": 1, "default": 1},
                           "where": {"type": "object", "additionalProperties": {"type": "string"},
                                     "description": "列名 → 值（字符串精确匹配）"},
                           **CURSOR_PROPS}),
            self._xlsx_rows, paginated=True, evidence_source_type="document")
        self.register(
            "file.csv.rows.query", "CSV 行查询（自动识别分隔符与编码）；首行为表头",
            object_schema({**file_ref, "where": {"type": "object", "additionalProperties": {"type": "string"}},
                           **CURSOR_PROPS}),
            self._csv_rows, paginated=True, evidence_source_type="document")

    def compliance_probes(self) -> dict[str, dict[str, Any]]:
        probes: dict[str, dict[str, Any]] = {"file.folder.files.list": {}}
        files = self._scan("**/*")
        xl = next((f for f in files if f[1] != ".csv"), None)
        cs = next((f for f in files if f[1] == ".csv"), None)
        if xl:
            from openpyxl import load_workbook
            wb = load_workbook(xl[0], read_only=True)
            sheet = wb.sheetnames[0]
            wb.close()
            rel = xl[0].relative_to(self.root.resolve()).as_posix()
            probes["file.folder.files.describe"] = {"path": rel}
            probes["file.excel.range.get"] = {"path": rel, "sheet": sheet, "range": "A1:D10"}
            probes["file.excel.rows.query"] = {"path": rel, "sheet": sheet}
        if cs:
            probes["file.csv.rows.query"] = {"path": cs[0].relative_to(self.root.resolve()).as_posix()}
        return probes

    # —— helpers ——
    def _scan(self, glob: str) -> list[tuple[Path, str]]:
        root = self.root.resolve()
        out = []
        for p in sorted(root.glob(glob)):
            if p.is_file() and p.suffix.lower() in SUFFIX_MIME and not p.name.startswith(("~$", ".")):
                safe_join(root, p.relative_to(root).as_posix())
                out.append((p, p.suffix.lower()))
        return out

    def _resolve(self, args: dict[str, Any]) -> tuple[Path, bytes, str, str]:
        if args.get("path"):
            p = safe_join(self.root, args["path"])
            if not p.is_file():
                raise ConnectorError("CONNECTOR_NOT_FOUND", f"file {args['path']!r} not found")
            data = p.read_bytes()
            sha = sha256_bytes(data)
            if args.get("blob_sha256") and args["blob_sha256"] != sha:
                raise ConnectorError("CONNECTOR_NOT_FOUND", "blob_sha256 does not match file content")
        elif args.get("blob_sha256"):
            for p, _ in self._scan("**/*"):
                data = p.read_bytes()
                if sha256_bytes(data) == args["blob_sha256"]:
                    sha = args["blob_sha256"]
                    break
            else:
                raise ConnectorError("CONNECTOR_NOT_FOUND", "no file with this blob_sha256")
        else:
            raise ConnectorError("CONNECTOR_INPUT_INVALID", "path or blob_sha256 required")
        mime = SUFFIX_MIME.get(p.suffix.lower())
        if mime is None:
            raise ConnectorError("CONNECTOR_INPUT_INVALID", "unsupported file type")
        return p, data, sha, mime

    def _workbook(self, data: bytes):  # noqa: ANN202
        from openpyxl import load_workbook
        return load_workbook(io.BytesIO(data), read_only=False, data_only=True)

    # —— handlers ——
    def _files_list(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        root = self.root.resolve()
        rows = []
        for p, suf in self._scan(args.get("glob") or "**/*"):
            data = p.read_bytes()
            st = p.stat()
            sha = sha256_bytes(data)
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).replace(microsecond=0)
            rel = p.relative_to(root).as_posix()
            rows.append({
                "record_key": rel,
                "fields": {"path": rel, "size": st.st_size, "mime": SUFFIX_MIME[suf], "blob_sha256": sha,
                           "modified_at": mtime},
                "locator": {"blob_sha256": sha, "mime": SUFFIX_MIME[suf]},
                # 水位：mtime（纳秒，补零定长）+ 路径；文件改动 → mtime 变大 → 增量可见
                "_wm": f"{st.st_mtime_ns:020d}|{rel}",
            })
        rows.sort(key=lambda r: r["_wm"])
        return rows

    def _files_describe(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        p, data, sha, mime = self._resolve(args)
        rel = p.relative_to(self.root.resolve()).as_posix()
        if mime == CSV_MIME:
            rows = read_csv_rows(data)
            return [{"record_key": f"{sha}#csv", "fields": {"path": rel, "sheet": None, "rows": len(rows),
                                                            "columns": max((len(r) for r in rows), default=0),
                                                            "header": rows[0] if rows else []},
                     "locator": {"blob_sha256": sha, "mime": mime}}]
        wb = self._workbook(data)
        out = []
        for ws in wb.worksheets:
            header = [_cell_value(c.value)[0] for c in next(ws.iter_rows(min_row=1, max_row=1), [])]
            out.append({"record_key": f"{sha}#{ws.title}",
                        "fields": {"path": rel, "sheet": ws.title, "rows": ws.max_row, "columns": ws.max_column,
                                   "hidden": ws.sheet_state != "visible", "header": header},
                        "locator": {"blob_sha256": sha, "mime": mime, "sheet": ws.title}})
        return out

    def _range_get(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        p, data, sha, mime = self._resolve(args)
        if mime == CSV_MIME:
            raise ConnectorError("CONNECTOR_INPUT_INVALID", "range.get requires an Excel file")
        wb = self._workbook(data)
        if args["sheet"] not in wb.sheetnames:
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"sheet {args['sheet']!r} not found")
        ws = wb[args["sheet"]]
        rng = args["range"]
        cells = ws[rng] if ":" in rng else ((ws[rng],),)
        if not isinstance(cells, tuple):
            cells = ((cells,),)
        out = []
        n = 0
        for row in cells:
            if not isinstance(row, tuple):
                row = (row,)
            for c in row:
                n += 1
                if n > MAX_CELLS:
                    raise ConnectorError("CONNECTOR_LIMIT", "range too large")
                text, dec, dtype = _cell_value(c.value)
                if dtype == "empty" and not args.get("include_empty"):
                    continue
                ref = f"{col_letter(c.column)}{c.row}"
                fields: dict[str, Any] = {"ref": ref, "value_text": text, "data_type": dtype}
                if dec is not None:
                    fields["value"] = dec
                if c.number_format and c.number_format != "General":
                    fields["number_format"] = c.number_format
                out.append({"record_key": f"{sha}#{ws.title}!{ref}", "fields": fields,
                            "locator": {"blob_sha256": sha, "mime": mime, "sheet": ws.title, "cell": ref}})
        return out

    def _table_rows(self, rows: list[list[Any]], header_row: int, sha: str, mime: str, sheet: str | None,
                    where: dict[str, str] | None) -> list[dict[str, Any]]:
        if len(rows) < header_row:
            return []
        header = [(_cell_value(h)[0] or f"col_{i + 1}") for i, h in enumerate(rows[header_row - 1])]
        out = []
        for ridx, row in enumerate(rows[header_row:], start=header_row + 1):
            values = list(row) + [None] * (len(header) - len(row))
            if all(v in (None, "") for v in values):
                continue
            fields: dict[str, Any] = {}
            for h, v in zip(header, values):
                text, dec, dtype = _cell_value(v)
                fields[h] = dec if dec is not None else text
            if where and any(str(fields.get(k)) != str(v) for k, v in where.items()):
                continue
            last = col_letter(max(len(header), 1))
            cell = f"A{ridx}:{last}{ridx}" if last != "A" else f"A{ridx}"
            loc: dict[str, Any] = {"blob_sha256": sha, "mime": mime, "cell": cell}
            if sheet:
                loc["sheet"] = sheet
            key = f"{sha}#{sheet + '!' if sheet else ''}row{ridx}"
            out.append({"record_key": key, "fields": {**fields, "_row": ridx}, "locator": loc})
        return out

    def _xlsx_rows(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        p, data, sha, mime = self._resolve(args)
        if mime == CSV_MIME:
            raise ConnectorError("CONNECTOR_INPUT_INVALID", "use file.csv.rows.query for CSV")
        wb = self._workbook(data)
        sheet = args.get("sheet") or wb.sheetnames[0]
        if sheet not in wb.sheetnames:
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"sheet {sheet!r} not found")
        rows = [list(r) for r in wb[sheet].iter_rows(values_only=True)]
        return self._table_rows(rows, int(args.get("header_row") or 1), sha, mime, sheet, args.get("where"))

    def _csv_rows(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        p, data, sha, mime = self._resolve(args)
        if mime != CSV_MIME:
            raise ConnectorError("CONNECTOR_INPUT_INVALID", "use file.excel.rows.query for Excel")
        return self._table_rows(read_csv_rows(data), 1, sha, mime, None, args.get("where"))
