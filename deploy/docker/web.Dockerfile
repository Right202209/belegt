# syntax=docker/dockerfile:1.7
# Spec: 01（审批台）、10 §3.4、07 §2.2（非 root、只读根文件系统）
# belegt-web：多阶段构建（node 构建 → nginx-unprivileged 托管静态文件）。构建上下文 = code/belegt：
#   docker build -f deploy/docker/web.Dockerfile -t ghcr.io/belegt/web:0.1.0 .
#   docker build -f deploy/docker/web.Dockerfile --build-arg VITE_API_BASE= -t belegt-web:demo .   # 空 = 内置 mock 演示
ARG NODE_IMAGE=node:22-bookworm-slim
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine

FROM ${NODE_IMAGE} AS build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
# 运行期经 nginx /api/ 反代到 belegt-api（同源，无 CORS）
ARG VITE_API_BASE=/api
ENV VITE_API_BASE=${VITE_API_BASE}
RUN npm run build && test -f dist/index.html

FROM ${NGINX_IMAGE} AS runtime
LABEL org.opencontainers.image.title="belegt-web" org.opencontainers.image.licenses="AGPL-3.0-only"
# nginx 官方镜像的 entrypoint 会对 /etc/nginx/templates/*.template 做 envsubst，输出到 /etc/nginx/conf.d
# （只读根文件系统时把 /etc/nginx/conf.d 挂成 tmpfs 或 emptyDir）
ENV BELEGT_API_UPSTREAM=http://belegt-api:8000 \
    NGINX_ENVSUBST_FILTER=^BELEGT_
COPY --chown=101:101 deploy/docker/nginx/default.conf /etc/nginx/templates/default.conf.template
COPY --from=build --chown=101:101 /web/dist /usr/share/nginx/html
USER 101
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
