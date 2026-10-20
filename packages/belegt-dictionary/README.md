# belegt-dictionary

Belegt 开源数据点字典：标准 `DataPoint` 定义、模板映射 `TemplateMapping`、单位与换算表，以及字典 CI 校验器 `belegt-dict`。规格见设计文档 02（数据点字典规范）。

- 字典发版 **v2027.1** = 包版本 **0.1.0**（首个公开版，全部条目 `status: draft`，随发版转 `active`）
- 91 个 DataPoint（02 §5），9 个 `template_id` 的 10 个映射文件（02 §4），DC-01…DC-12 全部检查（02 §6.2）
- 许可：数据 CC BY 4.0，代码 Apache-2.0（见 `LICENSE`）

字典只描述"是什么"（定义、单位、期间、口径、证据要求），**不含任何企业数值**；映射文件只存题号/单元格/字段名与我方撰写的短标签，**不含问卷原文**。

## 安装

```bash
pip install -e packages/belegt-dictionary
pytest packages/belegt-dictionary
```

依赖：Python ≥ 3.12、pydantic v2、PyYAML、jsonschema。数据文件随包分发（package data），无需网络。

## Python API

```python
from decimal import Decimal
from belegt_dictionary import load_dictionary, Period, EvidencePeriod, align_period

d = load_dictionary()                       # 默认读取随包数据；也可 load_dictionary("/path/to/root")
dp = d.get("dp:env.ghg.scope2_location_based")         # DataPoint（pydantic，字段名与 02 §1.1 一致）
dp.unit, dp.period_type, dp.boundary, dp.sensitivity   # 'tCO2e', 'reporting_period', [...], 'customer_shared'
d.get("dp:env.ghg.scope1_total@1.0.0")      # 带版本引用；版本不符抛 KeyError

# 别名（02 §2.5）：透明重定向并记录 warning
d.resolve_id("dp:env.ghg.scope2_total")     # → 'dp:env.ghg.scope2_location_based'

# 单位换算（02 §3.2）：Decimal + 有理系数，GJ→MWh 为精确 ÷3.6
d.convert(Decimal("4444.4412"), "GJ", "MWh")           # Decimal('1234.567')
d.convert("2012.5", "kWh", "MWh", round_digits=3)      # 仅导出时舍入，ROUND_HALF_EVEN → Decimal('2.012')
d.to_canonical("dp:env.ghg.scope1_total", "1500", "kgCO2e")   # 按 allowed_units 落库换算 → Decimal('1.5')
d.convert("1", "MWh", "tCO2e")              # UnitError：跨量纲是计算，不是单位换算

# 按模板字段查找（question-parser 的 exact_template / filler-file 回填）
m = d.find_by_template_field("vsme", "B3 Para 33(a)")
m[0].ref, m[0].datapoint_ids                # 'tm:vsme@eu-2026-1560#b3_33a_scope1', ['dp:env.ghg.scope1_total']
d.find_by_template_field("cdp_climate", "C6.1")          # 旧编号 legacy_ref 也能命中（FR-201）
d.find_by_template_field("cdp_climate", "7.7", column="market-based")
d.entry("tm:pact_pcf@3.0.0#declared_unit_v3")             # 直接解析条目引用

# 期间对齐（02 §3.4）：只做判定，不产出数字
fy = Period.parse({"start": "2025-10-01", "end": "2026-09-30"})
cy = Period.parse({"start": "2026-01-01", "end": "2026-12-31"})
dec = align_period(cy, [EvidencePeriod(fy, "ans_…", "year")], customer_accepts_reporting_period=True)
dec.step, dec.action, dec.min_risk_level    # 3, 'use_reporting_period', 'L2'
```

主要对象：`Dictionary`（`.datapoints`、`.template_mappings`、`.units`、`.aliases`、`.enums`、`.convert()`）、`DataPoint`、`TemplateMapping`/`MappingEntry`/`Transform`、`UnitRegistry`、`Period`/`align_period`/`align_point_in_time`/`validate_period`。

## 命令行

```bash
belegt-dict validate [ROOT] [--baseline OLD_ROOT] [--corpus TXT_DIR] [--only DC-03 ...] [--format json]
belegt-dict lint ...                         # validate 的别名（02 §6.2 用词）
belegt-dict diff OLD_ROOT NEW_ROOT --release v2027.2 --package-version 0.2.0 \
                 -o migrations/v2027.2.yaml --markdown migrations/v2027.2.md
belegt-dict fmt [ROOT] [--check]             # 规范化 YAML 格式
belegt-dict coverage [ROOT]                  # DC-12 覆盖报告
```

`ROOT` 省略时使用随包数据。也可 `python -m belegt_dictionary.cli validate`。任何 `error` 级发现使退出码为 1。

| 检查 | 实现要点 |
|---|---|
| DC-01 schema lint | DataPoint / TemplateMapping / 迁移文件过 JSON Schema；YAML 必须为规范格式（`belegt-dict fmt`）；02 §1.1 字段约束（数字的证据要求、排放数字禁止人工确认、`product_per_unit` 必须复合单位、含 `organization` 必填 `consolidation` 等） |
| DC-02 ID 唯一 | 全仓 ID 唯一且与路径 `datapoints/<domain>/<topic>/<metric>[.<qualifier>].yaml` 一致；映射文件 ID/路径唯一、`entry_id` 文件内唯一；带 `--baseline` 时 `retired` ID 不得复用 |
| DC-03 ID 规范 | 正则、长度（≤ 120 / 段 ≤ 48）、qualifier 已登记、命名禁忌：模板名、单位、年份/期间、口径/租户、否定式、未登记缩写 |
| DC-04 映射引用存在 | `datapoint_id`、`transform`（split/aggregate）、`condition` 引用的数据点存在且非 `retired`；`deprecated` 给 warn；`boundary_hint` 必须在数据点允许口径内 |
| DC-05 单位可换算 | 数据点单位在单位表且不是计算内部单位；`allowed_units`、`unit_hint`、`unit_convert` 同量纲且换算表有路径 |
| DC-06 枚举映射完备 | `enum_ref` 文件存在；`enum_map` 目标值在枚举中（布尔数据点须为 true/false） |
| DC-07 别名无环 | 别名不指向别名、不与现行 ID 冲突、目标存在；数据点 `aliases` 与别名表一致 |
| DC-08 i18n 完整 | `label.de/en/zh` 非空，`definition.en` 非空 |
| DC-09 版本递增 | 需 `--baseline`：按 02 §6.3 判定 PATCH/MINOR/MAJOR，改义（单位、类型、期间类型等）必须新 ID；已发布条目不得删除；`status: frozen` 的映射文件不得改动 |
| DC-10 原文检测 | `definition`、`short_label` 与模板原文语料做 n-gram 包含度检测（≥ 60% 阻断）。语料只以哈希指纹存于 `refs/text_fingerprints.yaml`，或用 `--corpus` 在本地临时计算 |
| DC-11 关系一致 | `sum_of` 两侧单位一致；`replaced_by`、`related_datapoints` 存在；`plausibility.expr` 属于 CEL 子集且引用存在；`methodology_refs` 已登记 |
| DC-12 覆盖报告 | 每模板映射条目数、覆盖数据点数、对带该模板标签数据点的覆盖率、待核对条目数；外部贡献占比（读 `contributions.yaml`）。只输出 info |

## 目录结构

```text
packages/belegt-dictionary/
├── LICENSE  LICENSES/{CC-BY-4.0,Apache-2.0}.txt  README.md  CONTRIBUTING.md  CHANGELOG.md
├── rfcs/0000-template.md
├── pyproject.toml
├── src/belegt_dictionary/
│   ├── dictionary.py  models.py  units.py  periods.py  cel.py      # 加载、模型、换算、期间对齐、表达式检查
│   ├── validate.py  versioning.py  diff.py  cli.py  yamlio.py      # DC-01…12、SemVer 判定、迁移说明、CLI
│   └── data/                                                       # ← 字典数据（CC BY 4.0），02 §6.1 结构
│       ├── release.yaml
│       ├── schema/{datapoint,template_mapping,migration}.schema.json
│       ├── datapoints/<domain>/<topic>/<metric>[.<qualifier>].yaml # 91 个
│       ├── mappings/<template_id>/<template_version>.yaml          # 10 个
│       ├── units/{units,conversions}.yaml
│       ├── enums/*.yaml   refs/{methodologies,abbreviations,text_fingerprints}.yaml
│       ├── aliases/aliases.yaml
│       └── migrations/v2027.1.{yaml,md}
└── tests/
```

## 约定速查

- ID：`dp:<domain>.<topic>.<metric>[.<qualifier>]`；引用版本写 `dp:…@1.0.0`；映射条目引用 `tm:<template_id>@<version>#<entry_id>`。
- `locator.verify: true` = 02 §4.3 中的"[需核对]"：发版前须对照官方当年版本逐条确认。
- 可人工确认的数字（员工人数等）以标签 `human_confirmable_number` 显式标注；排放类数字一律 `human_confirmation_allowed: false`。
- 标签中的模板 ID（`vsme`、`cdp_climate`、`pact_pcf`、`catenax_pcf`、`cbam_communication`、`eudr_dds`、`reach_scip`、`cmrt`、`eu_battery_passport`、`oem_custom`）对应 02 §5"覆盖模板"列，供 DC-12 计算覆盖率。

## 实现取舍与文档未定之处

1. **02 §1.3 schema 的条件分支修正**：原文 `if: {properties: {cardinality: {const: many}}}` 在缺省 `cardinality` 时也成立，会让所有数据点（包括 §1.2 示例本身）都被要求 `record_fields`。随包 schema 在该 `if` 中加了 `required: [cardinality]`（`status` 分支同样处理），其余与原文逐字一致。
2. **映射文件数**：02 §5 说"9 个映射文件（8 个模板族）"，§4.3.3 却列出 `pact_pcf@2.3.0`、`pact_pcf@3.0.0`、`catenax_pcf@cx-0136` 三个文件。本包按 §4.3 提供 9 个 `template_id`、10 个文件。
3. **CMRT 拆分条目**：§4.3.7 示例的 `datapoint_id: dp:mat.conflict_minerals.intentionally_added` 不在 §5 清单中，会被 DC-04 拒绝。映射 schema 允许 `split`/`aggregate` 条目省略 `datapoint_id`（目标在 `outputs`/`inputs` 中），CMRT 条目即如此。
4. **目录**：02 §6.1 的顶层 `datapoints/` 等目录按 CONTRACTS.md 的 src 布局放到 `src/belegt_dictionary/data/` 下（内部结构不变）。05 §1.3 里另一种写法（`templates/<id>/<version>/mapping.yaml`、`MIGRATION/`）与 02 冲突，以 02 为准。`MAINTAINERS.md`、`SECURITY.md`、`bench/`、`dist/` 构建产物与 npm 包未在本包中提供。
5. **自定 schema**：02 未给出 `template_mapping.schema.json` 与 `migration.schema.json`，由本包定义。额外字段：映射 `status: active|frozen`（支持 §6.3"旧文件只读"）、`notes`、`locator.verify` 及 `column/columns/row/field/per_metal`；迁移 `version_from/version_to/note/mapping_files_removed`，首发版 `previous: null`。
6. **"可人工确认的数字"**：02 §1.1 未给标注方式，采用标签 `human_confirmable_number`。§1.1 只列 bool/enum/date/text，但 §1.4 冶炼厂清单（`file`）示例为 `true`，故 `file` 也允许。
7. **DC-03 缩写检测是启发式**：段内 ≤ 4 个字母且不在 `short_words` 中的词，或不含元音的词，视为缩写，须在 `refs/abbreviations.yaml` 登记。v0.1 以 `RFC-0000` 名义登记了 cbam、eudr、scip、rohs、eori、nace、fte、hq、id、url、pct、coc、iso、iatf、kwh。单位禁忌对 `per_<unit>` 放行（§5 自有 `carbon_footprint_per_kwh`），`pct` 不视为单位（§5 自有 `reduction_pct`）。法规名作 topic 的例外限 cbam/eudr/scip/reach/rohs。
8. **单位量纲**：`tCO2e`/`kgCO2e` 与 `t`/`kg` 设为不同量纲，`t→tCO2e` 会被拒绝（02 §3.6 的 tCO2 ≠ tCO2e）。`%→1` 标为 `export_only`。`kgCO2e/kWh↔tCO2e/MWh` 数值相等，但 §3.2 未收录，故无路径。"tCO2e/百万 EUR"未建单位。反向换算取精确倒数；`MWh→GJ ×3.6` 按原表显式保留。
9. **Decimal 精度**：沿用 05 §1.2，`prec=34`、`ROUND_HALF_EVEN`；除不尽的换算（如 1 GJ→MWh）得到 34 位有效数字，只在显式 `round_digits`（模板导出）时量化。
10. **期间对齐**：第 2 步要求月/季度证据无缝、无重叠地拼满目标期间。第 3 步的"≥ 9 个月"按从重叠起点数整月计算。02 未定义数据点如何"禁止按时间折算"，故用调用参数 `proration_allowed_by_datapoint`，未增加 schema 字段。第 3 步是否适用于 `calendar_year` 数据点（如 CBAM）文档未定，交由调用方决定。第 1、2 步的风险等级文档未给，返回 `None`。
11. **DC-09 级别判定**：文档未覆盖的情况取舍如下。`sensitivity`、`evidence_requirements`、放宽 `boundary`/`consolidation`、有效期参数变化算 MINOR。新增 `block` 级合理性校验或收紧 min/max/regex 算 MAJOR。`value_type`/`unit`/`cardinality`/`period_type`/`enum_ref`/`record_fields` 结构变化算改义，必须用新 ID。实际升级大于要求时只给 warn。枚举文件新增值未按数据点逐一检查版本。
12. **DC-10**：因版权不随包分发任何模板原文，指纹库为空。阈值（标签 4-gram、定义 5-gram、包含度 ≥ 60%）为经验值。
13. **DC-12 外部贡献占比**：文档未定数据来源，读取可选的 `contributions.yaml`（`[{path, contributor, external}]`）。
14. **数据内容**：§5 只给了 ID、类型、单位和覆盖模板，以下内容为本包撰写或判断，发版前需要领域审阅：三语定义（德语、中文按 NFR-06 须母语审阅）、`sensitivity`、证据要求（如 `max_age_days: 550`）、`validity_rule`、关系，以及 VSME B1/B4–B11/C3/C6 等段落号。映射中未经 02 原文给出的定位器均标 `verify: true`（119 条中 87 条）。CDP 取水以兆升计而单位表无 ML、CDP 7.30.1 外购电不含自发电，均在条目 `notes` 中注明。
15. **别名 since**：02 §2.5 示例写 `dp:env.ghg.scope2_total` 自 `v2027.2`；本包作为 v2027.1 已收录该重定向，故记为 `v2027.1`。
16. **枚举**：NACE 采用 Rev. 2（门类字母 + 两位大类号，如 `C25`；NACE Rev. 2.1 的切换待定）；`pcf_declared_unit` 含 `piece`（PACT v3 是否有此值待核对）；`cbam_production_route` 只收钢铁路线子集。
17. CEL 表达式只做静态检查（子集语法 + 引用存在），求值属于 `consistency-auditor`（belegt-core）。`belegt-dict new datapoint|mapping` 骨架生成未实现。
