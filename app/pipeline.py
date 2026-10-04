"""Meeting pipeline: audio -> transcript -> report -> approval -> delivery, plus Telegram update handling."""
import datetime as dt
import logging
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import analyze, bitrix, config, deadlines, render, settings_store, telegram, transcribe
from .db import DeadlineRequest, Employee, Meeting, SessionLocal, Task, TelegramChat, now

log = logging.getLogger("sysai.pipeline")
POOL = ThreadPoolExecutor(max_workers=config.WORKERS, thread_name_prefix="sysai-job")
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".opus", ".webm", ".flac", ".aac", ".mp4", ".wma", ".amr"}


# ----------------------------------------------------------------------------- helpers
def _set(meeting_id: int, **kw):
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        for k, v in kw.items():
            setattr(m, k, v)
        s.commit()


def load(s, meeting_id: int) -> Meeting:
    m = s.get(Meeting, meeting_id)
    _ = [t.assignee for t in m.tasks]  # eager-load for rendering outside the session
    return m


def tg_safe(fn, *a, **kw):
    """Telegram errors must not crash the pipeline; they are logged and returned."""
    if not config.telegram_enabled():
        log.info("telegram off: %s %s", fn.__name__, a[:1])
        return None
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("telegram %s failed: %s", fn.__name__, e)
        return None


def submit(meeting_id: int):
    if config.TESTING:
        process(meeting_id)
    else:
        POOL.submit(process, meeting_id)


# ----------------------------------------------------------------------------- processing
def create_meeting(filename: str, audio_path: str = "", source: str = "web", title: str = "",
                   meeting_date: dt.date | None = None, options: dict | None = None) -> int:
    with SessionLocal() as s:
        m = Meeting(filename=filename, audio_path=audio_path, source=source, title=title, options=options or {},
                    meeting_date=meeting_date or deadlines.today(), status="queued", progress="В очереди")
        s.add(m)
        s.commit()
        return m.id


def meeting_settings(meeting_id: int) -> dict:
    with SessionLocal() as s:
        return settings_store.for_meeting(s.get(Meeting, meeting_id).options)


def process(meeting_id: int):
    settings = meeting_settings(meeting_id)
    try:
        with SessionLocal() as s:
            m = s.get(Meeting, meeting_id)
            audio_path, source, mdate = m.audio_path, m.source, m.meeting_date or deadlines.today()
        _set(meeting_id, status="transcribing", progress="Расшифровка записи", error="")
        if source == "demo" or not audio_path:
            total, segments = transcribe.mock_transcribe()
        else:
            total, segments = transcribe.transcribe(audio_path, settings,
                                                    progress=lambda p: _set(meeting_id, progress=p))
        _set(meeting_id, transcript=segments, duration_sec=total, status="analyzing",
             progress="Составление отчёта и задач")
        with SessionLocal() as s:
            employees = s.query(Employee).filter(Employee.active.is_(True)).all()
        report = analyze.analyze(segments, employees, settings, mdate)
        _save_report(meeting_id, report, settings)
        if audio_path and pathlib.Path(audio_path).exists():
            pathlib.Path(audio_path).unlink()  # audio is not kept after processing
            _set(meeting_id, audio_path="")
        if settings["approval_required"]:
            request_approval(meeting_id)
        else:
            deliver(meeting_id)
    except Exception as e:  # noqa: BLE001
        log.exception("meeting %s failed", meeting_id)
        _set(meeting_id, status="error", progress="Ошибка", error=str(e)[:2000])


def _save_report(meeting_id: int, report: dict, settings: dict):
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        m.report = report
        m.title = m.title or report.get("title", "")
        m.tasks.clear()
        for t in report.get("tasks", []):
            d = dt.date.fromisoformat(t["deadline"]) if t.get("deadline") else None
            src = "stated" if d else "none"
            if not d and settings["deadline_mode"] == "default":
                d, src = deadlines.add_business_days(deadlines.today(), int(settings["default_deadline_days"])), "default"
            m.tasks.append(Task(title=t["title"], description=t.get("description", ""), assignee_id=t.get("employee_id"),
                                assignee_name=t.get("assignee_name", ""), deadline=d, deadline_source=src,
                                deadline_quote=t.get("deadline_quote", ""), priority=t.get("priority", "medium"),
                                source_quote=t.get("quote", ""), source_time=t.get("time", "")))
        s.commit()


def request_approval(meeting_id: int):
    settings = meeting_settings(meeting_id)
    _set(meeting_id, status="awaiting_approval", progress="Ожидает проверки")
    with SessionLocal() as s:
        m = load(s, meeting_id)
    approvers = settings.get("approver_chat_ids") or []
    if not approvers:
        _set(meeting_id, progress="Ожидает проверки в панели (проверяющие в Telegram не выбраны)")
        return
    pdf = render.report_pdf(m, settings)
    buttons = [[{"text": "✅ Отправить", "callback_data": f"ap:{meeting_id}"},
                {"text": "❌ Отклонить", "callback_data": f"rj:{meeting_id}"}]]
    if config.PUBLIC_URL:
        buttons.append([{"text": "✏️ Править в панели", "url": f"{config.PUBLIC_URL}/meetings/{meeting_id}"}])
    for chat in approvers:
        tg_safe(telegram.send_document, chat, render.pdf_name(m), pdf, "Черновик отчёта")
        tg_safe(telegram.send_message, chat, render.summary_message(m, settings, draft=True), buttons)


def deliver(meeting_id: int):
    settings = meeting_settings(meeting_id)
    _set(meeting_id, status="sending", progress="Отправка", approved_at=now())
    with SessionLocal() as s:
        m = load(s, meeting_id)
    pdf = render.report_pdf(m, settings)
    summary = render.summary_message(m, settings)
    for chat in settings.get("report_chat_ids") or []:
        tg_safe(telegram.send_message, chat, summary)
        tg_safe(telegram.send_document, chat, render.pdf_name(m), pdf, f"📎 Полный отчёт: {render.e(m.title)}")

    orphan = []
    with SessionLocal() as s:
        m = load(s, meeting_id)
        by_chat: dict[str, list[Task]] = {}
        for t in m.tasks:
            chat = t.assignee.telegram_chat_id if t.assignee else ""
            if settings["send_tasks_to_assignees"] and chat:
                by_chat.setdefault(chat, []).append(t)
                t.status = "sent"
            elif not chat:
                t.status = "no_recipient"
                orphan.append(t)
            if settings.get("bitrix_enabled") and t.assignee and t.assignee.bitrix_user_id and not t.bitrix_task_id:
                try:
                    t.bitrix_task_id = bitrix.create_task(t, m)
                except Exception as e:  # noqa: BLE001
                    log.warning("bitrix task failed: %s", e)
        s.commit()
        for chat, tasks in by_chat.items():
            tg_safe(telegram.send_message, chat, render.tasks_digest(tasks, m))
            if settings["deadline_mode"] == "ask":
                for t in tasks:
                    if not t.deadline:
                        _ask_deadline(s, t, chat)
        s.commit()
        if orphan:
            text = "⚠️ <b>Задачи без получателя</b> (нет исполнителя или его Telegram):\n" + "\n".join(
                f"• {render.e(t.title)} — {render.e(t.assignee_name or 'не назначен')}" for t in orphan)
            for chat in settings.get("approver_chat_ids") or []:
                tg_safe(telegram.send_message, chat, text)
    _set(meeting_id, status="done", progress="Отправлено", sent_at=now())


def _ask_deadline(s, task: Task, chat: str):
    buttons = [[{"text": "Сегодня", "callback_data": f"dl:{task.id}:d0"},
                {"text": "Завтра", "callback_data": f"dl:{task.id}:d1"}],
               [{"text": "3 рабочих дня", "callback_data": f"dl:{task.id}:b3"},
                {"text": "Неделя", "callback_data": f"dl:{task.id}:w1"}]]
    msg = tg_safe(telegram.send_message, chat,
                  f"⏳ Какой срок поставить по задаче «<b>{render.e(task.title)}</b>»?\n"
                  "Нажмите кнопку или ответьте сообщением: «до пятницы», «15.10», «через 5 дней».", buttons)
    task.status = "awaiting_deadline"
    s.add(DeadlineRequest(task_id=task.id, chat_id=chat, message_id=(msg or {}).get("message_id")))


def set_deadline(task_id: int, date: dt.date, source: str):
    with SessionLocal() as s:
        t = s.get(Task, task_id)
        t.deadline, t.deadline_source = date, source
        if t.status == "awaiting_deadline":
            t.status = "sent"
        for r in s.query(DeadlineRequest).filter_by(task_id=task_id, resolved=False):
            r.resolved = True
        if t.bitrix_task_id:
            try:
                bitrix.update_deadline(t)
            except Exception as e:  # noqa: BLE001
                log.warning("bitrix update failed: %s", e)
        s.commit()
        return t.title


def reject(meeting_id: int):
    _set(meeting_id, status="rejected", progress="Отклонено проверяющим")


# ----------------------------------------------------------------------------- telegram updates
def _remember_chat(chat: dict, active: bool = True):
    with SessionLocal() as s:
        c = s.get(TelegramChat, str(chat["id"])) or TelegramChat(chat_id=str(chat["id"]))
        c.kind = chat.get("type", "")
        c.title = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
        c.username = chat.get("username") or ""
        c.active = active
        s.merge(c)
        s.commit()


def handle_update(u: dict):
    settings = settings_store.all_settings()
    if "my_chat_member" in u:
        st = u["my_chat_member"]["new_chat_member"]["status"]
        _remember_chat(u["my_chat_member"]["chat"], active=st not in ("left", "kicked"))
        return
    if "channel_post" in u:
        _remember_chat(u["channel_post"]["chat"])
        return
    if "callback_query" in u:
        return _handle_callback(u["callback_query"], settings)
    msg = u.get("message")
    if not msg:
        return
    chat = msg["chat"]
    _remember_chat(chat)
    text = (msg.get("text") or "").strip()
    cid = str(chat["id"])
    if text.startswith("/start") or text.startswith("/id"):
        with SessionLocal() as s:
            emp = s.query(Employee).filter_by(telegram_chat_id=cid).first()
        hello = f"Здравствуйте, {render.e(emp.name)}! " if emp else "Здравствуйте! "
        tg_safe(telegram.send_message, cid, hello + "Бот SYSAI подключён: сюда будут приходить отчёты и задачи.\n"
                f"Ваш chat_id: <code>{cid}</code>" + ("" if emp else " — администратор привяжет его в панели."))
        return
    if chat.get("type") != "private" or not text:
        return
    with SessionLocal() as s:
        q = s.query(DeadlineRequest).filter_by(chat_id=cid, resolved=False)
        reply_id = (msg.get("reply_to_message") or {}).get("message_id")
        req = (q.filter_by(message_id=reply_id).first() if reply_id else None) or q.order_by(DeadlineRequest.id).first()
        task_id = req.task_id if req else None
    if not task_id:
        return
    d = deadlines.parse(text)
    if not d:
        tg_safe(telegram.send_message, cid, "Не понял срок 🙏 Напишите, например: «до пятницы», «15.10» или «через 3 дня».")
        return
    title = set_deadline(task_id, d, "asked")
    tg_safe(telegram.send_message, cid, f"✅ Срок по задаче «{render.e(title)}»: <b>{deadlines.fmt(d)}</b>")


def _handle_callback(cb: dict, settings: dict):
    data = cb.get("data") or ""
    who = str(cb["from"]["id"])
    msg = cb.get("message") or {}
    chat_id = str(msg.get("chat", {}).get("id", who))
    approvers = {str(x) for x in settings.get("approver_chat_ids") or []}
    kind, _, rest = data.partition(":")
    if kind in ("ap", "rj"):
        if who not in approvers and chat_id not in approvers:
            return telegram.answer_callback(cb["id"], "Нет прав на проверку")
        mid = int(rest)
        with SessionLocal() as s:
            status = s.get(Meeting, mid).status
        if status != "awaiting_approval":
            return telegram.answer_callback(cb["id"], "Уже обработано")
        tg_safe(telegram.edit_buttons, chat_id, msg.get("message_id"), None)
        if kind == "ap":
            telegram.answer_callback(cb["id"], "Отправляю")
            submit_deliver(mid)
            tg_safe(telegram.send_message, chat_id, "✅ Отчёт и задачи отправлены")
        else:
            reject(mid)
            telegram.answer_callback(cb["id"], "Отклонено")
            tg_safe(telegram.send_message, chat_id, "❌ Черновик отклонён. Его можно исправить в панели и отправить заново.")
        return
    if kind == "dl":
        task_id, code = rest.split(":")
        base = deadlines.today()
        d = {"d0": base, "d1": base + dt.timedelta(days=1), "b3": deadlines.add_business_days(base, 3),
             "w1": base + dt.timedelta(weeks=1)}.get(code)
        if d:
            title = set_deadline(int(task_id), d, "asked")
            telegram.answer_callback(cb["id"], "Срок сохранён")
            tg_safe(telegram.edit_buttons, chat_id, msg.get("message_id"), None)
            tg_safe(telegram.send_message, chat_id, f"✅ Срок по задаче «{render.e(title)}»: <b>{deadlines.fmt(d)}</b>")


def submit_deliver(meeting_id: int):
    if config.TESTING:
        deliver(meeting_id)
    else:
        POOL.submit(_safe_deliver, meeting_id)


def _safe_deliver(meeting_id: int):
    try:
        deliver(meeting_id)
    except Exception as e:  # noqa: BLE001
        log.exception("deliver failed")
        _set(meeting_id, status="error", error=str(e)[:2000])


# ----------------------------------------------------------------------------- background loops
def deadline_timeouts():
    settings = settings_store.all_settings()
    limit = now() - dt.timedelta(hours=float(settings["ask_timeout_hours"]))
    with SessionLocal() as s:
        reqs = s.query(DeadlineRequest).filter(DeadlineRequest.resolved.is_(False)).all()
        due = [(r.task_id, r.chat_id) for r in reqs if (r.asked_at if r.asked_at.tzinfo else
                                                       r.asked_at.replace(tzinfo=dt.timezone.utc)) < limit]
    for task_id, chat in due:
        d = deadlines.add_business_days(deadlines.today(), int(settings["default_deadline_days"]))
        title = set_deadline(task_id, d, "default")
        tg_safe(telegram.send_message, chat, f"⏳ Ответа не было — поставил срок по умолчанию для «{render.e(title)}»: "
                                             f"<b>{deadlines.fmt(d)}</b>")


def mic_scan():
    """MOCK microphone: files dropped into MIC_INBOX_DIR are picked up when auto-ingest is on."""
    if not settings_store.get("auto_ingest"):
        return
    inbox = config.MIC_INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)
    for f in sorted(inbox.iterdir()):
        if f.suffix.lower() in AUDIO_EXT and f.is_file():
            dst = config.DATA_DIR / "uploads" / f"mic_{int(time.time())}_{f.name}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            f.rename(dst)
            submit(create_meeting(f.name, str(dst), source="microphone"))


def start_background():
    def loop():
        while True:
            for fn in (mic_scan, deadline_timeouts):
                try:
                    fn()
                except Exception:
                    log.exception("%s failed", fn.__name__)
            time.sleep(30)
    threading.Thread(target=loop, daemon=True, name="sysai-bg").start()
    # resume jobs interrupted by a restart
    with SessionLocal() as s:
        stuck = [m.id for m in s.query(Meeting).filter(Meeting.status.in_(["queued", "transcribing", "analyzing"]))]
    for mid in stuck:
        submit(mid)
