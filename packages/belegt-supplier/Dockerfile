# Spec: 08 §6.2 自托管：单容器（ghcr.io/belegt/supplier）
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BELEGT_SUPPLIER_DATA_DIR=/data \
    BELEGT_SUPPLIER_PUBLIC_URL=http://localhost:8080
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --uid 10001 --home /data belegt && mkdir -p /data && chown belegt /data
USER belegt
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"
ENTRYPOINT ["belegt-supplier"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
