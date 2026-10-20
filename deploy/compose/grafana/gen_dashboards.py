#!/usr/bin/env python3
# Spec: 10 §5.2（DSH-01…DSH-06）、§5.1（指标名）、§5.3（SLO）
"""生成 Grafana 仪表盘 JSON（dashboards/dsh-0X-*.json）。改面板请改本脚本后重新运行：

    python3 deploy/compose/grafana/gen_dashboards.py

Helm chart 通过 ``files/dashboards`` 的副本把同样的 JSON 渲染为带 ``grafana_dashboard: "1"`` 标签的 ConfigMap。
标签禁止 org_id（10 §5.1）；按租户的成本明细从 ``obs.trace_span`` 聚合（PostgreSQL 数据源，见 DSH-03 说明面板）。
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards"
DS = {"type": "prometheus", "uid": "belegt-prom"}


class Board:
    def __init__(self, uid: str, title: str, desc: str) -> None:
        self.uid, self.title, self.desc = uid, title, desc
        self.panels: list[dict] = []
        self.y = 0
        self.x = 0
        self.pid = 1

    def _pos(self, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.x, self.y = 0, self.y + h
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        return pos

    def row(self, title: str) -> None:
        if self.x:
            self.x, self.y = 0, self.y + 8
        self.panels.append({"id": self.pid, "type": "row", "title": title, "collapsed": False,
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.pid += 1
        self.y += 1

    def add(self, title: str, exprs: list[tuple[str, str]], kind: str = "timeseries", unit: str = "short",
            w: int = 12, h: int = 8, desc: str = "", thresholds: list[tuple[str, float | None]] | None = None) -> None:
        p = {
            "id": self.pid, "type": kind, "title": title, "description": desc, "datasource": DS,
            "gridPos": self._pos(w, h),
            "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lf}
                        for i, (e, lf) in enumerate(exprs)],
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
            "options": {},
        }
        if thresholds:
            p["fieldConfig"]["defaults"]["thresholds"] = {
                "mode": "absolute", "steps": [{"color": c, "value": v} for c, v in thresholds]}
        if kind == "stat":
            p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                            "colorMode": "background", "graphMode": "none"}
        self.pid += 1
        self.panels.append(p)

    def text(self, title: str, md: str, w: int = 24, h: int = 4) -> None:
        self.panels.append({"id": self.pid, "type": "text", "title": title, "gridPos": self._pos(w, h),
                            "options": {"mode": "markdown", "content": md}})
        self.pid += 1

    def dump(self) -> dict:
        return {
            "uid": self.uid, "title": self.title, "description": self.desc, "tags": ["belegt", self.uid],
            "timezone": "Europe/Berlin", "schemaVersion": 39, "version": 1, "editable": False,
            "time": {"from": "now-24h", "to": "now"}, "refresh": "1m",
            "templating": {"list": [{
                "name": "datasource", "type": "datasource", "query": "prometheus", "current": {"text": "Prometheus",
                                                                                               "value": "belegt-prom"},
                "hide": 2}]},
            "annotations": {"list": []},
            "links": [{"title": "Runbooks", "type": "link", "url": "deploy/ops/alerts-runbook.md", "targetBlank": True}],
            "panels": self.panels,
        }


def boards() -> list[Board]:
    out = []
    # ---------------------------------------------------------------- DSH-01 平台健康
    b = Board("dsh-01", "DSH-01 平台健康", "各服务 RED/USE、PG 复制延迟、Temporal 积压（10 §5.2）")
    b.row("SLO")
    b.add("SLO-01 API 成功率（非 5xx 且 < 2 s，1 h）", [("1 - belegt:slo01_error_ratio:rate1h", "SLO-01")], "stat",
          "percentunit", 6, 4, thresholds=[("red", None), ("orange", 0.995), ("green", 0.999)])
    b.add("SLO-06 写入成功率（1 h）", [("1 - belegt:slo06_error_ratio:rate1h", "SLO-06")], "stat", "percentunit", 6, 4,
          thresholds=[("red", None), ("orange", 0.999), ("green", 0.9995)])
    b.add("SLO-02 读 ≤ 300 ms（30 d）", [("belegt:slo02_good_ratio:rate30d", "SLO-02")], "stat", "percentunit", 6, 4,
          thresholds=[("red", None), ("green", 0.99)])
    b.add("SLO-01 错误预算剩余（30 d）", [("1 - (1 - avg_over_time((1 - belegt:slo01_error_ratio:rate1h)[30d:1h])) / 0.001",
                                       "budget")], "stat", "percentunit", 6, 4,
          thresholds=[("red", None), ("orange", 0.5), ("green", 0.75)],
          desc="10 §7.3：耗尽 50% 冻结非紧急发布；耗尽 100% 每个发布需平台负责人批准")
    b.row("RED")
    b.add("API 请求速率（按状态类）", [("sum by (status_class) (rate(belegt_http_request_duration_seconds_count[5m]))",
                                 "{{status_class}}")], unit="reqps")
    b.add("API 延迟 P50/P95/P99", [
        ("histogram_quantile(0.5, sum by (le) (rate(belegt_http_request_duration_seconds_bucket[5m])))", "P50"),
        ("histogram_quantile(0.95, sum by (le) (rate(belegt_http_request_duration_seconds_bucket[5m])))", "P95"),
        ("histogram_quantile(0.99, sum by (le) (rate(belegt_http_request_duration_seconds_bucket[5m])))", "P99")], unit="s")
    b.add("证据图写入 P95（按类型/结果）", [("histogram_quantile(0.95, sum by (le, type, result) "
                                     "(rate(belegt_graph_write_duration_seconds_bucket[5m])))", "{{type}} {{result}}")],
          unit="s")
    b.add("证据图查询 P95（NFR-10）", [("histogram_quantile(0.95, sum by (le, query) "
                                   "(rate(belegt_graph_query_duration_seconds_bucket[5m])))", "{{query}}")], unit="s")
    b.row("数据层与编排")
    b.add("PG 复制延迟（ALR-08 > 60 s）", [("max(pg_replication_lag_seconds)", "lag")], unit="s",
          thresholds=[("green", None), ("red", 60)])
    b.add("PG 连接数", [("sum by (datname) (pg_stat_database_numbackends)", "{{datname}}")])
    b.add("Temporal schedule-to-start P95（ALR-07 > 30 s）", [(
        "histogram_quantile(0.95, sum by (le, task_queue) (rate(task_schedule_to_start_latency_bucket[5m])))",
        "{{task_queue}}")], unit="s")
    b.add("采集目标存活", [("up", "{{job}}")], "stat", "short", 12, 8, thresholds=[("red", None), ("green", 1)])
    out.append(b)

    # ---------------------------------------------------------------- DSH-02 问卷流水线
    b = Board("dsh-02", "DSH-02 问卷流水线", "Request.status 计数、阶段 P50/P95（10 §3.1）、最慢关口、awaiting_* 停留")
    b.add("首稿时长 P95（NFR-07：MVP ≤ 60 min，V1 ≤ 30 min）",
          [("belegt:first_draft_seconds:p95_1d", "{{deploy_profile}}")], unit="s",
          thresholds=[("green", None), ("orange", 1800), ("red", 3600)])
    b.add("首稿时长 P50", [("histogram_quantile(0.5, sum by (le) (rate(belegt_request_first_draft_seconds_bucket[1d])))",
                         "P50")], unit="s")
    b.add("阶段 P95（§3.1 时间预算）", [("histogram_quantile(0.95, sum by (le, stage) "
                                 "(rate(belegt_request_stage_duration_seconds_bucket[1h])))", "{{stage}}")], unit="s", w=24)
    b.add("阶段 P50", [("histogram_quantile(0.5, sum by (le, stage) (rate(belegt_request_stage_duration_seconds_bucket[1h])))",
                      "{{stage}}")], unit="s")
    b.add("最慢关口分布（按模板）", [("topk(5, histogram_quantile(0.95, sum by (le, stage, template_id) "
                               "(rate(belegt_request_stage_duration_seconds_bucket[1d]))))", "{{template_id}} / {{stage}}")],
          "bargauge", "s")
    b.add("文档解析页数（OCR / 文本层）", [("sum by (mime, ocr) (rate(belegt_docparse_pages_total[5m])) * 3600",
                                    "{{mime}} ocr={{ocr}}")], unit="short", desc="页/小时（10 §3.3）")
    out.append(b)

    # ---------------------------------------------------------------- DSH-03 LLM 成本与 token
    b = Board("dsh-03", "DSH-03 LLM 成本与 token", "按 Agent/模型/档位的 cost_eur、单份成本 P50/P95（目标 P95 ≤ €3，熔断 €5）")
    b.add("单份问卷成本 P95（24 h，NFR-11）", [("belegt:request_cost_eur:p95_24h", "P95")], "stat", "currencyEUR", 6, 4,
          thresholds=[("green", None), ("orange", 2.5), ("red", 3)])
    b.add("单份问卷成本 P50（24 h）", [("histogram_quantile(0.5, sum by (le) (rate(belegt_request_llm_cost_eur_bucket[24h])))",
                                 "P50")], "stat", "currencyEUR", 6, 4)
    b.add("今日 LLM 支出（按 org_tier）", [("belegt:llm_cost_eur:increase1d", "{{org_tier}}")], "stat", "currencyEUR", 12, 4)
    b.add("成本速率（按 Agent）", [("sum by (agent) (rate(belegt_llm_cost_eur_total[1h])) * 3600", "{{agent}}")],
          unit="currencyEUR", desc="€/小时")
    b.add("成本速率（按模型/档位）", [("sum by (model, tier) (rate(belegt_llm_cost_eur_total[1h])) * 3600",
                                "{{tier}} {{model}}")], unit="currencyEUR")
    b.add("token 速率（入/出）", [("sum by (direction) (rate(belegt_llm_tokens_total[5m])) * 60", "{{direction}}")],
          unit="short", desc="token/分钟（provider TPM 余量见 DSH-06）")
    b.add("LLM 调用延迟 P95（按 provider）", [("histogram_quantile(0.95, sum by (le, provider) "
                                        "(rate(belegt_llm_request_duration_seconds_bucket[5m])))", "{{provider}}")], unit="s")
    b.add("SLO-04 llm-proxy 成功率", [("belegt:slo04_success_ratio:rate1h", "SLO-04")], "stat", "percentunit", 12, 4,
          thresholds=[("red", None), ("green", 0.995)])
    b.text("按租户明细", "按租户的成本**不**进 Prometheus（禁止 `org_id` 标签，10 §5.1）。查询 `obs.trace_span`："
           "`SELECT org_id, request_id, sum(cost_eur) FROM obs.trace_span WHERE started_at > now() - interval '30 days' "
           "GROUP BY 1, 2 ORDER BY 3 DESC`（以 `belegt_auditor` 或运维只读账号执行）。见 deploy/ops/cost-dashboard.md。",
           12, 4)
    out.append(b)

    # ---------------------------------------------------------------- DSH-04 Agent 质量
    b = Board("dsh-04", "DSH-04 Agent 质量", "晋升率、ClosedQuestion 生成率、unmapped 率、PC-04 失败、影子评测")
    b.add("晋升结果（按规则）", [("sum by (rule, result) (rate(belegt_promotion_total[1h]))", "{{rule}} {{result}}")],
          unit="ops", w=24)
    b.add("PC-04 失败率（ALR-10 > 2%）", [("sum(rate(belegt_promotion_total{rule=\"PC-04\",result=\"fail\"}[1d])) / "
                                      "clamp_min(sum(rate(belegt_promotion_total{rule=\"PC-04\"}[1d])), 1e-9)", "PC-04")],
          "stat", "percentunit", 8, 4, thresholds=[("green", None), ("red", 0.02)])
    b.add("映射自动晋升率（PROMO-MAP-*）", [("sum(rate(belegt_promotion_total{rule=~\"PROMO-MAP.*\",result=\"promoted\"}[1d])) / "
                                      "clamp_min(sum(rate(belegt_promotion_total{rule=~\"PROMO-MAP.*\"}[1d])), 1e-9)", "")],
          "stat", "percentunit", 8, 4)
    b.add("证据自动核验率（EV-AUTO-*）", [("sum(rate(belegt_promotion_total{rule=~\"EV-AUTO.*\",result=\"promoted\"}[1d])) / "
                                    "clamp_min(sum(rate(belegt_promotion_total{rule=~\"EV-AUTO.*\"}[1d])), 1e-9)", "")],
          "stat", "percentunit", 8, 4)
    b.text("影子评测", "FR-004 影子评测 Top-1 与黄金集结果由 `belegt-bench` 写入评测报告（11）；下降 > 2 个百分点触发 ALR-10。")
    out.append(b)

    # ---------------------------------------------------------------- DSH-05 守卫与安全
    b = Board("dsh-05", "DSH-05 守卫与安全", "GP 拒绝、halted 原因、egress.block、注入标记、审计链校验")
    b.add("guard 决策（按检查/结果）", [("sum by (check, result) (rate(belegt_guard_decisions_total[5m]))",
                                   "{{check}} {{result}}")], unit="ops", w=24)
    b.add("halted（policy / budget）", [("sum by (result) (increase(belegt_guard_decisions_total{result=~\"halted.*\"}[1h]))",
                                      "{{result}}")], unit="short")
    b.add("外发结果（egress，ALR-11）", [("sum by (channel, result) (rate(belegt_egress_messages_total[5m]))",
                                    "{{channel}} {{result}}")], unit="ops")
    b.add("审计链断点（SLO-07 必须为 0）", [("max(belegt_audit_chain_breaks)", "breaks")], "stat", "short", 8, 4,
          thresholds=[("green", None), ("red", 1)])
    b.add("距上次审计链校验", [("time() - max(belegt_audit_chain_last_verified_timestamp_seconds)", "")], "stat", "s", 8, 4,
          thresholds=[("green", None), ("red", 7200)])
    b.add("RLS / 授权拒绝（authz.deny）", [("sum(rate(belegt_authz_denied_total[5m]))", "authz.deny")], "stat", "ops", 8, 4,
          desc="NFR-01 ②：生产 authz.deny 计数")
    out.append(b)

    # ---------------------------------------------------------------- DSH-06 容量与 FinOps
    b = Board("dsh-06", "DSH-06 容量与 FinOps", "DB/对象存储增长、provider TPM 余量、GPU 利用率、每租户基础设施成本（NFR-20）")
    b.add("PG 数据库大小", [("sum by (datname) (pg_database_size_bytes)", "{{datname}}")], unit="bytes")
    b.add("PG 存储增长（7 d 线性外推 30 d）", [("predict_linear(sum(pg_database_size_bytes)[7d:1h], 30 * 86400)", "30 d 后")],
          unit="bytes", desc="10 §8.6：PG 存储 > 70% 触发扩容")
    b.add("provider TPM 使用（入+出）", [("sum by (model) (rate(belegt_llm_tokens_total[1m])) * 60", "{{model}}")],
          unit="short", desc="峰值 > 配额 60% 触发扩容（10 §8.6）")
    b.add("GPU 利用率（DCGM，可选）", [("avg by (gpu) (DCGM_FI_DEV_GPU_UTIL)", "GPU {{gpu}}")], unit="percent",
          thresholds=[("orange", None), ("green", 20), ("red", 70)], desc="> 70% 扩卡，< 20% 缩卡；NFR-20 V2 目标 ≥ 40%")
    b.add("Temporal 积压（ALR-07 预警 > 5 s）", [(
        "histogram_quantile(0.95, sum by (le) (rate(task_schedule_to_start_latency_bucket[1h])))", "P95")], unit="s")
    b.text("FinOps", "每活跃租户基础设施成本 = 云账单 ÷ 活跃租户（月报，NFR-20：MVP ≤ €150，V1 ≤ €60，V2 ≤ €40）。"
           "账单不进 Prometheus，见 deploy/ops/cost-dashboard.md 的月报口径。", 12, 8)
    out.append(b)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    names = {"dsh-01": "platform-health", "dsh-02": "pipeline", "dsh-03": "llm-cost", "dsh-04": "agent-quality",
             "dsh-05": "guard-security", "dsh-06": "capacity-finops"}
    for b in boards():
        p = OUT / f"{b.uid}-{names[b.uid]}.json"
        p.write_text(json.dumps(b.dump(), ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(p.name, len(b.panels), "panels")


if __name__ == "__main__":
    main()
