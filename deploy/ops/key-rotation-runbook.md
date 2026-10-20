# 密钥轮换 Runbook

> Spec：10 §8.5（密钥轮换表、每季度"无预告轮换"演练）、07 §4.5（密钥管理、BYOK/HYOK）、§4.4（SPIFFE）、§4.6（会话与魔法链接）、
> 03 §6.7（crypto-erase）、§7（审计包 Ed25519 签名）。告警：ALR-12（14 天内到期）。
> 原则：**全部脚本化、新旧并行、先验证后撤旧、每次轮换写 `key.rotate` AuditEvent**。不在聊天/工单里传递任何密钥值。

## 0. 总表（10 §8.5）

| 密钥 | 存放 | 周期 | 重叠期 | 验证 | 章节 |
|---|---|---|---|---|---|
| 数据库应用凭据（`belegt_app` 等） | Vault database secrets engine 动态凭据；compose 为 `.env` | TTL 24 h（托管）/ 90 天（compose） | 连接池热切换 | `/readyz` 200、写入冒烟 | §1 |
| 迁移管理员 / 超级用户 | Vault KV（双人保管） | 180 天 | — | 迁移 Job `--check` | §1.3 |
| 工作负载身份（SPIFFE SVID） | SPIRE 自动签发 | TTL 1 h | 自动 | 过期即拒绝（GP-01） | §2 |
| LLM provider API 密钥 | Vault，仅 `llm-proxy`/worker 可读 | 90 天 | 7 天双密钥 | 金丝雀调用 | §3 |
| 租户 KEK / 审计包签名 / 锚定签名 | KMS（非导出） | 每年 | 旧公钥永久保留用于验证 | `key.rotate` + `verify-pack` 回归 | §4 |
| Webhook HMAC、魔法链接签名、清单 JWS | Vault | 180 天 | 新旧并行校验 | 供应商端双密钥验签 | §5 |
| 对象存储访问密钥 | Vault / 云 IAM | 90 天 | 双密钥 | 上传/下载冒烟 | §6 |
| TLS 证书 | cert-manager（ACME） | 到期前 30 天自动续签 | 自动 | ALR-12 | §7 |
| Grafana / MinIO 管理员 | Vault KV | 180 天 | — | 登录 | §6 |

## 1. 数据库凭据

### 1.1 托管 / Helm（Vault database engine）

1. Vault 角色 `belegt-app`：`creation_statements` = `CREATE ROLE "{{name}}" LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}' IN ROLE belegt_writer, belegt_reader_trusted, belegt_app_portable NOBYPASSRLS;`（组角色由迁移 Job 创建，03 §6.1）。
2. External Secrets Operator 每 12 h 刷新 `belegt-secrets/app-db-password`，Deployment 带 `reloader.stakater.com/auto: "true"`
   或滚动重启：`kubectl -n belegt-app rollout restart deploy/belegt-api && kubectl -n belegt-trusted rollout restart deploy/belegt-worker`。
3. 验证：`kubectl -n belegt-app rollout status deploy/belegt-api`；`curl /readyz` = 200；`/metrics` 中 SLO-06 无 5xx 抬升。
4. 旧凭据由 Vault 在 TTL 到期时 `DROP ROLE`；**不要**手工删除仍有会话的角色（先 `pg_terminate_backend`）。

### 1.2 compose（静态密码）

```sh
NEW=$(openssl rand -hex 32)
# 1) 改密码（迁移 Job 的 _ensure_login 也会按 .env 重设密码，等价）
docker compose exec postgres psql -U postgres -c "ALTER ROLE belegt_app PASSWORD '$NEW'"
# 2) 更新 .env 的 BELEGT_DB_APP_PASSWORD=$NEW，然后滚动重建依赖它的服务
docker compose up -d --no-deps belegt-api belegt-worker
docker compose ps belegt-api belegt-worker     # healthy
```
隔离区（`BELEGT_DB_QUARANTINE_PASSWORD`）与审计只读账号同法。

### 1.3 迁移管理员 / 超级用户

仅迁移流水线使用（10 §8.4）。轮换后执行 `belegt-migrate --check` 验证；超级用户会话由 pgaudit 记录到独立日志账户（07 §7.3）。

## 2. 工作负载身份（SPIFFE）

SPIRE 自动轮换（TTL 1 h）。故障处置：SVID 过期 → GP-01 拒绝 → `halted`。检查 `spire-agent` DaemonSet 与节点时钟（`belegtctl doctor`）。

## 3. LLM provider API 密钥（90 天，双密钥重叠 7 天）

1. 在 provider 控制台创建新密钥（只限欧盟区端点、ZDR 合同项目，07 §5.3）。
2. 写入 Vault：`vault kv put secret/belegt/llm api_key_next=<新>`（保留 `api_key` 旧值）。
3. 金丝雀：把 1 个 worker 副本切到新密钥（`llm-api-key` 指向 `api_key_next`），观察 30 min：
   `belegt:slo04_success_ratio:rate1h ≥ 0.995`、无 401/403。
4. 全量切换：`api_key = api_key_next`，滚动重启 worker；7 天后在 provider 控制台吊销旧密钥。
5. 写审计：`store.record_audit(Actor service:ops, "key.rotate", details={"key": "llm.<provider>", "fingerprint": "<后 4 位>"})`。

## 4. KMS 密钥（每年）

| 密钥 | 步骤 |
|---|---|
| 租户 KEK | KMS 新建版本（自动轮换）；DEK 按对象生成，旧 DEK 由新 KEK 重新包裹（后台作业按租户分批，SLO 不受影响）；写 `key.rotate` |
| 审计包签名（Ed25519，03 §7） | KMS 新建签名密钥 → 发布新公钥到 `global/audit-anchors/keys/` 与租户文档 → 切换签名 → 旧公钥**永久保留**用于 `verify-pack` 验证历史审计包 → 回归：对最近 10 个审计包跑 `belegt-calc verify-pack` |
| 审计锚定签名 | 独立 KMS 项目（与业务密钥分离，07 §4.5）；同上，切换当天的 Merkle 根用新旧两把各签一次 |
| BYOK 租户 | 租户在自有 KMS 轮换；Belegt 只验证 wrap/unwrap 授权仍有效。租户撤销授权 = 事实上的加密擦除（`crypto.erase`） |

## 5. 供应商端签名密钥（180 天，新旧并行）

`BELEGT_SUPPLIER_SECRET`（魔法链接派生/签名、CSRF）、`BELEGT_SUPPLIER_MANIFEST_SECRET`（清单 JWS）、`BELEGT_SUPPLIER_WEBHOOK_SECRET`（HMAC）。

1. 清单/Webhook 密钥：先让**接收方**同时接受新旧两把（双密钥校验窗口 ≥ 最长在途请求 14 天），再切换**发送方**，窗口结束后移除旧密钥。
   自托管供应商实例（08 §6.2）需提前 30 天书面通知对方运维。
2. 实例密钥 `BELEGT_SUPPLIER_SECRET`：改动会让**所有未完成的魔法链接失效**。只在泄露时紧急轮换；常规轮换安排在
   没有 `sent/reminded` 状态 SupplierRequest 的窗口，或轮换后由 upstream-chaser 重新发放链接（走 egress 审批）。

## 6. 对象存储与管理员口令

- S3/MinIO 访问密钥：新建第二把 → 更新 Secret → 滚动重启 api/worker → 冒烟（`POST /blobs` 上传 + 读取）→ 删除旧密钥。
- MinIO root / Grafana admin：`.env` / Vault 更新后重建容器；root 只用于初始化，日常用最小权限的服务账号。

## 7. TLS 证书

cert-manager 自动续签（到期前 30 天）。ALR-12 触发说明续签失败：检查 `kubectl describe certificate`、ACME 挑战与 DNS。
compose 形态 TLS 在主机反向代理（Caddy 自动 / certbot）。

## 8. 泄露时的紧急轮换

1. 按 `incident-response-runbook.md` 定级（凭据泄露至少 P2；涉及跨租户数据 P1）。
2. **先吊销后补发**：数据库 `ALTER ROLE … NOLOGIN` / provider 控制台吊销 / Vault `lease revoke -prefix`。
3. 必要时全局冻结外发：对相关 Request 写 `halted`（`policy.halt`），或把 egress allowlist 清空并 `squid -k reconfigure`。
4. 补发新密钥（上面各节），确认服务恢复；审计中检索泄露窗口内该凭据的全部访问（`audit.audit_event_public`、pgaudit）。

## 9. 季度"无预告轮换"演练（10 §8.5）

每季度随机抽取 2 类密钥在工作时间无预告轮换，记录：开始/结束时间、是否触发告警、SLO 影响、手册偏差。结果写入 ISMS 记录，手册偏差当周修正。
