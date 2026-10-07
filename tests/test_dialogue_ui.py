from app import pipeline, settings_store
from app.db import ConversationEscalation, ConversationMessage, Meeting, SessionLocal


def test_conversation_settings_are_saved_and_paid_models_rejected(client):
    form = {"conversation_model": "test/model:free", "dialogue_enabled": "on",
            "conversation_tone": "Коротко и дружелюбно", "allow_deadline_proposals": "on",
            "secretary_chat_ids_extra": "999,888"}
    assert client.post("/settings", data=form, follow_redirects=False).status_code == 303
    settings = settings_store.all_settings()
    assert settings["conversation_model"] == "test/model:free"
    assert settings["conversation_tone"] == form["conversation_tone"]
    assert settings["secretary_chat_ids"] == ["999", "888"]
    assert settings["dialogue_enabled"] and settings["allow_deadline_proposals"]
    assert client.post("/settings", data={"conversation_model": "paid/model"}).status_code == 400
    assert settings_store.get("conversation_model") == "test/model:free"
    page = client.get("/settings")
    assert "Диалоги с сотрудниками" in page.text and "Бесплатная модель переписки" in page.text


def test_history_and_escalations_visible_and_resolution_is_scoped(client):
    with SessionLocal() as s:
        m = Meeting(title="Тест диалогов", status="done", report={"title": "Тест диалогов", "summary": "Итог"})
        other = Meeting(title="Другая встреча", status="done")
        s.add_all([m, other])
        s.flush()
        issue = ConversationEscalation(meeting_id=m.id, chat_id="111", reason="Нужны полномочия на перенос")
        message = ConversationMessage(meeting_id=m.id, chat_id="111", message_id=1, role="user",
                                      text="Можно перенести на следующую неделю?", status="done")
        s.add_all([issue, message])
        s.commit()
        mid, other_id, issue_id = m.id, other.id, issue.id
    page = client.get(f"/meetings/{mid}")
    assert page.status_code == 200 and "Можно перенести" in page.text and "Нужны полномочия" in page.text
    assert 'id="employee-dialogues"' in page.text
    before = client.get(f"/meetings/{mid}/status").json()["dialogue_version"]
    client.post(f"/meetings/{other_id}/escalations/{issue_id}/resolve")
    with SessionLocal() as s:
        assert s.get(ConversationEscalation, issue_id).status == "open"
    client.post(f"/meetings/{mid}/escalations/{issue_id}/resolve")
    with SessionLocal() as s:
        assert s.get(ConversationEscalation, issue_id).status == "resolved"
    after = client.get(f"/meetings/{mid}/status").json()["dialogue_version"]
    assert before != after


def test_disabled_dialogue_does_not_guess_between_multiple_deadline_requests(monkeypatch, tg):
    from app.db import DeadlineRequest, Employee, Task
    settings_store.set_many({"dialogue_enabled": False})
    with SessionLocal() as s:
        person = Employee(name="Иван", telegram_chat_id="111")
        meeting = Meeting(title="Тест")
        meeting.tasks = [Task(title="Первая", assignee=person), Task(title="Вторая", assignee=person)]
        s.add(meeting)
        s.flush()
        for i, task in enumerate(meeting.tasks):
            s.add(DeadlineRequest(task_id=task.id, chat_id="111", message_id=100+i))
        s.commit()
    pipeline.handle_update({"message": {"message_id": 10, "chat": {"id": 111, "type": "private"}, "text": "до пятницы"}})
    with SessionLocal() as s:
        assert all(task.deadline is None for task in s.query(Task))
    assert any("Уточните" in call[1].get("text", "") for call in tg)


def test_deadline_callback_rejects_foreign_employee_and_repeated_change(tg):
    import datetime as dt
    from app.db import DeadlineRequest, Employee, Task
    with SessionLocal() as s:
        person = Employee(name="Иван", telegram_chat_id="111")
        meeting = Meeting(title="Тест", options={"deadline_mode": "ask"})
        task = Task(title="Отчёт", assignee=person, status="awaiting_deadline")
        meeting.tasks = [task]
        s.add(meeting)
        s.flush()
        s.add(DeadlineRequest(task_id=task.id, chat_id="111", message_id=100))
        s.commit()
        tid = task.id

    def callback(who, code):
        return {"callback_query": {"id": "test", "from": {"id": who}, "data": f"dl:{tid}:{code}",
                                   "message": {"message_id": 100, "chat": {"id": who}}}}

    pipeline.handle_update(callback(222, "d1"))
    with SessionLocal() as s:
        assert s.get(Task, tid).deadline is None
    pipeline.handle_update(callback(111, "d1"))
    with SessionLocal() as s:
        agreed = s.get(Task, tid).deadline
        assert isinstance(agreed, dt.date)
        assert s.query(DeadlineRequest).one().resolved
    pipeline.handle_update(callback(111, "w1"))
    with SessionLocal() as s:
        assert s.get(Task, tid).deadline == agreed
