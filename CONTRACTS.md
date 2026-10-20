# Belegt 实现契约（所有子任务必须遵守）

设计文档在 `../../docs/`（相对本文件：`/workspace/projects/dcf527dd-64c7-48e3-a2d5-34cbab52d81b/docs/`），`_baseline.md` 是命名基准。
代码以文档为规格；文档与此文件冲突时以此文件的工程约定为准、规格以文档为准。

## 布局（monorepo，根 = code/belegt）
| 目录 | Python 包名 | 许可 | 规格 |
|---|---|---|---|
| packages/belegt-dictionary | `belegt_dictionary`（数据 + 校验器） | CC BY 4.0（数据）/Apache-2.0（工具） | 02 |
| packages/belegt-calc | `belegt_calc` | Apache-2.0 | 05（API 见 05 §8） |
| packages/belegt-core | `belegt_core` | AGPL-3.0-only | 03、04、06、07、10 |
| packages/belegt-io | `belegt_io` | Apache-2.0 | 01、02 §4、03 §3.3、05 §4.6 |
| packages/belegt-supplier | `belegt_supplier` | Apache-2.0 | 08 |
| packages/belegt-connectors | `belegt_connectors` | Apache-2.0 | 04 §6.1 |
| packages/belegt-bench | `belegt_bench` | Apache-2.0 / CC BY 4.0 | 11 |
| web/ | React + TypeScript (Vite) 审批台 | AGPL-3.0-only | 01 |
| deploy/ | docker compose、Helm、Postgres 迁移 | — | 10、03 §6 |

每个 Python 包：`src/` 布局，`pyproject.toml`（setuptools，requires-python>=3.12），`tests/`（pytest），`README.md`（中文），`LICENSE`。
安装：`pip install -e packages/<pkg>`；根目录 `make test` 跑全部。

## 工程约定
- Python 3.12，类型标注，pydantic v2 做模型；数字一律 `decimal.Decimal`，序列化为十进制字符串（DEC-STR，03 §2.2）。
- ID：`<prefix>_<ULID>`（`python-ulid`）；引用 `ObjectRef = "<id>#v<version>"`。
- 时间 UTC ISO 8601；日期 ISO。
- 测试不得访问网络，不得需要真实 LLM key。LLM 统一通过 `belegt_core.llm.LLMClient` 协议，提供 `OpenAICompatibleClient`（httpx，读 `BELEGT_LLM_BASE_URL`/`BELEGT_LLM_API_KEY`/`BELEGT_LLM_MODEL`）与确定性 `MockLLM`（规则/脚本化回复）。
- 存储：SQLAlchemy 2.x；测试与本地 demo 用 SQLite，生产 PostgreSQL 16（迁移 + RLS 在 deploy/ 与 belegt-core/migrations）。
- 依赖方向：dictionary ← calc ← core；io、supplier、connectors 只依赖 core 的公开模型（或无依赖）；bench 依赖全部。calc 运行期只用标准库 + pyyaml（读因子文件）。
- 错误码沿用文档：`GRAPH_*`（03 §6.8）、`CALC_*`（05）、`EGRESS_DENIED_*`（04 §2.15）。
- 每个模块文件头注明对应文档章节（如 `# Spec: 03 §3.7`）。
- 已安装：fastapi pydantic sqlalchemy alembic pytest jsonschema openpyxl pyyaml httpx python-ulid temporalio langgraph mcp。缺的包可 pip install，但需写进 pyproject。

## 跨包接口
- `belegt_calc.calculate(...)` 签名按 05 §8.1；`belegt_calc.input_hash` 按 03 §4.5。
- `belegt_dictionary.load_dictionary(path=None) -> Dictionary`（`.datapoints: dict[str, DataPoint]`、`.template_mappings: dict[str, TemplateMapping]`、`.units`、`.convert(value: Decimal, from_unit, to_unit) -> Decimal`）。数据文件随包分发（package data）。
- `belegt_core.models`：03 全部核心对象的 pydantic 模型（字段名与 03 完全一致），`belegt_core.schemas/` 放 03 §5 JSON Schema 文件。
- `belegt_core.store.GraphStore`：`write(type, obj, expected_version=None, idempotency_key=None, actor=...) -> ObjectRef`，`get(id, version=None)`, `head(id)`, `query(type, **filters)`, `lineage(ref)`, `impact(ref)`；全部 03 写入校验规则在 `belegt_core.rules` 中实现，失败抛 `GraphError(code, rule_id, path, message)`。
- `belegt_core.api.app`：FastAPI，路由按 03 §6.8 + 01 所需的 UI 端点（`/requests`、`/inbox`、`/approvals`、`/closed-questions`、`/conflicts`）。
- `belegt_io`：`parse_questionnaire(path) -> ParsedQuestionnaire(questions=[QuestionDraft...])`（xlsx/csv）；`fill_back(src_path, answers: dict[locator, value], out_path)`；`export_pact(...)`、`export_cbam_communication(...)`。
