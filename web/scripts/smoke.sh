#!/usr/bin/env bash
# 前后端对接冒烟：起 `belegt serve --dev-auth`（演示租户、内存库）→ 经 HTTP 把短问卷推进到 in_review →
# 用 web 的 HttpDataSource（vitest，Node）跑 列表 / 收件箱 / 单条批准 / 批量 + 撤销 → 结果写 docs/smoke-result.json
set -euo pipefail
export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"
cd "$(dirname "$0")/.."
ROOT="$(cd .. && pwd)"
PORT="${SMOKE_PORT:-8765}"
BASE="http://127.0.0.1:${PORT}"
OUT="${SMOKE_OUT:-docs/smoke-result.json}"
CSV="${SMOKE_CSV:-$ROOT/examples/oem_fragebogen_kurz.csv}"
BELEGT="${BELEGT_BIN:-$(command -v belegt || echo "$HOME/.local/bin/belegt")}"

"$BELEGT" serve --dev-auth --port "$PORT" --log-level warning >/tmp/belegt-smoke-server.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do curl -fs "$BASE/healthz" >/dev/null 2>&1 && break; sleep 0.5; done
curl -fs "$BASE/healthz" >/dev/null

PREP="$(python3 scripts/smoke/prepare_backend.py "$BASE" "$CSV")"
echo "prepare: $PREP"
REQ="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["request_id"])' "$PREP")"

BELEGT_SMOKE_API="$BASE" BELEGT_SMOKE_REQUEST="$REQ" BELEGT_SMOKE_OUT="$OUT" npx vitest run src/api/http.smoke.test.ts
echo "smoke result → $OUT"
