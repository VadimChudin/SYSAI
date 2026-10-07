"""Report rendering: designed PDF (WeasyPrint), and Telegram HTML messages."""
import datetime as dt
import html
import pathlib
import zoneinfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config, deadlines
from .analyze import mmss

TPL = Environment(loader=FileSystemLoader(pathlib.Path(__file__).parent / "templates"),
                  autoescape=select_autoescape(["html"]))
PRIORITY = {"high": "Высокий", "medium": "Средний", "low": "Низкий"}
PRIORITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}
TG_LIMIT = 4000


def e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def context(meeting, settings: dict) -> dict:
    r = meeting.report or {}
    return dict(m=meeting, r=r, tasks=meeting.tasks, s=settings, mmss=mmss, fmt=deadlines.fmt,
                priority=PRIORITY, generated=dt.datetime.now(zoneinfo.ZoneInfo(config.TIMEZONE)).strftime("%d.%m.%Y %H:%M"),
                include_transcript=settings.get("include_transcript_in_pdf"), segments=meeting.transcript or [])


def report_html(meeting, settings: dict) -> str:
    return TPL.get_template("report.html").render(**context(meeting, settings))


def report_pdf(meeting, settings: dict) -> bytes:
    from weasyprint import HTML
    return HTML(string=report_html(meeting, settings), base_url=str(pathlib.Path(__file__).parent)).write_pdf()


def pdf_name(meeting) -> str:
    d = (meeting.meeting_date or dt.date.today()).strftime("%Y-%m-%d")
    return f"Отчёт_{d}_{meeting.id}.pdf"


def _clip(text: str, limit: int = TG_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit - 40].rsplit("\n", 1)[0] + "\n…\n<i>Полностью — в PDF</i>"


def summary_message(meeting, settings: dict, draft: bool = False) -> str:
    r = meeting.report or {}
    d = (meeting.meeting_date or dt.date.today()).strftime("%d.%m.%Y")
    lines = []
    if draft:
        lines.append("📝 <b>ЧЕРНОВИК — проверьте перед отправкой</b>\n")
    lines.append(f"📋 <b>{e(r.get('title') or meeting.title)}</b>")
    lines.append(f"🗓 {d} · ⏱ {mmss(meeting.duration_sec)}")
    if r.get("summary"):
        lines += ["", "<b>Кратко</b>", e(r["summary"])]
    if r.get("decisions"):
        lines += ["", "<b>✅ Решения</b>"] + [f"• {e(x.get('text') if isinstance(x, dict) else x)}" for x in r["decisions"]]
    if meeting.tasks:
        lines += ["", "<b>📌 Задачи</b>"]
        for t in meeting.tasks:
            who = t.assignee.name if t.assignee else (t.assignee_name or "❗️не назначен")
            lines.append(f"{PRIORITY_ICON.get(t.priority, '•')} {e(t.title)} — <b>{e(who)}</b>, {e(deadlines.fmt(t.deadline))}")
    if r.get("open_questions"):
        lines += ["", "<b>❓ Открытые вопросы</b>"] + [f"• {e(x)}" for x in r["open_questions"]]
    if r.get("risks"):
        lines += ["", "<b>⚠️ Риски</b>"] + [f"• {e(x)}" for x in r["risks"]]
    if r.get("next_meeting"):
        lines += ["", f"📅 Следующая встреча: {e(r['next_meeting'])}"]
    return _clip("\n".join(lines))


def task_message(task, meeting) -> str:
    r = meeting.report or {}
    parts = [f"📌 <b>Новая задача по итогам совещания</b>", f"«{e(r.get('title') or meeting.title)}»", "",
             f"<b>{e(task.title)}</b>"]
    if task.description:
        parts.append(e(task.description))
    parts += ["", f"Срок: <b>{e(deadlines.fmt(task.deadline))}</b>",
              f"Приоритет: {PRIORITY_ICON.get(task.priority, '')} {PRIORITY.get(task.priority, '')}"]
    if task.source_quote:
        parts += ["", f"<i>Из совещания [{e(task.source_time)}]: «{e(task.source_quote)}»</i>"]
    return _clip("\n".join(parts))


def tasks_digest(tasks, meeting) -> str:
    """One message with all tasks of a person from one meeting."""
    person = tasks[0].assignee if tasks else None
    name = person.name.split()[0] if person and person.name else ""
    greeting = f"Здравствуйте{', ' + e(name) if name else ''}! Я SYSAI, помощник по поручениям.\n\n"
    if len(tasks) == 1:
        return _clip(greeting + task_message(tasks[0], meeting))
    r = meeting.report or {}
    parts = [f"📌 <b>Ваши задачи по итогам совещания</b>", f"«{e(r.get('title') or meeting.title)}»", ""]
    for i, t in enumerate(tasks, 1):
        parts.append(f"{i}. {PRIORITY_ICON.get(t.priority, '')} <b>{e(t.title)}</b> — срок {e(deadlines.fmt(t.deadline))}")
        if t.description:
            parts.append(f"   {e(t.description)}")
    return _clip(greeting + "\n".join(parts))
