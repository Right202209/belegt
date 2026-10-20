# Belegt

[English](README.md) | 中文

证据优先（Evidence-first）的多智能体合规问卷系统。

> **核心原则：** 所有数字仅来源于证据或确定性计算——大语言模型（LLM）绝不生成数字。所有对象均为仅追加（append-only）、可审计且可重放的。

## 关于

Belegt 自动化可持续发展与合规问卷的端到端生命周期——从导入供应商数据到生成可审计、可提交的答案。其多智能体流水线将问题映射到 91 个规范数据点，从异构来源收集证据，执行确定性计算（范围 1/2、PCF、CBAM），并生成带有完整溯源信息的哈希链式审计追踪。

## 功能特性

- **自动化问卷处理** — 在一个流水线中完成导入、映射、证据收集、计算、起草、审计、批准、回填和提交。
- **证据图** — 仅追加、哈希链式的审计追踪，为每个数据点提供完整的溯源信息。
- **确定性计算** — 范围1（Scope 1）、范围2（Scope 2，基于位置/市场）、PCF (ISO 14067/PACT)、CBAM SEE、CBAM 成本差异（§ 6.6）。
- **91个规范数据点** — 包含模板映射、单位转换和内置验证器。
- **多格式 I/O** — xlsx/csv 解析与回填，以及 PACT v3.0.3、Catena-X、CBAM、CMRT 和电池护照（Battery Passport）导出。
- **供应商门户** — 自托管门户，支持魔法链接（magic-link）认证、国际化（de/en/tr/zh）、催办策略、合理性规则和 Webhooks。
- **审批看板** — React + TypeScript 构建的 UI，包含收件箱视图（L0–L3）、溯源面板、冲突解决和过期跟踪。
- **可扩展连接器** — 用于文件夹、SQL、ERP、IMAP 和公用事业账单 PDF 的 MCP 只读连接器。
- **基准测试套件** — 包含用于映射、证据、注入召回率和校准的黄金集（golden-set）评估与质量门禁。
- **生产级部署** — PostgreSQL 16 + RLS、Temporal 工作流、MinIO、包含信任分区网络策略的 Helm charts。

## 前提条件

- Python ≥ 3.12
- Node.js（用于 Web 看板）
- Make

## 快速开始

```bash
# 安装所有包（可编辑模式）
make install

# 运行完整的测试套件（离线，无需 LLM 密钥）
make test

# 运行 Web 看板的测试
make test-web

# 使用包含 100 个问题的德国 OEM 问卷进行端到端演示
belegt demo --out out/demo

# 启动 API 服务器（带临时 SQLite 的开发模式）
BELEGT_DEV_AUTH=1 belegt serve --dev-auth

# 启动 Web 看板
cd web && VITE_API_BASE=http://127.0.0.1:8000 npm run dev
```

## 项目结构

```
belegt/
├── packages/
│   ├── belegt-dictionary   # 91个数据点、模板映射、单位转换、验证器
│   ├── belegt-calc         # 计算方法（范围1/2、PCF、CBAM）、因子集、CLI
│   ├── belegt-core         # 证据图、审计链、智能体、工作流、API、数据库迁移
│   ├── belegt-io           # 问卷解析、回填、证据提取、导出
│   ├── belegt-supplier     # 供应商门户：魔法链接、国际化、催办策略、Webhooks
│   ├── belegt-connectors   # MCP 只读连接器（文件夹、SQL、ERP、IMAP、PDF）
│   └── belegt-bench        # 黄金集基准测试、指标、质量门禁、CI 集成
├── web/                    # React + TypeScript 审批看板 (de/en/zh)
├── deploy/                 # Dockerfile、Docker Compose、Helm chart、运行手册、SLOs
├── examples/               # 示例问卷、模板、演示输出
├── docs/                   # 文档
├── CONTRACTS.md            # 跨包契约与约定
└── Makefile
```

每个包的 README 都记录了其公共 API、扩展点和注意事项。跨包契约在 [`CONTRACTS.md`](CONTRACTS.md) 中描述。

## 配置

| 环境变量 | 描述 |
|---|---|
| `BELEGT_LLM_BASE_URL` | 兼容 OpenAI 的 LLM 端点的基础 URL（vLLM、Ollama 等） |
| `BELEGT_LLM_API_KEY` | LLM 端点的 API 密钥 |
| `BELEGT_LLM_MODEL` | 要使用的模型标识符 |

当未设置任何 LLM 环境变量时，Belegt 会回退到一个确定性的 `MockLLM` —— 无需外部模型。

## 部署

关于生产环境部署（PostgreSQL 16 + RLS、Temporal、MinIO、可观测性栈、Helm），请参阅 [`deploy/README.md`](deploy/README.md)。

## 文档

完整的设计文档位于 [`docs/`](docs/) 目录，涵盖架构、数据模型、计算方法论、安全性等内容。

## 贡献

欢迎贡献。请先开 Issue 讨论您的提议，再提交 Pull Request。

## 许可证

| 包 | 许可证 |
|---|---|
| **belegt-core**、**web** | AGPL-3.0-only |
| **belegt-dictionary** | CC BY 4.0 / Apache-2.0 |
| **belegt-calc**、**belegt-io**、**belegt-supplier**、**belegt-connectors**、**belegt-bench** | Apache-2.0 |

有关完整的许可证详细信息，请参阅各个包。