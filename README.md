# Belegt

English | [中文](README.zh.md)

Evidence-first multi-agent compliance questionnaire system.

> **Core principle:** Numbers come exclusively from evidence or deterministic calculation — the LLM never generates numbers. All objects are append-only, auditable, and replayable.

## About

Belegt automates the end-to-end lifecycle of sustainability and compliance questionnaires — from ingesting supplier data to producing auditable, submission-ready answers. Its multi-agent pipeline maps questions to 91 canonical data points, gathers evidence from heterogeneous sources, performs deterministic calculations (Scope 1/2, PCF, CBAM), and produces hash-chained audit trails with full provenance.

## Features

- **Automated questionnaire processing** — ingest, map, gather evidence, calculate, draft, audit, approve, backfill, and submit in one pipeline.
- **Evidence graph** — append-only, hash-chained audit trail with full provenance for every data point.
- **Deterministic calculations** — Scope 1, Scope 2 (location/market), PCF (ISO 14067/PACT), CBAM SEE, CBAM cost-differential (§ 6.6).
- **91 canonical data points** — with template mappings, unit conversion, and built-in validators.
- **Multi-format I/O** — xlsx/csv parsing & backfill, plus PACT v3.0.3, Catena-X, CBAM, CMRT, and Battery Passport export.
- **Supplier portal** — self-hosted portal with magic-link auth, i18n (de/en/tr/zh), chase policies, plausibility rules, and webhooks.
- **Approval dashboard** — React + TypeScript UI with inbox views (L0–L3), provenance panel, conflict resolution, and expiry tracking.
- **Extensible connectors** — MCP read-only connectors for folders, SQL, ERP, IMAP, and utility-bill PDFs.
- **Benchmark suite** — golden-set evaluation with quality gates for mapping, evidence, injection recall, and calibration.
- **Production-ready deployment** — PostgreSQL 16 + RLS, Temporal workflows, MinIO, Helm charts with trust-partition network policies.

## Prerequisites

- Python ≥ 3.12
- Node.js (for the web dashboard)
- Make

## Getting Started

```bash
# Install all packages (editable mode)
make install

# Run the full test suite (offline, no LLM key needed)
make test

# Run the web dashboard tests
make test-web

# End-to-end demo with a 100-question German OEM questionnaire
belegt demo --out out/demo

# Start the API server (dev mode with temporary SQLite)
BELEGT_DEV_AUTH=1 belegt serve --dev-auth

# Start the web dashboard
cd web && VITE_API_BASE=http://127.0.0.1:8000 npm run dev
```

## Project Structure

```
belegt/
├── packages/
│   ├── belegt-dictionary   # 91 data points, template mappings, unit conversion, validators
│   ├── belegt-calc         # Calculation methods (Scope 1/2, PCF, CBAM), factor sets, CLI
│   ├── belegt-core         # Evidence graph, audit chain, agents, workflows, API, migrations
│   ├── belegt-io           # Questionnaire parsing, backfill, evidence extraction, exports
│   ├── belegt-supplier     # Supplier portal: magic links, i18n, chase policies, webhooks
│   ├── belegt-connectors   # MCP read-only connectors (folder, SQL, ERP, IMAP, PDF)
│   └── belegt-bench        # Golden-set benchmark, metrics, quality gates, CI integration
├── web/                    # React + TypeScript approval dashboard (de/en/zh)
├── deploy/                 # Dockerfile, Docker Compose, Helm chart, runbook, SLOs
├── examples/               # Sample questionnaires, templates, demo output
├── docs/                   # Documentation
├── CONTRACTS.md            # Cross-package contracts and conventions
└── Makefile
```

Each package README documents its public API, extension points, and caveats. Cross-package contracts are described in [`CONTRACTS.md`](CONTRACTS.md).

## Configuration

| Variable | Description |
|---|---|
| `BELEGT_LLM_BASE_URL` | Base URL of an OpenAI-compatible LLM endpoint (vLLM, Ollama, etc.) |
| `BELEGT_LLM_API_KEY` | API key for the LLM endpoint |
| `BELEGT_LLM_MODEL` | Model identifier to use |

When none of the LLM variables are set, Belegt falls back to a deterministic `MockLLM` — no external model required.

## Deployment

For production deployment (PostgreSQL 16 + RLS, Temporal, MinIO, observability stack, Helm), see [`deploy/README.md`](deploy/README.md).

## Documentation

Full design documentation is available in the [`docs/`](docs/) directory, covering architecture, data models, calculation methodology, security, and more.

## Contributing

Contributions are welcome. Please open an issue to discuss your proposed changes before submitting a pull request.

## License

| Package | License |
|---|---|
| **belegt-core**, **web** | AGPL-3.0-only |
| **belegt-dictionary** | CC BY 4.0 / Apache-2.0 |
| **belegt-calc**, **belegt-io**, **belegt-supplier**, **belegt-connectors**, **belegt-bench** | Apache-2.0 |

See each package for full license details.
