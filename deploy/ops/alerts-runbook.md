# 告警 Runbook（ALR-01…ALR-12）

> Spec：10 §5.3（"每条告警必须链接运行手册，无运行手册的告警不得上线"）。规则：`deploy/compose/prometheus/rules/belegt-slo.yml`。
> 仪表盘：Grafana 文件夹 Belegt（DSH-01…06）。安全类告警（ALR-06、ALR-11）转 `incident-response-runbook.md`。

## ALR-01 … ALR-03 SLO 燃烧率

**含义**：SLO-01（API 非 5xx 且 < 2 s，99.9%）或 SLO-06（写入非 5xx，99.95%）错误预算消耗过快。ALR-01/02 呼叫，ALR-03 工单。

1. DSH-01 → "API 请求速率（按状态类）"与"API 延迟"：区分 5xx 还是慢请求。
2. 5xx：`kubectl logs deploy/belegt-api | jq 'select(.level=="error")'`，按 `error_code` 聚合。常见：
   - 数据库不可达（`/readyz` 503）→ 见 ALR-08；连接耗尽 → 检查 `pg_stat_activity`、扩 API 副本前先确认 PG `max_connections`。
   - `graph.commit_version` 报 `[db]` 错误（`rule_id=DB`）→ 物理表与可移植表分叉或约束触发，见 belegt-core `store/pg_commit.py` 说明，
     对照 `graph.object_head` 与 `graph_object_head` 的版本。
3. 慢请求：DSH-01 "证据图查询 P95"；检查 `pg_stat_statements` Top 语句、缺失索引（03 §6.3）。
4. 最近发布引起 → 回滚（金丝雀自动回滚未触发时手动 `helm rollback`；contract 迁移之后只能 PITR，10 §6.5）。
5. 错误预算政策见 `slo.md` §2.2。

## ALR-04 halted

- **halted:policy（呼叫）**：guard 判定策略违规（GP-*）。DSH-05 找出 `check`；若为注入/越界类，按安全事件处理（P2 起）。
  恢复需人工 `resume`（04 §5），不要批量解除。
- **halted:budget_* 日比例 > 5%（工单）**：熔断频繁（10 §4.4 目标 €3、熔断 €5）。查 DSH-03 哪个 Agent/档位超支；
  常见原因：新模板无缓存（C1/C2 命中低）、扫描件 OCR 走 `tier-V`、提示词变更。租户熔断值只能下调，上调需 `org_admin` 留痕。

## ALR-05 成本

- **单份成本 24 h P95 > €3（工单）**：DSH-03 按 Agent/模型拆分；对照 10 §4.2 估算表找偏差最大的 Agent。检查是否误用方案 A（全大模型）路由。
- **某 org_tier 日支出 > 7 日均值 3×（呼叫）**：可能是循环/重试风暴。查 `obs.trace_span`：
  `SELECT request_id, count(*), sum(cost_eur) FROM obs.trace_span WHERE started_at > now() - interval '1 day' GROUP BY 1 ORDER BY 3 DESC LIMIT 20`，
  对异常 Request 写 `halted`。

## ALR-06 审计链断裂

**P1 安全事件**。见 `incident-response-runbook.md#审计链断裂alr-06`。校验函数：`audit.verify_chain()`（0005 迁移，SECURITY DEFINER）。
校验作业应每小时对每个租户执行并推送 `belegt_audit_chain_breaks` / `belegt_audit_chain_last_verified_timestamp_seconds`。

## ALR-07 Temporal 积压

**含义**：task queue schedule-to-start P95 > 30 s 持续 10 min（指标名以 Temporal 版本为准）。

1. DSH-01：哪个 `task_queue`？worker 副本是否就绪（`kubectl get pods -l app.kubernetes.io/component=worker`）。
2. worker CPU 打满 → HPA 是否已到 `maxReplicas`；按 10 §3.3 应以积压驱动扩容（KEDA temporal scaler）。
3. worker 正常但不消费 → 检查 worker 日志中的 Temporal 连接错误、namespace/task queue 配置（`BELEGT_TEMPORAL_*`）。
4. Temporal Server 自身 → history/matching 服务延迟、PG 慢查询。

## ALR-08 PostgreSQL 复制 / WAL

1. 复制延迟 > 60 s：备库 IO、网络、长事务（`pg_stat_activity` 中 `xact_start` 最老的会话）。
2. WAL 归档失败：`pg_stat_archiver.last_failed_wal`；检查归档目标桶凭据与容量（WAL-G/pgBackRest 日志）。归档失败会让 PITR 窗口出现缺口 → 视为 SLO-08 风险。
3. 主库磁盘 > 70%（10 §8.6）→ 扩容；不要删除 `pg_wal`。

## ALR-09 备份

**含义**：备份连续 2 次失败或最新可恢复点 > 2 天（SLO-08）。

1. 查看 `belegt-backup` 日志（compose：`docker compose --profile backup logs belegt-backup`）。
2. 常见：磁盘满（`BACKUP_DIR`）、凭据轮换后未更新、备份桶 Object Lock 拒绝覆盖（脚本按时间戳目录写入，不覆盖）。
3. 手动补跑：`docker compose --profile backup run --rm belegt-backup /ops/backup.sh`，确认 MANIFEST 生成并上传。
4. 每季度恢复演练（`restore.sh` 到 staging）是 SLO-08 的一部分。

## ALR-10 Agent 质量

PC-04 失败率 > 2%（疑似幻觉）或影子评测 Top-1 下降 > 2 个百分点 → Agent 组工单。DSH-04 定位规则与 Agent；对照最近的
`model-routing` / 提示词变更；必要时回退到上一个 `model-routing@<semver>`（只改路由，不改代码，NFR-05）。

## ALR-11 egress.block / GP-07 突增

**呼叫安全值班**。见 `incident-response-runbook.md#出口阻断或注入突增alr-11`。

## ALR-12 证书 / 令牌 / 密钥 14 天内到期

见 `key-rotation-runbook.md`。证书由 cert-manager 自动续签，告警说明续签失败；密钥类到期按对应章节轮换。
