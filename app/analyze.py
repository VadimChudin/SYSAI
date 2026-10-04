"""Transcript -> structured meeting report (summary, decisions, tasks with owners and deadlines)."""
import datetime as dt
import json

from . import config, deadlines, llm

SYSTEM = """Ты — опытный секретарь руководителя. По расшифровке совещания составь полный структурированный отчёт.
Требования:
- Пиши по-русски, деловым языком, конкретно. Ничего не выдумывай: только то, что прозвучало.
- Задачи: каждое поручение отдельно. Формулируй как действие («Подготовить…», «Согласовать…»).
  Исполнитель — только если он явно назван или очевиден из контекста. Сопоставь со справочником сотрудников
  (по имени, псевдонимам, должности) и укажи employee_id. Если не уверен — employee_id null и assignee_name как прозвучало.
- Срок: если назван («до пятницы», «к 15-му», «через неделю») — переведи в дату YYYY-MM-DD относительно даты
  совещания {meeting_date} ({weekday}) и процитируй в deadline_quote. Если срок не назван — deadline null.
- Для задач и тем укажи время в записи (MM:SS) и короткую цитату-основание.
- Решения — то, о чём договорились. Открытые вопросы — что осталось нерешённым. Риски — проблемы и опасения.
- Участники: сопоставь метки говорящих («Спикер 1») с людьми, если это понятно из разговора.
Справочник сотрудников (JSON): {employees}
Термины компании: {glossary}
Верни ТОЛЬКО JSON по схеме:
{{"title": str, "summary": str (4-8 предложений), "participants": [{{"speaker": str, "name": str, "employee_id": int|null, "role": str}}],
 "topics": [{{"title": str, "summary": str, "notes": [str], "time": "MM:SS"}}],
 "decisions": [{{"text": str, "time": "MM:SS"}}],
 "tasks": [{{"title": str, "description": str, "employee_id": int|null, "assignee_name": str|null,
            "deadline": "YYYY-MM-DD"|null, "deadline_quote": str|null, "priority": "high"|"medium"|"low",
            "time": "MM:SS", "quote": str}}],
 "open_questions": [str], "risks": [str], "notes": [str], "next_meeting": str|null}}"""


def mmss(sec: float) -> str:
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def transcript_text(segments: list) -> str:
    return "\n".join(f"[{mmss(s['start'])}] {s['speaker']}: {s['text']}" for s in segments)


def employees_json(employees) -> str:
    return json.dumps([{"employee_id": e.id, "name": e.name, "aliases": e.alias_list(), "position": e.position}
                       for e in employees], ensure_ascii=False)


def analyze(segments: list, employees, settings: dict, meeting_date: dt.date) -> dict:
    if not config.llm_enabled():
        return mock_report(employees, meeting_date)
    names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    system = SYSTEM.format(meeting_date=meeting_date.isoformat(), weekday=names[meeting_date.weekday()],
                           employees=employees_json(employees), glossary=settings.get("glossary", ""))
    text = llm.chat(settings["report_model"], [
        {"role": "system", "content": system},
        {"role": "user", "content": "Расшифровка совещания:\n" + transcript_text(segments)},
    ], json_mode=True, temperature=0.1, max_tokens=16000)
    return normalize(llm.parse_json(text), employees)


def normalize(r: dict, employees) -> dict:
    ids = {e.id for e in employees}
    out = {k: r.get(k) or [] for k in ("participants", "topics", "decisions", "tasks", "open_questions", "risks",
                                       "notes")}
    out["title"] = (r.get("title") or "Совещание").strip()
    out["summary"] = (r.get("summary") or "").strip()
    out["next_meeting"] = r.get("next_meeting")
    tasks = []
    for t in out["tasks"]:
        if not isinstance(t, dict) or not t.get("title"):
            continue
        eid = t.get("employee_id")
        try:
            eid = int(eid) if eid is not None else None
        except (TypeError, ValueError):
            eid = None
        d = None
        if t.get("deadline"):
            try:
                d = dt.date.fromisoformat(str(t["deadline"])[:10])
            except ValueError:
                d = deadlines.parse(str(t["deadline"]))
        tasks.append({"title": str(t["title"]).strip(), "description": str(t.get("description") or "").strip(),
                      "employee_id": eid if eid in ids else None,
                      "assignee_name": (t.get("assignee_name") or "").strip(),
                      "deadline": d.isoformat() if d else None, "deadline_quote": t.get("deadline_quote") or "",
                      "priority": t.get("priority") if t.get("priority") in ("high", "medium", "low") else "medium",
                      "time": t.get("time") or "", "quote": t.get("quote") or ""})
    out["tasks"] = tasks
    return out


def mock_report(employees, meeting_date: dt.date) -> dict:
    """Deterministic demo report used without an API key."""
    emp = {e.name.split()[0].lower(): e for e in employees}
    def eid(first):
        e = emp.get(first)
        return e.id if e else None
    friday = meeting_date + dt.timedelta(days=(4 - meeting_date.weekday()) % 7 or 7)
    return normalize({
        "title": "Планёрка по запуску продукта (демо)",
        "summary": "Обсудили запуск новой версии продукта, рекламную кампанию и бюджет на IV квартал. "
                   "Решили перенести релиз на неделю из-за незакрытых ошибок в оплате. Маркетинг готовит "
                   "креативы к пятнице, бухгалтерия — сверку бюджета. Открытым остался вопрос с подрядчиком "
                   "по дизайну.",
        "participants": [{"speaker": "Спикер 1", "name": "Директор", "employee_id": None, "role": "ведущий"},
                         {"speaker": "Спикер 2", "name": "Иван", "employee_id": eid("иван"), "role": "разработка"},
                         {"speaker": "Спикер 3", "name": "Мария", "employee_id": eid("мария"), "role": "маркетинг"}],
        "topics": [{"title": "Релиз новой версии", "summary": "Есть две критичные ошибки в оплате.",
                    "notes": ["Ошибки воспроизводятся на iOS", "Тестирование займёт 3 дня"], "time": "00:15"},
                   {"title": "Рекламная кампания", "summary": "Запуск после релиза, креативы нужны заранее.",
                    "notes": ["Бюджет на тест — 1 500 $", "Каналы: Telegram Ads и таргет"], "time": "02:40"}],
        "decisions": [{"text": "Перенести релиз на одну неделю", "time": "01:50"},
                      {"text": "Тестовый бюджет рекламы — 1 500 $", "time": "03:30"}],
        "tasks": [{"title": "Исправить ошибки оплаты на iOS", "description": "Две критичные ошибки, затем регресс.",
                   "employee_id": eid("иван"), "assignee_name": "Иван", "deadline": friday.isoformat(),
                   "deadline_quote": "до пятницы", "priority": "high", "time": "01:05",
                   "quote": "Иван, ошибки с оплатой нужно закрыть до пятницы."},
                  {"title": "Подготовить креативы для рекламной кампании", "description": "3 варианта баннеров и тексты.",
                   "employee_id": eid("мария"), "assignee_name": "Мария", "deadline": None, "deadline_quote": "",
                   "priority": "medium", "time": "03:10", "quote": "Мария, с тебя креативы."},
                  {"title": "Найти альтернативного подрядчика по дизайну", "description": "",
                   "employee_id": None, "assignee_name": "", "deadline": None, "deadline_quote": "",
                   "priority": "low", "time": "04:20", "quote": "Кто-то должен поискать замену подрядчику."}],
        "open_questions": ["Продлевать ли договор с текущим подрядчиком по дизайну?"],
        "risks": ["Перенос релиза сдвигает рекламную кампанию"],
        "notes": ["Следующая планёрка — в понедельник в 10:00"],
        "next_meeting": "Понедельник, 10:00",
    }, employees)
