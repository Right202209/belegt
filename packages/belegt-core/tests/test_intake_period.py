# Spec: 04 §2.2 intake-triage（报告期：模型未给出时取问卷中第一道带期间的题；首题可能没有 period_requested）
"""回归：首题无 ``period_requested`` 时 intake 不崩（曾经直接取 ``questions[0].period_requested.start``）。"""
from __future__ import annotations

from agentkit import new_env
from belegt_core.agents import IntakeInput, IntakeTriage
from belegt_core.llm.mock import MockLLM

NO_PERIOD_LLM = {"triage": {"is_questionnaire": True, "title": "Fragebogen", "language": "de", "due_date": None,
                            "reporting_period": None, "priority": "normal", "confidence": 0.95}}


def _intake(env, csv_text: str, idem: str):
    sha = env.store.put_blob(env.org_id, csv_text.encode("utf-8"), "text/csv")
    out = IntakeTriage(env.svc).run(IntakeInput([sha], channel="email", subject="Fragebogen", body="anbei",
                                                sender="einkauf@oem-a.example", filenames=["f.csv"]), idem_prefix=idem)
    return out, env.store.get(out.refs[0])


def test_first_question_without_period_uses_first_with_period():
    env = new_env(llm=MockLLM(script=NO_PERIOD_LLM))
    csv_text = ("Nr.;Frage;Antwort;Einheit\n1;Firmenname;;\n2;Anzahl Mitarbeitende zum 31.12.2025;;Anzahl\n"
                "3;Scope-1-Emissionen im Berichtsjahr 2025;;tCO2e\n")
    out, req = _intake(env, csv_text, "t:ip1")
    assert out.details["question_count"] == 3
    assert req["reporting_period"] == {"start": "2025-01-01", "end": "2025-12-31"}  # 优先取区间（非时点）期间


def test_point_in_time_only_period():
    env = new_env(llm=MockLLM(script=NO_PERIOD_LLM))
    out, req = _intake(env, "Nr.;Frage;Antwort;Einheit\n1;Firmenname;;\n2;Anzahl Mitarbeitende zum 31.12.2025;;Anzahl\n", "t:ip2")
    assert req["reporting_period"]["end"] == "2025-12-31"


def test_no_period_anywhere_does_not_crash():
    env = new_env(llm=MockLLM(script=NO_PERIOD_LLM))
    out, req = _intake(env, "Nr.;Frage;Antwort;Einheit\n1;Firmenname;;\n2;Rechtsform;;\n", "t:ip3")
    assert out.outcome == "received" and "reporting_period" not in req


def test_rule_fallback_with_first_question_without_period():
    env = new_env(llm=MockLLM(fail_tasks={"triage"}))
    out, req = _intake(env, "Nr.;Frage;Antwort;Einheit\n1;Firmenname;;\n2;Stromverbrauch im Berichtsjahr 2025;;MWh\n", "t:ip4")
    assert out.details["rule_based"] and req["reporting_period"] == {"start": "2025-01-01", "end": "2025-12-31"}
