# Spec: 06 §6（法律承诺条款识别：双通道、强制处理、德语例句）；FR-406
"""法律承诺条款识别（LC-01…LC-08）。

    from belegt_core.legal import detect_legal_clauses
    det = detect_legal_clauses("Der Lieferant sichert zu, dass …", classifier=my_llm_classifier)  # det.flagged, det.types
"""
from .detector import (LEGAL_LLM_THRESHOLD, LC_TYPES, LegalClassifier, LegalDetection, PatternHit, ScriptedClassifier,
                       detect_legal_clauses, load_patterns, notice_text)
from .enforce import cancel_legal_flag, enforce_on_answer, flag_question
from .flags import LEGAL_CANCELLED_TAG, legal_tags, legal_types, question_legal_flag

__all__ = [
    "detect_legal_clauses", "LegalDetection", "PatternHit", "LegalClassifier", "ScriptedClassifier", "load_patterns",
    "LC_TYPES", "LEGAL_LLM_THRESHOLD", "notice_text", "enforce_on_answer", "flag_question", "cancel_legal_flag",
    "question_legal_flag", "legal_types", "legal_tags", "LEGAL_CANCELLED_TAG",
]
