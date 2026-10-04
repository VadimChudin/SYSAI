"""Deadlines: business-day defaults and parsing of free-text answers in Russian ("до пятницы", "3 дня", "15.10")."""
import datetime as dt
import re
import zoneinfo

from . import config

WEEKDAYS = {"понедельник": 0, "пн": 0, "вторник": 1, "вт": 1, "сред": 2, "ср": 2, "четверг": 3, "чт": 3,
            "пятниц": 4, "пт": 4, "суббот": 5, "сб": 5, "воскресень": 6, "вс": 6}
MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
          "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}


def today() -> dt.date:
    return dt.datetime.now(zoneinfo.ZoneInfo(config.TIMEZONE)).date()


def add_business_days(start: dt.date, n: int) -> dt.date:
    d = start
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def parse(text: str, base: dt.date | None = None) -> dt.date | None:
    """Best-effort parser; returns None when unsure (the bot then asks again)."""
    base = base or today()
    t = text.lower().strip().replace("ё", "е")
    if not t:
        return None
    if re.search(r"\bсегодня\b", t):
        return base
    if re.search(r"\bпослезавтра\b", t):
        return base + dt.timedelta(days=2)
    if re.search(r"\bзавтра\b", t):
        return base + dt.timedelta(days=1)
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else base.year
        y = y + 2000 if y < 100 else y
        try:
            res = dt.date(y, mo, d)
            if not m.group(3) and res < base:
                res = dt.date(y + 1, mo, d)
            return res
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})\s+([а-я]+)", t)
    if m:
        for stem, mo in MONTHS.items():
            if m.group(2).startswith(stem) and not (stem == "ма" and not m.group(2).startswith("мая")):
                try:
                    res = dt.date(base.year, mo, int(m.group(1)))
                    return res if res >= base else dt.date(base.year + 1, mo, int(m.group(1)))
                except ValueError:
                    return None
    m = re.search(r"(\d+)\s*(раб\w*\s*)?(дн|день|дня|недел|нед|месяц|мес|час)", t)
    if m:
        n = int(m.group(1)); unit = m.group(3)
        if unit.startswith("час"):
            return base
        if unit.startswith("нед"):
            return base + dt.timedelta(weeks=n)
        if unit.startswith("мес"):
            return base + dt.timedelta(days=30 * n)
        return add_business_days(base, n) if m.group(2) else base + dt.timedelta(days=n)
    if re.search(r"\bнедел", t):
        return base + dt.timedelta(weeks=1)
    if re.search(r"\bмесяц", t):
        return base + dt.timedelta(days=30)
    for stem, wd in WEEKDAYS.items():
        pattern = r"\b" + stem + (r"\b" if len(stem) == 2 else "")  # "чт" must not match "что"
        if re.search(pattern, t):
            delta = (wd - base.weekday()) % 7 or 7
            if "следующ" in t:
                delta += 7 if delta < 7 else 0
            return base + dt.timedelta(days=delta)
    return None


def fmt(d: dt.date | None) -> str:
    if not d:
        return "без срока"
    names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    return f"{d.strftime('%d.%m.%Y')} ({names[d.weekday()]})"
