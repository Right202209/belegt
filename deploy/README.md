# Belegt 部署与运维

> 规格：10-非功能与运维（全文）、07 §2.2 / §4.5 / §5、03 §6（PostgreSQL 物理设计）、04 §1.3（部署形态）。
> 一套代码、同一镜像、同一 Helm chart，差异只在 `deploy-profile` 与模型路由（10 §1-4）。

| 目录 | 内容 |
|---|---|
| `docker/` | `core.Dockerfile`（target `api` / `worker` / `migrate`，同一 venv 层）、`web.Dockerfile`（node 构建 → nginx-unprivileged）、`entry/`（ASGI 工厂、worker 健康端点、迁移 Job）、`nginx/default.conf`；供应商端沿用 `packages/belegt-supplier/Dockerfile` |
| `compose/` | `docker-compose.yml`（SaaS 开发 / 私有化一键）、`.env.example`、egress-gateway（squid 白名单）、OTel Collector、Prometheus（SLO 记录规则 + ALR-01…12）、Grafana（DSH-01…06）、Temporal 动态配置 |
| `helm/belegt/` | Helm chart + 叠加文件 `values-eu-residency.yaml`、`values-external-postgres.yaml`、`values-private-airgap.yaml`、`values-dev.yaml` |
| `ops/` | `backup.sh` / `restore.sh`、密钥轮换、事件响应、告警 runbook、SLO 定义、成本仪表盘说明 |

## 0. 共同前提

- **镜像**（构建上下文 = 仓库根 `code/belegt`；多架构用 `docker buildx build --platform linux/amd64,linux/arm64`）：
  ```sh
  docker build -f deploy/docker/core.Dockerfile --target api     -t ghcr.io/belegt/core-api:0.1.0 .
  docker build -f deploy/docker/core.Dockerfile --target worker  -t ghcr.io/belegt/core-worker:0.1.0 .
  docker build -f deploy/docker/core.Dockerfile --target migrate -t ghcr.io/belegt/core-migrate:0.1.0 .
  docker build -f deploy/docker/web.Dockerfile                   -t ghcr.io/belegt/web:0.1.0 .
  docker build packages/belegt-supplier                          -t ghcr.io/belegt/supplier:0.1.0
  ```
  全部镜像以非 root 运行（core/supplier UID 10001，web UID 101），支持只读根文件系统（只写 `/tmp` tmpfs），带 HEALTHCHECK。
  生产镜像须 cosign 签名 + SBOM（10 §8.2），由 admission controller 只放行已验签镜像（07 §9.1）。
- **数据库迁移**由一次性 Job 完成（compose 的 `belegt-migrate`、Helm 的 Hook），执行
  `packages/belegt-core/migrations/postgres/0001…0006.sql`（schema、分区表、索引、RLS、`graph.commit_version()`、
  `audit.verify_chain()`、pgvector、所有权），建 GraphStore 可移植表，并按信任区建登录角色：

  | 登录角色 | 组角色 | 使用者 |
  |---|---|---|
  | `BELEGT_DB_APP_USER` | `belegt_writer` + `belegt_reader_trusted` + `belegt_app_portable` | belegt-api、可信区 worker |
  | `BELEGT_DB_QUARANTINE_USER` | `belegt_reader_quarantine` | 隔离区 worker（07 §2.2） |
  | `BELEGT_DB_AUDITOR_USER` | `belegt_auditor` | 审计只读 |

  迁移幂等、可重复执行；文件未变化时跳过（`public.belegt_schema_migrations` 记 sha256）。`BELEGT_PG_HASH_MODULUS`
  只在首次建表时决定分区数（私有化 1，多租户 32，03 §6.4）。
- **写入路径**：`BELEGT_PG_COMMIT=on`（默认）时 belegt-api 使用 `belegt_core.store.pg_commit.PgCommitGraphStore`：
  Python 侧完成全部 03 写入规则后，在**同一事务**内以 `belegt_writer` 调用 `graph.commit_version()`，
  数据库层再做租户/执行者/信任区/乐观锁/CHECK/R-ONE/content_hash 复核，并写分区表 + RLS + 审计哈希链 + outbox。
  worker 入口（`entry/belegt_worker.py`）在 PostgreSQL 上给服务工厂构造的每个 `GraphStore` 实例挂同一适配器，保证 API 与 worker 的写入都经数据库函数（否则物理表 head 会分叉）。
- **出网**：只有 `egress-gateway`（FQDN 白名单 CONNECT 代理）能访问公网；worker 等通过 `HTTPS_PROXY` 使用它（10 §6.2）。
  白名单默认只有欧盟区 LLM 端点。

---

## 1. SaaS（`saas-multi`，欧盟驻留，Helm）

适用：Belegt 托管多租户（10 §6.1、§6.2；主区 Hetzner FSN/NBG 或 AWS eu-central-1，灾备另一欧盟国家）。

**集群前置**（chart 之外）：
1. K8s ≥ 1.28，CNI 支持 NetworkPolicy（Cilium/Calico）；节点打区域标签 `topology.kubernetes.io/region`，
   隔离区与 guard 独立节点池：`belegt.io/pool=quarantine|guard` + 同名 taint（07 §2.2）；可选 gVisor `RuntimeClass`。
2. PostgreSQL 16 主 + 同步备（托管或 Patroni），已装 pgvector；连续 WAL 归档（WAL-G/pgBackRest）到异地对象存储，PITR 35 天（10 §6.6）。
3. Temporal（官方 Helm chart，frontend/history/matching/worker 分离，`numHistoryShards=512`，持久化到 PG 的独立 database）。
4. S3 兼容对象存储：主桶开启 **Object Lock COMPLIANCE**（`blobs/`、`audit-packs/` 10 年），版本化 + 跨区复制；备份桶 Object Lock 35 天。
   *Hetzner 对象存储是否支持 compliance 模式须采购前核实（10 待决 1）。*
5. Vault（或外部欧盟 KMS）+ External Secrets Operator：同步 `belegt-secrets`（键见 `templates/secrets.yaml` 顶部注释）。
6. kube-prometheus-stack（ServiceMonitor/PrometheusRule CRD、Grafana sidecar）、OTel Collector（`belegt-obs` 命名空间）、cert-manager、ingress-nginx（启用 ModSecurity WAF）。

**安装**：
```sh
helm upgrade --install belegt deploy/helm/belegt -n belegt-app --create-namespace \
  -f deploy/helm/belegt/values-eu-residency.yaml \
  -f deploy/helm/belegt/values-external-postgres.yaml \
  --set postgresql.host=<pg-primary> --set image.tag=0.1.0
kubectl -n belegt-app logs job/belegt-migrate-1          # 迁移 Job（pre-install Hook）
kubectl -n belegt-app port-forward svc/belegt-api 8000 && curl -fsS localhost:8000/readyz
```
`values-eu-residency.yaml` 做了：五个分区命名空间（Pod Security `restricted`）、节点区域强制约束、隔离区/guard 节点池、
`dataResidency=eu`（渲染期校验：`llm.baseUrl` 与出网白名单必须是欧盟端点或显式列入 `extraAllowedEgressDomains`，否则 `helm` 直接失败）、
NetworkPolicy 默认拒绝 + 按信任分区放行、HPA/PDB、ServiceMonitor、PrometheusRule（ALR-01…12）、Grafana 仪表盘 ConfigMap、Ingress + WAF。

**升级**（10 §6.5）：`helm upgrade …` 会先跑 pre-upgrade 迁移 Hook（expand 阶段），再滚动发布；回滚只在 expand 阶段允许，
contract 之后一律 PITR。私有化客户只能 N → N+1 minor 依次升级。

**专属 SaaS（`saas-dedicated`）**：同上，`global.deployProfile=saas-dedicated`、独立命名空间/集群 + 独立数据库，`postgresql.hashModulus=1`。

---

## 2. 私有化

### 2.1 单机 docker compose（`private-compose`，试点/小厂，目标 ≤ 30 min）

最低 8 vCPU / 32 GB / 500 GB NVMe；推荐 16 vCPU / 64 GB（10 §6.3）。需要 Docker Engine 24+ 与 Compose v2。

```sh
cd deploy/compose
cp .env.example .env
# 把所有标【必改】的值替换为随机值（URL 安全字符，会拼进连接串）：
sed -i "s/change-me-[a-z0-9-]*/$(openssl rand -hex 16)/" .env   # 示例：逐个替换更稳妥
docker compose config -q                     # 校验配置与必填变量
docker compose up -d --build                 # 构建并启动核心栈
docker compose ps                            # 等待 healthy；belegt-migrate 应为 exited (0)
```
| 入口（默认只绑 127.0.0.1，由主机 TLS 反向代理对外） | 地址 |
|---|---|
| 审批台（web，`/api/` 反代 graph-api） | http://127.0.0.1:8080 |
| 供应商端门户 | http://127.0.0.1:8090 |
| Grafana（DSH-01…06） | http://127.0.0.1:3000 |
| Temporal UI | http://127.0.0.1:8233 |

开发令牌在 `config/dev-tokens.example.json`（例：`Authorization: Bearer dev-compliance-owner`）。**生产必须换 OIDC**（07 §4.6），
并把 `BELEGT_DEV_TOKENS_FILE` 指向一个空 JSON 对象。

网络分区：`app`/`data`/`llm`/`quarantine`/`obs`/`egress-client` 都是 `internal: true` 网络，只有 `egress-gateway`
挂在可出网的 `egress` 网络。限制：Docker 要求发布端口的容器至少挂一个非 internal 网络，所以 `edge` 上的 web、supplier、grafana、
temporal-ui 技术上可以出网——它们不持有 LLM 密钥；生产请在主机防火墙对 compose 的 edge 网段做出站拒绝
（`iptables -I DOCKER-USER -s <edge 子网> ! -d <edge 子网> -m conntrack --ctstate NEW -j DROP`）。

备份（10 §6.6，RPO ≤ 24 h）：`docker compose --profile backup up -d`（每日 `pg_dump` 三个库：belegt、temporal、temporal_visibility，
镜像到 MinIO 锁定桶）。生产应把 `ops/backup.sh` 的上传目标设为异地对象存储（`BACKUP_UPLOAD=s3`），并开启 WAL 归档把 RPO 降到 15 min。
恢复：`ops/restore.sh <备份目录>`（校验 sha256 → 恢复角色 → 逐库恢复 → schema 自检 → `audit.verify_chain()`）。

### 2.2 客户 Kubernetes（`private-k8s`，目标 ≤ 2 h）

```sh
# 外部 PG（推荐，客户托管 PG 16 + pgvector）
helm upgrade --install belegt deploy/helm/belegt -n belegt --create-namespace \
  -f deploy/helm/belegt/values-external-postgres.yaml \
  --set postgresql.host=<pg> --set postgresql.hashModulus=1 \
  --set temporal.address=<temporal-frontend>:7233 \
  --set objectStorage.endpoint=https://<minio-or-ceph> --set objectStorage.bucket=belegt
# 开发/演示（内置 PG、chart 自建 Secret）：-f deploy/helm/belegt/values-dev.yaml
```
NetworkPolicy 的外部对端（PG 网段、Temporal/OTel/ingress/Prometheus 命名空间）在 `networkPolicy.peers` 中按实际环境填写。

---

## 3. 本地模型（FR-704、10 §6.4）

| 场景 | 方式 | 说明 |
|---|---|---|
| 笔记本演示 / 开发 | compose `--profile ollama` | 不进生产 SLA |
| 私有化生产（联网） | compose `--profile vllm` 或 Helm `llm.vllm.enabled=true` | 推荐 `gpu-l`（1×96 GB）：Qwen3-32B FP8 作 M/L 档 + 8B + 嵌入 |
| 离线（`private-airgap`） | Helm `values-private-airgap.yaml` | 出网白名单为空，`dataResidency=private`，`llm.baseUrl` 必须是集群内 `*.svc`（渲染期校验） |

**compose + Ollama**：
```sh
# .env：BELEGT_LLM_BASE_URL=http://ollama:11434/v1  BELEGT_LLM_MODEL=qwen3:8b
# 拉模型需临时在 egress/allowlist.txt 放行 registry.ollama.ai 并 reconfigure：
docker compose exec egress-gateway squid -k reconfigure
docker compose --profile ollama up -d ollama
docker compose exec ollama ollama pull qwen3:8b
docker compose up -d belegt-worker            # 重新读取 LLM 配置
```

**compose + vLLM**（需 NVIDIA 驱动 + nvidia-container-toolkit）：权重预先放入 `vllm-models` 卷（离线包，`VLLM_HF_OFFLINE=1`），
`.env` 设 `BELEGT_LLM_BASE_URL=http://vllm:8000/v1`、`BELEGT_LLM_MODEL=local-vllm`、`VLLM_MODEL=<本地路径或 HF id>`，然后
`docker compose --profile vllm up -d`。

**离线 Kubernetes**：
1. 用离线包（签名 OCI artifact，cosign 验签，10 §6.4）把全部镜像导入内部仓库（`global.imageRegistry`、`image.squid/postgres/vllm`）。
2. 模型权重放入 PVC（`llm.vllm.modelsPvc`），`HF_HUB_OFFLINE=1`。
3. `helm upgrade --install belegt deploy/helm/belegt -n belegt -f values-private-airgap.yaml -f values-external-postgres.yaml --set postgresql.host=<pg>`。
4. 验收（NFR-16）：抓包确认外联 0；本地 `model-routing` 单独过 11 黄金集（低于 90% 时 `PROMO-MAP-0.85` 自动收紧为 0.90）。
   注意 vLLM 官方镜像以 root 运行，不满足 Pod Security `restricted`；把 vLLM 放在单独的、`baseline` 级别的命名空间或节点池。

---

## 4. 验证记录（本次交付在沙箱中实际执行）

| 项 | 结果 |
|---|---|
| 迁移 SQL 真实执行 | PostgreSQL 16.15 + pgvector 0.8：`0001…0006` 全部执行成功，**重复执行两遍幂等**；修复了实跑发现的 5 个问题（见下） |
| 03 §5.6 串联实例 | `PgCommitGraphStore` 经 `graph.commit_version()` 写入 9 个对象及前序版本；物理 head = 可移植 head；content_hash 由数据库 JCS 复核一致；R-ONE、派生边、outbox、审计哈希链（`audit.verify_chain()`）通过 |
| RLS 负向 | 跨租户（`app.org_id` = 另一租户 / 未设置）不可见；隔离区角色只见 quarantine 版本、无权读 `answer_v`；`p_customer_scope`、`p_approved_only` 过滤；`belegt_writer` 无直接 DML |
| 现有 belegt-core 测试在 PG 上 | 用 `PgCommitGraphStore` 替换 conftest 的 store，246 个测试（store/doc_chain/audit/auditpack/rules/api/approval/review/consistency/legal）在真实 PG 上全部通过 |
| 迁移 Job + API 端到端 | `belegt_migrate.py`（MODULUS 1、建三个登录角色、自检）→ `belegt_asgi.create()` 以最小权限 `belegt_api` 账号写 Organization/Counterparty → 物理表与审计链落库；`/healthz`、`/readyz`、`/metrics` 正常 |
| 备份/恢复 | `backup.sh` → 删除数据库 → `restore.sh`（sha256、schema 自检、审计链校验）恢复成功；shellcheck 无告警 |
| compose | `docker compose config -q`（含全部 profile）通过；必填密钥缺失时报错 |
| Prometheus | `promtool check rules`（30 条）与 `check config` 通过；仪表盘 43 条 PromQL 全部可解析 |
| Helm | `helm lint --strict` 三种叠加通过；`kubeconform -strict`（K8s 1.30 + CRD 目录）53/45/29/25 个资源全部有效；渲染期驻留/离线校验的负向用例按预期失败 |
| web 构建 | `npm ci && VITE_API_BASE=/api npm run build` 成功（web.Dockerfile 构建阶段的同一命令） |
| 依赖解析 | core.Dockerfile 的 pip 安装集合 `pip install --dry-run` 解析成功（97 个包） |

**实跑修复的迁移问题**（`packages/belegt-core/migrations/postgres/`）：
1. `0002`：`ADD CONSTRAINT question_ordinal_uq` 不幂等 → 改为存在性检查。
2. `0005`：`audit.append_event` 的 `SET search_path = audit, graph` 找不到 public 中的 `gen_random_bytes` → 改用核心函数 `pg_catalog.gen_random_uuid()`。
3. `0006`（新增收尾段）：表由迁移账号（postgres）拥有，而 `SECURITY DEFINER` 函数属 `belegt_owner` → 函数内 INSERT 报权限错误；现把 7 个 schema 及其全部表/视图/序列转给 `belegt_owner`（FORCE RLS 对 owner 同样生效）。
4. `0004`：`p_customer_scope` / `p_approved_only` 写成 FOR ALL 时也充当 INSERT 的 WITH CHECK，写入会话未设 `app.cp_scope` 时无法插入客户限定的 Answer → 改为 `FOR SELECT`。
5. `0005`：新增 `audit.verify_chain()`（SECURITY DEFINER），审计员无权读含 IP 的原表也能做 SLO-07 链校验。
另：`0001` 的 `graph.create_hash_partitions()` 支持会话设置 `belegt.hash_modulus`（私有化 MODULUS 1），已有分区时不再追加。

## 5. 未验证 / 已知缺口

- **未实际 `docker build` / `docker compose up`**（沙箱无 Docker 守护进程）：镜像构建只验证了依赖解析与 web 构建命令；
  第三方镜像标签（`temporalio/auto-setup:1.26.2`、`temporalio/ui:2.34.0`、`ubuntu/squid:latest`、`minio/*:latest` 等）未拉取核实，
  生产请固定 digest。squid/temporal/minio 的健康检查命令未在容器内跑过。
- **Helm 未装到真实集群**：NetworkPolicy 的实际连通性、Hook 顺序、Pod Security `restricted` 下各镜像能否启动未验证。
- **隔离区 worker**：belegt-core 工作流目前只用一个 task queue（`belegt-requests`），`belegt-worker-quarantine`（compose profile `zones`、
  Helm `workerQuarantine.enabled`）是预留，代码拆分 `tq-quarantine` 后才有意义；当前所有 Agent 在可信区 worker 中运行。
- **llm-proxy / mail-fetcher / mcp-broker** 不是独立服务（代码中不存在）；LLM 调用由 worker 直接经 egress-gateway 出网。
- **业务指标未埋点**：`belegt_llm_*`、`belegt_request_*`、`belegt_guard_*`、`belegt_promotion_*`、`belegt_egress_*`、备份/审计链指标尚未由代码导出，
  对应面板为空、告警不会触发（见 `ops/cost-dashboard.md` §5）。API 只导出 `belegt_http_request_duration_seconds`、
  `belegt_graph_write_duration_seconds`、`belegt_graph_query_duration_seconds`。Temporal schedule-to-start 指标名需按部署版本核对。
- **生产认证**：API 仍用开发静态令牌表；OIDC `TokenResolver` 未实现（07 §4.6）。
- **pg_commit 为镜像模式**：Python 读路径仍读可移植表（`graph_object_version` 等，在 public schema，无 RLS），因此应用账号对可移植表有 DML 权限
  （`belegt_app_portable`），这与 03 §6.1"belegt_writer 对表无直接 DML"不完全一致；物理分区表 + RLS 是数据库层的权威副本与最后防线。
  `put_factor_rows` / `put_blob` / `record_trace` 未镜像到 `ref.factor` / `doc.blob` / `obs.trace_span`；拒绝写入的审计只写可移植审计表。
  物理表的 `created_at` 由数据库 `clock_timestamp()` 赋值，与可移植表（Python 时钟）不同。
- **alembic 的 PostgreSQL 路径**：`migrations/alembic/versions/0002_postgres_physical.py` 用 `exec_driver_sql` 执行含 `%I` 的 SQL，
  psycopg 3 把 `%` 当占位符报错（实测），不在本次可改范围；修法：`bind.connection.driver_connection.execute(sql)` 或改用迁移 Job。
  部署请使用迁移 Job（`belegt_migrate.py`）。
- `belegtctl`（doctor/upgrade/离线包导入）不存在，文中相关步骤需手工执行。
- 对象存储 Object Lock 桶由 compose 的 `minio-init` 创建；Helm 形态的桶需在 chart 外预置。应用代码目前未使用 `BELEGT_S3_*`（blob 仍存数据库/本地），
  变量为后续对象存储接入预留。
