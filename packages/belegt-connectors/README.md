# belegt-connectors

> Belegt 只读 MCP 连接器（Apache-2.0）。规格：`docs/04-AI-Agent设计.md` §6.1 连接器规范 `belegt-connector/1`；
> 需求：FR-005（ERP 连接器）、FR-101（邮箱收件）、FR-203（连接器开放规范）。

每个连接器都**同时**是：

1. 一个普通 Python 类（`connector.call(tool, args)` → envelope dict），可在 Temporal Activity / 测试里直接调用；
2. 一个 MCP server（stdio 或 Streamable HTTP），工具名、schema、注解与 Python 调用完全一致。

返回值中的每条记录都带 03 §3.5 `Locator`（含 `connector{connector_id, query_hash, record_key, fetched_at}`），
`record_to_evidence()` 一步转换成可写入证据图的 `Evidence` 草稿（通过 `belegt_core.models.Locator/Evidence` 校验）。

## 安装

```bash
pip install -e packages/belegt-connectors            # 核心（pydantic、jsonschema、sqlalchemy、openpyxl）
pip install -e "packages/belegt-connectors[mcp,pdf]"  # + MCP server（mcp 1.x FastMCP / 2.x MCPServer 均兼容）+ pypdf
pip install -e packages/belegt-io                     # 可选：电费单走 belegt-io 证据提取（页码 + bbox）
```

依赖方向（CONTRACTS）：本包**不依赖** belegt-core；`belegt_io` 为 try-import。

## 规范 `belegt-connector/1` 要点

| 项 | 约定 |
|---|---|
| 工具名 | `<category>.<system>.<resource>.<verb>`，verb ∈ `query / get / list / describe`；不支持 `.` 的 MCP 客户端用 `__`（`--underscore-names`） |
| 注解 | 每个工具 `readOnlyHint=true`、`destructiveHint=false`、`idempotentHint=true`；manifest `read_only=true`，scopes 一律 `*.read` |
| 输入 schema | JSON Schema Draft 2020-12，`additionalProperties=false`；分页工具声明 `cursor`、`page_size ≤ 1000` |
| 返回 envelope | `{records:[{record_key, fields, unit?, period?, locator}], query_hash, fetched_at, source_system, attested, connector_id, next_cursor?, sync_token?, total?}` |
| 数字 | `fields` 中的数值一律 DEC-STR（十进制字符串），不出现 JSON number |
| `query_hash` | `sha256:` + 规范化（工具名 + 语义参数）的哈希；`cursor/page_size/sync_token` 不参与 → 可重放 |
| 分页 | 不透明游标（绑定 query_hash，跨查询伪造被拒 `CONNECTOR_CURSOR_INVALID`），单页 ≤ 1000 |
| 增量同步 | 增量工具返回 `sync_token`（水位）；带回 token 只返回此后变化的记录 |
| 认证 | manifest `auth{mode, secret_ref}`；`secret_ref` 形如 `env:BELEGT_SECRET_ERP` / `vault:erp/sap-b1`（`SecretResolver(vault=…)` 注入），**秘密值从不出现在 manifest 或输出中** |
| 速率 | manifest 自报 `rate_limit{requests_per_minute, burst}` |
| 运行形态 | stdio（本地进程）或 Streamable HTTP（前置 OAuth 2.1 / mTLS 反向代理） |
| 错误 | `ConnectorError(code)`：`CONNECTOR_TOOL_UNKNOWN / TOOL_NAME_INVALID / NOT_READONLY / INPUT_INVALID / OUTPUT_INVALID / CURSOR_INVALID / NOT_FOUND / AUTH_MISSING / WRITE_DENIED / SOURCE_ERROR / PATH_DENIED / LIMIT`；MCP 中表现为 `isError=true` 且文本含错误码 |

`locator` 规则（03 §3.5）：`erp/system` 来源必带 `connector`；文档来源必带 `blob_sha256` + `page/cell/bbox` 之一。

## 连接器与工具

| 类 / CLI kind | connector_id（默认） | 工具 |
|---|---|---|
| `FileFolderConnector` / `files` | `conn:files:<目录名>` | `file.folder.files.list`、`file.folder.files.describe`、`file.excel.range.get`、`file.excel.rows.query`、`file.csv.rows.query` |
| `SqlReadOnlyConnector` / `sql` | `conn:sql:default` | `db.sql.tables.list`、`db.sql.tables.describe`、`db.sql.rows.query`（结构化过滤，不接受原始 SQL）、`db.sql.named_queries.query`（运维预登记的只读 SQL，参数绑定） |
| `MockErpConnector` / `erp-mock` | `conn:erp:sap_b1-mock` | `erp.sap_b1.purchase_invoices.query`、`erp.sap_b1.items.get`、`erp.sap_b1.items.list`、`erp.sap_b1.business_partners.list`、`erp.datev.accounts_bookings.query`、`erp.datev.accounts.list` |
| `MailboxConnector` / `mailbox` | `conn:mailbox:default` | `mail.imap.folders.list`、`mail.imap.messages.list`、`mail.imap.messages.get`、`mail.imap.attachments.get` |
| `UtilityBillPdfConnector` / `utility-bills` | `conn:utility-bills:<目录名>` | `docs.utility_bills.files.list`、`docs.utility_bills.facts.query`、`docs.utility_bills.pages.get` |

说明：

- **SQL 只读三道闸**：工具层只生成 `SELECT`（SQLAlchemy Core，表白名单 + 列/运算符白名单）；命名查询登记时用
  `is_readonly_sql()` 拒绝 DML/DDL/多语句；引擎/会话级只读（SQLite `PRAGMA query_only=ON`、PostgreSQL
  `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`、MySQL `SET SESSION TRANSACTION READ ONLY`）。`rows.query` 支持 `watermark_column` 增量；省略时用单列整数主键作水位（只适用 append-only 表）。
- **模拟 ERP**：随包样例数据（`data/erp_sample/`）仿 SAP B1 Service Layer（OPCH/PCH1、OITM、OCRD）与 DATEV 导出
  （EXTF 字段），含 2025 年电力 4 820 000 kWh、燃气、区域供热、柴油（L）、热轧钢卷（t，CN 72085120，原产地 TR）、
  铝锭等采购；`attested=true`（ERP 来源可走 `EV-AUTO-ERP-ATTEST`）。`purchase_invoices.query` 按 `UpdateDate` 增量。
- **邮箱**：`LocalMailBackend` 读本地 mbox / `.eml` 目录（测试与离线演示），`ImapBackend` 走 IMAP4-SSL，**只用
  `EXAMINE`（readonly select）与 `BODY.PEEK[]`**，不会改变已读标记；按 UID 增量。每封邮件识别截止日期候选
  （`bis 15.01.2027`、`by January 31, 2027`、“尽快”→ 无）、问卷附件候选、回复/催办（In-Reply-To）；正文作为外部数据原样返回，注入检测由 guard 负责（`external_data=true`）。
- **电费/燃气单**：优先 `belegt_io.extract_pdf_evidence`（页码 + bbox + text_excerpt_hash）；不可用时退回内置文本层
  提取（pypdf 或纯 Python 解析），此时置信度封顶 0.85，不满足自动核验阈值。

## 用法

### Python

```python
from belegt_connectors import MockErpConnector, record_to_evidence

erp = MockErpConnector()
env = erp.call("erp.sap_b1.purchase_invoices.query",
               {"date_from": "2025-01-01", "date_to": "2025-12-31", "item_code": "E-STROM", "page_size": 100})
rec = env["records"][0]
ev = record_to_evidence(env, rec, fact_key="activity.electricity.purchased")   # Evidence 草稿（DEC-STR + locator）

for page in erp.iter_all("erp.datev.accounts_bookings.query", {"account_from": "6325", "account_to": "6325"}):
    ...
records, token = erp.sync("erp.sap_b1.purchase_invoices.query", {"date_from": "2025-01-01", "date_to": "2026-12-31"})
new, token = erp.sync("erp.sap_b1.purchase_invoices.query", {...}, token)      # 只返回新增/变更
```

### MCP server

```bash
belegt-connector serve erp-mock                                   # stdio
belegt-connector serve files --root ./belege --transport streamable-http --port 8765
belegt-connector serve sql --dsn-secret env:BELEGT_SECRET_DB --table energy_readings
belegt-connector serve mailbox --imap-host imap.example.de --imap-user intake@x.de --imap-secret env:BELEGT_SECRET_IMAP
belegt-connector serve utility-bills --root ./rechnungen --underscore-names
```

```python
from belegt_connectors import build_mcp_server, UtilityBillPdfConnector
server = build_mcp_server(UtilityBillPdfConnector("./rechnungen"))   # 进程内（测试 / 嵌入）
```

### CLI 其他子命令

```bash
belegt-connector tools erp-mock                      # 打印 manifest（工具、schema、scopes、auth、rate_limit）
belegt-connector call files --root ./x --tool file.csv.rows.query --args '{"path":"a.csv","page_size":5}'
belegt-connector check utility-bills --root ./x      # 跑合规套件，失败退出 1
belegt-connector samples ./samples                   # 生成离线样例（CSV/XLSX、mbox+eml、电费单 PDF）
```

## 合规测试套件（社区连接器合并门槛）

`run_compliance(connector, secrets=[...])` / `belegt-connector check`：

| 检查 | 内容 |
|---|---|
| CC-01 | 工具命名与 verb |
| CC-02 | 只读注解、manifest `read_only`、scopes `*.read` |
| CC-03 | 输入 schema 合法、`additionalProperties=false`、分页参数 |
| CC-04 | envelope 符合 schema；`query_hash`、`fetched_at`（UTC ISO 8601）格式 |
| CC-05 | `fields` 无 JSON number（DEC-STR） |
| CC-06 | 每条记录带 Locator，`connector` 子对象与 envelope 一致 |
| CC-07 | `page_size=1` 逐页遍历：无重复、可终止、与整页一致；伪造游标被拒 |
| CC-08 | 可重放：`query_hash` 稳定且不受分页参数影响 |
| CC-09 | 秘密值不出现在 manifest / 输出 |
| CC-10 | 增量工具返回 `sync_token`，数据未变时带回 token 返回 0 条 |
| CC-11 | 注册写 verb 被拒 |

## 测试

```bash
pytest -q packages/belegt-connectors      # 73 项，全离线（IMAP 用假服务器，MCP 走进程内与本地 stdio 子进程）
```

## 取舍

- MCP 是可选依赖：没有 `mcp` 包时 Python 类照常可用，只有 `serve/build_mcp_server` 不可用。
- Streamable HTTP 的 OAuth 2.1 / mTLS 不在本包内实现，由前置网关负责（manifest 中声明 `auth.mode`）。
- 模拟 ERP 不是 SAP/DATEV 的完整模型，只覆盖能源与采购评测/演示所需的字段；字段名尽量沿用原系统，便于替换为真实适配器。
- 电费单退回提取器是“尽力而为”：只覆盖常见德/英标签（Wirkarbeit、Verbrauch、Fernwärme…），不做 OCR。
