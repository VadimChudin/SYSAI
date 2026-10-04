"""SYSAI web app: upload form, meetings, draft editing/approval, settings, employees, Telegram webhook."""
import datetime as dt
import hmac
import logging
import os
import pathlib
import shutil
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import bitrix, config, deadlines, pipeline, render, settings_store, telegram
from .analyze import mmss
from .ui_icons import ICONS
from .db import Employee, Meeting, SessionLocal, Task, TelegramChat, init_db

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
def meetings_list(request: Request):
    guard(request)
    meetings, counts = _meetings()
    return page(request, "meetings.html", meetings=meetings, counts=counts)


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
    mid = pipeline.create_meeting("demo.mp3", "", "microphone" if mic else "demo",
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
        return JSONResponse({"status": m.status, "progress": m.progress, "error": m.error})


def _apply_form(mid: int, form):
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        r = dict(m.report or {})
        r["title"] = form.get("title", r.get("title", ""))
        r["summary"] = form.get("summary", r.get("summary", ""))
        m.report, m.title = r, r["title"]
        for t in list(m.tasks):
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
        s.commit()


@app.post("/meetings/{mid}/save")
async def meeting_save(request: Request, mid: int):
    guard(request)
    _apply_form(mid, await request.form())
    return RedirectResponse(f"/meetings/{mid}?saved=1", 303)


@app.post("/meetings/{mid}/approve")
async def meeting_approve(request: Request, mid: int):
    """Saves the edited draft and sends it."""
    guard(request)
    form = await request.form()
    if form:
        _apply_form(mid, form)
    pipeline.submit_deliver(mid)
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
        if not m.transcript:
            raise HTTPException(400, "Нет расшифровки")
        m.status, m.progress = "analyzing", "Повторный анализ"
        s.commit()

    def job():
        try:
            settings = settings_store.all_settings()
            with SessionLocal() as s:
                m = s.get(Meeting, mid)
                emps = s.query(Employee).filter(Employee.active.is_(True)).all()
                segs, mdate = m.transcript, m.meeting_date or deadlines.today()
            from . import analyze
            pipeline._save_report(mid, analyze.analyze(segs, emps, settings, mdate), settings)
            pipeline._set(mid, status="awaiting_approval", progress="Ожидает проверки")
        except Exception as e:  # noqa: BLE001
            pipeline._set(mid, status="error", error=str(e)[:2000])
    job() if config.TESTING else pipeline.POOL.submit(job)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/delete")
def meeting_delete(request: Request, mid: int):
    guard(request)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        if m:
            if m.audio_path:
                pathlib.Path(m.audio_path).unlink(missing_ok=True)
            s.delete(m)
            s.commit()
    return RedirectResponse("/meetings", 303)


@app.get("/meetings/{mid}/pdf")
def meeting_pdf(request: Request, mid: int):
    guard(request)
    with SessionLocal() as s:
        m = pipeline.load(s, mid)
    pdf = render.report_pdf(m, settings_store.for_meeting(m.options))
    from urllib.parse import quote
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(render.pdf_name(m))}"})


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
    return page(request, "error.html", code=exc.status_code, detail=exc.detail)
