# CHANGELOG

本文件记录 `belegt-dictionary` 的字典发版（`vYYYY.Q`）与包版本（SemVer）。格式参照 Keep a Changelog；每个发版另附机器可读迁移 `data/migrations/vYYYY.Q.yaml` 与人读说明 `data/migrations/vYYYY.Q.md`（02 §6.5）。

## [v2027.1] = 0.1.0 — 未发布（首个公开版，MVP，FR-701）

### 新增
- 02 §5 全部 91 个 DataPoint（org 9、env 17、energy 7、prod 20、mat 14、supply 7、cert 5、gov 4、social 8），状态均为 `draft`、版本 `1.0.0`、`introduced_in: v2027.1`；随 v2027.1 发版转 `active`。
- 9 个 `template_id` 的 10 个 TemplateMapping 文件：`tm:vsme@eu-2026-1560`、`tm:cdp_climate@2025`、`tm:pact_pcf@2.3.0`、`tm:pact_pcf@3.0.0`、`tm:catenax_pcf@cx-0136`、`tm:cbam_communication@2024`、`tm:eudr_dds@2023-1115`、`tm:reach_scip@echa-scip-3`、`tm:cmrt@6.5`、`tm:eu_battery_passport@2023-1542`（共 119 条；`locator.verify: true` 标注待对照官方版本核对的条目）。
- JSON Schema：`datapoint.schema.json`（02 §1.3）、`template_mapping.schema.json`、`migration.schema.json`。
- 单位表 `units/units.yaml`（02 §3.1，含计算内部单位）与换算表 `units/conversions.yaml`（02 §3.2，`GJ→MWh` 为精确 `1/3.6`）。
- 枚举：`iso3166_alpha2`（249）、`nace_rev2`、`vsme_module_option`、`pcf_declared_unit`、`cbam_production_route`、`battery_chemistry`、`conflict_metals`、`qualifiers`。
- 别名表 `aliases/aliases.yaml`（`dp:env.ghg.scope2_total`、`dp:energy.consumption.green_power_share`）。
- 方法论引用登记 `refs/methodologies.yaml`、缩写白名单 `refs/abbreviations.yaml`、DC-10 指纹库 `refs/text_fingerprints.yaml`（空）。
- Python 包 `belegt_dictionary`：`load_dictionary()`、pydantic 模型、Decimal 精确单位换算、期间对齐（02 §3.4）、别名解析、按模板字段查找。
- CLI `belegt-dict validate|lint|diff|fmt|coverage`，实现 DC-01…DC-12。
- RFC 模板 `rfcs/0000-template.md`。
