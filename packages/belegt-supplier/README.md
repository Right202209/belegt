# belegt-supplier — Belegt 供应商端（可自托管、多语言）

> 许可：Apache-2.0 · 规格：`docs/08-上游追数与供应商端.md`（另见 03 §3.5/§3.10、04 §2.9–§2.10/§2.15、07）

`belegt-supplier` 是 Belegt 上游追数的**供应商侧**应用：供应商点击邮件中的魔法链接即可（免注册）查看请求、按数据点填写、上传证据、提交；
同时它也提供追数侧可复用的确定性组件（消息模板库、催办策略、合理性校验、出口网关）。
本包**不依赖** `belegt_core`（AGPL），与客户侧只通过 08 定义的 HTTP 契约（请求清单 JWS、提交包、Webhook）交互，供应商自托管时不接触 AGPL 代码。

## 功能一览

| 模块 | 内容 | 规格 |
|---|---|---|
| `messages` | 消息模板库 `stpl:<family>.<locale>@<semver>`，YAML + Jinja2（StrictUndefined），变量校验、模板 lint、审校状态（`draft/reviewed/deprecated`，只有 `reviewed` 能外发；en MAJOR 升级时其他语言自动降为 draft）| 08 §3 |
| `chase` / `workcal` | 催办策略 `chase-policy@1.0.0`：供应商时区工作日 T+5/T+10/T+14、长窗口 T+7/T+21/due−7、分级 A/B/C/响应良好/曾投诉、静默期与节假日顺延、due 计算与顺延、上限与升级、到期 —— 纯函数 | 08 §4 |
| `plausibility` | 合理性校验 PLAUS-001…027（数据）+ PLAUS-030…035（注入/异常内容），每条有 ID、严重度、正反例测试；结果映射到 `Evidence.plausibility.{flag,basis}` | 08 §5 |
| `statemachine` | SupplierRequest 本地状态机（含 `reminded→expired`、`escalated→responded`），首次发送校验已批准 Approval 引用 | 03 §3.10、08 §2.3 |
| `egress` | 出口网关：模板已注册+已审校、收件人白名单、UPC-LIM-01…07、静默期、审批、DLP；错误码统一 `EGRESS_DENIED_*`，限频/静默期返回 `retry_after`；SMTP 适配 + 开发用 `.eml` outbox | 04 §2.15、08 §4.3 |
| `web` / `service` | 魔法链接门户（服务端渲染 Jinja2 + 少量原生 JS，零构建）、`supplier-api` v1、Webhook（HMAC）、Admin API | 08 §6–§7 |
| `core_client` | `CoreClient` 协议（`HttpCoreClient` / `InMemoryCoreClient`） | 08 §6.5 |

## 快速开始（开发）

```bash
pip install -e packages/belegt-supplier[test]
cd packages/belegt-supplier && pytest -q          # 全部离线，无需 LLM key

export BELEGT_SUPPLIER_DATA_DIR=./data BELEGT_SUPPLIER_SECRET=dev-secret \
       BELEGT_SUPPLIER_PUBLIC_URL=http://localhost:8080
belegt-supplier demo --locale de      # 创建一个演示请求，打印门户链接
belegt-supplier serve --port 8080     # 打开上面的链接
belegt-supplier templates list        # 模板与审校状态
belegt-supplier templates lint
belegt-supplier templates render stpl:cbam_see_request.tr@1.0.0
belegt-supplier templates review stpl:cbam_see_reminder.de@1.0.0 --by user:native-de
belegt-supplier chase-tick            # 执行一次催办调度（由 cron/调度器定时调用）
```

开发模式下外发邮件写入 `$BELEGT_SUPPLIER_DATA_DIR/outbox/*.eml`（或 `BELEGT_SUPPLIER_OUTBOX_DIR`）。

## 自托管

单容器即可运行（08 §6.2、§6.5）：

```bash
docker build -t belegt-supplier packages/belegt-supplier
docker run -d --name belegt-supplier -p 8080:8080 -v belegt-supplier-data:/data \
  -e BELEGT_SUPPLIER_PUBLIC_URL=https://supplier.example-steel.com \
  -e BELEGT_SUPPLIER_SECRET="$(openssl rand -hex 32)" \
  -e BELEGT_SUPPLIER_MANIFEST_SECRET=<与客户侧约定的清单签名密钥> \
  -e BELEGT_SUPPLIER_WEBHOOK_SECRET=<与客户侧约定的 Webhook 密钥> \
  -e BELEGT_SUPPLIER_CORE_URL=https://kunde.belegt.app \
  -e BELEGT_SUPPLIER_SMTP_HOST=smtp.example-steel.com -e BELEGT_SUPPLIER_MAIL_FROM=cbam@example-steel.com \
  belegt-supplier
```

- 默认 SQLite（`/data/supplier.db`）；PostgreSQL：`BELEGT_SUPPLIER_DB_URL=postgresql+psycopg://…`（需自行安装驱动）。
- 放在 TLS 反向代理之后；如需信任 `X-Forwarded-For`，设 `BELEGT_SUPPLIER_TRUST_PROXY=true`。
- 访问日志默认关闭（URL 含魔法链接 token，08 §7.1 要求 token 不进日志）；反向代理也应关闭或脱敏 `/r/*`、`/pause/*`、`/supplier-api/v1/requests/*` 的路径日志。
- **必须**显式配置 `BELEGT_SUPPLIER_SECRET`：未配置时进程会生成随机密钥，重启后所有链接失效。
- 数据流：客户侧把**签名请求清单**（JWS HS256）推送到 `POST /supplier-api/v1/manifests`，响应里拿到门户链接放进邮件；
  供应商提交后，本实例**主动推送**签名提交包到客户侧 `POST /supplier-inbound/v1/submissions`（客户侧从不拉取供应商实例）。
  推送失败进入 Webhook 外发队列，`POST /supplier-api/v1/admin/webhooks/deliver` 或 `chase-tick` 重试。

### 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `BELEGT_SUPPLIER_PUBLIC_URL` | 门户对外地址（链接域名，也进入 DLP 链接白名单） | `http://localhost:8080` |
| `BELEGT_SUPPLIER_DATA_DIR` | 数据目录（SQLite、上传文件 `blobs/`、outbox） | `./data` |
| `BELEGT_SUPPLIER_DB_URL` | SQLAlchemy URL | `sqlite:///<data>/supplier.db` |
| `BELEGT_SUPPLIER_SECRET` | 实例密钥：魔法链接派生与签名、CSRF、提交包 JWS | 随机（仅开发） |
| `BELEGT_SUPPLIER_MANIFEST_SECRET` | 校验客户侧请求清单 JWS | = Webhook 密钥 |
| `BELEGT_SUPPLIER_WEBHOOK_SECRET` | 与客户侧互发 Webhook 的 HMAC 密钥 | 随机（仅开发） |
| `BELEGT_SUPPLIER_CORE_URL` | 客户侧根地址（`/supplier-inbound/v1/*`） | 无（Webhook 留在队列） |
| `BELEGT_SUPPLIER_ADMIN_KEY` | 启用 `/supplier-api/v1/admin/*`（Bearer） | 无（禁用） |
| `BELEGT_SUPPLIER_SMTP_HOST/PORT/USER/PASSWORD/STARTTLS`、`BELEGT_SUPPLIER_MAIL_FROM` | SMTP 外发 | 无 → outbox |
| `BELEGT_SUPPLIER_OUTBOX_DIR` | 开发：邮件写文件 | `<data>/outbox` |
| `BELEGT_SUPPLIER_INTERNAL_DOMAINS` | 内部通知（升级）允许的收件域名，逗号分隔 | 空 |
| `BELEGT_SUPPLIER_LINK_DOMAINS` | 邮件正文额外允许的链接域名（如租户隐私页） | 空 |
| `BELEGT_SUPPLIER_DEVICE_BINDING` | 新设备打开需邮箱验证码 | `true` |

## API（`supplier-api` v1）

OpenAPI：`GET /supplier-api/v1/openapi.json`（Swagger UI：`/supplier-api/v1/docs`）。

| 方法与路径 | 用途 | 鉴权 |
|---|---|---|
| `GET /supplier-api/v1/requests/{token}` | 请求清单 + 草稿 + 已上传文件 | 魔法链接 token（+ 设备 cookie） |
| `PUT …/{token}/draft` | 保存草稿（乐观锁 `draft_version`，返回即时提示） | token |
| `POST …/{token}/uploads` | 上传（multipart `file`），返回 `sha256` 与扫描结果 | token |
| `POST …/{token}/parse/cbam-communication` | 解析已上传 CBAM Excel（调用 `belegt_io`，未安装返回 501） | token |
| `POST …/{token}/questions` | 提问 | token |
| `POST …/{token}/decline` | 无法提供（`not_available|not_responsible|confidential|no_longer_supplier|other` + 文本） | token |
| `POST …/{token}/submit` | 提交（`attestation=true`，可带 `Idempotency-Key`），返回提交包哈希 | token |
| `POST …/{token}/otp`、`…/otp/verify` | 新设备验证码（本包扩展） | token |
| `POST …/{token}/pause` | 暂停提醒（UPC-LIM-07，本包扩展；网页版为 `/pause/{token}`） | token |
| `POST /supplier-api/v1/auth/magic-link` | `{email, token}` 过期后重新签发（邮箱须为原收件人，3 次/小时） | — |
| `GET /supplier-api/v1/me/submissions`、`/me/shares` | 可选账户 / 共享（FR-306/307） | 本版返回 501 |
| `POST /supplier-api/v1/manifests` | `{manifest_jws, status, approval_ref, reissue}` 接收客户侧签名清单 | 清单 JWS |
| `POST /supplier-api/v1/webhooks/core` | 客户侧事件（`request.status`、`link.revoke`） | `Belegt-Signature` HMAC |
| `/supplier-api/v1/admin/*` | 模板列表/审校/预览、建草稿、首次发送、链接、吊销、暂停催办、催办 tick、Webhook 重投 | `Authorization: Bearer <ADMIN_KEY>` |

错误体统一为 `{"error": {"code", "message", "details?", "retry_after_s?"}}`；429 带 `Retry-After`。

### Webhook 签名

`Belegt-Signature: t=<unix>,v1=<hex(HMAC-SHA256(secret, "<t>." + body))>`，容差 300 s，可同时携带多个 `v1`（密钥轮换）。
校验：`belegt_supplier.verify_webhook(secret, body, header)`。供应商端 → 客户侧：`/supplier-inbound/v1/submissions`（提交包）、
`/supplier-inbound/v1/questions`、`/supplier-inbound/v1/events`（`decline.created`、`link.opened`、`reminders.paused`、`request.escalated`… 本包扩展）。

### 提交包 `belegt-supplier-submission/1`

```json
{"format":"belegt-supplier-submission/1","request_manifest_hash":"sha256:…","sreq_ref":"sreq_…#v1",
 "submitted_at":"2026-11-03T08:00:00Z","locale":"tr",
 "values":[{"datapoint_id":"dp:prod.cbam.see_direct","value":"1.85","unit":"tCO2e/t",
            "period":{"start":"2026-01-01","end":"2026-12-31"},"boundary":"product:prod_…",
            "source":{"kind":"form"},"note":null}],
 "blobs":[{"sha256":"sha256:…","mime":"application/pdf","filename":"pruefbericht.pdf"}],
 "attestation":{"by":"ayse@…","statement_version":"belegt-supplier-attestation@1.0.0"},
 "signature":"<分离 JWS HS256>"}
```

数字一律 DEC-STR；提交包哈希 = 规范化 JSON（排序键、无空白）的 sha256。

## Python 公开 API

```python
from belegt_supplier import (
    TemplateRegistry,                     # .load_builtin() .render(id, ctx, variant=) .lint() .review(id, by) .is_sendable(id)
    build_schedule, next_action, compute_due_date, ChaseState, BusinessCalendar,   # chase-policy@1.0.0
    run_plausibility, PlausValue, PlausContext, scan_file, scan_text_fields,      # PLAUS-001…035
    transition, TRANSITIONS,                                                      # 状态机
    DefaultEgressGateway, EgressDenied, OutboundMessage, OutboxTransport, SmtpTransport,
    CoreClient, HttpCoreClient, InMemoryCoreClient, webhook_signature, verify_webhook, jws_sign, jws_verify,
    SupplierService, create_app, Settings,
)
```

## 安全要点（08 §7、07）

- token `slk_…`：256 位（由实例密钥对 `sreq_id + generation + 过期时间` 做 HMAC 派生）+ 截断 HMAC 签名；库中只存 `sha256:` 哈希；篡改/伪造在查库前即 404。
- 一个 token 只对应一个 SupplierRequest；默认有效期 `due_date + 14 天`，最长 90 天；`closed/expired` 后 7 天只读再失效；补发后旧 token 立即失效。
- 首次打开签发 HttpOnly 设备 cookie；新设备需向**原收件邮箱**发送 6 位验证码，5 次错误锁定 15 分钟。
- 速率限制：每 token 60 次/分、每天 20 次上传、最多 10 次提交；同 IP 未知 token 10 次/分并指数退避；补发链接每邮箱 3 次/小时；单文件 25 MB、单请求 200 MB。
- 同一 token 出现过多来源（≥10 个不同 IP）自动冻结并通知租户（PLAUS-035）。
- 页面：严格 CSP、`noindex`、`Referrer-Policy: no-referrer`、无第三方脚本；表单 CSRF；`/pause/{token}` 的 GET 只展示确认页，POST 才生效（防邮件扫描器预取误触）。
- 上传：类型白名单、魔数与 MIME 一致性、压缩炸弹、PDF JavaScript、宏/外链/DDE 拒收（PLAUS-031…033）；可注入 `virus_scanner`（如 ClamAV），默认记 `av=not_configured`。

## 已知限制 / 待办

- 08 只给出了 CBAM 首次请求、EUDR、PCF 的正文；催办 `r1/r2/r3`、补充请求、升级内部通知、PCF 德语、证书续期（本包扩展族 `certificate_renewal`）为实现方起草，状态为 `draft`，**须母语审校后**（`templates review`）才能外发。
- PLAUS-004/005/006/007 行业区间、PLAUS-011/021 编号格式、PLAUS-012 附件 I 清单、PLAUS-013 碳定价国家清单均为默认值，标 `[需核对]`（08 待决问题 4）。
- PLAUS-016/017 内置 `BBoxGeoOracle` 只是国家外接矩形 + 大洋样区的粗判定；生产应注入基于 Natural Earth 1:10m 的实现。面积为球面近似（误差 < 0.5%）。
- 静默期覆盖表 2026–2027 的宗教节日/春节日期需按官方公告核对（08 待决问题 5）。未安装 `holidays` 时只用内置固定假日 + 复活节推算。
- 可选账户（FR-306）、共享（FR-307，V2）、地图绘制（V2）、离线 IndexedDB 草稿（目前为 localStorage 尽力而为）、it/pl UI 语言未实现。
