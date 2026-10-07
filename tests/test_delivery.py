import concurrent.futures
import copy
import datetime as dt
import threading

import httpx
import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app import config, db, delivery, pipeline, settings_store, telegram
from app.db import DeadlineRequest, Delivery, Employee, Meeting, SessionLocal, Task, now


def make_meeting(status="awaiting_approval", task_chats=("111", "222")):
    with SessionLocal() as s:
        meeting = Meeting(title="Тестовая встреча", status=status,
                          report={"title": "Тестовая встреча", "summary": "Итог"}, options={})
        people = [Employee(name=f"Сотрудник {chat}", telegram_chat_id=chat) for chat in task_chats]
        meeting.tasks = [Task(title=f"Задача {chat}", assignee=person) for chat, person in zip(task_chats, people)]
        s.add(meeting)
        s.commit()
        return meeting.id, [task.id for task in meeting.tasks]


def fake_telegram(monkeypatch, fail=None):
    calls = []

    def send(method, chat, payload):
        call = (method, str(chat), copy.deepcopy(payload))
        calls.append(call)
        error = fail(call) if fail else None
        if error:
            raise error
        return {"message_id": len(calls)}

    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(telegram, "send_message",
                        lambda chat, text, buttons=None, reply_to=None: send(
                            "sendMessage", chat, {"text": text, "buttons": buttons}))
    monkeypatch.setattr(telegram, "send_document",
                        lambda chat, filename, content, caption="": send(
                            "sendDocument", chat, {"filename": filename, "document": content, "caption": caption}))
    return calls


def delivery_rows(meeting_id):
    with SessionLocal() as s:
        return s.query(Delivery).filter_by(meeting_id=meeting_id).order_by(Delivery.id).all()


def test_permanent_error_keeps_failed_task_and_marks_partial_delivery(monkeypatch):
    calls = fake_telegram(monkeypatch, lambda call: telegram.TelegramError("blocked") if call[1] == "111" else None)
    meeting_id, task_ids = make_meeting()

    assert pipeline.submit_deliver(meeting_id)

    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        tasks = [s.get(Task, tid) for tid in task_ids]
        rows = s.query(Delivery).filter_by(meeting_id=meeting_id, phase="final").all()
        assert meeting.status == "delivery_failed"
        assert [t.status for t in tasks] == ["delivery_failed", "sent"]
        assert sorted(r.status for r in rows) == ["failed", "sent"]
    assert {call[1] for call in calls} == {"111", "222"}


def test_manual_retry_retries_failed_only_and_does_not_duplicate_sent(monkeypatch):
    fail_first = {"111": True}

    def fail(call):
        if call[1] == "111" and fail_first["111"]:
            fail_first["111"] = False
            return telegram.TelegramError("blocked")
        return None

    calls = fake_telegram(monkeypatch, fail)
    meeting_id, _ = make_meeting()
    assert pipeline.submit_deliver(meeting_id)
    assert [call[1] for call in calls].count("222") == 1

    assert pipeline.retry_delivery(meeting_id)

    assert [call[1] for call in calls].count("111") == 2
    assert [call[1] for call in calls].count("222") == 1
    rows = delivery_rows(meeting_id)
    assert all(row.status == "sent" for row in rows)
    failed_then_retried = next(row for row in rows if row.chat_id == "111")
    assert failed_then_retried.attempts == 2
    assert failed_then_retried.cycle_attempts == 1
    with SessionLocal() as s:
        assert s.get(Meeting, meeting_id).status == "done"


def test_temporary_error_retries_after_due_time(monkeypatch):
    fail_once = {"111": True}

    def fail(call):
        if call[1] == "111" and fail_once["111"]:
            fail_once["111"] = False
            return telegram.TelegramError("rate limited", retryable=True, retry_after=60)
        return None

    calls = fake_telegram(monkeypatch, fail)
    meeting_id, _ = make_meeting(task_chats=("111",))
    assert pipeline.submit_deliver(meeting_id)
    row = delivery_rows(meeting_id)[0]
    assert row.status == "pending" and row.attempts == 1
    assert row.next_attempt_at is not None
    assert row.next_attempt_at.replace(tzinfo=dt.timezone.utc) >= now() + dt.timedelta(seconds=59)
    assert len(calls) == 1

    with SessionLocal() as s:
        s.get(Delivery, row.id).next_attempt_at = now() - dt.timedelta(seconds=1)
        s.commit()
    pipeline.retry_due_deliveries()

    assert len(calls) == 2
    assert delivery_rows(meeting_id)[0].status == "sent"


def test_disabled_telegram_is_a_failed_delivery_not_success(monkeypatch):
    calls = fake_telegram(monkeypatch)
    monkeypatch.setattr(config, "TELEGRAM_MODE", "off")
    meeting_id, task_ids = make_meeting(task_chats=("111",))

    assert pipeline.submit_deliver(meeting_id)

    assert calls == []
    assert delivery_rows(meeting_id)[0].status == "failed"
    with SessionLocal() as s:
        assert s.get(Meeting, meeting_id).status == "delivery_failed"
        assert s.get(Task, task_ids[0]).status == "delivery_failed"


def test_deadline_request_created_only_after_dependent_digest_and_question_succeed(monkeypatch):
    calls = fake_telegram(monkeypatch)
    settings_store.set_many({"deadline_mode": "ask", "send_tasks_to_assignees": True})
    meeting_id, task_ids = make_meeting(task_chats=("111",))

    assert pipeline.submit_deliver(meeting_id)

    rows = delivery_rows(meeting_id)
    digest = next(row for row in rows if row.kind == "tasks")
    question = next(row for row in rows if row.kind == "deadline")
    assert digest.status == question.status == "sent"
    assert question.depends_on == digest.id
    assert len(calls) == 2 and "Какой срок" in calls[1][2]["text"]
    with SessionLocal() as s:
        requests = s.query(DeadlineRequest).filter_by(task_id=task_ids[0]).all()
        assert len(requests) == 1
        assert requests[0].message_id == question.message_id
        assert s.get(Task, task_ids[0]).status == "awaiting_deadline"


def test_deadline_question_waits_for_successful_digest(monkeypatch):
    calls = fake_telegram(monkeypatch, lambda call: telegram.TelegramError("blocked")
                          if "Задача 111" in call[2].get("text", "") else None)
    settings_store.set_many({"deadline_mode": "ask", "send_tasks_to_assignees": True})
    meeting_id, task_ids = make_meeting(task_chats=("111",))

    assert pipeline.submit_deliver(meeting_id)

    rows = delivery_rows(meeting_id)
    digest = next(row for row in rows if row.kind == "tasks")
    question = next(row for row in rows if row.kind == "deadline")
    assert digest.status == "failed" and question.depends_on == digest.id and question.status == "pending"
    assert len(calls) == 1
    with SessionLocal() as s:
        assert s.query(DeadlineRequest).filter_by(task_id=task_ids[0]).count() == 0
        assert s.get(Task, task_ids[0]).status == "delivery_failed"


def test_read_timeout_is_uncertain_and_not_automatically_resent(monkeypatch):
    calls = fake_telegram(monkeypatch, lambda call: httpx.ReadTimeout("response lost"))
    meeting_id, _ = make_meeting(task_chats=("111",))
    assert pipeline.submit_deliver(meeting_id)

    row = delivery_rows(meeting_id)[0]
    assert row.status == "uncertain" and row.attempts == 1
    pipeline.retry_due_deliveries()
    pipeline.retry_delivery(meeting_id)
    assert len(calls) == 1
    assert delivery_rows(meeting_id)[0].status == "uncertain"


def test_approval_claim_is_idempotent_sequentially_and_concurrently(monkeypatch):
    submitted = []

    def record(fn, *args):
        submitted.append(args)

    monkeypatch.setattr(config, "TESTING", False)
    monkeypatch.setattr(pipeline, "POOL", type("RecordingPool", (), {"submit": staticmethod(record)})())
    meeting_id, _ = make_meeting()

    assert pipeline.submit_deliver(meeting_id)
    assert pipeline.submit_deliver(meeting_id) is False

    other_id, _ = make_meeting()
    barrier = threading.Barrier(2)

    def approve():
        barrier.wait()
        return pipeline.submit_deliver(other_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as callers:
        results = list(callers.map(lambda _: approve(), range(2)))

    assert sorted(results) == [False, True]
    assert len(submitted) == 2


def test_recovery_marks_interrupted_send_uncertain_and_sends_pending(monkeypatch):
    calls = fake_telegram(monkeypatch)
    meeting_id, _ = make_meeting(status="sending", task_chats=())
    with SessionLocal() as s:
        s.add_all([
            Delivery(meeting_id=meeting_id, key="final:tasks:111:", phase="final", kind="tasks", chat_id="111",
                     payload={"text": "interrupted"}, task_ids=[], status="sending", attempts=1),
            Delivery(meeting_id=meeting_id, key="final:orphan:222:", phase="final", kind="orphan", chat_id="222",
                     payload={"text": "queued"}, task_ids=[], status="pending", attempts=0),
        ])
        s.commit()

    pipeline.resume_jobs()

    rows = delivery_rows(meeting_id)
    assert [row.status for row in rows] == ["uncertain", "sent"]
    assert rows[0].attempts == 1
    assert len(calls) == 1 and calls[0][1] == "222"


def test_delivery_payloads_are_snapshots_after_settings_change(monkeypatch):
    monkeypatch.setattr(pipeline.render, "report_pdf", lambda meeting, settings: b"pdf snapshot")
    settings_store.set_many({"report_chat_ids": ["report-before"], "send_tasks_to_assignees": True,
                             "deadline_mode": "ask", "company_name": "До"})
    meeting_id, _ = make_meeting(status="delivery_queued", task_chats=("111",))
    with SessionLocal() as s:
        meeting = pipeline.load(s, meeting_id)
        settings = settings_store.for_meeting(meeting.options)
    pipeline._plan_delivery(meeting, settings)
    before = [(row.phase, row.kind, row.chat_id, copy.deepcopy(row.payload), row.document,
               list(row.task_ids), row.depends_on) for row in delivery_rows(meeting_id)]

    settings_store.set_many({"report_chat_ids": ["report-after"], "send_tasks_to_assignees": False,
                             "deadline_mode": "none", "company_name": "После"})

    after = [(row.phase, row.kind, row.chat_id, row.payload, row.document,
              list(row.task_ids), row.depends_on) for row in delivery_rows(meeting_id)]
    assert after == before
    assert {item[2] for item in after} == {"report-before", "111"}
    assert all(item[0] == "final" for item in after)


def test_draft_failure_remains_in_delivery_journal(monkeypatch):
    calls = fake_telegram(monkeypatch, lambda call: telegram.TelegramError("document rejected")
                          if call[0] == "sendDocument" else None)
    monkeypatch.setattr(pipeline.render, "report_pdf", lambda meeting, settings: b"draft")
    monkeypatch.setattr(pipeline.render, "summary_message", lambda meeting, settings, draft=False: "draft summary")
    settings_store.set_many({"approval_required": True, "approver_chat_ids": ["999"]})
    meeting_id, _ = make_meeting(task_chats=())

    pipeline.request_approval(meeting_id)

    rows = delivery_rows(meeting_id)
    assert {(row.phase, row.kind, row.status) for row in rows} == {
        ("draft", "pdf", "failed"), ("draft", "summary", "sent")}
    with SessionLocal() as s:
        assert s.get(Meeting, meeting_id).status == "awaiting_approval"
    assert [call[0] for call in calls] == ["sendDocument", "sendMessage"]


def test_telegram_http_5xx_is_uncertain_and_not_automatically_retried(monkeypatch):
    posts = []
    monkeypatch.setattr(config, "telegram_token", lambda: "fake-token")

    def http_5xx(*args, **kwargs):
        posts.append((args, kwargs))
        return type("Response", (), {"status_code": 502})()

    monkeypatch.setattr(httpx, "post", http_5xx)
    meeting_id, _ = make_meeting(task_chats=("111",))
    assert pipeline.submit_deliver(meeting_id)

    row = delivery_rows(meeting_id)[0]
    assert row.status == "uncertain" and row.attempts == 1
    assert len(posts) == 1
    pipeline.retry_due_deliveries()
    assert len(posts) == 1
    assert delivery_rows(meeting_id)[0].status == "uncertain"


def test_no_recipients_means_ready_not_sent(monkeypatch):
    calls = fake_telegram(monkeypatch)
    meeting_id, _ = make_meeting(task_chats=())
    assert pipeline.submit_deliver(meeting_id)
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        assert m.status == "ready" and m.sent_at is None
        assert m.options["delivery_planned"] is True
    assert calls == []


def test_legacy_approval_is_reissued_on_restart(monkeypatch):
    fake_telegram(monkeypatch)
    monkeypatch.setattr(pipeline.render, "report_pdf", lambda meeting, settings: b"draft")
    settings_store.set_many({"approver_chat_ids": ["999"]})
    meeting_id, _ = make_meeting(task_chats=())
    answers = []
    monkeypatch.setattr(telegram, "answer_callback", lambda callback_id, text="": answers.append(text))
    pipeline.handle_update({"callback_query": {"id": "legacy", "from": {"id": 999}, "data": f"ap:{meeting_id}",
                                               "message": {"chat": {"id": 999}}}})
    assert answers == ["Черновик изменён — откройте новую версию"]
    pipeline.resume_jobs()
    rows = delivery_rows(meeting_id)
    assert len(rows) == 2 and all(row.status == "sent" for row in rows)
    with SessionLocal() as s:
        assert s.get(Meeting, meeting_id).status == "awaiting_approval"
        assert s.get(Meeting, meeting_id).options["approval_revision"] == 1


def test_retryable_delivery_stops_after_max_attempts(monkeypatch):
    calls = fake_telegram(monkeypatch, lambda call: telegram.TelegramError(
        "temporary failure", retryable=True, retry_after=1))
    meeting_id, _ = make_meeting(task_chats=("111",))
    assert pipeline.submit_deliver(meeting_id)

    for expected_attempt in range(2, delivery.MAX_ATTEMPTS + 1):
        row = delivery_rows(meeting_id)[0]
        assert row.status == "pending" and row.attempts == expected_attempt - 1
        with SessionLocal() as s:
            s.get(Delivery, row.id).next_attempt_at = now() - dt.timedelta(seconds=1)
            s.commit()
        pipeline.retry_due_deliveries()

    row = delivery_rows(meeting_id)[0]
    assert row.status == "failed" and row.attempts == delivery.MAX_ATTEMPTS
    assert len(calls) == delivery.MAX_ATTEMPTS
    pipeline.retry_due_deliveries()
    assert len(calls) == delivery.MAX_ATTEMPTS


def test_stale_telegram_draft_callback_cannot_approve_after_panel_save(client, monkeypatch):
    fake_telegram(monkeypatch)
    monkeypatch.setattr(pipeline.render, "report_pdf", lambda meeting, settings: b"draft")
    settings_store.set_many({"approver_chat_ids": ["999"]})
    meeting_id, _ = make_meeting(status="analyzing", task_chats=())
    pipeline.request_approval(meeting_id)
    old_summary = next(row for row in delivery_rows(meeting_id) if row.kind == "summary")
    stale_callback = old_summary.payload["buttons"][0][0]["callback_data"]
    assert stale_callback.endswith(":1")

    response = client.post(f"/meetings/{meeting_id}/save", data={"title": "Правка панели", "summary": "Обновлено"},
                           follow_redirects=False)
    assert response.status_code == 303
    answers = []
    monkeypatch.setattr(telegram, "answer_callback", lambda callback_id, text="": answers.append(text))
    pipeline.handle_update({"callback_query": {
        "id": "stale-callback", "from": {"id": 999}, "data": stale_callback,
        "message": {"message_id": 1, "chat": {"id": 999, "type": "private"}},
    }})

    assert answers == ["Черновик изменён — откройте новую версию"]
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        assert meeting.status == "awaiting_approval"
        assert meeting.title == "Правка панели"
        assert s.query(Delivery).filter_by(meeting_id=meeting_id, phase="final").count() == 0


def test_legacy_draft_callback_without_revision_is_stale_after_panel_save(client, monkeypatch):
    fake_telegram(monkeypatch)
    monkeypatch.setattr(pipeline.render, "report_pdf", lambda meeting, settings: b"draft")
    settings_store.set_many({"approver_chat_ids": ["999"]})
    meeting_id, _ = make_meeting(status="analyzing", task_chats=())
    pipeline.request_approval(meeting_id)

    response = client.post(f"/meetings/{meeting_id}/save", data={"title": "Обновлённая панель"},
                           follow_redirects=False)
    assert response.status_code == 303
    answers = []
    monkeypatch.setattr(telegram, "answer_callback", lambda callback_id, text="": answers.append(text))
    pipeline.handle_update({"callback_query": {
        "id": "legacy-stale-callback", "from": {"id": 999}, "data": f"ap:{meeting_id}",
        "message": {"message_id": 1, "chat": {"id": 999, "type": "private"}},
    }})

    assert answers == ["Черновик изменён — откройте новую версию"]
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        assert meeting.status == "awaiting_approval"
        assert meeting.options["approval_revision"] > 1
        assert s.query(Delivery).filter_by(meeting_id=meeting_id, phase="final").count() == 0


def test_repeated_panel_approval_cannot_overwrite_sent_data_and_done_routes_conflict(client, monkeypatch):
    calls = fake_telegram(monkeypatch)
    settings_store.set_many({"send_tasks_to_assignees": True, "report_chat_ids": []})
    meeting_id, task_ids = make_meeting(task_chats=("111",))
    with SessionLocal() as s:
        task = s.get(Task, task_ids[0])
        employee_id = task.assignee_id
    task_fields = {f"title_{task_ids[0]}": "Утверждённая задача",
                   f"assignee_{task_ids[0]}": str(employee_id)}

    response = client.post(f"/meetings/{meeting_id}/approve", data={
        "title": "Утверждённый отчёт", "summary": "Отправленная версия", **task_fields,
    }, follow_redirects=False)
    assert response.status_code == 303
    assert len(calls) == 1
    second = client.post(f"/meetings/{meeting_id}/approve", data={
        "title": "Перезаписанный отчёт", "summary": "Не должно примениться",
        **{f"title_{task_ids[0]}": "Перезаписанная задача", f"assignee_{task_ids[0]}": str(employee_id)},
    }, follow_redirects=False)
    assert second.status_code == 303

    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        assert meeting.status == "done"
        assert meeting.report["title"] == "Утверждённый отчёт"
        assert meeting.report["summary"] == "Отправленная версия"
        assert s.get(Task, task_ids[0]).title == "Утверждённая задача"
        assert s.query(Delivery).filter_by(meeting_id=meeting_id, status="sent").count() == 1
    assert len(calls) == 1
    assert client.post(f"/meetings/{meeting_id}/save", data={"title": "Поздняя правка"}).status_code == 409
    assert client.post(f"/meetings/{meeting_id}/reanalyze").status_code == 409


def test_request_approval_pdf_failure_rolls_back_status_and_outbox(monkeypatch):
    settings_store.set_many({"approver_chat_ids": ["999"]})
    meeting_id, _ = make_meeting(status="analyzing", task_chats=())
    with SessionLocal() as s:
        s.get(Meeting, meeting_id).options = {"approval_revision": 7, "keep": "value"}
        s.commit()
    monkeypatch.setattr(pipeline.render, "report_pdf", lambda meeting, settings: (_ for _ in ()).throw(
        RuntimeError("PDF render failed")))

    with pytest.raises(RuntimeError, match="PDF render failed"):
        pipeline.request_approval(meeting_id)

    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        assert meeting.status == "analyzing"
        assert meeting.options == {"approval_revision": 7, "keep": "value"}
        assert s.query(Delivery).filter_by(meeting_id=meeting_id).count() == 0


def test_request_approval_enqueue_failure_rolls_back_status_and_partial_outbox(monkeypatch):
    settings_store.set_many({"approver_chat_ids": ["999"]})
    meeting_id, _ = make_meeting(status="analyzing", task_chats=())
    monkeypatch.setattr(pipeline.render, "report_pdf", lambda meeting, settings: b"draft")
    real_enqueue = delivery.enqueue
    calls = []

    def enqueue_then_fail(*args, **kwargs):
        calls.append(kwargs.get("kind", args[3] if len(args) > 3 else None))
        if len(calls) == 2:
            raise RuntimeError("outbox insert failed")
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(delivery, "enqueue", enqueue_then_fail)
    with pytest.raises(RuntimeError, match="outbox insert failed"):
        pipeline.request_approval(meeting_id)

    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        assert meeting.status == "analyzing"
        assert meeting.options == {}
        assert s.query(Delivery).filter_by(meeting_id=meeting_id).count() == 0


def test_init_db_adds_outbox_without_changing_existing_data_or_unique_key():
    meeting_id, task_ids = make_meeting(status="done", task_chats=("111",))
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        meeting.options = {"preserve": ["existing", 3]}
        meeting.report = {"title": "Existing report"}
        task = s.get(Task, task_ids[0])
        task.title = "Existing task"
        s.commit()

    Delivery.__table__.drop(db.engine)
    db.init_db()

    assert "deliveries" in inspect(db.engine).get_table_names()
    unique_sets = {tuple(constraint["column_names"]) for constraint in
                   inspect(db.engine).get_unique_constraints("deliveries")}
    assert ("meeting_id", "key") in unique_sets
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        assert meeting.status == "done"
        assert meeting.options == {"preserve": ["existing", 3]}
        assert meeting.report == {"title": "Existing report"}
        assert s.get(Task, task_ids[0]).title == "Existing task"
        s.add_all([
            Delivery(meeting_id=meeting_id, key="same-key", phase="final", kind="summary", chat_id="1",
                     payload={"text": "one"}, task_ids=[]),
            Delivery(meeting_id=meeting_id, key="same-key", phase="final", kind="summary", chat_id="1",
                     payload={"text": "duplicate"}, task_ids=[]),
        ])
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()


def test_init_db_migrates_cycle_attempts_without_changing_delivery_record():
    meeting_id, _ = make_meeting(status="delivery_failed", task_chats=())
    with SessionLocal() as s:
        row = Delivery(meeting_id=meeting_id, key="legacy-delivery", phase="final", kind="summary",
                       chat_id="report", payload={"text": "preserve"}, task_ids=[], status="failed", attempts=4)
        s.add(row)
        s.commit()
        delivery_id = row.id

    with db.engine.begin() as con:
        con.exec_driver_sql("ALTER TABLE deliveries DROP COLUMN cycle_attempts")
    assert "cycle_attempts" not in {column["name"] for column in inspect(db.engine).get_columns("deliveries")}

    db.init_db()

    assert "cycle_attempts" in {column["name"] for column in inspect(db.engine).get_columns("deliveries")}
    with SessionLocal() as s:
        row = s.get(Delivery, delivery_id)
        assert row.meeting_id == meeting_id
        assert row.key == "legacy-delivery"
        assert row.status == "failed" and row.attempts == 4 and row.cycle_attempts == 0
        assert row.payload == {"text": "preserve"}
