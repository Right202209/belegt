"""06 §6 法律承诺条款识别：模式库（每条正例/反例）、§6.4 德语例句、双通道 OR 融合、§6.3 强制处理与提示文案。"""
from __future__ import annotations

import pytest

from belegt_core.legal import (LC_TYPES, ScriptedClassifier, detect_legal_clauses, enforce_on_answer, flag_question,
                               legal_types, load_patterns, notice_text, question_legal_flag)

GERMAN_EXAMPLES = [  # 06 §6.4
    ("Der Lieferant sichert zu, dass die übermittelten Daten richtig und vollständig sind.", {"LC-01", "LC-06"}),
    ("Der Lieferant stellt den Auftraggeber von sämtlichen Ansprüchen Dritter frei, die aus fehlerhaften Angaben entstehen.", {"LC-02"}),
    ("Der Auftraggeber ist berechtigt, die Einhaltung durch Audits vor Ort zu überprüfen.", {"LC-03"}),
    ("Hiermit erkennen wir den Verhaltenskodex für Lieferanten der XY AG als verbindlich an.", {"LC-04"}),
    ("Wir verpflichten uns, unsere Scope-1- und Scope-2-Emissionen bis 2030 um 42 % zu reduzieren (SBTi).", {"LC-05"}),
    ("Wir bestätigen nach bestem Wissen und Gewissen die Richtigkeit der oben gemachten Angaben.", {"LC-06"}),
    ("Bei Verstoß gegen diese Verpflichtungen ist der Auftraggeber zur fristlosen Kündigung berechtigt.", {"LC-07"}),
    ("Änderungen der Zertifizierung sind dem Auftraggeber unverzüglich schriftlich mitzuteilen.", {"LC-08"}),
]


@pytest.mark.parametrize("text,types", GERMAN_EXAMPLES)
def test_german_examples(text, types):
    det = detect_legal_clauses(text, "de")
    assert det.flagged and types <= set(det.types), det.types
    assert det.channels == ["rule"] and det.patterns_version == "legal-patterns@1.0.0"


ENGLISH = [
    ("The supplier represents and warrants that all data is accurate.", "LC-01"),
    ("The supplier shall indemnify and hold harmless the buyer.", "LC-02"),
    ("The buyer has the right to audit the supplier's facilities.", "LC-03"),
    ("We acknowledge and accept the Supplier Code of Conduct.", "LC-04"),
    ("We commit to a 50% reduction of scope 1 emissions and net zero by 2040.", "LC-05"),
    ("We confirm that the information is true, accurate and complete.", "LC-06"),
    ("Liquidated damages apply in case of late delivery.", "LC-07"),
    ("The supplier shall promptly notify the buyer of any change.", "LC-08"),
]


@pytest.mark.parametrize("text,lc", ENGLISH)
def test_english_patterns(text, lc):
    assert lc in detect_legal_clauses(text, "en").types


@pytest.mark.parametrize("text", [
    "Wie hoch war Ihr Stromverbrauch 2025 in MWh?",
    "Bitte geben Sie die Anzahl der Mitarbeitenden zum 31.12. an.",
    "Haben Sie ein zertifiziertes Umweltmanagementsystem (ISO 14001)?",
    "What were your scope 1 emissions in 2025?",
    "Do you have a code of conduct?",
])
def test_ordinary_questions_not_flagged(text):
    assert not detect_legal_clauses(text).flagged


def test_pattern_library_examples_and_counterexamples():
    version, pats = load_patterns()
    assert version == "legal-patterns@1.0.0" and {p.type for p in pats} == set(LC_TYPES)
    for p in pats:
        for e in p.examples:
            assert p.regex.search(e), (p.id, e)
        for c in p.counterexamples:
            assert not any(x.regex.search(c) for x in pats), (p.id, c)


def test_dual_channel_or_fusion():
    # T-08：LLM p=0.22 < 0.30，但规则通道命中 → 标记
    t8 = detect_legal_clauses("Wir bestätigen nach bestem Wissen die Richtigkeit der Angaben.",
                              classifier=ScriptedClassifier({"LC-06": 0.22}))
    assert t8.flagged and t8.channels == ["rule"] and t8.llm_probs == {"LC-06": 0.22}
    # T-09：无关键词，LLM p(LC-02)=0.41 ≥ 0.30 → 标记
    t9 = detect_legal_clauses("Etwaige Kosten aus unrichtigen Angaben trägt der Lieferant.",
                              classifier=ScriptedClassifier({"LC-02": 0.41}))
    assert t9.flagged and t9.types == ["LC-02"] and t9.channels == ["llm"]
    # 阈值边界
    assert detect_legal_clauses("Neutraler Text.", classifier=ScriptedClassifier({"LC-03": 0.30})).flagged
    assert not detect_legal_clauses("Neutraler Text.", classifier=ScriptedClassifier({"LC-03": 0.29})).flagged
    # LLM 不可用 → 规则通道照常
    d = detect_legal_clauses("Der Lieferant gewährleistet die Einhaltung.", classifier=ScriptedClassifier(fail=True))
    assert d.flagged and d.llm_error and d.types == ["LC-01"]
    # I18nText 输入
    assert detect_legal_clauses({"de": "Kein Bezug.", "en": "We guarantee compliance."}).types == ["LC-01"]


def test_flag_and_enforce():
    det = detect_legal_clauses("Der Lieferant stellt den Auftraggeber von Ansprüchen frei.")
    qb = flag_question({"id": "q_x", "tags": ["oem"]}, det)
    assert question_legal_flag(qb) and legal_types(qb) == ["LC-02"] and "oem" in qb["tags"]
    out = enforce_on_answer({"legal_commitment": False}, [qb])
    assert out["patch"] == {"legal_commitment": True} and out["risk_level"] == "L3" and out["no_reuse"]
    assert out["drafting"]["default_value"] is None
    assert "Freistellung" in out["notice"]["de"] and "indemnity" in out["notice"]["en"] and "赔偿" in out["notice"]["zh"]
    assert enforce_on_answer({}, [{"tags": ["legal_cancelled", "legal:LC-02"]}])["risk_level"] is None


def test_notice_texts_match_spec():
    n = notice_text(["LC-02"])
    assert n["en"] == ("This question contains a legally binding statement (type: indemnity). Please read the original "
                       "text and approve it individually.")
    assert n["zh"] == "此题包含具有法律约束力的声明（类型：赔偿）。请阅读原文并单条确认。"
    assert n["de"].startswith("„Diese Frage enthält eine rechtlich bindende Erklärung (Typ: Freistellung).")
