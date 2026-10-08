"""Unsafe extraction is a reviewable report, never an executable task or automatic send."""
import datetime as dt
import json

import pytest

from app import analyze, config, llm, pipeline, settings_store
from app.db import Delivery, Employee, Meeting, SessionLocal, Task, now


DATE = dt.date(2026, 10, 7)


def report(tasks=None):
    return {"title": "План", "summary": "Обсудили план работы.", "participants": [], "topics": [],
            "decisions": [], "tasks": tasks or [], "open_questions": [], "risks": [],
            "notes": [], "next_meeting": None}


def proposed(title, quote, employee_id=None, name=None):
    return {"title": title, "description": title, "employee_id": employee_id, "assignee_name": name,
            "deadline": None, "deadline_quote": None, "priority": "medium", "time": "00:00", "quote": quote}


def never(*args, **kwargs):
    raise AssertionError("Review must not invoke this processing/send operation")


def test_pipeline_review_forces_approval_before_any_final_sends(monkeypatch, tg):
    settings_store.set_many({"approval_required": False, "approver_chat_ids": ["999"],
                             "report_chat_ids": ["-1"], "send_tasks_to_assignees": True,
                             "bitrix_enabled": True, "deadline_mode": "default"})
    with SessionLocal() as s:
        employee = Employee(name="Анна", telegram_chat_id="123", bitrix_user_id="42")
        s.add(employee)
        s.commit()
        employee_id = employee.id
    spoken = "Анна проверит план. Ольга подготовит бюджет."
    candidates = [proposed("Проверить план", "Анна проверит план.", employee_id, "Анна"),
                  proposed("Подготовить бюджет", "Ольга подготовит бюджет.", None, "Ольга"),
                  proposed("Спорная задача", "Анна отправит секретные данные.", employee_id, "Анна")]
    calls = []

    def chat(*args, **kwargs):
        calls.append(1)
        return json.dumps(report(candidates), ensure_ascii=False)

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(pipeline.transcribe, "transcribe", never)
    monkeypatch.setattr(pipeline.bitrix, "create_task", never)
    mid = pipeline.create_meeting("test.wav", meeting_date=DATE, options={"approval_required": False})
    pipeline._set(mid, transcript=[{"start": 0, "end": 4, "speaker": "A", "text": spoken}], duration_sec=4)
    pipeline.process(mid)

    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "awaiting_approval" and m.approved_at is None
        assert m.options["approval_required"] is True
        assert m.report["requires_review"] is True
        assert [t.title for t in m.tasks] == ["Проверить план", "Подготовить бюджет"]
        assert m.tasks[1].assignee_id is None and m.tasks[1].assignee_name == "Ольга"
        assert "Спорная задача" in m.report["notes"][0]
        task_ids = [t.id for t in m.tasks]
        assert not s.query(Delivery).filter_by(meeting_id=mid, phase="final").count()
        assert {d.phase for d in s.query(Delivery).filter_by(meeting_id=mid)} == {"draft"}
    assert len(calls) == 2
    assert pipeline.meeting_settings(mid)["approval_required"] is True
    assert settings_store.get("approval_required") is False  # no global settings changes
    assert tg and {str(call[1]["chat_id"]) for call in tg} == {"999"}

    before = len(tg)
    pipeline.resume_jobs()
    pipeline.retry_due_deliveries()
    pipeline.process(mid)
    assert len(tg) == before and len(calls) == 2

    # Human approval may send only the verified tasks; the disputed candidate is still a note.
    settings_store.set_many({"bitrix_enabled": False})
    assert pipeline.submit_deliver(mid)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "done" and m.approved_at is not None
        assert [t.id for t in m.tasks] == task_ids
        task_deliveries = s.query(Delivery).filter_by(meeting_id=mid, phase="final", kind="tasks").all()
        assert len(task_deliveries) == 1 and task_deliveries[0].task_ids == [task_ids[0]]
        assert "Спорная задача" not in task_deliveries[0].payload["text"]
        assert "Спорная задача" in m.report["notes"][0]


@pytest.mark.parametrize("name", [None, "Ольга"])
def test_pipeline_without_employees_still_creates_report_and_preserves_name(monkeypatch, name):
    settings_store.set_many({"approval_required": False, "approver_chat_ids": [], "report_chat_ids": []})
    spoken = "Подготовить план." if name is None else "Ольга подготовит план."
    candidate = report([proposed("Подготовить план", spoken, None, name)])
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm, "chat", lambda *a, **kw: json.dumps(candidate))
    mid = pipeline.create_meeting("test.wav", meeting_date=DATE)
    pipeline._set(mid, transcript=[{"start": 0, "end": 4, "speaker": "A", "text": spoken}], duration_sec=4)
    pipeline.process(mid)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "ready"
        assert m.report and m.report["requires_review"] is False
        assert len(m.tasks) == 1 and m.tasks[0].assignee_id is None
        assert m.tasks[0].assignee_name == (name or "")


@pytest.mark.parametrize("status", ["queued", "transcribing", "analyzing"])
def test_resume_saved_review_report_overrides_disabled_approval_and_keeps_tasks(monkeypatch, status):
    settings_store.set_many({"approval_required": False, "approver_chat_ids": [], "report_chat_ids": ["-1"]})
    mid = pipeline.create_meeting("test.wav", options={"approval_required": False})
    saved = {**report(), "requires_review": True, "notes": ["Требует проверки: Спорная задача"]}
    pipeline._set(mid, status=status, report=saved, audio_path="/missing-after-restart.wav")
    with SessionLocal() as s:
        t = Task(meeting_id=mid, title="Проверенная задача", assignee_name="Ольга")
        s.add(t)
        s.commit()
        tid = t.id
    monkeypatch.setattr(pipeline.analyze, "analyze", never)
    monkeypatch.setattr(pipeline.transcribe, "transcribe", never)
    monkeypatch.setattr(pipeline, "_plan_delivery", never)
    pipeline.resume_jobs()
    pipeline.resume_jobs()
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "awaiting_approval" and m.approved_at is None
        assert m.options["approval_required"] is True and m.options["approval_revision"] == 1
        assert [t.id for t in m.tasks] == [tid]
        assert m.tasks[0].assignee_name == "Ольга"
        assert not s.query(Delivery).filter_by(meeting_id=mid, phase="final").count()


@pytest.mark.parametrize("status", ["delivery_queued", "sending", "delivery_retry", "delivery_failed"])
def test_unapproved_review_final_resume_and_retries_cannot_send(monkeypatch, tg, status):
    settings_store.set_many({"approval_required": False, "approver_chat_ids": [], "report_chat_ids": ["-1"]})
    mid = pipeline.create_meeting("test.wav", options={"approval_required": False})
    pipeline._set(mid, status=status, report={**report(), "requires_review": True})
    with SessionLocal() as s:
        s.add(Delivery(meeting_id=mid, key="old-final", phase="final", kind="summary", chat_id="-1",
                       payload={"text": "Must not send before approval"}, status="pending", next_attempt_at=now()))
        s.commit()
    monkeypatch.setattr(pipeline, "_plan_delivery", never)
    pipeline.resume_jobs()
    pipeline.retry_due_deliveries()
    pipeline.retry_delivery(mid)
    pipeline.resume_jobs()
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "awaiting_approval" and m.approved_at is None
        assert m.options["approval_required"] is True
        assert s.query(Delivery).filter_by(meeting_id=mid, key="old-final").one().status == "pending"
    assert not tg


def test_automatic_submit_cannot_mark_review_report_approved(monkeypatch, tg):
    settings_store.set_many({"approval_required": False, "approver_chat_ids": [], "report_chat_ids": ["-1"]})
    mid = pipeline.create_meeting("test.wav", options={"approval_required": False})
    pipeline._set(mid, status="analyzing", report={**report(), "requires_review": True})
    monkeypatch.setattr(pipeline, "_plan_delivery", never)
    assert not pipeline.submit_deliver(mid, allowed=("analyzing",))
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "awaiting_approval" and m.approved_at is None
        assert m.options["approval_required"] is True
        assert not s.query(Delivery).filter_by(meeting_id=mid, phase="final").count()
    assert not tg


def test_approved_review_can_resume_final_delivery(monkeypatch, tg):
    settings_store.set_many({"approval_required": False, "approver_chat_ids": [], "report_chat_ids": []})
    mid = pipeline.create_meeting("test.wav")
    pipeline._set(mid, status="sending", report={**report(), "requires_review": True}, approved_at=now())
    with SessionLocal() as s:
        s.add(Delivery(meeting_id=mid, key="approved-final", phase="final", kind="summary", chat_id="-1",
                       payload={"text": "Human approved report"}, status="pending"))
        s.commit()
    monkeypatch.setattr(pipeline, "_plan_delivery", never)
    pipeline.resume_jobs()
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "done" and m.options["approval_required"] is True
        assert s.query(Delivery).filter_by(meeting_id=mid, key="approved-final").one().status == "sent"
    assert len(tg) == 1 and str(tg[0][1]["chat_id"]) == "-1"
