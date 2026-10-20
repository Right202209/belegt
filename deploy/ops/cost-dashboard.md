# 成本仪表盘说明（DSH-03 LLM 成本 / DSH-06 容量与 FinOps）

> Spec：10 §4（成本预算：P95 ≤ €3/份、硬熔断 €5/份、方案 A/B/C）、§5.1（指标、禁止 `org_id` 标签）、§5.2（DSH-03、DSH-06）、
> §5.5（`obs.trace_span` 保留）、NFR-11、NFR-20；04 §5.2（guard 成本记账）。
> 仪表盘 JSON：`deploy/compose/grafana/dashboards/dsh-03-llm-cost.json`、`dsh-06-capacity-finops.json`（由 `gen_dashboards.py` 生成）。

## 1. 数据来源与口径

| 层 | 来源 | 粒度 | 用途 |
|---|---|---|---|
| 事实 | `Trace` 对象 → `obs.trace_span`（`tokens_in`、`tokens_out`、`cost_eur`、`model`、`request_id`），**100% 落库，不受采样影响** | 每次 LLM 调用 | 按租户/问卷对账、审计、计费 |
| 指标 | `belegt_llm_cost_eur_total{agent,tier,model}`、`belegt_llm_tokens_total{agent,tier,model,direction}`、`belegt_request_llm_cost_eur`（直方图，每份问卷一次观测） | 15 s 抓取 | 趋势、告警（ALR-05）、P50/P95 |
| 视图 | Langfuse（SaaS/Helm 可选） | 采样后 | 提示版本、评分 |

- **单份问卷成本** = 一个 `Request` 首稿 + 审批返工的 `cost_eur` 之和，不含门户提交与上游追数（NFR-11 口径）；门户与追数单列。
- 价格按 10 §4.1（2026-10 查询）配置在 `llm` 路由价表中；美元按 ECB 参考汇率换算；托管合同口径（EU 区域端点 +10%）已含在价表里。
- **禁止 `org_id` 进 Prometheus 标签**（高基数 + 租户信息外泄）。按租户的明细一律从 `obs.trace_span` 查询（§3）。

## 2. 面板说明

### DSH-03 LLM 成本与 token

| 面板 | 表达式要点 | 解读 / 阈值 |
|---|---|---|
| 单份问卷成本 P95（24 h） | `belegt:request_cost_eur:p95_24h` | 绿 < €2.5，橙 €2.5–3，红 > €3（ALR-05 工单）。方案 B 预期 €1.48（首稿 P95） |
| 单份问卷成本 P50 | `histogram_quantile(0.5, …belegt_request_llm_cost_eur_bucket[24h])` | 方案 B 预期 ≈ €1.01，方案 A ≈ €2.43 |
| 今日 LLM 支出（按 org_tier） | `belegt:llm_cost_eur:increase1d` | 7 日均值 3× → ALR-05 呼叫 |
| 成本速率（按 Agent / 模型·档位） | `rate(belegt_llm_cost_eur_total[1h]) * 3600` | 对照 10 §4.2：`evidence-miner` 与 `answer-drafter` 应占 ~55% |
| token 速率 | `rate(belegt_llm_tokens_total[5m]) * 60` | 与 provider TPM 配额比较（≥ 峰值 1.5 倍，10 §3.2） |
| LLM 调用延迟 P95（按 provider） | `belegt_llm_request_duration_seconds` | provider 抖动协变量（10 §3.6） |
| SLO-04 | `belegt:slo04_success_ratio:rate1h` | ≥ 99.5% |

### DSH-06 容量与 FinOps

| 面板 | 解读 |
|---|---|
| PG 数据库大小 / 30 天外推 | 每份问卷 10–20 MB（10 §8.6）；存储 > 70% 扩容 |
| provider TPM 使用 | 峰值 > 配额 60% 时提前申请配额（9 月 CBAM 冲刺前 6 周锁定） |
| GPU 利用率（DCGM） | > 70% 扩卡，< 20% 缩卡；本地模型月量 ≥ 600 份才比方案 B 便宜（10 §4.3），主要价值是隔离 |
| Temporal 积压 | schedule-to-start P95 > 5 s 为扩容触发（10 §8.6） |

## 3. 按租户明细（SQL）

以运维只读账号（`belegt_auditor` 组）执行，结果不得导出到第三方工具：

```sql
-- 近 30 天每租户、每问卷 LLM 成本（NFR-11 P50/P95 计算的原始数据）
SELECT org_id, request_id, sum(cost_eur) AS cost_eur, sum(tokens_in) AS tok_in, sum(tokens_out) AS tok_out
FROM obs.trace_span
WHERE started_at >= now() - interval '30 days' AND request_id IS NOT NULL
GROUP BY 1, 2;

-- 每租户 P50/P95 单份成本
WITH per_req AS (
  SELECT org_id, request_id, sum(cost_eur) AS c FROM obs.trace_span
  WHERE started_at >= now() - interval '30 days' AND request_id IS NOT NULL GROUP BY 1, 2)
SELECT org_id, count(*) AS requests,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY c) AS p50_eur,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY c) AS p95_eur
FROM per_req GROUP BY 1 ORDER BY p95_eur DESC;
```

## 4. 月度 FinOps 报告（NFR-20）

| 指标 | 计算 | 目标 |
|---|---|---|
| 每活跃租户基础设施成本 | 云账单（不含 LLM）÷ 当月有 ≥ 1 个 Request 的租户数 | MVP ≤ €150、V1 ≤ €60、V2 ≤ €40 |
| LLM 变动成本占定价比 | Σ`cost_eur` ÷ 订阅收入 | ≤ 10%（A-09），V2 ≤ 8% |
| 共享 GPU 利用率 | DCGM 月均 | V2 ≥ 40% |

账单数据（Hetzner/AWS 账单导出）不进 Prometheus；由财务按月导入 FinOps 表格，与 §3 查询结果合并后进入 DSH-06 的 FinOps 文本面板链接的月报。
参考基线：10 §4.5（SaaS 多租户 ≈ €2 210/月不含 GPU；20 租户 ≈ €110/租户，50 租户 ≈ €44/租户）。

## 5. 埋点要求（给 belegt-core / llm-proxy）

仪表盘与 ALR-05 依赖以下指标；**目前 belegt-core 尚未导出这些业务指标**（只有 belegt-api 的 HTTP/写入/查询直方图），
在埋点前相应面板为空、相关告警不会触发：

- `belegt_llm_cost_eur_total` / `belegt_llm_tokens_total` / `belegt_llm_request_duration_seconds`：在 `belegt_core.llm` 的客户端包装层（`CostLedger` 记账处）累加；
- `belegt_request_llm_cost_eur`（直方图，buckets `0.25,0.5,1,1.5,2,2.5,3,4,5,7.5`）：Request 进入 `in_review` 时按该 Request 的 `Trace.cost_eur` 求和观测一次；
- `belegt_request_first_draft_seconds{deploy_profile}`、`belegt_request_stage_duration_seconds{stage,template_id}`：工作流状态迁移处；
- 标签只用 `agent`、`tier`、`model`、`provider`、`org_tier`、`deploy_profile`，**不得**出现 `org_id`、`request_id`。
