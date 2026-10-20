# SLO / SLA 定义

> Spec：10 §5.3（内部 SLO 与 burn-rate 告警）、§7（对外 SLA、可用性定义、错误预算政策）、§2.2 NFR-07/09/12/13。
> 机器可读版本：`deploy/compose/prometheus/rules/belegt-slo.yml`（记录规则 + ALR-01…12，Helm 的 PrometheusRule 用同一文件）。

## 1. 原则

1. **SLO 先于 SLA**：对外承诺（§3）比内部目标（§2）宽一档；用错误预算驱动发布节奏。
2. **确定性部分不可降级**：证据图写入、`calc-service`、`consistency-auditor` 判定、`approval-router`、`guard` 属"必须可用"层；
   LLM 能力属"可降级"层。LLM 主备都不可用时状态页标"降级"，不计入不可用分钟（同月累计 > 8 h 按 SEV-2 处理）。
3. 所有窗口为 **30 天滚动**；时间一律 UTC 存储、Europe/Berlin 展示。

## 2. 内部 SLO（10 §5.3）

| ID | SLI（Prometheus 实现） | 目标 | 错误预算（30 d） | 告警 |
|---|---|---|---|---|
| SLO-01 | API 请求成功：非 5xx 且 < 2 s。`belegt_http_request_duration_seconds_bucket{le="2.0",status_class!="5xx"}` ÷ `_count` | 99.9% | 43.2 min | ALR-01/02/03 |
| SLO-02 | 读 API（GET）延迟 ≤ 300 ms 比例。`…_bucket{method="GET",le="0.3"}` ÷ `…_count{method="GET"}` | 99% | 7.2 h | 仪表盘 DSH-01 |
| SLO-03 | Request 首稿在 NFR-07 阶段目标内完成比例（`belegt_request_first_draft_seconds`：MVP ≤ 60 min，V1 ≤ 30 min） | 95% | — | DSH-02、发布门禁 |
| SLO-04 | `llm-proxy` 调用成功率（含切换备用模型）。`belegt_llm_request_duration_seconds_count{outcome=~"ok\|fallback_ok"}` | 99.5% | 3.6 h | DSH-03 |
| SLO-05 | 外发 15 min 内投递成功（不含收件方拒收）。`belegt_egress_messages_total{result="delivered_15m"}` | 99.5% | 3.6 h | DSH-05 |
| SLO-06 | 证据图写入成功（非 5xx）。`POST /graph/objects*` 的 5xx 比例 | 99.95% | 21.6 min | ALR-01/02/03 |
| SLO-07 | 审计哈希链每小时校验连续（`audit.verify_chain()` 返回空） | 100%（不变量） | 无 | ALR-06 → P1 安全事件 |
| SLO-08 | 备份成功且最新可恢复点 ≤ RPO | 100%（不变量） | 无 | ALR-09 |

### 2.1 燃烧率告警（Google SRE 多窗口多燃烧率）

| 告警 | 条件（SLO-01 预算 0.1%；SLO-06 预算 0.05%） | 级别 |
|---|---|---|
| ALR-01 | 1 h 与 5 min 窗口错误率都 > 14.4 × 预算（1 h 耗 2% 月预算） | 呼叫 |
| ALR-02 | 6 h 与 30 min 都 > 6 × 预算 | 呼叫 |
| ALR-03 | 3 d 与 6 h 都 > 1 × 预算 | 工单 |

其他告警（ALR-04…12）见 `alerts-runbook.md`。

### 2.2 错误预算政策（10 §7.3）

| SLO-01/06 月预算消耗 | 动作 |
|---|---|
| < 50% | 正常发布（金丝雀 5% → 25% → 100%，每步 30 min） |
| ≥ 50% | 冻结非紧急功能发布，只合并可靠性修复 |
| ≥ 100% | 每个发布须平台负责人批准 |
| CBAM 冲刺月（2027-09） | 预算按 50% 计；只发 patch，全组轮值 |

## 3. 对外 SLA（10 §7.1）

| 项 | 标准版 | 企业版 / 专属 SaaS |
|---|---|---|
| 月可用性 | 99.5%（≤ 3 h 39 min） | 99.9%（≤ 43.8 min） |
| RPO / RTO（区域灾难） | 15 min / 8 h | 15 min / 8 h（V2：4 h） |
| 计划维护 | 周日 02:00–06:00 Europe/Berlin，每月 ≤ 4 h，提前 5 个工作日通知 | 提前 10 个工作日；每年 9 月不做计划维护 |
| 支持 | 周一至周五 08:00–18:00 Europe/Berlin，德/英 | 同左 + SEV-1 7×24 |
| 首次响应 SEV-1 / 2 / 3 / 4 | 4 工作小时 / 1 工作日 / 3 工作日 / 尽力 | 1 h / 4 工作小时 / 1 工作日 / 3 工作日 |
| 服务赔偿 | < 99.5%：10%；< 99.0%：25%；< 95.0%：50% | < 99.9%：10%；< 99.5%：25%；< 99.0%：50% |

- 赔偿上限为当月费用 50%，客户须在 30 天内申请，赔偿为唯一补救。
- 私有化（`private-*`）不承诺可用性，只提供支持订阅的响应时效。
- 安全事件通知时限以 07 §9.4 与 DPA 为准（个人数据泄露 24 h 内通知控制者），不受本表约束。

### 3.1 可用性测量（10 §7.2）

- **可用**：两个欧盟探测点每分钟执行合成事务（登录 → 读工作台 → 创建并读取一个测试对象），且服务端 SLO-01 达标；
  连续 2 次探测失败计 1 分钟不可用。
- **排除**：客户侧原因、客户门户或邮箱服务商故障、不可抗力、客户未执行的已通知升级、客户自有 provider 密钥失效。
- 合成探测账号使用独立测试租户（`org_synthetic_*`），其对象按 10 §5.5 保留期清理。

## 4. RPO / RTO 场景（10 §6.6）

| 场景 | RPO | RTO | 手段 |
|---|---|---|---|
| Pod/节点故障 | 0 | ≤ 5 min | 副本 + PDB + HPA（Helm） |
| PG 主库故障（切同步备） | 0 | ≤ 15 min | 托管 PG / Patroni 同步备 |
| 逻辑误删 / 数据损坏 | ≤ 5 min | ≤ 4 h | WAL-G / pgBackRest PITR（35 天） |
| 区域灾难（切 Helsinki） | ≤ 15 min | 标准版 ≤ 8 h；企业版 ≤ 4 h（V2） | WAL 流复制到灾备区 + 对象存储跨区复制 |
| `private-compose` | ≤ 24 h（每晚 `backup.sh`）；开 WAL 归档后 ≤ 15 min | 客户自负，参考 ≤ 8 h | `deploy/ops/backup.sh` / `restore.sh` |

演练：每季度自动恢复到 `staging` 并跑验收（审计链 + 抽样 `verify-pack` + 合成问卷端到端）；每年一次区域切换实战演练，结果写入 ISMS 记录。
