#!/bin/sh
# Spec: 10 §6.6（恢复一致性：Temporal 与证据图同一恢复点；恢复后验收 = 审计哈希链校验 + 抽样 verify-pack + 合成问卷端到端）、
#       §6.5（contract 之后的回滚一律走恢复）、07 §9.1（季度恢复演练）
#
# 从 backup.sh 生成的目录恢复到一个**空**实例（默认拒绝覆盖已有数据库）。
# 用法：
#   PGHOST=… PGUSER=postgres PGPASSWORD=… ./restore.sh /backups/20261019T020000Z
#   RESTORE_FROM_S3=s3://belegt-eu-dev-backup/pg/20261019T020000Z ./restore.sh /tmp/restore   # 先下载
#   RESTORE_DROP_EXISTING=yes …                     # 演练环境：先删除同名数据库（生产禁止）
#   RESTORE_VERIFY_ORGS="org_… org_…"               # 恢复后对这些租户调用 audit.verify_chain()
# 步骤：①校验 MANIFEST sha256 ②恢复 globals（角色）③逐库 pg_restore ④自检（commit_version 属主、FORCE RLS）⑤审计链校验。
# 应用侧（停写、切流量、Temporal worker 重新连接）见 deploy/ops/incident-response-runbook.md「数据恢复」。
set -eu

SRC="${1:?usage: restore.sh <backup-dir>}"
log() { printf '%s restore %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [ -n "${RESTORE_FROM_S3:-}" ]; then
  mkdir -p "$SRC"
  ep=""; [ -n "${BACKUP_S3_ENDPOINT:-}" ] && ep="--endpoint-url $BACKUP_S3_ENDPOINT"
  # shellcheck disable=SC2086
  aws $ep s3 cp --recursive --only-show-errors "$RESTORE_FROM_S3" "$SRC/"
fi
[ -f "$SRC/MANIFEST.yaml" ] || { log "no MANIFEST.yaml in $SRC"; exit 2; }

# ① 完整性
awk '/file:/{f=$2} /sha256:/{print $2"  "f}' "$SRC/MANIFEST.yaml" > "$SRC/.sha256"
( cd "$SRC" && sha256sum -c --quiet .sha256 ) || { log "checksum mismatch — abort"; exit 3; }
log "manifest ok ($(grep -c 'file:' "$SRC/MANIFEST.yaml") files)"

# ② 角色（已存在的角色报错可忽略）
psql -X -v ON_ERROR_STOP=0 -q -d postgres -f "$SRC/globals.sql" >/dev/null 2>&1 || true
log "globals applied (passwords not included: re-run belegt-migrate or set role passwords from the secret store)"

# ③ 逐库恢复
for dump in "$SRC"/*.dump "$SRC"/*.dump.gpg; do
  [ -e "$dump" ] || continue
  f="$dump"
  if [ "${f%.gpg}" != "$f" ]; then
    gpg --batch --yes -o "${f%.gpg}" -d "$f"; f="${f%.gpg}"
  fi
  db="$(basename "$f" .dump)"
  exists="$(psql -XAtc "select 1 from pg_database where datname = '$db'" -d postgres)"
  if [ "$exists" = 1 ]; then
    if [ "${RESTORE_DROP_EXISTING:-no}" = yes ]; then
      log "drop existing $db"; psql -Xqc "drop database \"$db\" with (force)" -d postgres
    else
      log "database $db exists — refuse to overwrite (set RESTORE_DROP_EXISTING=yes on drill targets only)"; exit 4
    fi
  fi
  psql -Xqc "create database \"$db\"" -d postgres
  pg_restore -d "$db" --exit-on-error -j "${RESTORE_JOBS:-4}" "$f"
  log "restored $db"
done

# ④ 自检（与迁移 Job 相同的不变量）
check="$(psql -XAt -d "${BELEGT_DB_NAME:-belegt}" <<'SQL'
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                         WHERE n.nspname = 'graph' AND p.proname = 'commit_version' AND p.prosecdef
                           AND pg_get_userbyid(p.proowner) = 'belegt_owner')
             AND NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                             WHERE n.nspname = 'graph' AND c.relkind = 'p' AND c.relname LIKE '%\_v' AND NOT c.relforcerowsecurity)
            THEN 'ok' ELSE 'self-check failed' END;
SQL
)"
[ "$check" = ok ] || { log "schema self-check failed: $check"; exit 6; }
log "schema self-check ok"

# ⑤ 审计哈希链（SLO-07；不连续 = P1 安全事件，07 §9.4）
for org in ${RESTORE_VERIFY_ORGS:-}; do
  broken="$(psql -XAt -d "${BELEGT_DB_NAME:-belegt}" -c "select set_config('app.org_id', '$org', false); select count(*) from audit.verify_chain()" | tail -1)"
  if [ "$broken" != 0 ]; then log "audit chain BROKEN for $org ($broken events)"; exit 5; fi
  log "audit chain ok for $org"
done
log "done. 下一步：抽样 belegt-calc verify-pack 审计包 + 合成问卷端到端（10 §6.6 验收）"
