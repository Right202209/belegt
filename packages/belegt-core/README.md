# belegt-core

Belegt 核心证据图层（`belegt_core`，AGPL-3.0-only）。规格：03 数据模型与证据图（全文）、04 §2.1/§3.1–§3.2、
07 §2/§4/§7、`_baseline`（含 §M 统一修订）。本包是"第一部分"：模型、写入规则、存储、审计、审计包、Graph API、
PostgreSQL 物理设计。后续子任务在同一包下新增 `consistency/`、`approval/`、`agents/`、`workflow/`、`llm/`、
`guard/`、`egress/` 等模块。

```bash
pip install -e packages/belegt-core[test]     # 可选：[postgres] [migrations] [sign]
pytest packages/belegt-core/tests             # 不访问网络，不需要 LLM key
uvicorn belegt_core.api:app                   # 内存 SQLite + BELEGT_DEV_TOKENS 令牌表
```

## 模块边界

| 模块 | 职责 | 规格 |
|---|---|---|
| `models/` | 03 全部对象的 pydantic v2 模型（字段名与 03 一致）：`Request` `Question` `Mapping` `Evidence` `Calculation` `Answer` `Approval` `Conflict`（含 `resolution.divergence` ADR/1） `SupplierRequest` `ClosedQuestion`（含 `file_request`） `Organization` `Counterparty` `Site` `Product` `FactorSet` `DataPoint`（图内视图） `AuditEvent` `Trace`；通用类型 `DecStr` `Period` `Locator` `ObjectRefStr` 等 | 03 §2–§3 |
| `schemas/` | 03 §5 JSON Schema（Draft 2020-12，`$id` 前缀 `https://schemas.belegt.org/graph/2026-10/`，`schema_version=belegt.graph/2026-10`）：common/answer/evidence/calculation/conflict 取自原文，其余对象按 §3 补齐 | 03 §5 |
| `ids.py` | ULID ID 生成、`ObjectRef`（`<id>#v<n>`）解析、前缀↔类型 | 03 §1.2 |
| `canonical.py` | RFC 8785 JCS + 03 §4.5 规范化（NFC、删 null、ES 数字格式）、DEC-STR 规范化、`content_hash` | 03 §1.4-6、§2.2、§4.5 |
| `hashing.py` | `input_hash`（优先 `belegt_calc.input_hash`，否则参考实现）、`output_hash`、`answer_key`、`conflict_key` | 03 §4.5、SSOT-5 |
| `units.py` | 单位量纲与线性换算（优先 `belegt_dictionary`，否则内置 02 §3.1/§3.2 子集） | R-UNIT |
| `datapoints.py` | DataPoint 注册表（字典包精确 SemVer 优先，内置最小集补缺，可 `register()`） | 03 §3.1、§4.3 |
| `authz.py` | `Actor`（user:/agent:/service:）、信任区、类型级写权限（03 §1.6 + 07 §4.1 RBAC 的 C 列）、读过滤（模拟 RLS：租户、p_zone、cp_scope、approved_only）、审批角色表 | 03 §1.6、07 §4 |
| `statemachines.py` | 全部对象状态机；Request 为 04 §3.2 T01…T23（`awaiting_human`/`halted` 的 `return_to`）；SupplierRequest 含 `reminded→expired`、`escalated→responded` | 04 §3、03 §3 |
| `rules/` | 03 §2.3 写入规则全集：预检（R-NUM/R-LOC/R-EST/R-FS1 形状）→ R-SCHEMA → GRAPH_HALTED → R-ZONE → R-PROMO → R-REF → R-STATE → 类型规则（R-VAL/R-UNIT/R-PERIOD/R-ONE/R-HASH/R-FS1/R-APR/R-SUBMIT/GRAPH_NUM_IN_TEXT/DataPoint 弃用…）；`refspec.py` 是引用字段↔边的唯一规格 | 03 §2.3、§1.5、§6.8 |
| `store/` | `GraphStore`（SQLAlchemy 2.x，SQLite/PostgreSQL）：append-only 版本、head + `expected_version` 乐观锁、幂等键（24 h）、派生边、`effective_answer`、as-of 双时间查询、lineage/impact、outbox、重放、blob/因子行/Trace 登记、SSOT-3 边校验与 SSOT-7 重建 | 03 §1.4、§4、§6 |
| `audit.py` | AuditEvent 哈希链（`prev_hash`/`event_hash`）、链/片段校验；拒绝的写入也记审计 | 03 §3.14、07 §7 |
| `auditpack.py` | `belegt-auditpack/1` 导出（zip：manifest、全部对象版本、证据原件、replay 原文 M、因子行、边、lineage、traces、审计片段与证明；可 Ed25519 签名）与 4 步核验 | 03 §7 |
| `api/` | FastAPI：03 §6.8 全部端点 + 01 所需最小 UI 列表端点；开发用静态 Bearer 令牌表；错误响应 `{error:{code,message,rule_id,path,details}}` | 03 §6.8 |
| `migrations/postgres/` | PostgreSQL 16 物理设计 SQL：schema、角色、HASH 分区表、索引、RLS、`graph.commit_version()`/`commit_batch()`、审计哈希链函数、pgvector | 03 §6 |
| `migrations/alembic/` | alembic：`0001_portable`（GraphStore 可移植表）、`0002_postgres_physical`（仅 PostgreSQL 执行上面的 SQL） | — |

依赖方向：`belegt_core` 运行期可选使用 `belegt_dictionary`（单位、DataPoint）与 `belegt_calc`（`input_hash`/`output_hash`/`replay`）；
两者都缺失时全部功能仍可用（测试覆盖两种情况）。

## 公开 API 摘要（`from belegt_core import ...`）

```python
from belegt_core import (Actor, GraphStore, WriteItem, GraphError, ObjectRef, new_id, new_trace_id, ref,
                         canonicalize, content_hash, input_hash, answer_key, check_transition, create_app,
                         export_audit_pack, verify_audit_pack)

store = GraphStore("sqlite+pysqlite:///:memory:")          # 或 postgresql+psycopg://...
co = Actor.of("user:u_compliance01", "org_…", ["compliance_owner"])
calc = Actor.of("agent:calc-service", "org_…")

r = store.write("Answer", body, actor=calc)                # -> "ans_…#v1"；失败抛 GraphError
r = store.write("Answer", {"id": aid, "status": "in_review", "risk_level": "L2"},
                expected_version=1, actor=router, patch_mode="merge_from_head",
                idempotency_key="wf:req_…:act:route:3")
refs = store.write_batch([WriteItem("Approval", apr, 1, actor=co), WriteItem("Answer", ans, 2)], actor=router)
store.validate(WriteItem("Answer", body), actor=calc)      # 只校验不写（POST /graph/validate）

store.get("ans_…")                                          # head；get(id, version=2)；get("ans_…#v2")
store.get("ans_…", as_of_tx="2027-03-01T10:00:00Z", as_of_valid="2026-12-31", actor=co)
store.head("ans_…")                                         # HeadInfo(id, org_id, type, version, status, trust_zone, updated_at)
store.history("ans_…"); store.query("Answer", org_id=…, status="approved", datapoint_base="dp:env.ghg.scope2_market_based")
store.lineage("ans_…#v3"); store.impact("ev_…#v2")          # {root, nodes, edges, calculations, answers, submitted_requests}
store.effective(org_id, answer_key)
store.replay_calculation("calc_…#v1"); store.replay_answer("ans_…#v3")   # status: ok | drift | integrity_error
store.record_audit(actor, "egress.send", object_ref="sreq_…#v1")         # 非对象写入类审计
store.verify_audit_chain(org_id); store.audit_events(org_id)
store.outbox_pending(); store.outbox_mark_published(seqs)
store.put_blob(org_id, data, mime); store.put_factor_rows(fs_id, rows); store.record_trace(trace)
store.verify_edges(org_id); store.rebuild_derived(org_id)                # SSOT-3 / SSOT-7
data, manifest = export_audit_pack(store, "req_…", actor=co); verify_audit_pack(data)
app = create_app(store, token_resolver, routers=[my_router])             # FastAPI
```

`GraphError(code, message, rule_id, path, details)`，`.http_status`，`.to_response()`；错误码表 `belegt_core.errors.ERROR_CODES`（03 §6.8，
另加 API 层 `GRAPH_UNAUTHENTICATED` 401、`GRAPH_NOT_FOUND` 404、`GRAPH_BATCH_TOO_LARGE` 400）。

写入契约要点：
- 执行者由 `actor` 决定（API 由令牌推出），对象体内 `created_by` 若与之不同即 `GRAPH_ACTOR_NOT_ALLOWED`；`version`/`created_at`/`content_hash`/`answer_key`/`conflict_key` 由服务端赋值，客户端值被忽略；新版本的 `supersedes` 由服务端设为上一版本。
- 新建对象 `expected_version` 省略或 0；已有对象必须等于 head，否则 `GRAPH_VERSION_CONFLICT`。
- 浮动引用（无 `#v`）在写入时钉住为当前 head；库中只存钉住引用。
- `WriteItem.actor` 仅供进程内编排在一个事务里混合执行者（如 03 §5.6 的 Approval(user)+Answer(approval-router)）；HTTP 批量端点不暴露。
- 跨对象替换（新对象 v1 的 `supersedes` 指向另一对象）、`Evidence.corrects`、`ClosedQuestion` 作答，都会在同一事务由 `service:graph-api` 自动追加目标对象新版本（批内已手工写入目标者跳过）。

## 扩展点（供后续子任务）

| 扩展 | 方式 |
|---|---|
| 新路由（`approval/`、`workflow/`、审批台 UI） | `create_app(store, tokens, routers=[APIRouter…])`；路由内 `Depends(belegt_core.api.current_actor)` / `Depends(belegt_core.api.get_store)` |
| 守卫策略（`guard/`：预算熔断、限速、循环检测） | `GraphStore(policy_hook=fn)`，`fn(actor, type, obj)` 抛 `GraphError("GRAPH_RATE_LIMITED"/"GRAPH_HALTED")`；Request `halted` 时 Agent 写入自动 423 |
| 新写入规则（如 06 的 CR-007…、审批策略细则） | 在 `rules/objects.py` 的 `TYPE_RULES` 或 `rules/answer.py` 的 `ANSWER_RULES` 追加 `fn(ctx: RuleContext)`；读图只经 `ctx.reader`/`ctx.resolve()` |
| 新引用字段/边 | 只改 `rules/refspec.py`（同时驱动钉住、R-REF、边派生、SSOT-3 校验） |
| 状态机调整 | `statemachines.py` 的 `Transition` 表（执行者模式 `user[role]`、`agent:<id>`、`service:<id>`、`agent:*trusted`） |
| 一致性/出口/工作流消费变更 | 轮询 `store.outbox_pending()`（`event_type=graph.object.committed`），处理后 `outbox_mark_published()` |
| 生产认证 | 实现 `api.auth.TokenResolver.resolve(token) -> Actor`（OIDC/SPIFFE，07 §4.4/§4.6） |
| DataPoint/单位来源 | `GraphStore(datapoints=DataPointRegistry(...), units=...)` |
| LLM | 本部分不含；契约要求的 `belegt_core.llm.LLMClient` 由 `llm/` 子任务提供 |

## 与文档的出入（实现时的判断，均已在测试中固化）

1. **03 §5.6 的 4 个 `content_hash` 与 03 §4.5 规则不一致**：Request、Calculation 含 `null` 字段（`template_ref`、`supersedes`），§4.5-3 要求删除；两个 Evidence 的 `bbox` 含 `72.0` 等整数浮点，RFC 8785 输出 `72`。文档值由 `work/gen_chain.py` 用 `json.dumps(sort_keys)` 生成（保留 null、浮点按 Python repr）。本实现按规范算；其余 5 个对象、`input_hash`（= TV-04）、`output_hash`、`answer_key` 与文档逐字一致。修正文档时用 `belegt_core.content_hash()` 重算这 4 个值即可。
2. **03 §5.6.3 Question v2 无 `promotion`**：叙述是 question-parser 产出 quarantine v1、guard 写 trusted v2，按 §2.1/§1.6 这是晋升版本，必须带 `promotion`，且 04 PROMO-Q-STRUCT 要求晋升时 `status` 保持 `parsed`。测试改用人工录入的 trusted v1（RBAC 允许 compliance_owner 创建 Question），v2 只是 guard 的 parsed→mapped，所以 9 个对象都能原样写入。另有一条测试固化了"按叙述写入会返回 `GRAPH_PROMOTION_DENIED`"。
3. **T04 执行者**：03 摘要表写 `agent:guard`，但 03 §5.6.2 Request v3 是 `service:workflow` 写的，04 §3.2 写"question-parser + guard"。实现两者都允许。
4. **Answer → approved 的执行者**：03 状态机写 `user`，§1.6 也写 approval-router "禁止裁决"，但 03 §5.6.9 的 Answer v3 是 `agent:approval-router` 写的。实现允许 approval-router 落地已裁决的 Approval，R-APR 会校验 Approval 的 decided_by 角色、L3 非批量、职责分离，并要求 Approval 钉住紧前的 in_review 版本。
5. **03 §5.3 evidence.json 缺 `properties.promotion`**：`additionalProperties:false` 会让 PROMO-EV-SUP（supplier_response 证据晋升）无法写入，已补上；common.json 也补了 `promotion`、`date`、`dateTime`、`userActor`、`locator` 定义。
6. **R-ZONE 与 03 §1.6/§3.10/04 T08 冲突**：supplier-reply-reviewer 是隔离区执行者，却被列为 SupplierRequest `responded/reviewed` 和 Request T08 的写入者，这两类对象都是 trusted。实现按 R-ZONE 拒绝它写 trusted，并允许 `service:workflow` 代为写这些转移。
7. **R-SUBMIT 用状态而非日期判断"有效"**：03 §5.6 的电费单 `valid_until=2025-12-31` 早于提交日，按日期判断会永远阻止提交。实现按 R-SUBMIT 括注"非 expired"取 head 状态；按日期到期由 consistency-auditor 写 `expired`（R-STATE 要求 `valid_until` 已过）。
8. **一个批次一个令牌**：03 §6.8 的批量端点隐含单一执行者，但 §5.6 示例 Approval(user) 与 Answer(approval-router) 同事务。因此在 store 层加了 `WriteItem.actor`，HTTP 不暴露。
9. **重放漂移（实测）**：已安装的 belegt-calc 0.4.0 重放 03 §5.6.8（记录为 0.3.0）时 `input_hash` 一致，但 `result.rounding.digits`（3 vs 1）与 `breakdown` 不同，报告为 `drift`。这正是 §4.7 要发现的漂移，需 05/calc 与 03 示例对齐。
10. **CQ 作答派生晋升**：作答后自动派生 quarantine 目标的 trusted 版本时，按 PC-08 不写 `decided_by_cq`（否则改值）；裁决经 CQ 的 `resolves` 边与 `promotion.rule=human` 追溯。
11. **本包新增标识**：`service:graph-api`（写入 API 自动追加的版本）；AuditEvent `details` 字段（拒绝记录的 code/rule_id/path，参与哈希）；审计链创世 `prev_hash = sha256:0…0`；`output_hash` 在缺 `breakdown` 时按 `[]` 计（与 belegt_calc 一致）；API `GET /graph/objects/{id}/history`、`GET /graph/audit/verify`、`POST /blobs`；错误码 `GRAPH_UNAUTHENTICATED`、`GRAPH_NOT_FOUND`、`GRAPH_BATCH_TOO_LARGE`。
12. **存储布局**：Python `GraphStore` 使用可移植表（`graph_object_version` 等，SQLite 与 PostgreSQL 均可）。03 §6 的按类型分区表、RLS 与 `graph.commit_version()` 在 `migrations/postgres/`，SQL 已通过 pglast 语法解析，但**沙箱无 PostgreSQL 服务器，未实跑**。GraphStore 目前没有调用 `commit_version()`，接入是后续部署工作。

## 一致性与审批（第二部分：`consistency/`、`approval/`、`legal/`、`expiry/`、`api/routes_review.py`）

规格：06 全文（§1 引擎、§2 CR-001…CR-043、§3 冲突生命周期、§4 风险分级与 `approval-policy@1.0.0`、§5 批量与撤销窗口、
§6 法律承诺、§7 有效期、§8.1 测试）、04 §2.6/§2.8/§2.16、03 §3.7–§3.9/§3.11、01 审批收件箱/卡片/冲突验收标准、
`web/README.md` 端点表。全部确定性，不依赖 `llm` 包；LLM 只经协议注入（`Explainer`、`LegalClassifier`）。

| 模块 | 职责 | 规格 |
|---|---|---|
| `consistency/cel.py` | CEL 求值（`cel-python`）+ Decimal 函数 `dec`/`dec_eq`/`dec_abs`/`dec_min`/`dec_max`/`dec_sum`；编译期拒绝 double 字面量 | 06 §1.2 |
| `consistency/rules/cr-001…043.yaml` | 规则 YAML（`rule_id`、`rule_version`、`category`、`severity`、`scope`、`triggers`、`plugin`、`when`、`assert`、`severity_expr`、`tolerance`、`default_action`、`explain_template`、`tests`），规则集 `consistency-rules@1.0.0` | 06 §1.2、§2.2 |
| `consistency/plugins.py` | 每条规则的确定性插件：选择输入集、单位换算到 canonical unit、期间/口径归一，产出 `Binding(vars, subjects, detail…)`；判定由 YAML 的 CEL `assert` 完成。字典派生实例 `CR-003:<dp>`（`sum_of`）、`CR-033:<dp>`（`plausibility` 编译为 Decimal-CEL） | 06 §1.2、§2.1、§2.2 |
| `consistency/context.py`、`meta.py` | 只读图快照 `AuditContext`（全部版本、钉住引用、有效答案、继任对象）；DataPoint 元数据（字典包优先，内置补缺） | 06 §1.4、§2.1 |
| `consistency/engine.py` | `ConsistencyEngine`：T1–T4 触发、`pass/fail/not_evaluable/unknown`、fail-closed 门禁、Conflict 写入（`conflict_key` 去重、`block→blocking`/`warn→warning`、`info` 只写 `obs.audit_finding`）、`detail.ruleset_version/rule_version/evaluated_inputs`、终态后再 fail 生成新 open 并 `supersedes`、`auto_cleared`、`obs.audit_run` 结果哈希 | 06 §1.3、§1.4、§3.1、§8.2-2/7 |
| `consistency/explain.py` | `Explainer` 协议 + 德/英/中确定性模板；guard 数字扫描（同 `GRAPH_NUM_IN_TEXT`），不通过/异常回退模板 | 06 §1.5 |
| `consistency/lifecycle.py` | `resolve_conflict`：A 统一口径（human_confirmation Evidence + 更正 Answer）/ B 接受差异（ADR/1 `resolution.divergence` 全部硬性校验）/ C 驳回（误报样本、30 天 ≥5 次触发复审）；同事务写 `Approval(subject_ref=cfl_…)` | 06 §3.2、§3.3 |
| `approval/approval-policy-1.0.0.yaml`、`policy.py` | 策略原文、`org-overlay` 校验（双人确认、只许改允许键、`floors`、撤销窗口 5–60） | 06 §4.4 |
| `approval/risk.py` | D1…D8 维度计算 + 判定表 `RISK_TABLE`（RISK-00…10，自上而下命中即停，floors 取 max）；策略不可用 → L3 | 06 §4.1、§4.2、04 §2.8 |
| `approval/router.py` | `ApprovalRouter`：定级 → `Answer(in_review, risk_level)` + `Approval(pending, required_role, policy_version)`；L3 法律承诺且 tier_a 仅 `executive`；SLA（工作日、≤ due−2 天）与升级（不自动批准）；stale 作废重建；单条裁决（L2 估算知悉/warn 处置说明、L3 step-up + 条款确认、modified 必选出处）；`supplier_request_first_send` | 06 §4.3、§4.4、§5.1-5、03 §3.8 |
| `approval/batch.py` | `check_batch`（§5.1 条件 1–5 + 摘要）、`BatchStager`（服务端暂存 `apb_`，只存 ObjectRef；窗口内整批/部分撤销；到期复检条件 2–4 后同事务提交或整批作废并通知；due_date 近时可立即提交；可注入时钟） | 06 §5 |
| `legal/` | 模式库 `legal-patterns@1.0.0`（LC-01…08 德/英，含正例/反例）、`detect_legal_clauses`（规则通道 OR LLM 通道 p≥0.30）、`flag_question`、`enforce_on_answer`、`cancel_legal_flag`（仅 compliance_owner、理由必填、样本进评测集） | 06 §6 |
| `expiry/` | `scan_expiry`（90/60/30/14/7/0 天梯度、通知对象、动作、已提交客户）、`apply_expiry`（过期 verified Evidence → `expired`） | 06 §7、FR-404 |
| `api/routes_review.py` | `review_router`：`/inbox`、`/approvals`（单条/批量/首次外发）、`/approvals/undo`、`/approvals/batches/{id}`、`/approvals/batch-check`、`/approvals/route`、`/approvals/escalations`、`/closed-questions`、`/closed-questions/{id}/answer`、`/conflicts`、`/conflicts/{id}/resolve`、`/expiry`、`/consistency/run`、`/requests/{id}/gate`、`/questions/{id}/legal-flag/cancel` | 01、web/README |

```python
from belegt_core.consistency import ConsistencyEngine, resolve_conflict, AuditConfig
from belegt_core.approval import ApprovalRouter, BatchStager, load_policy
from belegt_core.legal import detect_legal_clauses, flag_question, cancel_legal_flag
from belegt_core.expiry import scan_expiry, apply_expiry
from belegt_core.api.routes_review import review_router, configure_review

eng = ConsistencyEngine(store, explainer=None, config=AuditConfig(reach_candidate_list_latest="2026-07-01"))
run = eng.run("answer_write", org_id=org, answer_refs=["ans_…#v2"])     # T1；run.conflicts_written / run.failed("CR-001")
run = eng.gate(org, "req_…", submit_date="2027-03-01")                  # T2；run.gate ∈ pass|block|unknown，run.gate_blocks
run = eng.daily(org); run = eng.run("context_change", org_id=org, evidence_refs=["ev_…#v3"])   # T3 / T4
resolve_conflict(store, "cfl_…", actor=co, kind="accepted_divergence", note="…", divergence={...ADR/1...})

router = ApprovalRouter(store)                       # 或 ApprovalRouter(store, load_policy(overlay, 2, approvers=[co, admin]))
router.route(org, request_id="req_…")               # -> RouteResult(routed=[Routed(risk_level, rule_id, required_role, sla_due)], blocked=[RISK-00…])
router.decide("apr_…", actor=co, decision="approved", confirmations={"estimate_ack": True})
router.decide_supplier_first_send("sreq_…", actor=co)
stager = BatchStager(store, router, eng, clock=clock)
b = stager.stage(co, ["apr_…", …]); stager.undo(co, b.batch_id, ["apr_…"]); stager.settle()

det = detect_legal_clauses("Der Lieferant sichert zu, dass …", classifier=my_classifier)   # det.flagged, det.types, det.channels
alerts = scan_expiry(store, org)                     # [ExpiryAlert(tier="d30", level="warn", notify_roles=[…], actions=[…])]
app = create_app(store, tokens, routers=[review_router]); configure_review(app, clock=clock)   # 时钟/策略/解释器可注入
```

### 图外输入的约定（03 模型没有对应字段时，用 `tags` 的 `key:value`）

| 标签 | 位置 | 用途 |
|---|---|---|
| `legal_commitment`、`legal:LC-xx`、`legal_cancelled` | Question | §6 标记与取消（CR-041、RISK-01、批量排除） |
| `scope2_method:location_based\|market_based` | Question | CR-027 选用哪种 Scope 2（默认 location） |
| `raw_unit:tCO2` | Evidence | CR-019 原始单位（`tCO2` 不在单位表，Evidence 不能以它为 `unit`） |
| `estimate` | Evidence | CR-039 区分估算来源与实测证据 |
| `export_note` / `estimate_note`；`estimate_flag` | Answer；Question | CR-038 导出说明 / 模板有估算标识栏 |
| `period_note`；`correction` | Answer | CR-023 期间注明；CR-005 标注为更正 |
| `amount_eur:<DEC>`（或 Calculation `params.amount_eur`） | Answer | D6 金额影响 |
| `fy_start:YYYY-MM-DD`；`purpose:<cbam\|catenax\|cdp\|contract\|…>` | Counterparty / Request；Request | CR-043 |
| `supplier_tier:A`；`custom_note` | Counterparty；SupplierRequest | 首次外发升 L2 |
| `policy_overlay:ov<N>` | Approval | 覆盖版本（见出入 3） |

CR-030 读 `activity.production*`（产量）与 `allocation.*`（分配系数）verified Evidence（`boundary=product:…`）、`Product.site_ids`
的场址 Scope 1/2；CR-031 取 `see_indirect` 所引 Calculation 中 `tCO2e/MWh` 的因子；CR-017 的 reg-radar 最新清单日期经
`AuditConfig.reach_candidate_list_latest` 注入（缺失为 `not_evaluable`）；CR-043 的 Annex II 清单默认取 DataPoint 标签
`vsme_annex_ii|vsme|vsme_basic`（06 待决 4），可用 `AuditConfig.annex_ii_datapoints` 覆盖。

### 与文档的出入（第二部分）

1. **改动了已有模块（3 处）**：`api/__init__.py` 的 `create_app` 把 `routers` 挂在最小 `ui_router` 之前（否则 `GET /inbox`、
   `/conflicts` 等被只读列表端点遮蔽，无法返回 web 需要的 `InboxItem`/`ConflictView`）；`pyproject.toml` 新增依赖
   `cel-python>=0.5` 与规则/策略/模式 YAML 的 package-data；`belegt_core/__init__.py` 末尾追加延迟导出。
2. **规则文件位置**：06 写 `belegt-core/rules/consistency/`、`belegt-core/rules/legal/`；实现放在包内
   `consistency/rules/*.yaml`、`legal/patterns.yaml`（随 wheel 分发）。
3. **`Approval.policy_version` 不能带 `+ov<N>`**：03 `approval.json` 的模式为 `^approval-policy@x.y.z$`，与 06 §4.4 冲突。
   实现写基础版本，覆盖版本记为 Approval 标签 `policy_overlay:ov<N>`；`ApprovalPolicy.version` 仍返回带后缀的完整版本。
   修 03 schema 后改 `ApprovalPolicy.approval_fields()` 即可。
4. **审计动作枚举是封闭的**（_baseline §M `AUDIT_ACTIONS`）：批次暂存/撤销/提交/作废与法律标记取消用
   `action=authz.change` + `details.event ∈ {approval.batch.stage|undo|commit|void, legal.flag.cancel}`。
5. **`Conflict.detail.values[].value` 只能是 DEC-STR**：非数值规则（CR-007、CR-009、CR-013…）写序号码，并加
   `detail.value_encoding="ordinal"` 与 `detail.observed[]`（原值）。
6. **CR-001 与 T-02**：CR-001 的"同口径"包含 consolidation，两侧 consolidation 不同则 CR-001 本就不触发；T-02 测试用
   同口径两值、ADR/1 `reason_code=consolidation`。
7. **CR-004 的 0 天**：§2.2 写"0 < d ≤ 30 → warn"，§7 写"0 天 block"；实现取 `d ≤ 0 → block`（`valid_until` 当日按 block）。
8. **冲突复检策略**：同 `conflict_key` 的终态冲突若钉住的正是当前版本，不重开（处置结论仍有效）；版本变化才生成新 open
   并 `supersedes` 旧冲突。严重度变化（如 CR-004 warn→block）时旧冲突 `auto_cleared`、新冲突 supersedes 它。CR-042 的失败
   原因是暂态的（"进入批次"），复检通过即 `auto_cleared`，不要求关联对象 superseded（06 §3.1 只写了后者）。
9. **冲突处理的审批**：`Approval(subject_ref=cfl_…)` 由 approval-router 建、用户同事务批准；级别取所涉 Answer 最高级（至少
   L2），CR-001/CR-007、法律题、`accepted_divergence` 为 L3。Conflict 的写权限与状态机只允许 `compliance_owner`，因此
   06 §3.2"A 可由任何具备该 Answer 审批权限的角色发起"中 `executive` 目前不能落图。
10. **首次外发的采购负责人**：06 §4.4 允许租户配置的 `contributor` 审批 L1 首次外发，但已有 `authz.APPROVER_ROLES` 与
    `ROLE_WRITE` 不允许 contributor 写/裁决 Approval；实现明确拒绝并提示，只有 `compliance_owner` 可批。`confirm:
    [rendered_preview_viewed]` 未强制（web 请求体未带）。
11. **L2 批量"每条至少打开一次详情"**：服务端无从得知，批量请求需带 `viewed_approval_ids`（web 目前不发送）；估算知悉用
    `confirmations.estimate_ack`，L2 有 warn 时用 `confirmations.warn_note`。
12. **web 的批量流程**：web 现在本地暂存 10 分钟后才 `POST /approvals`，接后端后会再被服务端暂存一个窗口（返回 202）。
    应按 web README"已知取舍"改为服务端暂存，前端只展示 `undo_until` 倒计时并调用 `/approvals/undo`。`immediate=true`
    仅在 Request `due_date` 剩余时间小于窗口时允许。另：已有 `POST /blobs` 返回 `{sha256}` 而 web 期望 `{blob_sha256}`；web
    用 `X-Belegt-Actor` 头，本路由沿用 core 的 Bearer 令牌（`Depends(current_actor)`）。
13. **字典派生规则**：CR-033 跳过已由专门规则覆盖的 plausibility（scope2_location→CR-028、eac_volume→CR-029、
    headcount→CR-003 lte），避免重复冲突；CR-003 的 `lte` 关系目前只有内置的"总人数 ≥ 女 + 男"（06 示例），`eq` 关系取字典
    `related_datapoints.relation=sum_of`。
14. **RISK-03** 只对 `value_type=number` 生效（"排放类数字"）；RISK-07 的"至少 L2"与 floors 一起按 max 合并；D1 中引用
    未 verified 证据的答案落到 RISK-10（L2）；D4 承诺类数据点 = 标签 `commitment|legal` 或 `dp:env.ghg_target.*`。
15. **暂存与观测数据在进程内**：`BatchStager` 的批次、`InMemoryObsSink` 的 `obs.audit_finding/audit_run` 都在内存，重启即失；
    生产应放进 approval-router 的 Temporal 工作流状态与 `obs` 表（04 §4.3、03 §6）。SLA 提醒/升级只返回清单，不发通知。
16. **未实现（不在本次范围）**：§8.2 的变异测试、Hypothesis 属性测试、夜间增量/全量双跑比对、90 天回溯扫描、规则治理流程、
    T3 的 02:00 Europe/Berlin 调度（由工作流调用 `engine.daily()` + `apply_expiry()`）；冲突 L3 处理审批未要求 step-up。
17. **Question 处于 `answered` 时不能取消法律标记**：状态机只允许 approval-router 更新 answered 的 Question。

## Agent 与工作流（`llm/`、`guard/`、`egress/`、`agents/`、`workflow/`、`demo/`）

规格：04 §2（Agent 契约与晋升规则）、§3（问卷级状态机 T01…T23）、§4.3（Temporal 划分）、§5（守卫）、§7（模型路由/缓存/成本）、
07 §2–§3（信任分区、注入防护）、10 §4（成本）。依赖方向自上而下：`demo → workflow → agents → {guard, egress, llm} → store`；
`workflow/logic.py`、`workflow/states.py` 只依赖标准库（Temporal 沙箱内可导入）。

| 模块 | 职责 | 边界（不做什么） | 规格 |
|---|---|---|---|
| `llm/` | `LLMClient` 协议（`complete(LLMRequest) -> LLMResponse`）、`OpenAICompatibleClient`（httpx，`BELEGT_LLM_BASE_URL/API_KEY/MODEL`，json_schema→json_object 退化）、确定性 `MockLLM`（按 `task` 路由规则处理器，只读结构化 `data`）、`RoutedLLM`（档位 tier-S/M/L/E/V → 主/备/本地，`data_residency=private` 只走本地）、`CachedLLM`/`RefCache`（C1…C5，只存 ObjectRef）、`CostLedger`/`PriceTable`（Decimal EUR）、`HashingEmbedder`（1024 维确定性嵌入）、`complete_json`（schema 校验 + 1 次带错误反馈重试） | 不写图、不知道 Agent 业务；缓存不是事实源（SSOT-7） | 04 §2.0、§7 |
| `guard/` | `Guard`：`GraphStore(policy_hook=guard.policy_hook)` 接入（GP-01 身份、GP-02 区隔离、halt 冻结、限速）、`guard.llm()` 包装（GP-04 注入/金丝雀、GP-09 预算、GP-11 驻留、Trace 子 span）、`promotion`（PROMO-REQ-0.90 / Q-STRUCT / MAP-0.85 + CQ-MAP-BAND / EV-SUP：只盖章不改值，失败转 ClosedQuestion）、`budget`（三级预算，80% 预警、100% `BudgetExceeded`）、`loops`（LD-01…05 → `LoopDetected`）、`injection`（多语种扫描、spotlight 数据块、金丝雀）、`trace`（03 §3.15 核心字段 + 扩展 blob）；`halt()`/`release(budget_patch)` | 不决定业务值；异常由工作流转为 `halted`（T20）并等 `resume` | 04 §2.1、§2.14、§5 |
| `egress/` | `CoreEgressGateway`：唯一出站通道（邮件、供应商链接、门户 HTTP），检查 halt、模板登记、收件人、链接域名、DLP、静默期/频率（`retry_after`）、外发幂等；放行写 `egress.send`、拒绝写 `egress.block`；`MemoryTransport`（测试/演示） | 不排队重试（`EGRESS_DENIED_QUIET_PERIOD`/`RATE_LIMITED` 由工作流 timer 重提） | 04 §2.15 |
| `agents/` | 12 个 Agent（`AgentBase.run(...) -> AgentOutput{refs, outcome, cq_refs, gaps, details}`）：intake-triage、question-parser（hints → 隔离区映射候选）、evidence-miner（连接器/文档/历史答卷 → Evidence）、calc-service（belegt-calc，唯一数字通道）、consistency-auditor（调 `ConsistencyEngine`）、answer-drafter（禁止写数字答案）、approval-router（调 `ApprovalRouter`）、upstream-chaser、supplier-reply-reviewer（隔离区）、filler（文件回填 / 门户沙箱）、reg-radar、sales-assistant；`Services`（一个租户的依赖束：store、llm、guard、egress、connectors、prompts、embedder、ref_cache、OrgConfig）、`PromptLibrary`（`agents/prompts/*.yaml`，`prompt:<agent>@<semver>`）、确定性 ID `det_id(type, idem_key)`；`connectors.py` 本地 ERP/文档连接器（接口同 `mcp.client.MCPConnector`） | Agent 之间不直接调用，只经证据图 + 工作流；输出只含 ObjectRef 与计数 | 04 §2.2–§2.13 |
| `workflow/` | `logic.request_workflow(wf, req_ref)`（主状态机，两种执行器共用）、`activities.Activities`（每个方法 `(idem, *args) -> dict`，幂等键 `wf:<req>:act:<name>:<seq>`）、`local.LocalRunner`（单进程确定性重放 + `wf_journal`/`wf_run` 日志表；`run/signal/resume/cancel/progress`）、`temporal.py` / `temporal_worker.py`（见下节）、`states.py`（转移 → 写入执行者） | 工作流代码里不出现业务值（只有 ObjectRef 与计数）；人工等待一律上浮为信号 | 04 §3、§4.3 |
| `demo/` | `build_env()`（演示租户 Musterteile GmbH：GraphStore + Guard + MockLLM + egress + 连接器 + 主数据 + 历史答卷 + 文档 + 令牌表 `demo-owner/demo-exec/demo-sales`）、`upload()`、`drive()`（遇人工节点交给 `DemoHuman`、遇 `awaiting_supplier` 模拟供应商提交）、`run_demo()`、`summary()/format_summary()` | 只用于演示与测试；`DemoHuman` 不处置冲突、默认不批答案 | CLI `belegt demo` |

## 集成（Temporal / MCP / A2A / API）

```bash
pip install -e "packages/belegt-core[server,temporal,mcp]"
belegt demo [问卷] [--json] [--until in_review|submitted] [--out DIR]   # 演示：调 demo.run_demo；csv 停在待审批
belegt serve --dev-auth [--engine local|temporal] [--port 8000] # API + 审批台静态文件（web/dist 存在时挂 /ui）
belegt worker --address localhost:7233 --services belegt_core.demo:build_env   # = python -m belegt_core.workflow.temporal_worker
belegt mcp --demo --token demo-owner                           # MCP server（stdio）；--transport streamable-http
```

| 模块 | 职责 | 规格 |
|---|---|---|
| `workflow/temporal.py` | `RequestWorkflow`（一份问卷一个工作流，ID `req-<req_id>`，复用 `logic.request_workflow`）、`QuestionBatchWorkflow`（每 25 题一个子工作流，映射/起草并行扇出）；signal `human_answered`（只是唤醒，工作流用 `check_pending` 重新核对图状态）、`supplier_responded`（`{sreq_id, blob_sha256, kind}`，按 sreq 去重）、`resume`、`cancel`，以及 04 §4.3 别名 `cq_answered/approval_decided/conflict_resolved/supplier_replied`；query `status`（别名 `progress`：`state ∈ running/awaiting_human/in_review/awaiting_supplier/halted/timer/completed/cancelled/failed`、`pending`、`revision`、`progress`…）；`act()` 统一 RetryPolicy（2 s、×2、5 次，`NON_RETRYABLE` 不重试）；guard 熔断 → 写 `halted`（return_to）→ 等 `resume` → 重试同一 Activity（同一幂等键）；人工超时 → `escalate`（最多 `max_escalations` 次，永不自动批准）；`TemporalEngine`（客户端：`start/signal/status/wait_for`） | 04 §3、§4.3、§5.2 |
| `workflow/temporal_worker.py` | `build_activities(svc)`：把 `Activities` 的 26 个方法 + 4 个只读核对（`request_status/check_pending/approvals_outcome/advance_clock`）包装为同名 `@activity.defn`，输入 `{idem, args}`；异常映射 `GraphError → ApplicationError(type=code)`、`BudgetExceeded/LoopDetected/Halted` → 同名不重试错误；`create_worker(client, svc)`（SQLite 时 Activity 串行）、`load_services("module:factory")`、`main()` | 04 §4.3 |
| `mcp/service.py` | `ApprovedReadService`：只读已批准（`approved_only` 强制 + `status ∈ {approved, submitted}`）、按 `org_id` 与 `cp_scope` 授权（`customer_buyer` 必须带 cp_scope；隔离区/supplier_user/无读角色拒绝）、`sensitivity=restricted` 默认不列出；`get_answer_with_provenance` 只给 Evidence 的 `source_type` 与 locator 摘要（不给原件/原文），内部对象（Question/Request/Mapping）不外露 | 04 §6.2、07 §4.3 |
| `mcp/server.py` | `create_mcp_server(store, actor | () -> actor)`：工具 `list_approved_answers`、`get_answer_with_provenance`、`get_datapoint`、`search_dictionary`（全部 `readOnlyHint=true`、`destructiveHint=false`；`spec_names=True` 额外注册 04 §6.2 点号名）；stdio 用固定令牌，HTTP 用 `token_actor_resolver(tokens)` 按访问令牌解析 | 04 §6.2 |
| `mcp/client.py` | `MCPConnector(target)`：连接外部只读连接器 MCP server（进程内对象 / URL / `StdioServerParameters`），拒绝非只读工具与不合规工具名（`<category>.<system>.<resource>.<verb>`，`__` 映射回 `.`），工具描述哈希锁定（RT-PI-11）；`call()` 与 `LocalERPConnector.call` 同签名；`envelope_to_candidates()` → `EvidenceCandidate`（DEC-STR、`locator.connector{connector_id, query_hash, record_key, fetched_at}`、attested 0.95 / 否则 0.8，`to_evidence_fields()`） | 04 §6.1、03 §3.5 |
| `a2a/` | `a2a_router`：Agent Card（`/.well-known/agent.json` 与 `agent-card.json`）、JSON-RPC `/a2a`（`message/send` 的 `belegt.subscribe_datapoints`、`tasks/get`）、`/a2a/subscriptions`（创建/列表/详情/取消/`dispatch`）；`SubscriptionService`：同意检查（`Counterparty.data_sharing_grant{datapoints[], valid_until, revoked}`）、HMAC-SHA256 签名 webhook（`X-Belegt-Signature: sha256=…` over `"<ts>.<body>"`，`verify_signature()` 300 s 容差）、状态差分推送（每个已批准答案版本每订阅成功一次，失败重试 ≤ 5 次），撤回/过期即时暂停，交付写 `egress.send` 审计 | 04 §6.3、07 §2 |
| `api/routes_workflow.py` | `workflow_router`：`POST /requests`（multipart 上传 → intake-triage → LocalRunner 或 Temporal）、`GET /requests`（web `RequestView`）、`GET /requests/{id}`（+ `workflow` 状态）、`GET /requests/{id}/questions`（web `QuestionRow`）、`POST /requests/{id}/advance`（可带 `{signal, payload}`）、`/resume`（T21，仅 halted，`budget_patch` 形如 `{"request": {"cost_eur": "8"}}`）、`/cancel`；`blobs_router`：`POST /blobs` 同时返回 `sha256` 与 `blob_sha256`；`DevActorMiddleware`：`X-Belegt-Actor` → 静态令牌表中该执行者的令牌（仅 `BELEGT_DEV_AUTH=1`） | 01 UI-04、04 §2.2、web/README |
| `app_factory.py` | `create_full_app(settings)`：`create_app` + `blobs_router`（前置）+ `workflow_router` + `review_router` + `a2a_router` + `configure_workflow/configure_a2a` + `web/dist`（`/ui`）+ CORS（开发）+ `/healthz`；`Settings.from_env()` | — |
| `cli.py` | `belegt serve / demo / worker / mcp` | — |

环境变量（`Settings.from_env`）：`BELEGT_DB_URL`、`BELEGT_SERVICES_FACTORY`（默认 `belegt_core.demo:build_env`；`none` 关闭工作流）、
`BELEGT_WORKFLOW_ENGINE`（`local`|`temporal`）、`BELEGT_LOCAL_BACKGROUND`、`BELEGT_TEMPORAL_ADDRESS/NAMESPACE/TASK_QUEUE/OPTIONS`、
`BELEGT_DEV_TOKENS`、`BELEGT_DEV_AUTH`、`BELEGT_DEV_DEFAULT_ACTOR`、`BELEGT_CORS_ORIGINS`、`BELEGT_WEB_DIST`、`BELEGT_PUBLIC_BASE_URL`、
`BELEGT_A2A_ALLOW_HTTP`、`BELEGT_A2A_WEBHOOK_HOSTS`；MCP stdio：`BELEGT_MCP_TOKEN`。

```python
from belegt_core import create_full_app, Settings, TemporalEngine, create_worker, create_mcp_server, MCPConnector
app = create_full_app(Settings(workflow_engine="local", dev_auth=True))           # 或 services=DemoEnv/Services
eng = await TemporalEngine.connect("localhost:7233"); await eng.start("req_…#v1")
await eng.signal("req_…", "human_answered", {}); st = await eng.status("req_…")   # {"state": "awaiting_human", "pending": [...]}
async with create_worker(client, svc): ...                                          # temporalio Worker
server = create_mcp_server(store, actor)                                            # mcp MCPServer
cands = MCPConnector(server_or_url, connector_id="conn:erp-sap").fetch_candidates("erp.sap_b1.purchase_invoices.query", {...})
```

### 测试（离线）

`test_temporal.py` 用 `WorkflowEnvironment.start_time_skipping()`：只使用本地已有的测试服务器二进制（`BELEGT_TEMPORAL_TEST_SERVER`
或 temporalio 缓存 `$TMPDIR/temporal-test-server-sdk-python-<ver>`），否则 `pytest.skip`；`BELEGT_TEMPORAL_DOWNLOAD=1` 允许下载。
覆盖：伪 Activity 的完整主路径（T18/T19、首次外发确认、静默期 timer、T07/T08 供应商信号去重、T20 熔断 → resume → 同幂等键重试、
审批、提交确认）、人工超时升级与取消、真实 Activities + 演示租户与 LocalRunner 结果一致；`test_mcp.py` 进程内 `mcp.Client`；
`test_a2a.py` 用 `httpx.MockTransport` 收 webhook 并验签。

### 与文档的出入（集成部分）

1. **Agent Card 路径**：04 §6.3 写 `/.well-known/agent-card.json`，任务要求 `/.well-known/agent.json`；两者都提供，内容相同。
2. **MCP 工具名**：04 §6.2 用 `belegt.answers.list_approved` 等点号名；按集成任务约定主名为 `list_approved_answers`、
   `get_answer_with_provenance`、`get_datapoint`、`search_dictionary`，`spec_names=True` 时同时注册点号别名。
   `belegt.auditpack.request` 未实现（需 compliance_owner 审批 + egress 限时链接）。
3. **`approval_ref` 不派生边**（refspec），谱系里没有 Approval；`get_answer_with_provenance` 单独补 Approval 节点（`kind=approval_ref`）。
4. **A2A 推送绕过 `CoreEgressGateway`**：04 §2.15 规定 A2A 响应也走出口网关；网关目前只支持模板化邮件/链接。推送直接用 httpx，
   但写同样的 `egress.send`/`egress.block` 审计（含载荷哈希）；DLP/频率限制待网关支持 webhook 后接入。订阅 secret 明文存在
   `a2a_subscription` 表（生产应进保险库）。`data_sharing_grant` 的内部结构为本实现约定（03 只规定为 object）；无授权时直接拒绝
   （`consent_required`），不自动发 ClosedQuestion 给 compliance_owner（04 §6.3 时序图中的分支未实现）。
5. **SupplierChaseWorkflow / PortalFillWorkflow 未拆成子工作流**：供应商等待在 `RequestWorkflow` 内（信号 + 截止日 timer，
   到期 `expire_supplier_requests`），与 LocalRunner 行为一致；催办节奏由 egress/upstream-chaser 负责。
6. **Temporal 中 guard 状态在 worker 进程内**：预算快照不像 LocalRunner 那样写 `wf_run`；`resume` 的 `budget_patch` 在 API 进程调用
   `guard.release()`，并随信号载荷传给工作流（worker 侧 guard 需同一 `Services` 或共享存储才生效）。生产应把预算状态放进图/obs 表。
7. **演示时钟与 Temporal 时钟**：egress 静默期的 `retry_after` 用证据图时钟（演示 2026-11-03），工作流 timer 用 Temporal 时钟；
   `WorkflowOptions.max_timer_s` 可封顶 timer，之后 `advance_clock` Activity 推进外发时间（同 LocalRunner `auto_advance_timers`）。
8. **`Services` 是单租户的**：工作流端点要求执行者 `org_id` 与配置的租户一致（否则 403）；多租户需按 org 构造 `Services`。
9. **`POST /requests` 的执行者**：只允许 `compliance_owner`/`org_admin`（及 service）；Request 由 `agent:intake-triage` 写入（T01）。
10. **`/resume` 只在 `halted` 时接受**：LocalRunner/Temporal 都按"resume 次数 ≥ halt 次数"放行，提前 resume 会预先授权下一次熔断。
11. **本地取消**：LocalRunner 只在下一个 Activity 前检查取消，挂在人工节点时重放会再次停在等待点；`/cancel` 以图状态为准把运行
    记为 `cancelled`。
