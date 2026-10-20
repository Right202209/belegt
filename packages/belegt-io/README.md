# belegt-io

Belegt 的文件导入导出包（Apache-2.0）。只做**确定性**的事：问卷切题、原格式回填、证据定位、标准格式导出。
不调用 LLM，不依赖 `belegt_core`（自带轻量数据类，字段名与 03 `Question` / `Evidence` 一致）。

| 能力 | 规格 | 入口 |
|---|---|---|
| 问卷解析（xlsx / xlsm / csv → `QuestionDraft[]`） | 03 §3.3、04 §2.2–§2.3 | `parse_questionnaire` |
| 已知模板指纹 + 精确映射（`exact_template`） | 02 §4（9 个 `template_id`） | `detect_template`、`detect_json_template` |
| 原格式回填 + 回填报告 + `export_input_hash` | 01 FR-501（AC-501-1/2/3）、04 §2.11 | `fill_back` |
| 文档证据确定性辅助（PDF 页码/bbox、xlsx 单元格） | 03 §3.5 `Locator`、R-LOC | `extract_pdf_evidence`、`extract_xlsx_evidence` |
| 带单位数字抽取与清洗（含德语 `1.234,5`） | 02 §3.6、§3.1 | `extract_quantities`、`parse_number` |
| PACT v3.0.3 `ProductFootprint` | 05 §4.6、01 AC-503-1 | `export_pact` |
| Catena-X PCF（`io.catenax.pcf:10.0.0`） | 02 §4.3.3 | `export_catenax_pcf` |
| CBAM communication（运营者 → 进口商）xlsx | 05 §6.9、02 §4.3.4 | `export_cbam_communication` |
| CMRT 简化导出 | 02 §4.3.7 | `export_cmrt` |
| 电池护照碳足迹字段片段 | 02 §4.3.8、01 AC-503-2 | `export_battery_passport_cf` |

## 安装与测试

```bash
pip install -e packages/belegt-io
pytest packages/belegt-io            # 不访问网络，不需要 LLM key
python -m belegt_io.examples <目录>   # 重新生成示例文件（字节级可复现）
```

依赖：`openpyxl`、`pyyaml`、`pydantic`、`jsonschema`（导出校验）、`pypdf`（PDF 文本与坐标）。

## 快速上手

```python
from decimal import Decimal
from belegt_io import parse_questionnaire, fill_back

pq = parse_questionnaire("examples/oem_fragebogen_de_100.xlsx")
len(pq.questions)                      # 100
q = pq.by_native_id()["2.01"]
q.locator                              # sha256:…#2 Umwelt & Energie!C6（答案单元格）
q.answer_format, q.unit_requested      # ('number', 'tCO2e')
q.period_requested                     # 2025-01-01 – 2025-12-31（来自表头"Berichtsjahr: 2025"）
q.to_question_fields("req_…")          # 03 Question 业务字段

report = fill_back("examples/oem_fragebogen_de_100.xlsx",
                   {q.locator: Decimal("1835.2"), "1 Unternehmen!C6": "Musterfirma GmbH"},
                   "out.xlsx", parsed=pq)
report.ok, report.export_input_hash
print(report.to_markdown())            # 变更单元格列表（AC-501-2）
```

## 设计要点

### Locator
- 对象形式与 03 §3.5 一致：`{blob_sha256, mime, page?, sheet?, cell?, bbox?, text_excerpt_hash?}`，`cell` 不含工作表名。
- 字符串形式（回填答案字典的键）：`sha256:<hex>#<Sheet>!<Cell>`；CSV 为 `sha256:<hex>#C5`；PDF 为 `sha256:<hex>#p2`。
  也接受不带哈希的 `Sheet!B12`。带哈希时必须与源文件一致，否则 `IO_HASH_MISMATCH`。
- `QuestionDraft.locator` 指**答案单元格**（回填用）；题干位置在 `text_locator`。合并单元格一律取左上角。

### 问卷解析
1. 表头识别：前 40 行内按关键词（德/英）给列分角色：`id`（Nr./Frage-Nr./Question number…）、`question`、`answer`、`unit`、`comment`、`evidence`、`section`、`period`、`options`；无答案列表头时取题干右侧第一个空表头列。
2. 分节行：横跨到答案列的合并行、或无题号的粗体短行 → `section`。
3. 数据验证：`list` 下拉（字面量 / 同表或跨表区域 / 定义名称）→ `options` + `answer_format=enum|bool`；`date` → `date`；`decimal`/`whole` → `number`。
4. 启发式：语言（停用词 + 字符特征）、期望值类型、单位（单位列优先，其次题干）、期间（题干年份 / 日期区间 / "zum 31.12.2025" 时点 / 文档级"Berichtsjahr: 2025"；"Geschäftsjahr" 只给 `fiscal_year` 提示不猜起止）。
5. 隐藏工作表、列表页跳过；多工作表题号全局连续编号（`ordinal`）。
6. 模板：第一轮按工作表名 + 首屏文本打分，第二轮加入题号；置信度 ≥ 0.85 才挂 `exact_mappings`（02 §4.4），否则退回语义映射（由 `question-parser` 做）。CBAM/CMRT 用模板专用版面（`form`/`table`/`matrix`/`questionnaire`）切题。

### 回填 `fill_back`
- 白名单：只写解析出的答案单元格（`strict=True`）；题干、公式、他人文件的单元格一律拒绝并记录错误码。
- 适配：枚举按下拉选项匹配（大小写不敏感；`True/False` → 该问卷的 Ja/Nein 或 Yes/No）；日期写 Excel 日期并在 General 格式时补 `DD.MM.YYYY`（德）/`YYYY-MM-DD`；数字写 Decimal，支持 `{"value": v, "round": n}`（02 §3.2 导出舍入，ROUND_HALF_EVEN），百分比格式单元格自动 ÷100（字典存 0–100）；德语数字串 `"1.234,5"` 可解析，歧义串拒写。
- 写前校验：枚举合法性、`whole`/`decimal` 数据验证边界。
- 输出确定：固定 zip 时间戳与 `docProps/core.xml` 的 modified，同一源文件 + 同一答案集 → 同一输出 sha256；`export_input_hash = sha256(源文件哈希 + 规范化答案集)`。

### 数字清洗（02 §3.6）
- `1.234,5`/`1,234.5`/`1.234.567,89`/`1'234.5`/`1 234,5` 无歧义直接解析；`1.234`、`1,234` 这类相差 1000 倍的写法：语言 + 同文档交叉验证一致才采用，否则 `needs_confirmation=True`。
- 单位归一到 02 §3.1 代码；`ton` 歧义、`tCO2 ≠ tCO2e`、`GWh/MJ/TJ` 不在字典单位表均打标；`%` 且值 ≤ 1 打标。
- `< 0,1 %`、`0,3–1,0 %` 不取中值：`value=None`、`is_range`、`is_estimate`。
- `classify_null`：`zero` / `not_applicable` / `no_data` / `confidential`。

### 导出
- **PACT v3.0.3**：结构用随包 `schemas/pact-productfootprint-3.0.3.schema.json`（由官方 OpenAPI 3.0.3 派生）校验；另加规则：SHALL 字段、`exemptedEmissionsPercent ≤ 3`、`primaryDataShare` 0–100、DQI 2027 起必填（之前为警告）、`productMassPerDeclaredUnit` 对质量相关声明单位须 > 0、`referencePeriodEnd` = 闭区间 end + 1 天、十进制字符串规范化。`pcf_input_from_datapoints` 按 05 §4.6 把 `dp:` 值映射到字段。
- **Catena-X**：按 Tractus-X 官方生成的 `io.catenax.pcf:10.0.0` JSON Schema（draft-04）校验；数值为 JSON number，用 Decimal 精确写出；国家 → UN M49 子区域内置常用映射。
- **CBAM communication / CMRT**：先生成（或用调用方提供的）模板，再走 `parse_questionnaire` + `fill_back`，导出物与解析器同一套 locator，可往返。SEE 默认 6 位、比电耗 3 位（05 §1.2）。
- **电池护照**：`mode="upstream"` 输出 Art. 7(1)(d)(e) 数值与 Annex II 阶段拆分并标 `not_a_declaration`；`mode="passport"` 按 AC-503-2 不含碳足迹声明字段，`omitted_fields` 写明原因供界面展示。

## 示例文件（`examples/`）

| 文件 | 说明 |
|---|---|
| `oem_fragebogen_de_100.xlsx` | OEM 德语 100 题：公司信息、Scope 1/2、能源、CBAM、REACH、证书、供应链；含合并单元格、跨表/定义名称/字面量下拉、日期与数字校验、公式、条件格式、隐藏列表页 |
| `cbam_communication_template.xlsx` / `_sample.xlsx` | CBAM 沟通模板（Belegt 样例版面）空表 / 示例填写 |
| `cmrt_simplified_template.xlsx` / `cmrt_sample.xlsx` | CMRT 简化版空表 / 示例 |
| `oem_fragebogen_kurz.csv` | 15 题 CSV（`;` 分隔，UTF-8 BOM） |
| `stromrechnung_2025.pdf` | 证据提取演示用电费单（德式数字） |
| `pact_productfootprint_sample.json`、`catenax_pcf_sample.json`、`battery_passport_cf_fragment_sample.json` | 导出样例 |

示例数值均为虚构演示数据。

## 错误码（本包新增）

`IO_UNSUPPORTED_FORMAT`、`IO_FILE_UNREADABLE`（含口令保护）、`IO_HASH_MISMATCH`、`IO_NOT_ANSWER_CELL`、`IO_ENUM_INVALID`、`IO_TYPE_MISMATCH`、`IO_FORMULA_CELL`、`IO_LOCATOR_INVALID`、`IO_EXPORT_INVALID`。PACT 规则 ID：`PACT-R-ID|DATETIME|DEC|SHALL|EXEMPT|PDS|DQI|MASS`。

## 已知限制

- openpyxl 往返会丢失图片/图表、部分 `extLst` 扩展（含旧式跨表数据验证扩展）；公式保留但缓存值清空（Excel 打开时重算）。源文件含 `xl/media`/`xl/charts` 时回填报告给出警告。
- CBAM/CMRT 生成的是按 02 §4.3 映射文件定义的样例版面，不是欧盟委员会 / RMI 官方文件；官方模板的单元格位置 [需核对]。用户上传官方模板时请通过 `template_path` 回填，并先核对 `mappings/*.yaml` 的标签/列名。
- PDF 的 bbox 为基于字体大小的估算（PDF 点坐标，[x0,y0,x1,y1]），足以高亮定位，不保证逐字精确；扫描件需先 OCR。
- 题干语言/类型/单位/期间均为启发式，`answer_format_confidence` 给出把握度，最终映射由 `question-parser` 与人工确认。
