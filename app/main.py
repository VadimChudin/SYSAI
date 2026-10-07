"""SYSAI web app: upload form, meetings, draft editing/approval, settings, employees, Telegram webhook."""
import datetime as dt
import hmac
import logging
import os
import pathlib
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from urllib.parse import quote, urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import bitrix, config, deadlines, pipeline, render, report_archive, settings_store, telegram
from .analyze import mmss
from .ui_icons import ICONS
from .db import ConversationEscalation, Delivery, Employee, Meeting, ReportVersion, SessionLocal, Task, TelegramChat, init_db, now

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sysai")
BASE = pathlib.Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    startup()
    yield


app = FastAPI(title="SYSAI", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, max_age=14 * 86400)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")
tpl.env.globals.update(mmss=mmss, fmt=deadlines.fmt, priority=render.PRIORITY, config=config)

STATUS = {"queued": ("В очереди", "gray"), "transcribing": ("Расшифровка", "blue"), "analyzing": ("Анализ", "blue"),
          "awaiting_approval": ("Ждёт проверки", "amber"), "sending": ("Отправка", "blue"), "done": ("Отправлено", "green"),
          "error": ("Ошибка", "red"), "rejected": ("Отклонено", "gray")}
STATUS.update(delivery_queued=("В очереди отправки", "blue"), delivery_retry=("Повтор доставки", "amber"),
              delivery_failed=("Ошибка доставки", "red"), ready=("Отчёт готов", "green"))
tpl.env.globals.update(STATUS=STATUS, ICONS=ICONS)


def startup():
    init_db()
    (config.DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    config.MIC_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    if config.TESTING:
        return
    pipeline.start_background()
    if config.telegram_enabled():
        try:
            if config.TELEGRAM_MODE == "polling":
                telegram.start_polling(pipeline.handle_update)
            else:
                telegram.set_webhook()
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram setup failed: %s", e)


def authed(request: Request) -> bool:
    return bool(request.session.get("auth"))


def guard(request: Request):
    if not authed(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


def page(request: Request, name: str, **ctx):
    return tpl.TemplateResponse(request, name, {"request": request, "llm": config.llm_enabled(),
                                                "tg": config.telegram_enabled(), "ephemeral": EPHEMERAL, **ctx})


EPHEMERAL = config.DATABASE_URL.startswith("sqlite") and bool(os.getenv("RENDER"))


# ----------------------------------------------------------------------------- auth
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    if authed(request):
        return RedirectResponse("/app", 303)
    return tpl.TemplateResponse(request, "login.html", {"request": request, "error": error, "username": ""})


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(...)):
    ok_user = hmac.compare_digest(username.strip().lower(), config.ADMIN_USERNAME.lower())
    if ok_user and hmac.compare_digest(password, config.ADMIN_PASSWORD):
        request.session["auth"] = True
        request.session["user"] = config.ADMIN_USERNAME
        return RedirectResponse("/app", 303)
    time.sleep(1)
    return RedirectResponse("/login?error=1", 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@app.get("/health")
def health():
    return {"ok": True, "llm": config.llm_enabled(), "telegram": config.telegram_enabled()}


# ----------------------------------------------------------------------------- meetings
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return tpl.TemplateResponse(request, "landing.html", {"request": request, "authed": authed(request),
                                                          "year": dt.date.today().year})


def _meetings(limit: int = 100):
    with SessionLocal() as s:
        meetings = s.query(Meeting).order_by(Meeting.id.desc()).limit(limit).all()
        return meetings, {m.id: len(m.tasks) for m in meetings}


@app.get("/app", response_class=HTMLResponse)
def dashboard(request: Request):
    guard(request)
    meetings, counts = _meetings(8)
    with SessionLocal() as s:
        stats = dict(meetings=s.query(Meeting).count(), tasks=s.query(Task).count(),
                     pending=s.query(Meeting).filter_by(status="awaiting_approval").count(),
                     employees=s.query(Employee).filter(Employee.active.is_(True)).count())
    return page(request, "dashboard.html", meetings=meetings, counts=counts, stats=stats,
                settings=settings_store.all_settings(), employees_count=stats["employees"])


@app.get("/meetings", response_class=HTMLResponse)
def meetings_list(request: Request, q: str = "", status: str = "", source: str = "", date_from: str = "", date_to: str = "", p: int = 1):
    guard(request)
    q = q.strip()[:200]
    if status and status not in STATUS:
        raise HTTPException(400, "Некорректный статус")
    if source and source not in ("web", "microphone", "demo", "api"):
        raise HTTPException(400, "Некорректный источник")
    try:
        first = dt.date.fromisoformat(date_from) if date_from else None
        last = dt.date.fromisoformat(date_to) if date_to else None
    except ValueError:
        raise HTTPException(400, "Некорректная дата фильтра") from None
    if first and last and first > last:
        raise HTTPException(400, "Начальная дата не должна быть позже конечной")
    with SessionLocal() as s:
        query = s.query(Meeting)
        if q:
            # Python casefold keeps Cyrillic search consistent across SQLite and PostgreSQL.
            matches = [mid for mid, title, filename in s.query(Meeting.id, Meeting.title, Meeting.filename)
                       if q.casefold() in (title or "").casefold() or q.casefold() in (filename or "").casefold()]
            query = query.filter(Meeting.id.in_(matches))
        if status:
            query = query.filter(Meeting.status == status)
        if source:
            query = query.filter(Meeting.source == source)
        if first:
            query = query.filter(Meeting.meeting_date >= first)
        if last:
            query = query.filter(Meeting.meeting_date <= last)
        total = query.count()
        pages = max(1, (total + 23) // 24)
        number = max(1, min(p, pages))
        meetings = query.order_by(Meeting.id.desc()).offset((number - 1) * 24).limit(24).all()
        counts = {m.id: len(m.tasks) for m in meetings}
        versions = {m.id: report_archive.latest(s, m.id) for m in meetings}
    filters = dict(q=q, status=status, source=source, date_from=date_from, date_to=date_to)
    link = lambda n: "/meetings?" + urlencode(dict(filters, p=n))
    return page(request, "meetings.html", meetings=meetings, counts=counts, versions=versions, **filters,
                page_number=number, total_pages=pages, total_count=total, archive_error="",
                prev_url=link(number - 1) if number > 1 else None, next_url=link(number + 1) if number < pages else None)


@app.get("/app/new", response_class=HTMLResponse)
def new_meeting(request: Request, source: str = "file"):
    guard(request)
    with SessionLocal() as s:
        mic = s.query(Meeting).filter_by(source="microphone").order_by(Meeting.id.desc()).limit(3).all()
    return page(request, "new.html", s=settings_store.all_settings(), chats=known_chats(), source=source,
                today=deadlines.today().isoformat(), mic_meetings=mic)


def _options(form) -> dict:
    """Per-meeting overrides from the upload page."""
    if "deadline_mode" not in form:
        return {}
    return {"approval_required": bool(form.get("approval_required")),
            "send_tasks_to_assignees": bool(form.get("send_tasks_to_assignees")),
            "include_transcript_in_pdf": bool(form.get("include_transcript_in_pdf")),
            "deadline_mode": form.get("deadline_mode", "default"),
            "report_chat_ids": _chat_ids(form, "report_chat_ids")}


@app.post("/settings/mic")
async def mic_toggle(request: Request):
    guard(request)
    data = await request.json()
    settings_store.set_many({"auto_ingest": bool(data.get("on"))})
    return {"auto_ingest": settings_store.get("auto_ingest")}


def recording_owner(request):
    guard(request)
    if "recording_owner" not in request.session:
        request.session["recording_owner"] = secrets.token_hex(24)
    return request.session["recording_owner"]


@app.post("/recordings")
async def recording_start(request: Request):
    from . import recording
    owner = recording_owner(request)
    form = await request.form()
    try:
        date = dt.date.fromisoformat(form.get("meeting_date")) if form.get("meeting_date") else deadlines.today()
    except ValueError:
        raise HTTPException(400, "Некорректная дата совещания") from None
    return recording.start(owner, form.get("mime_type", ""), (form.get("title") or "").strip()[:300], date, _options(form))


@app.post("/recordings/{rid}/chunks/{sequence}")
async def recording_chunk(request: Request, rid: str, sequence: int):
    from . import recording
    owner = recording_owner(request)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8 * 1024 * 1024:
            raise HTTPException(413, "Одна часть записи превышает 8 МБ")
        body.extend(chunk)
    return recording.append(owner, rid, sequence, bytes(body))


@app.post("/recordings/{rid}/finish")
def recording_finish(request: Request, rid: str):
    from . import recording
    return recording.finish(recording_owner(request), rid)


@app.post("/recordings/{rid}/cancel")
def recording_cancel(request: Request, rid: str):
    from . import recording
    return recording.cancel(recording_owner(request), rid)


@app.get("/recordings/{rid}/audio")
def recording_download(request: Request, rid: str):
    from . import recording
    path, mime = recording.audio_file(recording_owner(request), rid)
    return FileResponse(path, media_type=mime, filename=f"recording{path.suffix}")


@app.post("/upload")
async def upload(request: Request):
    guard(request)
    form = await request.form()
    file, title, meeting_date = form.get("file"), form.get("title", ""), form.get("meeting_date", "")
    if not getattr(file, "filename", ""):
        raise HTTPException(400, "Выберите аудиофайл")
    ext = pathlib.Path(file.filename or "").suffix.lower()
    if ext not in pipeline.AUDIO_EXT:
        raise HTTPException(400, f"Неподдерживаемый формат {ext}. Нужен аудиофайл: " + ", ".join(sorted(pipeline.AUDIO_EXT)))
    dst = config.DATA_DIR / "uploads" / f"{int(time.time() * 1000)}{ext}"
    size = 0
    with dst.open("wb") as out:
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > config.MAX_UPLOAD_MB * 1024 * 1024:
                out.close(); dst.unlink(missing_ok=True)
                raise HTTPException(413, f"Файл больше {config.MAX_UPLOAD_MB} МБ")
            out.write(chunk)
    d = dt.date.fromisoformat(meeting_date) if meeting_date else None
    mid = pipeline.create_meeting(file.filename or dst.name, str(dst), "web", title.strip(), d, _options(form))
    pipeline.submit(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/demo")
async def demo(request: Request):
    """Demo run without audio (bundled transcript). source=microphone simulates an auto-ingested recording."""
    guard(request)
    form = await request.form()
    mic = form.get("source") == "microphone"
    mid = pipeline.create_meeting("demo.mp3", "", "demo",
                                  "Запись с микрофона (имитация)" if mic else "", options=_options(form))
    pipeline.submit(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.get("/meetings/{mid}", response_class=HTMLResponse)
def meeting_view(request: Request, mid: int):
    guard(request)
    with SessionLocal() as s:
        m = pipeline.load(s, mid)
        employees = s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name).all()
    return page(request, "meeting.html", m=m, r=m.report or {}, employees=employees)


@app.get("/meetings/{mid}/status")
def meeting_status(request: Request, mid: int):
    guard(request)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        return JSONResponse({"status": m.status, "progress": m.progress, "error": m.error,
                             "dialogue_version": [[x.id, x.status] for x in m.dialogue] +
                                                [[x.id, x.status] for x in m.escalations] +
                                                [[x.id, x.status] for x in m.deliveries if x.phase == "dialogue"]})


def _apply_form(mid: int, form, approve=False):
    with SessionLocal() as s:
        values = {Meeting.progress: Meeting.progress}
        if approve:
            values = {Meeting.status: "delivery_queued", Meeting.approved_at: now(), Meeting.error: ""}
        changed = s.query(Meeting).filter(Meeting.id == mid, Meeting.status.in_(("awaiting_approval", "rejected"))).update(
            values, synchronize_session=False)
        if not changed:
            return False
        m = s.get(Meeting, mid)
        r = dict(m.report or {})
        r["title"] = form.get("title", r.get("title", ""))
        r["summary"] = form.get("summary", r.get("summary", ""))
        m.report, m.title = r, r["title"]
        for t in list(m.tasks):
            if f"title_{t.id}" not in form:  # task not on the submitted form: leave untouched
                continue
            if form.get(f"del_{t.id}"):
                m.tasks.remove(t)
                continue
            t.title = (form.get(f"title_{t.id}") or t.title).strip()
            t.description = form.get(f"desc_{t.id}", t.description)
            a = form.get(f"assignee_{t.id}", "")
            t.assignee_id = int(a) if a else None
            dl = form.get(f"deadline_{t.id}", "")
            new = dt.date.fromisoformat(dl) if dl else None
            if new != t.deadline:
                t.deadline, t.deadline_source = new, ("manual" if new else "none")
            t.priority = form.get(f"priority_{t.id}", t.priority)
        if (form.get("new_title") or "").strip():
            a = form.get("new_assignee", "")
            dl = form.get("new_deadline", "")
            m.tasks.append(Task(title=form["new_title"].strip(), assignee_id=int(a) if a else None,
                                deadline=dt.date.fromisoformat(dl) if dl else None,
                                deadline_source="manual" if dl else "none", priority=form.get("new_priority", "medium")))
        s.query(Delivery).filter_by(meeting_id=mid, phase="draft", status="pending").update(
            {Delivery.status: "cancelled"}, synchronize_session=False)
        if not approve:
            m.options = dict(m.options or {}, approval_revision=int((m.options or {}).get("approval_revision", 1)) + 1)
        else:
            s.flush()
            report_archive.ensure(s, m, settings_store.for_meeting(m.options))
        s.commit()
        return True


@app.post("/meetings/{mid}/save")
async def meeting_save(request: Request, mid: int):
    guard(request)
    if not _apply_form(mid, await request.form()):
        raise HTTPException(409, "Редактирование доступно только до утверждения отчёта")
    pipeline.request_approval(mid)
    return RedirectResponse(f"/meetings/{mid}?saved=1", 303)


@app.post("/meetings/{mid}/approve")
async def meeting_approve(request: Request, mid: int):
    """Saves the edited draft and sends it."""
    guard(request)
    form = await request.form()
    if form:
        if _apply_form(mid, form, approve=True):
            pipeline.dispatch_delivery(mid)
    else:
        pipeline.submit_deliver(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/retry-delivery")
def meeting_retry_delivery(request: Request, mid: int):
    guard(request)
    pipeline.retry_delivery(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/retry-processing")
def meeting_retry_processing(request: Request, mid: int):
    guard(request)
    pipeline.retry_processing(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/escalations/{escalation_id}/resolve")
def meeting_resolve_escalation(request: Request, mid: int, escalation_id: int):
    guard(request)
    with SessionLocal() as s:
        s.query(ConversationEscalation).filter_by(id=escalation_id, meeting_id=mid, status="open").update(
            {ConversationEscalation.status: "resolved"}, synchronize_session=False)
        s.commit()
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/dialogue-deliveries/{delivery_id}/retry")
async def meeting_retry_dialogue_delivery(request: Request, mid: int, delivery_id: int):
    guard(request)
    form = await request.form()
    with SessionLocal() as s:
        row = s.query(Delivery).filter_by(id=delivery_id, meeting_id=mid, phase="dialogue").first()
        if not row or row.status not in ("failed", "uncertain"):
            raise HTTPException(409, "Отправка уже обработана или недоступна")
        if row.status == "uncertain" and form.get("confirm") != "yes":
            raise HTTPException(400, "Сначала проверьте чат и подтвердите риск повторной отправки")
        s.query(Delivery).filter(Delivery.id == row.id, Delivery.status.in_(("failed", "uncertain"))).update(
            {Delivery.status: "pending", Delivery.next_attempt_at: None, Delivery.cycle_attempts: 0},
            synchronize_session=False)
        s.commit()
    from . import conversations, delivery
    delivery.send(delivery_id)
    conversations.run_pending()
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/deliveries/{delivery_id}/resend")
async def meeting_resend_uncertain(request: Request, mid: int, delivery_id: int):
    guard(request)
    form = await request.form()
    if form.get("confirm") != "yes":
        raise HTTPException(400, "Подтвердите риск повторной отправки")
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        if not m or m.status not in ("delivery_failed", "awaiting_approval"):
            raise HTTPException(409, "Рассылка уже обрабатывается")
        changed = s.query(Delivery).filter_by(id=delivery_id, meeting_id=mid, status="uncertain").update(
            {Delivery.status: "pending", Delivery.next_attempt_at: None, Delivery.cycle_attempts: 0}, synchronize_session=False)
        s.commit()
    if changed:
        pipeline.retry_delivery(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/reject")
def meeting_reject(request: Request, mid: int):
    guard(request)
    pipeline.reject(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/reanalyze")
def meeting_reanalyze(request: Request, mid: int):
    """Rebuild the report from the saved transcript (e.g. after adding employees)."""
    guard(request)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        changed = s.query(Meeting).filter(Meeting.id == mid, Meeting.status.in_(("awaiting_approval", "rejected", "error"))).update(
            {Meeting.status: "analyzing", Meeting.progress: "Повторный анализ"}, synchronize_session=False)
        if not changed:
            raise HTTPException(409, "Нельзя пересобрать утверждённый отчёт")
        if not m.transcript:
            raise HTTPException(400, "Нет расшифровки")
        m.options = dict(m.options or {}, approval_revision=int((m.options or {}).get("approval_revision", 1)) + 1)
        m.status, m.progress = "analyzing", "Повторный анализ"
        s.commit()

    def job():
        try:
            settings = pipeline.meeting_settings(mid)
            with SessionLocal() as s:
                m = s.get(Meeting, mid)
                emps = s.query(Employee).filter(Employee.active.is_(True)).all()
                segs, mdate, source = m.transcript, m.meeting_date or deadlines.today(), m.source
            from . import analyze
            report = analyze.mock_report(emps, mdate) if source == "demo" else analyze.analyze(
                segs, emps, settings, mdate, progress=lambda p: pipeline._set(mid, progress=p),
                checkpoint=pipeline.checkpoints.Store(mid))
            pipeline._save_report(mid, report, settings)
            pipeline.request_approval(mid)
        except Exception as e:  # noqa: BLE001
            pipeline._set(mid, status="error", error=str(e)[:2000])
    job() if config.TESTING else pipeline.POOL.submit(job)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/delete")
def meeting_delete(request: Request, mid: int):
    guard(request)
    with SessionLocal() as s:
        s.query(Meeting).filter_by(id=mid).update({Meeting.progress: Meeting.progress}, synchronize_session=False)
        m = s.get(Meeting, mid)
        if m:
            if m.status in ("queued", "transcribing", "analyzing", "sending", "delivery_queued", "delivery_retry"):
                raise HTTPException(409, "Дождитесь завершения обработки")
            if s.query(Delivery).filter(Delivery.meeting_id == mid, Delivery.status.in_(("pending", "sending"))).first():
                raise HTTPException(409, "Нельзя удалить совещание с незавершённой доставкой")
            if m.audio_path:
                pathlib.Path(m.audio_path).unlink(missing_ok=True)
            s.delete(m)
            s.commit()
    return RedirectResponse("/meetings", 303)


@app.get("/meetings/{mid}/pdf")
def meeting_pdf(request: Request, mid: int, download: bool = False):
    guard(request)
    with SessionLocal() as s:
        if not s.get(Meeting, mid):
            raise HTTPException(404, "Совещание не найдено")
        m = pipeline.load(s, mid)
        if not m.report:
            raise HTTPException(409, "Отчёт ещё не готов")
        archived = report_archive.latest(s, mid)
        if not archived and m.status in report_archive.APPROVED_STATUSES:
            s.query(Meeting).filter_by(id=mid).update({Meeting.progress: Meeting.progress}, synchronize_session=False)
            archived = report_archive.ensure(s, m, settings_store.for_meeting(m.options), origin="legacy")
            s.commit()
        pdf = archived.pdf if archived else render.report_pdf(m, settings_store.for_meeting(m.options))
        filename = archived.filename if archived else render.pdf_name(m)
    return pdf_response(pdf, filename, download)


def pdf_response(pdf, filename, download=False):
    disposition = "attachment" if download else "inline"
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "private, no-store",
    })


@app.get("/meetings/{mid}/reports/{version_id}/pdf")
def archived_pdf(request: Request, mid: int, version_id: int, download: bool = False):
    guard(request)
    with SessionLocal() as s:
        version = s.query(ReportVersion).filter_by(id=version_id, meeting_id=mid).first()
        if not version:
            raise HTTPException(404, "Версия отчёта не найдена")
        return pdf_response(version.pdf, version.filename, download)


# ----------------------------------------------------------------------------- settings
def known_chats():
    with SessionLocal() as s:
        return s.query(TelegramChat).filter(TelegramChat.active.is_(True)).order_by(TelegramChat.kind, TelegramChat.title).all()


@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, msg: str = ""):
    guard(request)
    st = settings_store.all_settings()
    masked = {k: settings_store.mask(st.get(k)) for k in settings_store.SECRET_KEYS}
    env = {"openrouter_api_key": bool(config.OPENROUTER_API_KEY), "telegram_bot_token": bool(config.TELEGRAM_BOT_TOKEN),
           "bitrix_webhook_url": bool(config.BITRIX_WEBHOOK_URL)}
    for k in settings_store.SECRET_KEYS:
        st.pop(k, None)  # secrets never go back to the browser, only a mask
    return page(request, "settings.html", s=st, chats=known_chats(), msg=msg, masked=masked, env=env,
                bitrix_url=bool(config.bitrix_url()))


def _chat_ids(form, name: str) -> list[str]:
    ids = [str(v) for v in form.getlist(name)]
    extra = form.get(name + "_extra", "")
    ids += [x.strip() for x in extra.replace("\n", ",").split(",") if x.strip()]
    return list(dict.fromkeys(ids))


@app.post("/settings")
async def settings_save(request: Request):
    guard(request)
    f = await request.form()
    conversation_model = f.get("conversation_model", "").strip() or settings_store.DEFAULTS["conversation_model"]
    if conversation_model != "openrouter/free" and not conversation_model.endswith(":free"):
        raise HTTPException(400, "Для переписки выберите openrouter/free или модель с суффиксом :free")
    settings_store.set_many({
        "auto_ingest": bool(f.get("auto_ingest")),
        "approval_required": bool(f.get("approval_required")),
        "approver_chat_ids": _chat_ids(f, "approver_chat_ids"),
        "report_chat_ids": _chat_ids(f, "report_chat_ids"),
        "send_tasks_to_assignees": bool(f.get("send_tasks_to_assignees")),
        "include_transcript_in_pdf": bool(f.get("include_transcript_in_pdf")),
        "deadline_mode": f.get("deadline_mode", "default"),
        "default_deadline_days": max(1, int(f.get("default_deadline_days") or 3)),
        "ask_timeout_hours": _num(f.get("ask_timeout_hours"), 24),
        "transcribe_model": f.get("transcribe_model", "").strip() or settings_store.DEFAULTS["transcribe_model"],
        "report_model": f.get("report_model", "").strip() or settings_store.DEFAULTS["report_model"],
        "conversation_model": conversation_model,
        "dialogue_enabled": bool(f.get("dialogue_enabled")),
        "conversation_tone": (f.get("conversation_tone") or settings_store.DEFAULTS["conversation_tone"]).strip()[:1000],
        "allow_deadline_proposals": bool(f.get("allow_deadline_proposals")),
        "secretary_chat_ids": _chat_ids(f, "secretary_chat_ids"),
        "chunk_minutes": max(5, min(60, int(f.get("chunk_minutes") or 30))),
        "glossary": f.get("glossary", ""),
        "company_name": f.get("company_name", "").strip() or "Компания",
        "accent_color": f.get("accent_color", "#2563eb"),
        "bitrix_enabled": bool(f.get("bitrix_enabled")),
    })
    old_token = config.telegram_token()
    secrets_upd = {}
    for k in settings_store.SECRET_KEYS:
        v = (f.get(k) or "").strip()
        if f.get("clear_" + k):
            secrets_upd[k] = ""
        elif v:
            secrets_upd[k] = v
    if secrets_upd:
        settings_store.set_many(secrets_upd)
    msg = "Сохранено"
    if config.telegram_token() != old_token and config.telegram_enabled() and not config.TESTING:
        try:
            msg += ". Telegram подключён" if telegram.set_webhook() else ". Токен сохранён, но адрес сайта не известен"
        except Exception as e:  # noqa: BLE001
            msg += f". Telegram: {str(e)[:150]}"
    return RedirectResponse("/settings?msg=" + msg, 303)


def _num(v, default):
    try:
        x = max(1.0, float(v))
    except (TypeError, ValueError):
        return default
    return int(x) if x.is_integer() else x


@app.post("/settings/test-openrouter")
def test_openrouter(request: Request):
    guard(request)
    from . import llm
    try:
        return RedirectResponse("/settings?msg=OpenRouter: " + llm.check_key(), 303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/settings?msg=OpenRouter: {str(e)[:200]}", 303)


@app.post("/settings/test-telegram")
def test_telegram(request: Request):
    guard(request)
    s = settings_store.all_settings()
    chats = list(dict.fromkeys((s["report_chat_ids"] or []) + (s["approver_chat_ids"] or [])))
    if not config.telegram_enabled():
        return RedirectResponse("/settings?msg=Telegram не настроен: вставьте токен бота в «Подключения»", 303)
    try:
        me = telegram.call("getMe")
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/settings?msg=Telegram отклонил токен: {str(e)[:150]}", 303)
    if not chats:
        return RedirectResponse(f"/settings?msg=Бот @{me.get('username')} работает. Выберите чаты для отчёта или проверки", 303)
    ok, bad = 0, []
    for c in chats:
        try:
            telegram.send_message(c, "✅ Тестовое сообщение SYSAI: доставка работает.")
            ok += 1
        except Exception as e:  # noqa: BLE001
            bad.append(f"{c}: {e}")
    return RedirectResponse(f"/settings?msg=Бот @{me.get('username')}: отправлено {ok} из {len(chats)}. " + "; ".join(bad)[:300], 303)


@app.post("/settings/test-bitrix")
def test_bitrix(request: Request):
    guard(request)
    try:
        return RedirectResponse(f"/settings?msg=Bitrix24 подключён: {bitrix.check()}", 303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/settings?msg=Bitrix24: {str(e)[:200]}", 303)


# ----------------------------------------------------------------------------- employees
@app.get("/employees", response_class=HTMLResponse)
def employees_view(request: Request, edit: int = 0):
    guard(request)
    with SessionLocal() as s:
        emps = s.query(Employee).order_by(Employee.active.desc(), Employee.name).all()
    linked = {e.telegram_chat_id for e in emps if e.telegram_chat_id}
    chats = [c for c in known_chats() if c.kind == "private"]
    return page(request, "employees.html", employees=emps, chats=chats, linked=linked,
                positions=settings_store.POSITIONS, edit=edit)


@app.post("/employees")
def employee_save(request: Request, id: int = Form(0), name: str = Form(...), aliases: str = Form(""),
                  position: str = Form(""), channel: str = Form("telegram"), telegram_chat_id: str = Form(""),
                  telegram_manual: str = Form(""), bitrix_user_id: str = Form(""), active: str = Form("")):
    guard(request)
    with SessionLocal() as s:
        e = s.get(Employee, id) if id else Employee()
        e.name, e.aliases, e.position, e.channel = name.strip(), aliases.strip(), position.strip(), channel
        e.telegram_chat_id = (telegram_manual.strip() or telegram_chat_id).strip()
        e.bitrix_user_id = bitrix_user_id.strip()
        e.active = bool(active) if id else True
        s.add(e)
        s.commit()
    return RedirectResponse("/employees", 303)


@app.post("/employees/{eid}/delete")
def employee_delete(request: Request, eid: int):
    guard(request)
    with SessionLocal() as s:
        for t in s.query(Task).filter_by(assignee_id=eid):
            t.assignee_id = None
        e = s.get(Employee, eid)
        if e:
            s.delete(e)
        s.commit()
    return RedirectResponse("/employees", 303)


# ----------------------------------------------------------------------------- telegram webhook
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != config.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(403)
    update = await request.json()
    try:
        pipeline.handle_update(update)
    except Exception:  # never make Telegram retry forever
        log.exception("update failed")
    return {"ok": True}


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], 303)
    if request.url.path.startswith("/telegram") or request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"detail": exc.detail}, exc.status_code)
    resp = page(request, "error.html", code=exc.status_code, detail=exc.detail)
    resp.status_code = exc.status_code
    return resp
