#!/bin/sh
# Spec: 10 §6.6（备份、恢复与灾备：private-compose 默认每晚备份 RPO ≤ 24 h；PITR 由 pgBackRest/WAL-G 承担）、
#       SLO-08（备份作业成功且最新可恢复点 ≤ RPO）、ALR-09；03 §6.7（对象存储布局，blobs/ 与 audit-packs/ 为 WORM）
#
# 逻辑备份：pg_dump（自定义格式，-Fc）每个数据库一份 + 清单（sha256），可选上传到 S3 兼容对象存储（备份桶开启 Object Lock 35 天）。
# 这是 private-compose / 小规模私有化的基线；saas-* 与 private-k8s 生产必须另配连续 WAL 归档（pgBackRest 或 WAL-G，PITR 35 天）。
#
# 用法：
#   PGHOST=… PGUSER=… PGPASSWORD=… ./backup.sh                       # 只写本地 BACKUP_DIR
#   BACKUP_UPLOAD=s3 BACKUP_S3_URI=s3://belegt-eu-dev-backup/pg ./backup.sh  # aws cli 上传（AWS_* 或 S3 端点变量）
#   BACKUP_UPLOAD=mc BACKUP_MC_TARGET=local/belegt-eu-dev-backup/pg ./backup.sh  # MinIO mc 上传
# 环境变量：
#   PGHOST PGPORT PGUSER PGPASSWORD   连接（超级用户或具备 pg_read_all_data 的备份账号）
#   PGDATABASES                       空格分隔，默认 "belegt temporal temporal_visibility"（Temporal 与证据图同一时刻备份 → 恢复点一致，10 §6.6）
#   BACKUP_DIR                        本地目录，默认 /backups
#   BACKUP_KEEP_LOCAL                 本地保留份数，默认 3
#   BACKUP_UPLOAD                     none | s3 | mc（默认 none）
#   BACKUP_S3_URI / BACKUP_S3_ENDPOINT / BACKUP_MC_TARGET
#   BACKUP_GPG_RECIPIENT              设置后用 gpg 公钥加密转储（备份离开主机前加密，07 §4.5）
#   BACKUP_PUSHGATEWAY_URL            设置后推送 belegt_backup_* 指标（ALR-09）
# 退出码：0 成功；非 0 失败（连续失败计数写入 $BACKUP_DIR/.consecutive_failures）。
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
PGDATABASES="${PGDATABASES:-belegt temporal temporal_visibility}"
BACKUP_KEEP_LOCAL="${BACKUP_KEEP_LOCAL:-3}"
BACKUP_UPLOAD="${BACKUP_UPLOAD:-none}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/$TS"
FAIL_FILE="$BACKUP_DIR/.consecutive_failures"

log() { printf '%s backup %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

push_metrics() {
  [ -n "${BACKUP_PUSHGATEWAY_URL:-}" ] || return 0
  ok="$1"; fails="$(cat "$FAIL_FILE" 2>/dev/null || echo 0)"
  body="belegt_backup_consecutive_failures $fails"
  if [ "$ok" = 1 ]; then
    body="$body
belegt_backup_last_success_timestamp_seconds $(date -u +%s)"
  fi
  printf '%s\n' "$body" | curl -fsS --data-binary @- "$BACKUP_PUSHGATEWAY_URL/metrics/job/belegt_backup" >/dev/null 2>&1 || true
}

on_fail() {
  n="$(cat "$FAIL_FILE" 2>/dev/null || echo 0)"; echo $((n + 1)) > "$FAIL_FILE"
  log "FAILED (consecutive=$((n + 1)))"
  push_metrics 0
  rm -rf "$OUT.partial"
}
# shellcheck disable=SC2154  # rc 在 trap 字符串内赋值
trap 'rc=$?; [ $rc -eq 0 ] || on_fail; exit $rc' EXIT

mkdir -p "$OUT.partial"
umask 077
log "start → $OUT (dbs: $PGDATABASES)"
{
  echo "format: belegt-backup/1"
  echo "created_at: $TS"
  echo "pg_server: $(psql -XAtc 'show server_version' -d postgres)"
  echo "databases:"
} > "$OUT.partial/MANIFEST.yaml"

for db in $PGDATABASES; do
  if ! psql -XAtc "select 1 from pg_database where datname = '$db'" -d postgres | grep -q 1; then
    log "skip $db (not found)"
    continue
  fi
  f="$OUT.partial/$db.dump"
  # --no-owner/--no-acl 不用：恢复时需要保留 belegt_owner 所有权与 RLS/GRANT（03 §6.1、§6.5）
  pg_dump -d "$db" -Fc -Z 6 --quote-all-identifiers -f "$f"
  pg_restore -l "$f" > /dev/null          # 自检：目录可读
  if [ -n "${BACKUP_GPG_RECIPIENT:-}" ]; then
    gpg --batch --yes --trust-model always -r "$BACKUP_GPG_RECIPIENT" -o "$f.gpg" -e "$f" && rm -f "$f"
    f="$f.gpg"
  fi
  sum="$(sha256sum "$f" | cut -d' ' -f1)"
  printf '  - name: %s\n    file: %s\n    sha256: %s\n    bytes: %s\n' "$db" "$(basename "$f")" "$sum" "$(wc -c < "$f" | tr -d ' ')" \
    >> "$OUT.partial/MANIFEST.yaml"
  log "dumped $db ($(wc -c < "$f" | tr -d ' ') bytes)"
done

# 全局对象（角色定义，不含密码哈希之外的数据）：恢复到新实例时需要先建角色
pg_dumpall --globals-only --no-role-passwords > "$OUT.partial/globals.sql"
printf '  - name: globals\n    file: globals.sql\n    sha256: %s\n' "$(sha256sum "$OUT.partial/globals.sql" | cut -d' ' -f1)" \
  >> "$OUT.partial/MANIFEST.yaml"
mv "$OUT.partial" "$OUT"

case "$BACKUP_UPLOAD" in
  none) ;;
  s3)
    : "${BACKUP_S3_URI:?BACKUP_S3_URI required for BACKUP_UPLOAD=s3}"
    ep=""; [ -n "${BACKUP_S3_ENDPOINT:-}" ] && ep="--endpoint-url $BACKUP_S3_ENDPOINT"
    # shellcheck disable=SC2086
    aws $ep s3 cp --recursive --only-show-errors "$OUT" "$BACKUP_S3_URI/$TS/"
    ;;
  mc)
    : "${BACKUP_MC_TARGET:?BACKUP_MC_TARGET required for BACKUP_UPLOAD=mc}"
    mc cp --recursive --quiet "$OUT/" "$BACKUP_MC_TARGET/$TS/"
    ;;
  *) log "unknown BACKUP_UPLOAD=$BACKUP_UPLOAD"; exit 2 ;;
esac

# 本地只保留最近 N 份（远端由备份桶生命周期 + Object Lock 35 天管理）
# shellcheck disable=SC2012  # 目录名固定为 UTC 时间戳
ls -1d "$BACKUP_DIR"/20*Z 2>/dev/null | sort | head -n -"$BACKUP_KEEP_LOCAL" | while read -r old; do rm -rf "$old"; done

echo 0 > "$FAIL_FILE"
push_metrics 1
log "done $TS"
