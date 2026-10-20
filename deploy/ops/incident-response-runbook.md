# 事件响应 Runbook

> Spec：07 §9.4（安全事件 P1–P4、时限、通报义务）、10 §8.7（On-call、SEV-1…4、流程）、§7.1（SLA 响应时效）、§6.6（恢复）、
> 03 §7（审计包）、04 §5（guard / halted）。两套分级可同时成立，**以更严者为准**。

## 1. 分级

### 1.1 可用性 / 功能（SEV，10 §7.1）

| 级别 | 定义 | 确认 | 指挥官+通讯员 | 状态页 |
|---|---|---|---|---|
| SEV-1 | 生产不可用，或客户申报截止日受威胁 | 5 min 内 | 必须 | 每 30 min |
| SEV-2 | 核心功能降级（如 LLM 主备都不可用 > 1 h、批量审批失败） | 15 min | 必须 | 每 30 min |
| SEV-3 | 一般问题（单功能异常，有绕行方案） | 工作时间 | 否 | 视情况 |
| SEV-4 | 咨询 / 需求 | — | 否 | 否 |

### 1.2 安全（P，07 §9.4）

| 级别 | 示例 | 定级时限 | 遏制时限 |
|---|---|---|---|
| **P1** | 跨租户数据泄露；注入导致外发/门户提交；**审计哈希链断裂**（ALR-06） | 发现后 1 h | 4 h |
| P2 | 凭据泄露（未证实被利用）；隔离区越界尝试被拦截但成功率异常；egress 白名单被绕过尝试 | 1 h | 24 h |
| P3 | 单个漏洞（未被利用）、配置偏差 | 1 工作日 | 按漏洞 SLA（严重 14 天 / 高危 30 天） |
| P4 | 信息类 | — | — |

## 2. 通用流程（10 §8.7）

1. **告警 → 确认**：值班在 PagerDuty/Opsgenie 确认；打开对应 runbook（告警 `runbook_url`）。
2. **定级**：SEV 与 P 各定一个；涉及个人数据时立即拉 DPO。
3. **组建**：SEV-1/2 或 P1/P2 指定指挥官（IC）与通讯员；在事件频道记录时间线（UTC）。
4. **遏制**（见 §3 动作库）→ **恢复** → **验证**（SLO 回到目标、合成探测通过）。
5. **通报**（§4）。
6. **复盘**：SEV 5 个工作日内、安全事件结案后 10 个工作日内无责复盘；改进项进积压并跟踪到关闭；必要时更新红队用例（07 §3.4）。

## 3. 遏制动作库

| 动作 | 命令 / 方法 | 影响 |
|---|---|---|
| 冻结单个 Request | 写 Request `status=halted`（`policy.halt` 审计）；Agent 写入自动 423 `GRAPH_HALTED` | 仅该问卷 |
| **全局冻结出网**（07 §9.4 P1） | Helm：`--set egressGateway.allowlist=null` 后 `helm upgrade`；compose：清空 `egress/allowlist.txt` 并 `docker compose exec egress-gateway squid -k reconfigure` | 所有 LLM 云端调用、外发、门户提交停止；确定性功能仍可用（降级） |
| 停止 worker | `kubectl -n belegt-trusted scale deploy/belegt-worker --replicas=0`；compose `docker compose stop belegt-worker` | 工作流暂停，Temporal 保留状态，恢复后继续（幂等键防重） |
| 吊销用户 / 令牌 | IdP 禁用账号；撤销刷新令牌；魔法链接一键吊销（`compliance_owner`，07 §4.6） | — |
| 吊销数据库凭据 | `ALTER ROLE <login> NOLOGIN; SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '<login>';` | 对应服务不可用 |
| 隔离工作负载 | 给 Pod 打 `quarantine=true` 标签并应用 deny-all NetworkPolicy；保留 Pod 做取证（不要先删） | — |
| 只读模式 | `REVOKE EXECUTE ON FUNCTION graph.commit_version(...) FROM belegt_writer;`（所有写入失败、读取正常） | 全平台写入停止 |

## 4. 通报义务与时限（07 §9.4）

| 对象 | 时限 | 负责人 | 内容 |
|---|---|---|---|
| 控制者（客户） | 知悉个人数据泄露后 **24 h** 内（DPA 承诺） | DPO + 客户成功 | Art. 33(3) 信息：性质、类别与大致数量、可能后果、已采取措施；可分批补充 |
| 监管机构 | 控制者在知悉后 72 h 内（Art. 33）；Belegt 作为控制者的事件由 Belegt 自行通报 | 控制者 / Belegt DPO | — |
| 数据主体 | 高风险时由控制者通知（Art. 34） | 控制者 | — |
| CRA（被积极利用的漏洞） | 24 h 预警 / 72 h 通报 / 14 天终报（09） | 安全负责人 | — |
| NIS2（若将来适用） | 24 h 预警 / 72 h 通报 / 1 个月终报（Art. 23） | 安全负责人 | — |
| 客户合同 | 汽配 OEM 合同常要求更短通知期 —— 按合同执行 | 客户成功 | — |
| 状态页 | SEV-1/2 每 30 min | 通讯员 | 不含任何客户名称与业务值 |

## 5. 场景手册

### 跨租户数据泄露（P1）

1. 立即：只读模式（§3）或冻结相关 API 路由；保全证据（不重启数据库）。
2. 确认范围：`audit.audit_event_public` 按 `object_ref` 与时间窗检索；PG 日志 + pgaudit；API 访问日志（`trace_id`）。
3. 复核 RLS：以 `belegt_reader_trusted` + 错误 `app.org_id` 执行 `tests/test_pg_commit.py::test_rls_cross_tenant_denied` 同款查询；
   检查是否有连接绕过 `rls_session`（直接读可移植表 `graph_object_version` 的路径须走 `authz.can_read` 过滤，见 belegt-core README）。
4. 修复 → 回归 RLS 负向套件（07 T-18）→ 通报（§4）。

### 审计链断裂（ALR-06）

1. **P1**。不要尝试"修复"审计行。
2. 定位断点：`SELECT set_config('app.org_id','<org>',false); SELECT * FROM audit.verify_chain();`（返回断链 seq）。
3. 与每日外部锚点（`global/audit-anchors/<org>/<date>.json`、RFC 3161 时间戳）比对，确定篡改发生在哪个锚定区间。
4. 检查 pgaudit 独立日志账户中该时间段的超级用户/owner 会话（07 §7.3）。
5. 从最近一个与锚点一致的备份恢复到隔离环境做差异比对（`restore.sh`）；结论写入事件报告，通知受影响租户。

### 出口阻断或注入突增（ALR-11）

1. 查看 DSH-05：`egress.block` 的 `EGRESS_DENIED_*` 错误码分布、GP-07 拒绝的 Agent 与 Request。
2. 若集中在某个 Request/供应商回复：冻结该 Request（`halted`），把样本加入红队集（07 §3.4）。
3. 若是白名单外域名的大量 CONNECT：确认是否有受控组件被利用（隔离区 worker 不应有到 egress-gateway 的连通性，检查 NetworkPolicy）。
4. 疑似注入导致外发/提交成功 → 升级 P1。

### LLM provider 全部不可用（SEV-2，降级）

确定性功能（审批、回填导出、审计包、证据查看）继续可用；状态页标"降级"。切换 `model-routing` 到备用 provider（04 §7.1），
或在私有化启用本地 vLLM/Ollama profile。同月累计 > 8 h 需书面说明（10 §7.2）。

### 数据恢复（逻辑误删 / 损坏）

1. 停写：只读模式（§3）+ worker 缩容到 0。
2. 选恢复点：托管 → PITR（WAL-G/pgBackRest，目标时间 = 误操作前 1 min）；compose → `deploy/ops/restore.sh <最近备份>`。
3. Temporal 与证据图**必须同一恢复点**（同实例不同 database，10 §6.6）。
4. 验收：`audit.verify_chain()` 全部租户为空；抽样 `belegt-calc verify-pack`；合成问卷端到端；`/readyz`。
5. 恢复写入；worker 扩容；在途工作流从最后持久化事件继续，幂等键防重复写，`egress-gateway` 以 `sreq_…#vN` 去重防重复发信。

## 6. 值班与交接（10 §8.7）

- MVP：工作时间值班 + 非工作时间尽力；V1 起企业版 SEV-1 7×24，主/副两级，每周交接。
- 遵守 ArbZG：夜间呼叫后次日补休。
- 冲刺期（2027-09）值班人数加倍，变更只发 patch。
