import datetime as dt
import html
import concurrent.futures
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from app import config, pipeline, render, report_archive, settings_store, telegram
from app.db import Delivery, Employee, Meeting, ReportVersion, SessionLocal, Task


PDF_BYTES = b"%PDF-archive-snapshot"


def make_meeting(*, title="Совещание", status="awaiting_approval", source="web", filename="call.wav",
                 meeting_date=dt.date(2026, 10, 1), assignee=True):
    with SessionLocal() as s:
        employee = Employee(name="Анна", telegram_chat_id="111") if assignee else None
        meeting = Meeting(
            title=title,
            status=status,
            source=source,
            filename=filename,
            meeting_date=meeting_date,
            duration_sec=123,
            transcript=[{"speaker": "Анна", "start": 0, "end": 2, "text": "Обсуждение"}],
            report={"title": title, "summary": "Исходный итог", "decisions": ["Решение"]},
            options={},
        )
        if assignee:
            meeting.tasks = [Task(title="Согласовать", assignee=employee,
                                  deadline=dt.date(2026, 10, 5), deadline_source="stated",
                                  source_quote="к пятому", source_time="01:02")]
        s.add(meeting)
        s.commit()
        return meeting.id, meeting.tasks[0].id if meeting.tasks else None, employee.id if employee else None


@pytest.fixture
def fake_pdf(monkeypatch):
    calls = []

    def make(meeting, settings):
        calls.append((meeting.id, dict(settings)))
        return PDF_BYTES

    monkeypatch.setattr(render, "report_pdf", make)
    return calls


def versions(mid):
    with SessionLocal() as s:
        return s.query(ReportVersion).filter_by(meeting_id=mid).order_by(ReportVersion.version).all()


def approve_without_recipients(monkeypatch, mid):
    settings_store.set_many({"report_chat_ids": [], "approver_chat_ids": []})
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    return pipeline.submit_deliver(mid)


def test_approval_archives_original_snapshot_and_pdf_is_immutable(monkeypatch, client, fake_pdf):
    mid, task_id, _ = make_meeting()
    settings_store.set_many({"company_name": "До утверждения", "accent_color": "#123456",
                             "include_transcript_in_pdf": True})
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")

    assert pipeline.submit_deliver(mid)

    with SessionLocal() as s:
        version = s.query(ReportVersion).filter_by(meeting_id=mid).one()
        assert version.origin == "approval"
        assert version.snapshot["report"]["summary"] == "Исходный итог"
        assert version.snapshot["tasks"][0]["id"] == task_id
        assert version.snapshot["tasks"][0]["deadline"] == "2026-10-05"
        assert version.snapshot["tasks"][0]["assignee_name"] == "Анна"
        assert version.snapshot["transcript"][0]["text"] == "Обсуждение"
        assert version.snapshot["formatting"] == {
            "company_name": "До утверждения", "accent_color": "#123456", "include_transcript_in_pdf": True
        }
        first_pdf = bytes(version.pdf)
        task = s.get(Task, task_id)
        task.deadline = dt.date(2027, 1, 1)
        task.title = "Изменённая задача"
        meeting = s.get(Meeting, mid)
        meeting.report = {"title": "Изменённый отчёт"}
        s.commit()

    settings_store.set_many({"company_name": "После утверждения", "accent_color": "#ffffff",
                             "include_transcript_in_pdf": False})
    response = client.get(f"/meetings/{mid}/pdf")
    assert response.status_code == 200
    assert response.content == first_pdf == PDF_BYTES
    assert len(versions(mid)) == 1
    assert len(fake_pdf) == 1


def test_final_delivery_sends_archived_pdf_bytes(monkeypatch, fake_pdf):
    mid, _, _ = make_meeting()
    sent = []
    settings_store.set_many({"report_chat_ids": ["111"], "approver_chat_ids": [],
                             "send_tasks_to_assignees": False})
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(telegram, "send_message", lambda *args, **kwargs: {"message_id": 1})
    monkeypatch.setattr(telegram, "send_document",
                        lambda chat, filename, content, caption="": sent.append(bytes(content)) or {"message_id": 2})

    assert pipeline.submit_deliver(mid)

    assert versions(mid)[0].pdf == PDF_BYTES
    assert sent == [PDF_BYTES]
    assert len(fake_pdf) == 1


def test_approval_without_recipients_still_archives_and_finishes_ready(monkeypatch, fake_pdf):
    mid, _, _ = make_meeting(assignee=False)
    assert approve_without_recipients(monkeypatch, mid)
    with SessionLocal() as s:
        assert s.get(Meeting, mid).status == "ready"
    assert versions(mid)[0].pdf == PDF_BYTES
    assert len(fake_pdf) == 1


def test_duplicate_approval_creates_one_version(monkeypatch, fake_pdf):
    mid, _, _ = make_meeting(assignee=False)
    assert approve_without_recipients(monkeypatch, mid)
    assert pipeline.submit_deliver(mid) is False
    assert len(versions(mid)) == 1
    assert len(fake_pdf) == 1


def test_pdf_render_failure_rolls_back_approval_claim_and_archive(monkeypatch):
    mid, _, _ = make_meeting(assignee=False)
    monkeypatch.setattr(render, "report_pdf", lambda *_: (_ for _ in ()).throw(RuntimeError("render failed")))

    with pytest.raises(RuntimeError, match="render failed"):
        pipeline.submit_deliver(mid)

    with SessionLocal() as s:
        assert s.get(Meeting, mid).status == "awaiting_approval"
        assert s.query(Delivery).filter_by(meeting_id=mid, phase="final").count() == 0
        assert s.query(ReportVersion).filter_by(meeting_id=mid).count() == 0


def test_panel_approval_archives_edits_and_new_task_after_flush(monkeypatch, client, fake_pdf):
    mid, task_id, employee_id = make_meeting()
    response = client.post(f"/meetings/{mid}/approve", data={
        "title": "Правленое название",
        "summary": "Правленый итог",
        f"title_{task_id}": "Обновлённая задача",
        f"desc_{task_id}": "Подробности",
        f"assignee_{task_id}": str(employee_id),
        f"deadline_{task_id}": "2026-10-12",
        f"priority_{task_id}": "high",
        "new_title": "Новая задача",
        "new_assignee": str(employee_id),
        "new_deadline": "2026-10-20",
        "new_priority": "medium",
    }, follow_redirects=False)

    assert response.status_code == 303
    snapshot = versions(mid)[0].snapshot
    assert snapshot["report"]["title"] == "Правленое название"
    assert snapshot["report"]["summary"] == "Правленый итог"
    assert [task["title"] for task in snapshot["tasks"]] == ["Обновлённая задача", "Новая задача"]
    assert snapshot["tasks"][0]["deadline"] == "2026-10-12"
    assert snapshot["tasks"][0]["deadline_source"] == "manual"
    assert snapshot["tasks"][1]["deadline"] == "2026-10-20"
    assert snapshot["tasks"][1]["assignee_name"] == "Анна"
    assert len(fake_pdf) == 1


def test_telegram_approval_reuses_draft_pdf_after_global_formatting_changes(monkeypatch):
    mid, _, _ = make_meeting(assignee=False)
    settings_store.set_many({"approver_chat_ids": ["999"], "report_chat_ids": ["999"],
                             "send_tasks_to_assignees": False, "company_name": "Черновик Ко"})
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake-token")
    rendered = []
    sent_documents = []

    def render_for_settings(meeting, settings):
        pdf = f"PDF|{settings['company_name']}|{meeting.title}|{meeting.report['summary']}".encode()
        rendered.append(pdf)
        return pdf

    monkeypatch.setattr(render, "report_pdf", render_for_settings)
    monkeypatch.setattr(telegram, "send_message", lambda *args, **kwargs: {"message_id": 1})
    monkeypatch.setattr(telegram, "send_document",
                        lambda chat, filename, content, caption="": sent_documents.append(bytes(content)) or
                        {"message_id": len(sent_documents)})
    monkeypatch.setattr(telegram, "answer_callback", lambda *args, **kwargs: None)
    monkeypatch.setattr(telegram, "edit_buttons", lambda *args, **kwargs: None)

    pipeline.request_approval(mid)
    with SessionLocal() as s:
        draft = s.query(Delivery).filter_by(meeting_id=mid, phase="draft", kind="pdf").one()
        draft_pdf = bytes(draft.document)
        callback_data = s.query(Delivery).filter_by(meeting_id=mid, phase="draft", kind="summary").one().payload[
            "buttons"][0][0]["callback_data"]
        assert draft_pdf == rendered[0]

    settings_store.set_many({"company_name": "Текущая Компания"})
    pipeline.handle_update({"callback_query": {
        "id": "approve-draft", "from": {"id": 999}, "data": callback_data,
        "message": {"message_id": 10, "chat": {"id": 999, "type": "private"}},
    }})

    archived = versions(mid)[0]
    assert archived.pdf == draft_pdf
    assert archived.snapshot["formatting"]["company_name"] == "Черновик Ко"
    assert archived.snapshot["report"]["summary"] == "Исходный итог"
    assert archived.snapshot["tasks"] == []
    assert len(rendered) == 1
    assert len(sent_documents) == 2
    assert sent_documents == [draft_pdf, draft_pdf]
    with SessionLocal() as s:
        final_delivery = s.query(Delivery).filter_by(meeting_id=mid, phase="final", kind="pdf").one()
        assert bytes(final_delivery.document) == draft_pdf


def test_panel_approval_rerenders_when_it_changes_draft_title(monkeypatch, client):
    mid, _, _ = make_meeting(assignee=False)
    settings_store.set_many({"approver_chat_ids": ["999"], "report_chat_ids": [],
                             "company_name": "Черновик Ко"})
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake-token")
    rendered = []

    def render_for_title(meeting, settings):
        pdf = f"PDF|{settings['company_name']}|{meeting.title}".encode()
        rendered.append(pdf)
        return pdf

    monkeypatch.setattr(render, "report_pdf", render_for_title)
    monkeypatch.setattr(telegram, "send_message", lambda *args, **kwargs: {"message_id": 1})
    monkeypatch.setattr(telegram, "send_document", lambda *args, **kwargs: {"message_id": 2})
    pipeline.request_approval(mid)
    draft_pdf = rendered[0]

    response = client.post(f"/meetings/{mid}/approve", data={
        "title": "Название панели",
        "summary": "Отредактированный итог",
    }, follow_redirects=False)

    assert response.status_code == 303
    assert len(rendered) == 2
    assert rendered[1] != draft_pdf
    archived = versions(mid)[0]
    assert archived.pdf == rendered[1]
    assert archived.snapshot["report"]["title"] == "Название панели"
    assert archived.snapshot["report"]["summary"] == "Отредактированный итог"


def test_concurrent_approval_claim_creates_only_one_report_version(monkeypatch, fake_pdf):
    mid, _, _ = make_meeting(assignee=False)
    settings_store.set_many({"report_chat_ids": [], "approver_chat_ids": []})
    monkeypatch.setattr(config, "TESTING", False)
    monkeypatch.setattr(pipeline, "POOL", type("RecordingPool", (), {"submit": staticmethod(lambda *args: None)})())
    barrier = threading.Barrier(2)

    def approve():
        barrier.wait()
        return pipeline.submit_deliver(mid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: approve(), range(2)))

    assert sorted(results) == [False, True]
    assert len(versions(mid)) == 1
    assert len(fake_pdf) == 1


def test_rejection_does_not_create_report_version():
    mid, _, _ = make_meeting()
    from app import pipeline as app_pipeline

    app_pipeline.reject(mid)

    with SessionLocal() as s:
        assert s.get(Meeting, mid).status == "rejected"
        assert s.query(ReportVersion).filter_by(meeting_id=mid).count() == 0


def test_meetings_filters_casefold_filename_dates_and_literal_percent(client):
    matching, _, _ = make_meeting(title="Обсуждение БЮДЖЕТА", filename="Совещание 100%.wav")
    make_meeting(title="Другое", filename="бюджетный-план.wav", status="done", source="api",
                 meeting_date=dt.date(2026, 9, 30), assignee=False)
    make_meeting(title="Несовпадающее", meeting_date=dt.date(2026, 10, 2), assignee=False)

    response = client.get("/meetings", params={"q": "бюджет", "status": "awaiting_approval", "source": "web",
                                                "date_from": "2026-10-01", "date_to": "2026-10-01"})
    assert response.status_code == 200
    assert "Обсуждение БЮДЖЕТА" in response.text
    assert "Другое" not in response.text
    assert "Несовпадающее" not in response.text

    by_filename = client.get("/meetings", params={"q": "СОВЕЩАНИЕ"})
    assert by_filename.status_code == 200
    assert "Обсуждение БЮДЖЕТА" in by_filename.text
    literal_percent = client.get("/meetings", params={"q": "%"})
    assert literal_percent.status_code == 200
    assert "Обсуждение БЮДЖЕТА" in literal_percent.text
    assert matching > 0


def test_meetings_filters_reject_invalid_dates(client):
    for params in ({"date_from": "not-a-date"}, {"date_to": "2026-02-30"},
                   {"date_from": "2026-10-02", "date_to": "2026-10-01"}):
        assert client.get("/meetings", params=params).status_code == 400


def test_meetings_pagination_is_24_and_preserves_filters(client):
    with SessionLocal() as s:
        s.add_all(Meeting(title=f"Team review {n:02}", status="done", source="web",
                          meeting_date=dt.date(2026, 10, 1), filename="review.wav", report=None)
                   for n in range(27))
        s.commit()

    filters = {"q": "Team", "status": "done", "source": "web", "date_from": "2026-10-01",
               "date_to": "2026-10-01"}
    first = client.get("/meetings", params=filters)
    assert first.status_code == 200
    assert "Страница 1 из 2" in first.text
    next_link = html.unescape(next(line.split('href="', 1)[1].split('"', 1)[0] for line in first.text.splitlines()
                                   if 'rel="next"' in line))
    query = parse_qs(urlparse(next_link).query)
    assert query == {**{key: [value] for key, value in filters.items()}, "p": ["2"]}
    second = client.get(next_link)
    assert second.status_code == 200
    assert "Страница 2 из 2" in second.text
    assert "Team review 00" in second.text
    assert "Team review 26" not in second.text


def test_archived_pdf_requires_auth_and_meeting_scope(client, fake_pdf):
    mid, _, _ = make_meeting(assignee=False)
    other_mid, _, _ = make_meeting(assignee=False)
    with SessionLocal() as s:
        version = report_archive.ensure(s, s.get(Meeting, mid), settings_store.all_settings())
        s.commit()
        version_id = version.id

    from fastapi.testclient import TestClient
    from app.main import app

    unauthenticated = TestClient(app).get(f"/meetings/{mid}/reports/{version_id}/pdf", follow_redirects=False)
    assert unauthenticated.status_code == 303
    assert client.get(f"/meetings/{other_mid}/reports/{version_id}/pdf").status_code == 404
    assert client.get(f"/meetings/{mid}/reports/999999/pdf").status_code == 404
    assert client.get(f"/meetings/{mid}/reports/{version_id}/pdf").content == PDF_BYTES


def test_old_pdf_adopts_stored_final_delivery_once(monkeypatch, client, fake_pdf):
    mid, _, _ = make_meeting(status="done", assignee=False)
    with SessionLocal() as s:
        s.add(Delivery(meeting_id=mid, key="existing-final-pdf", phase="final", kind="pdf", chat_id="111",
                       payload={"filename": "existing.pdf"}, document=b"stored-delivery-pdf", status="sent"))
        s.commit()

    first = client.get(f"/meetings/{mid}/pdf")
    second = client.get(f"/meetings/{mid}/pdf")
    assert first.content == second.content == b"stored-delivery-pdf"
    assert versions(mid)[0].origin == "delivery_recovery"
    assert len(versions(mid)) == 1
    assert fake_pdf == []


def test_old_pdf_legacy_recovery_generates_and_archives_once(monkeypatch, client, fake_pdf):
    mid, _, _ = make_meeting(status="done", assignee=False)

    first = client.get(f"/meetings/{mid}/pdf")
    second = client.get(f"/meetings/{mid}/pdf")
    assert first.content == second.content == PDF_BYTES
    assert versions(mid)[0].origin == "legacy"
    assert len(versions(mid)) == 1
    assert len(fake_pdf) == 1


def test_deleting_meeting_cascades_report_versions(fake_pdf):
    mid, _, _ = make_meeting(assignee=False)
    with SessionLocal() as s:
        report_archive.ensure(s, s.get(Meeting, mid), settings_store.all_settings())
        s.commit()
    assert len(versions(mid)) == 1

    with SessionLocal() as s:
        s.delete(s.get(Meeting, mid))
        s.commit()

    assert versions(mid) == []
