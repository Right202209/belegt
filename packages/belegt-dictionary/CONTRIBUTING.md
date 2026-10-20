# 贡献指南（CONTRIBUTING）

感谢你为 Belegt 数据点字典做贡献。字典仓库采用 **DCO（Developer Certificate of Origin）**，不要求签署 CLA（02 §6.6、09）。

## 1. 先判断要不要 RFC（02 §7.1）

| 需要 RFC（`rfcs/NNNN-<slug>.md`，模板见 `rfcs/0000-template.md`） | 普通 PR + 一名 `domain_reviewer` 批准即可 |
|---|---|
| 新增 domain/topic；新增 DataPoint（一次 ≥ 5 个可合并为一份 RFC）；任何 MAJOR 变更；重命名/拆分/合并/弃用；新增模板族；修改 schema 或换算表 | 翻译修正；PATCH；现有模板版本内新增映射条目；新增模板年度版本文件 |

## 2. 典型流程

1. fork 仓库，新建分支。
2. 新增或修改 YAML：DataPoint 放 `src/belegt_dictionary/data/datapoints/<domain>/<topic>/<metric>[.<qualifier>].yaml`（一个文件一个 DataPoint，路径与 ID 一致），映射放 `src/belegt_dictionary/data/mappings/<template_id>/<template_version>.yaml`。
3. 本地运行：
   ```bash
   pip install -e .
   belegt-dict fmt                 # 规范化 YAML 格式（DC-01）
   belegt-dict validate            # DC-01…DC-12，任一 error 不得合并
   belegt-dict validate --baseline <上一发版目录>   # 检查 DC-09 版本递增、DC-02 retired 复用
   pytest
   ```
4. 修改已发布条目必须按 02 §6.3 提升 `version`（PATCH：措辞/翻译；MINOR：新增允许单位、方法论引用、关系、放宽校验；MAJOR：删除允许单位、收紧口径）。改义一律新建 ID，旧 ID 走弃用（02 §2.4）。
5. 提交 PR，描述中写明涉及的模板版本与题号来源（不附原文）。`domain_reviewer` 审语义，`maintainer` 审命名。

## 3. 写作规则（摘要）

- `definition` 先写包含什么、再写不包含什么，点名相邻数据点；用自己的话，条款号写进 `methodology_refs`。
- `short_label` ≤ 60 字符，自己撰写；**不得复制问卷或法规原文**（DC-10 会按 n-gram 指纹阻断）。
- ID 不带模板名、单位、年份、口径或租户信息；缩写须在 `refs/abbreviations.yaml` 登记（DC-03）。
- 排放类数字 `human_confirmation_allowed` 必须为 `false`；可人工确认的数字（如员工人数）须带标签 `human_confirmable_number`。
- 字典里永远不放任何企业的数值。

## 4. DCO 签署

每个 commit 都必须带 `Signed-off-by` 行，表示你同意下列 Developer Certificate of Origin 1.1，并声明所提交的短标签与定义为自行撰写、未复制受版权保护的原文：

```bash
git commit -s -m "feat(dict): add dp:env.water.consumption_total"
# → Signed-off-by: Your Name <you@example.org>
```

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

数据贡献按 CC BY 4.0、代码贡献按 Apache-2.0 授权（见 `LICENSE`）。每个季度发版说明会列出外部贡献者与贡献占比（PRD §9 指标；`contributions.yaml` 供 DC-12 统计）。
