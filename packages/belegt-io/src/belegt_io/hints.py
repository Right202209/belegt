# Spec: 03 §3.3（answer_format、unit_requested、period_requested）；02 §3.3（period_type）；04 §2.3（确定性版面切分）
"""题干启发式：语言检测、期望值类型、单位与期间提示。全部确定性，不调用 LLM。"""
from __future__ import annotations

import re
from datetime import date

from .models import Period
from .numbers import find_unit_in_text

_STOPWORDS: dict[str, set[str]] = {
    "de": {"der", "die", "das", "und", "ist", "sind", "nicht", "ihr", "ihre", "ihres", "ihrer", "sie", "wie", "für",
           "mit", "von", "im", "in", "den", "dem", "des", "ein", "eine", "einen", "bitte", "werden", "wird", "haben",
           "hat", "gibt", "es", "zu", "auf", "aus", "über", "bei", "oder", "welche", "welcher", "unternehmen",
           "geben", "an", "angaben", "verfügt", "liegt", "jahr", "gesamt", "nach", "durch"},
    "en": {"the", "and", "is", "are", "not", "your", "you", "how", "for", "with", "of", "in", "a", "an", "please",
           "will", "has", "have", "does", "do", "there", "to", "on", "from", "about", "or", "which", "company",
           "provide", "state", "total", "year", "what", "by", "per"},
    "fr": {"le", "la", "les", "et", "est", "sont", "pas", "votre", "vous", "comment", "pour", "avec", "de", "du",
           "des", "un", "une", "veuillez", "société", "entreprise", "quelle", "quel", "au", "aux"},
    "it": {"il", "lo", "la", "gli", "le", "e", "è", "sono", "non", "vostra", "vostro", "come", "per", "con", "di",
           "del", "della", "un", "una", "si", "prega", "azienda", "quale"},
    "es": {"el", "la", "los", "las", "y", "es", "son", "no", "su", "usted", "cómo", "para", "con", "de", "del",
           "un", "una", "por", "favor", "empresa", "cuál"},
    "pl": {"i", "jest", "są", "nie", "pan", "pani", "jak", "dla", "z", "w", "na", "proszę", "firma", "czy", "który"},
}


def detect_language(text: str) -> tuple[str, float]:
    """停用词 + 字符特征打分；返回 (ISO 639-1, 置信度)。无法判定返回 ('und', 0)。"""
    if not text or not text.strip():
        return "und", 0.0
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh", 0.9
    words = re.findall(r"[a-zäöüßàâçéèêëîïôûùüÿñæœąćęłńóśźżčřšžě]+", text.lower())
    if not words:
        return "und", 0.0
    scores = {lang: sum(1 for w in words if w in sw) for lang, sw in _STOPWORDS.items()}
    lower = text.lower()
    if re.search(r"[äöüß]", lower):
        scores["de"] += 2
    if re.search(r"[ąćęłńśźż]", lower):
        scores["pl"] += 3
    if re.search(r"[ñ¿¡]", lower):
        scores["es"] += 2
    if re.search(r"[çœ]|\b(?:qu'|l'|d')", lower):
        scores["fr"] += 1
    # 德语名词合成词特征
    if re.search(r"(?:ung|keit|heit|schaft)\b", lower):
        scores["de"] += 1
    best = max(scores, key=lambda k: scores[k])
    total = sum(scores.values())
    if scores[best] == 0:
        return "und", 0.0
    conf = scores[best] / total if total else 0.0
    conf = min(0.99, 0.5 * conf + 0.5 * min(1.0, scores[best] / 3))
    return best, round(conf, 2)


def dominant_language(texts: list[str]) -> str:
    counts: dict[str, float] = {}
    for t in texts:
        lang, conf = detect_language(t)
        if lang != "und":
            counts[lang] = counts.get(lang, 0) + conf
    return max(counts, key=lambda k: counts[k]) if counts else "und"


# —— 期望值类型 ——

_BOOL_START = re.compile(
    r"^\s*(?:\d+[.)]\s*)?(?:ist|sind|hat|haben|verfügt|verfügen|gibt\s+es|liegt|liegen|werden|wird|enthält|enthalten|"
    r"besitzt|besitzen|setzt|setzen|führt|führen|nutzt|nutzen|kennen|kennt|können|kann|liefern|liefert|beziehen|bezieht|"
    r"erfassen|erfasst|betreiben|betreibt|planen|plant|verwenden|verwendet|zahlen|zahlt|existiert|existieren|"
    r"is|are|does|do|has|have|did|was|were|can|will)\b",
    re.IGNORECASE,
)
_FILE_RE = re.compile(r"\b(?:hochladen|laden\s+sie|beifügen|beilegen|anhängen|upload|attach|Kopie\s+des|copy\s+of)\b", re.I)
_DATE_RE = re.compile(r"\b(?:Datum|datum|gültig\s+bis|Gültigkeit|Ablaufdatum|läuft\s+ab|date|valid\s+until|expiry|expiration|seit\s+wann)\b", re.I)
_GEO_RE = re.compile(r"\b(?:Koordinaten|Geolokali[sz]ierung|Geolocation|GPS|Breitengrad|Längengrad|latitude|longitude|GeoJSON)\b", re.I)
_NUM_RE = re.compile(
    r"\b(?:wie\s+(?:viel|viele|hoch|groß)|Anzahl|Menge|Höhe|Summe|Gesamt\w*|Verbrauch|Emissionen|Anteil|Prozent|"
    r"Umsatz|Mitarbeitende|Mitarbeiter|Beschäftigte|Fläche|how\s+(?:much|many)|number\s+of|amount|total|"
    r"quantity|share|percentage|emissions|consumption|revenue|headcount)\b",
    re.I,
)
_TABLE_RE = re.compile(r"\b(?:listen\s+sie|Liste\s+(?:aller|der)|list\s+(?:all|the)|tabelle|table\s+below)\b", re.I)


def guess_answer_format(text: str, unit: str | None = None, options: list[str] | None = None) -> tuple[str, float]:
    """返回 (answer_format, 置信度)。选项存在时优先 enum/bool。"""
    if options:
        norm = {o.strip().lower() for o in options}
        if norm and norm <= {"ja", "nein", "yes", "no", "j", "n", "true", "false", "wahr", "falsch"} \
                and norm & {"ja", "yes", "true", "wahr"} and norm & {"nein", "no", "false", "falsch", "n"}:
            return "bool", 0.9
        return "enum", 0.9
    if _GEO_RE.search(text):
        return "geo", 0.8
    if _FILE_RE.search(text):
        return "file", 0.8
    if _BOOL_START.match(text) and text.strip().endswith("?"):
        return "bool", 0.7
    if unit:
        return "number", 0.85
    if _DATE_RE.search(text):
        return "date", 0.75
    if _TABLE_RE.search(text):
        return "table", 0.6
    if _NUM_RE.search(text):
        return "number", 0.6
    return "text", 0.5


def guess_unit(text: str, unit_cell: str | None = None) -> tuple[str | None, list[str]]:
    from .numbers import normalize_unit

    if unit_cell:
        code, reasons = normalize_unit(unit_cell)
        if code:
            return code, reasons
    return find_unit_in_text(text or "")


# —— 期间 ——

_PERIOD_CTX = re.compile(
    r"(?P<kind>Berichtsjahr|Berichtszeitraum|Geschäftsjahr|Kalenderjahr|Jahr|reporting\s+year|reporting\s+period|"
    r"fiscal\s+year|financial\s+year|calendar\s+year|FY|year|GJ|im\s+Jahr|in)\s*[:=]?\s*(?P<y>(?:19|20)\d{2})(?!\d)",
    re.I,
)
_DATE_RANGE = re.compile(
    r"(?P<d1>\d{1,2})\.(?P<m1>\d{1,2})\.(?P<y1>(?:19|20)\d{2})\s*(?:–|-|bis|to)\s*"
    r"(?P<d2>\d{1,2})\.(?P<m2>\d{1,2})\.(?P<y2>(?:19|20)\d{2})"
)
_ISO_RANGE = re.compile(r"(?P<a>(?:19|20)\d{2}-\d{2}-\d{2})\s*(?:–|-|bis|to|/)\s*(?P<b>(?:19|20)\d{2}-\d{2}-\d{2})")


def guess_period(text: str) -> tuple[Period | None, str | None]:
    """返回 (Period, period_type)。只在有明确语境时给出，不猜测财年起止（财年由 Organization 设置）。"""
    if not text:
        return None, None
    m = _DATE_RANGE.search(text)
    if m:
        try:
            p = Period(start=date(int(m["y1"]), int(m["m1"]), int(m["d1"])),
                       end=date(int(m["y2"]), int(m["m2"]), int(m["d2"])))
            return p, "reporting_period"
        except ValueError:
            pass
    m = re.search(r"\b(?:zum|per|Stand|Stichtag|as\s+of|at)\s+(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>(?:19|20)\d{2})", text, re.I)
    if m:
        try:
            d = date(int(m["y"]), int(m["m"]), int(m["d"]))
            return Period(start=d, end=d), "point_in_time"
        except ValueError:
            pass
    m = _ISO_RANGE.search(text)
    if m:
        try:
            return Period(start=date.fromisoformat(m["a"]), end=date.fromisoformat(m["b"])), "reporting_period"
        except ValueError:
            pass
    m = _PERIOD_CTX.search(text)
    if m:
        y = int(m["y"])
        kind = m["kind"].lower()
        if kind == "gj" and re.search(r"\d\s*GJ\b", text):
            return None, None  # Gigajoule 而非 Geschäftsjahr
        if "geschäfts" in kind or "fiscal" in kind or "financial" in kind or kind in ("fy", "gj"):
            # 财年起止由 Organization 决定：只给提示，period 留空
            return None, "fiscal_year"
        ptype = "calendar_year" if ("kalender" in kind or "calendar" in kind) else "reporting_period"
        return Period(start=date(y, 1, 1), end=date(y, 12, 31)), ptype
    if re.search(r"\b(?:gültig\s+bis|valid\s+until|Ablaufdatum|expiry)\b", text, re.I):
        return None, "validity_range"
    if re.search(r"\b(?:zum\s+Stichtag|per\s+31\.12\.|as\s+of|Stand)\b", text, re.I):
        return None, "point_in_time"
    return None, None
