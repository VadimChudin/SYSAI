from app import delivery, settings_store
from app.db import ConversationEscalation, ConversationMessage, Employee, Meeting, SessionLocal, Task


def seed():
    with SessionLocal() as s:
        meeting = Meeting(title="Проверка схемы", status="done", report={"summary": "Тест"})
        employee = Employee(name="Иван", telegram_chat_id="111")
        other = Employee(name="Мария", telegram_chat_id="222")
        meeting.tasks = [Task(title="Отчёт", assignee=employee), Task(title="Бюджет", assignee=other), Task(title="Без исполнителя")]
        s.add(meeting)
        s.flush()
        row = delivery.enqueue(s, meeting.id, "final", "tasks", "111", {"text": "Отчёт"}, task_ids=[meeting.tasks[0].id])
        row.status = "sent"
        queued = delivery.enqueue(s, meeting.id, "final", "tasks", "222", {"text": "Бюджет"}, task_ids=[meeting.tasks[1].id])
        s.commit()
        return meeting.id, row.id, queued.id


def test_board_truthful_statuses_no_fake_activity(client):
    mid, sent_id, pending_id = seed()
    response = client.get(f"/meetings/{mid}/board")
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    data = response.json()
    assert data["summary"] == {"recipients": 2, "delivered": 1, "responded": 0, "attention": 1}
    assert data["people"][0]["label"] == "Доставлено" and not data["people"][0]["active"]
    assert data["people"][1]["state"] == "pending" and not data["people"][1]["active"]
    assert len(data["unassigned"]) == 1
    assert client.get(f"/meetings/{mid}/board").json()["version"] == data["version"]
    with SessionLocal() as s:
        from app.db import Delivery
        s.get(Delivery, pending_id).status = "sending"
        s.commit()
    new = client.get(f"/meetings/{mid}/board").json()
    assert new["people"][1]["active"] and new["version"] != data["version"]


def test_board_response_escalation_and_dialogue_activity(client):
    mid, _, _ = seed()
    with SessionLocal() as s:
        message = ConversationMessage(meeting_id=mid, chat_id="111", message_id=1, role="user", text="Уточнение", status="pending")
        s.add(message)
        s.commit()
        message_id = message.id
    person = client.get(f"/meetings/{mid}/board").json()["people"][0]
    assert person["responded"] and not person["active"] and person["state"] == "pending"
    with SessionLocal() as s:
        s.get(ConversationMessage, message_id).status = "processing"
        s.commit()
    assert client.get(f"/meetings/{mid}/board").json()["people"][0]["active"]
    with SessionLocal() as s:
        s.get(ConversationMessage, message_id).status = "done"
        s.add(ConversationEscalation(meeting_id=mid, chat_id="111", reason="Вопрос"))
        s.commit()
    person = client.get(f"/meetings/{mid}/board").json()["people"][0]
    assert person["state"] == "attention" and not person["active"]
    assert person["latest_message"] == "Уточнение" and person["history_url"].endswith("#dialogue-111")


def test_board_missing_recipient_disabled_and_auth(client):
    mid, _, _ = seed()
    with SessionLocal() as s:
        s.query(Employee).filter_by(telegram_chat_id="222").one().telegram_chat_id = ""
        s.commit()
    assert client.get(f"/meetings/{mid}/board").json()["people"][1]["state"] == "no_recipient"
    assert client.get("/meetings/99999/board").status_code == 404
    client.get("/logout")
    assert client.get(f"/meetings/{mid}/board", follow_redirects=False).status_code == 303


def test_board_disabled_and_detail_versions_refresh_without_fake_work(client):
    from app.db import Delivery
    mid, _, pending_id = seed()
    with SessionLocal() as s:
        s.query(Delivery).filter_by(meeting_id=mid).delete()
        meeting = s.get(Meeting, mid)
        meeting.options = {"send_tasks_to_assignees": False}
        s.commit()
    data = client.get(f"/meetings/{mid}/board").json()
    assert all(person["state"] == "disabled" and not person["active"] for person in data["people"])
    with SessionLocal() as s:
        s.add(ConversationMessage(meeting_id=mid, chat_id="111", message_id=40, role="assistant",
                                  text="Сохранённый ответ", status="done"))
        s.commit()
    updated = client.get(f"/meetings/{mid}/board").json()
    assert updated["version"] != data["version"]
    assert updated["summary"]["responded"] == 0
    assert not any(person["active"] for person in updated["people"])
