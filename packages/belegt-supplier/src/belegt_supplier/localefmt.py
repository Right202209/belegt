# Spec: 08 §3.2（`|date_long` 本地化、`period_label` 按 locale 格式化、列表折叠）
"""最小本地化格式化（不依赖 Babel）。支持 de/en/tr/zh，其余回退 en。"""
from __future__ import annotations

from datetime import date

SUPPORTED = ("de", "en", "tr", "zh")

_MONTHS = {
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
    "tr": ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"],
}


def base_locale(locale: str | None) -> str:
    loc = (locale or "en").replace("_", "-").split("-")[0].lower()
    return loc if loc in SUPPORTED else "en"


def date_long(d: date, locale: str) -> str:
    loc = base_locale(locale)
    if loc == "zh":
        return f"{d.year}年{d.month}月{d.day}日"
    m = _MONTHS[loc][d.month - 1]
    if loc == "de":
        return f"{d.day}. {m} {d.year}"
    return f"{d.day} {m} {d.year}"


def percent(value: str, locale: str) -> str:
    loc = base_locale(locale)
    if loc == "de":
        return f"{value} %"
    if loc == "tr":
        return f"%{value}"
    return f"{value}%"


def period_label(start: date, end: date, locale: str) -> str:
    loc = base_locale(locale)
    if start == date(start.year, 1, 1) and end == date(start.year, 12, 31):
        return {"de": f"Kalenderjahr {start.year}", "en": f"calendar year {start.year}",
                "tr": f"{start.year} takvim yılı", "zh": f"{start.year} 日历年度"}[loc]
    for q in range(4):
        qs = date(start.year, 3 * q + 1, 1)
        qe = [date(start.year, 3, 31), date(start.year, 6, 30), date(start.year, 9, 30), date(start.year, 12, 31)][q]
        if start == qs and end == qe:
            return {"de": f"Q{q + 1} {start.year}", "en": f"Q{q + 1} {start.year}",
                    "tr": f"{start.year} Ç{q + 1}", "zh": f"{start.year} 年第 {q + 1} 季度"}[loc]
    return f"{date_long(start, loc)} – {date_long(end, loc)}"


def fold_note(total: int, locale: str) -> str:
    loc = base_locale(locale)
    return {"de": f"… insgesamt {total} Positionen, Details im Link",
            "en": f"… {total} items in total, see the link for details",
            "tr": f"… toplam {total} kalem, ayrıntılar bağlantıda",
            "zh": f"……共 {total} 项，详见链接"}[loc]


def mass(value: str, unit: str, locale: str) -> str:
    loc = base_locale(locale)
    if loc in ("de", "tr"):
        value = value.replace(".", ",")
    return f"{value} {unit}"
