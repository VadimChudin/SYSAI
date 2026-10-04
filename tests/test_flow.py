import datetime as dt
import subprocess

from app import analyze, deadlines, llm, render, settings_store, transcribe
from app.db import DeadlineRequest, Employee, Meeting, SessionLocal, Task, TelegramChat


def add_people():
    with SessionLocal() as s:
        s.add_all([Employee(name="Иван Петров", aliases="Ваня", position="Разработчик", telegram_chat_id="111"),
                   Employee(name="Мария Смирнова", position="Маркетолог", telegram_chat_id="222")])
        s.commit()


def sent_to(calls, chat):
    return [c for c in calls if str(c[1].get("chat_id")) == chat]


def test_deadline_parser():
    base = dt.date(2026, 10, 5)  # Monday
    assert deadlines.parse("до пятницы", base) == dt.date(2026, 10, 9)
    assert deadlines.parse("завтра", base) == dt.date(2026, 10, 6)
    assert deadlines.parse("15.10", base) == dt.date(2026, 10, 15)
    assert deadlines.parse("через 3 дня", base) == dt.date(2026, 10, 8)
    assert deadlines.parse("5 рабочих дней", base) == dt.date(2026, 10, 12)
    assert deadlines.parse("20 ноября", base) == dt.date(2026, 11, 20)
    assert deadlines.parse("неделя", base) == dt.date(2026, 10, 12)
    assert deadlines.parse("что-то непонятное", base) is None
    assert deadlines.add_business_days(dt.date(2026, 10, 9), 1) == dt.date(2026, 10, 12)


def test_full_flow_with_approval_and_deadline_questions(client, tg):
    add_people()
    settings_store.set_many({"approval_required": True, "approver_chat_ids": ["999"], "report_chat_ids": ["-100500"],
                             "deadline_mode": "ask"})
    r = client.post("/demo", follow_redirects=False)
    mid = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "awaiting_approval", m.error
        tasks = {t.title: t for t in m.tasks}
    assert len(tasks) == 3
    # draft went only to the approver: PDF + summary with buttons
    to_approver = sent_to(tg, "999")
    assert [c[0] for c in to_approver] == ["sendDocument", "sendMessage"]
    assert "ЧЕРНОВИК" in to_approver[1][1]["text"]
    assert not sent_to(tg, "111") and not sent_to(tg, "-100500")

    # approver presses "Отправить"
    upd = {"update_id": 1, "callback_query": {"id": "cb1", "from": {"id": 999}, "data": f"ap:{mid}",
                                              "message": {"message_id": 2, "chat": {"id": 999, "type": "private"}}}}
    assert client.post("/telegram/webhook", json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "hook"}).status_code == 200
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "done"
        st = {t.title: (t.status, t.deadline) for t in m.tasks}
    assert [c[0] for c in sent_to(tg, "-100500")] == ["sendMessage", "sendDocument"]
    ivan = sent_to(tg, "111")
    assert len(ivan) == 1 and "Исправить ошибки оплаты" in ivan[0][1]["text"]  # deadline stated -> no question
    maria = sent_to(tg, "222")
    assert len(maria) == 2 and "Какой срок" in maria[1][1]["text"]
    assert st["Подготовить креативы для рекламной кампании"][0] == "awaiting_deadline"
    assert st["Найти альтернативного подрядчика по дизайну"][0] == "no_recipient"
    assert any("без получателя" in c[1].get("text", "") for c in sent_to(tg, "999"))

    # Maria answers in free text
    upd = {"update_id": 2, "message": {"message_id": 50, "chat": {"id": 222, "type": "private", "first_name": "Мария"},
                                       "text": "до 20.10"}}
    client.post("/telegram/webhook", json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "hook"})
    with SessionLocal() as s:
        t = s.query(Task).filter_by(title="Подготовить креативы для рекламной кампании").one()
        assert t.deadline.month == 10 and t.deadline.day == 20 and t.deadline_source == "asked" and t.status == "sent"
        assert s.query(DeadlineRequest).filter_by(resolved=False).count() == 0


def test_webhook_requires_secret_and_registers_chats(client, tg):
    assert client.post("/telegram/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "bad"}).status_code == 403
    h = {"X-Telegram-Bot-Api-Secret-Token": "hook"}
    client.post("/telegram/webhook", json={"update_id": 3, "message": {"message_id": 1, "text": "/start",
                "chat": {"id": 555, "type": "private", "first_name": "Олег", "username": "oleg"}}}, headers=h)
    client.post("/telegram/webhook", json={"update_id": 4, "my_chat_member": {"chat": {"id": -1001, "type": "supergroup",
                "title": "Руководство"}, "new_chat_member": {"status": "member"}}}, headers=h)
    with SessionLocal() as s:
        assert s.get(TelegramChat, "555").username == "oleg"
        assert s.get(TelegramChat, "-1001").title == "Руководство"
    assert "chat_id" in sent_to(tg, "555")[0][1]["text"]


def test_no_approval_sends_immediately_with_default_deadlines(client, tg):
    add_people()
    settings_store.set_many({"approval_required": False, "report_chat_ids": ["-1"], "deadline_mode": "default",
                             "default_deadline_days": 3})
    client.post("/demo")
    with SessionLocal() as s:
        m = s.query(Meeting).one()
        assert m.status == "done"
        creatives = [t for t in m.tasks if t.title.startswith("Подготовить креативы")][0]
        assert creatives.deadline_source == "default"
        assert creatives.deadline == deadlines.add_business_days(deadlines.today(), 3)
    assert sent_to(tg, "-1") and not any("Какой срок" in c[1].get("text", "") for c in tg)


def test_edit_draft_then_approve_in_panel(client, tg):
    add_people()
    settings_store.set_many({"approval_required": True, "approver_chat_ids": [], "report_chat_ids": []})
    client.post("/demo")
    with SessionLocal() as s:
        m = s.query(Meeting).one()
        t = [t for t in m.tasks if t.assignee_id is None][0]
        maria = s.query(Employee).filter_by(name="Мария Смирнова").one()
    assert m.status == "awaiting_approval"
    form = {"title": "Новое название", "summary": "Правка", f"title_{t.id}": "Найти подрядчика",
            f"assignee_{t.id}": str(maria.id), f"deadline_{t.id}": "2026-12-01", f"priority_{t.id}": "high",
            "new_title": "Доп. задача", "new_assignee": "", "new_deadline": ""}
    for page in ("/", "/app", "/app/new", "/app/new?source=mic", "/meetings", "/settings", "/employees",
                 f"/meetings/{m.id}"):
        assert client.get(page).status_code == 200
    client.post(f"/meetings/{m.id}/approve", data=form)
    with SessionLocal() as s:
        m = s.get(Meeting, m.id)
        assert m.status == "done" and m.title == "Новое название" and len(m.tasks) == 4
        t2 = s.get(Task, t.id)
        assert t2.assignee_id == maria.id and t2.deadline == dt.date(2026, 12, 1) and t2.deadline_source == "manual"
    assert any("Найти подрядчика" in c[1].get("text", "") for c in sent_to(tg, "222"))


def test_pdf_and_telegram_message_limits(client):
    add_people()
    client.post("/demo")
    with SessionLocal() as s:
        m = s.query(Meeting).one()
        _ = [t.assignee for t in m.tasks]
    pdf = render.report_pdf(m, settings_store.all_settings())
    assert pdf[:4] == b"%PDF" and len(pdf) > 10_000
    m.report = dict(m.report, summary="очень длинный текст " * 1000)
    assert len(render.summary_message(m, {})) <= 4096
    r = client.get(f"/meetings/{m.id}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"


def test_transcribe_chunks_and_payload(monkeypatch, tmp_path):
    wav = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=300:d=150", str(wav)], check=True)
    seen = []

    def fake_chat(model, messages, **kw):
        part = messages[0]["content"][1]
        assert part["type"] == "input_audio" and part["input_audio"]["format"] == "mp3"
        seen.append(messages[0]["content"][0]["text"])
        return '```json\n{"segments": [{"speaker": "Спикер 1", "start": 5, "text": "фрагмент %d"}]}\n```' % len(seen)
    monkeypatch.setattr(transcribe.config, "OPENROUTER_API_KEY", "x")
    monkeypatch.setattr(llm, "chat", fake_chat)
    total, segs = transcribe.transcribe(str(wav), {**settings_store.DEFAULTS, "chunk_minutes": 1})
    assert round(total) == 150 and len(segs) == 3
    assert [s["start"] for s in segs] == [5.0, 65.0, 125.0]
    assert "продолжение записи" in seen[1]


def test_normalize_drops_unknown_employee_and_bad_dates():
    class E:  # minimal employee
        id, name, position = 1, "Иван", ""
        def alias_list(self): return []
    r = analyze.normalize({"title": "X", "tasks": [{"title": "A", "employee_id": 42, "deadline": "до пятницы"},
                                                   {"title": "B", "employee_id": "1", "deadline": "2026-10-09"},
                                                   {"description": "без названия"}]}, [E()])
    assert [t["employee_id"] for t in r["tasks"]] == [None, 1]
    assert r["tasks"][1]["deadline"] == "2026-10-09"


def test_login_landing_and_per_meeting_options(tg):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        assert "SYSAI" in c.get("/").text                       # public landing
        assert c.get("/app", follow_redirects=False).headers["location"] == "/login"
        c.post("/login", data={"username": "other", "password": "pw"})
        assert c.get("/app", follow_redirects=False).status_code == 303
        c.post("/login", data={"username": "SYSAI", "password": "pw"})
        assert c.get("/app").status_code == 200
        add_people()
        # global: approval on; this meeting: approval off, ask deadlines, report to -77
        settings_store.set_many({"approval_required": True, "approver_chat_ids": ["999"], "deadline_mode": "default"})
        c.post("/demo", data={"source": "demo", "deadline_mode": "ask", "send_tasks_to_assignees": "on",
                              "report_chat_ids": ["-77"]})
        with SessionLocal() as s:
            m = s.query(Meeting).one()
            assert m.status == "done" and m.options["approval_required"] is False
        assert [x[0] for x in sent_to(tg, "-77")] == ["sendMessage", "sendDocument"]
        assert not any(x[0] == "sendDocument" for x in sent_to(tg, "999"))  # no draft: approval was off
        assert any("Какой срок" in x[1].get("text", "") for x in sent_to(tg, "222"))
        r = c.post("/settings/mic", json={"on": True})
        assert r.json() == {"auto_ingest": True}
