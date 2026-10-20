# RFC-NNNN: <标题>
- 状态: draft | discussion | fcp | accepted | rejected | withdrawn
- 作者 / domain / 涉及模板:
- 目标发版: vYYYY.Q

<!-- 流程与时限见 02 §7：discussion ≥ 14 天，fcp 10 天；法规紧急变更快速通道 5 天 / 3 天。
     不得粘贴问卷或法规原文：只写题号、条款号与自己撰写的短标签（DCO 声明、DC-10 原文检测）。 -->

## 动机（哪些问卷、多少企业被问到；附题号，不附原文）

## 提案（新增/变更的 DataPoint YAML 全文；涉及的 TemplateMapping 条目）

```yaml
# datapoints/<domain>/<topic>/<metric>[.<qualifier>].yaml
id: dp:<domain>.<topic>.<metric>
version: 1.0.0
introduced_in: vYYYY.Q
status: draft
label: { de: "", en: "", zh: "" }
definition:
  en: >
    先说包含什么，再说不包含什么；点名相邻数据点 ID。
value_type: number
unit: <Belegt 单位代码>
period_type: reporting_period
boundary: [organization]
consolidation: [operational_control, financial_control, equity_share]
sensitivity: customer_shared
evidence_requirements:
  accepted_source_types: [document, erp]
  min_sources: 1
  requires_calculation: false
  human_confirmation_allowed: false
```

## 与现有数据点的关系（related_datapoints、为何不复用现有 ID）

## 迁移影响（rename/split/merge；auto_migrate 是否可行）

## 方法论与法规依据（条款号）

## 备选方案与不做的理由

## 未决问题
