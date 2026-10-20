# syntax=docker/dockerfile:1.7
# Spec: 10 §1-4（一套代码同一镜像）、§6.1、§8.2（可复现构建、多架构）；07 §2.2（非 root、只读根文件系统）；04 §1.3
#
# belegt-core 镜像（AGPL-3.0-only）。构建上下文 = 仓库根 code/belegt：
#   docker build -f deploy/docker/core.Dockerfile --target api     -t ghcr.io/belegt/core-api:0.1.0 .
#   docker build -f deploy/docker/core.Dockerfile --target worker  -t ghcr.io/belegt/core-worker:0.1.0 .
#   docker build -f deploy/docker/core.Dockerfile --target migrate -t ghcr.io/belegt/core-migrate:0.1.0 .
# 三个 target 共用同一 venv 层（同一份代码与依赖），只是入口不同。多架构：docker buildx build --platform linux/amd64,linux/arm64。
#
# 运行约束（compose / Helm 均已配置）：UID 10001、read_only 根文件系统、/tmp 为 tmpfs、无 Linux capabilities。

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---------------------------------------------------------------- builder：在独立 venv 中安装全部本地包
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY packages/belegt-dictionary ./packages/belegt-dictionary
COPY packages/belegt-calc ./packages/belegt-calc
COPY packages/belegt-io ./packages/belegt-io
COPY packages/belegt-supplier ./packages/belegt-supplier
COPY packages/belegt-connectors ./packages/belegt-connectors
COPY packages/belegt-core ./packages/belegt-core
# 依赖方向 dictionary ← calc ← core；io/supplier/connectors 为 core 的 Agent 运行期所需（CONTRACTS.md）
RUN pip install \
      ./packages/belegt-dictionary ./packages/belegt-calc ./packages/belegt-io ./packages/belegt-supplier \
      "./packages/belegt-connectors[mcp,pdf]" "./packages/belegt-core[postgres,sign]" \
      "uvicorn[standard]>=0.30" "temporalio>=1.7" "langgraph>=0.2" "mcp>=1.0" "prometheus-client>=0.20" \
      "opentelemetry-sdk>=1.27" "opentelemetry-exporter-otlp-proto-grpc>=1.27" \
 && python -c "import belegt_core, belegt_calc, belegt_dictionary, belegt_core.store.pg_commit"

# ---------------------------------------------------------------- base：运行时（无编译器、无 pip 缓存）
FROM ${PYTHON_IMAGE} AS base
LABEL org.opencontainers.image.source="https://github.com/belegt/belegt" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.title="belegt-core"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/entry \
    HOME=/tmp TMPDIR=/tmp \
    BELEGT_MIGRATIONS_DIR=/app/migrations/postgres \
    BELEGT_DEPLOY_PROFILE=private-compose
RUN groupadd --system --gid 10001 belegt \
 && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin belegt
COPY --from=builder /opt/venv /opt/venv
COPY --chown=root:root packages/belegt-core/migrations /app/migrations
COPY --chown=root:root deploy/docker/entry /app/entry
# 代码与 venv 归 root 且只读；进程以 10001 运行，只写 /tmp（tmpfs）
RUN python -m compileall -q /app/entry /opt/venv/lib >/dev/null 2>&1 || true
WORKDIR /app
USER 10001:10001

# ---------------------------------------------------------------- api：Graph API + 审批路由（03 §6.8、06）
FROM base AS api
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)"]
ENTRYPOINT ["uvicorn", "--factory", "belegt_asgi:create", "--host", "0.0.0.0", "--port", "8000", \
            "--proxy-headers", "--no-server-header", "--no-access-log"]
CMD ["--workers", "2"]

# ---------------------------------------------------------------- worker：Temporal worker（04 §4.3）
FROM base AS worker
EXPOSE 9464
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9464/healthz',timeout=2).status==200 else 1)"]
ENTRYPOINT ["python", "-m", "belegt_worker"]
CMD []

# ---------------------------------------------------------------- migrate：一次性迁移 Job（10 §6.5 ②、§8.4）
FROM base AS migrate
HEALTHCHECK NONE
ENTRYPOINT ["python", "-m", "belegt_migrate"]
CMD []
