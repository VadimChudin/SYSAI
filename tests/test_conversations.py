import datetime as dt
import json
import threading

import pytest

from app import config, conversations, deadlines, delivery, llm, settings_store
from app.db import (ConversationEscalation, ConversationMessage, Delivery, Employee, Meeting, SessionLocal, Task)


def seed(tasks_by_chat=None, deadline_mode="ask"):
    tasks_by_chat = tasks_by_chat or {
        "101": [("Подготовить квартальный отчет", None)],
        "202": [("Согласовать бюджет проекта", None)],
        "303": [("Обновить план найма", None)],
    }
    ids, digests = {}, {}
    with SessionLocal() as s:
        meeting = Meeting(title="Планирование", status="done", options={"deadline_mode": deadline_mode},
                          report={"summary": "Обсудили план работ."})
        people = {chat: Employee(name=f"Сотрудник {chat}", telegram_chat_id=chat)
                  for chat in tasks_by_chat}
        meeting.tasks = [Task(title=title, deadline=deadline, status="sent", assignee=people[chat],
                              source_quote=f"Источник: {title}")
                         for chat, specs in tasks_by_chat.items() for title, deadline in specs]
        s.add(meeting)
        s.flush()
        for chat in tasks_by_chat:
            task_ids = [task.id for task in meeting.tasks if task.assignee.telegram_chat_id == chat]
            row = delivery.enqueue(s, meeting.id, "final", "tasks", chat,
                                   {"text": "список задач"}, task_ids=task_ids)
            row.status, row.message_id = "sent", 800 + len(digests)
            digests[chat] = row.message_id
            ids[chat] = task_ids
        s.commit()
        return meeting.id, ids, digests


def send_update(chat, message_id, text, reply_to=None):
    message = {"message_id": message_id, "chat": {"id": int(chat)}, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return message


def model_reply(monkeypatch, action="reply", reply="Ответ", task_id=None, deadline=None, reason=""):
    captured = []

    def fake_chat(model, messages, **kwargs):
        captured.append((model, messages))
        return json.dumps({"action": action, "reply": reply, "task_id": task_id,
                           "deadline": deadline, "reason": reason}, ensure_ascii=False)

    monkeypatch.setattr(llm, "chat", fake_chat)
    return captured


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    settings_store.set_many({"dialogue_enabled": True, "conversation_model": "openrouter/free",
                             "allow_deadline_proposals": True, "secretary_chat_ids": [],
                             "approver_chat_ids": []})
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake-token")
    conversations._queues.clear()
    conversations._scheduled.clear()


def test_chat_scope_reply_chain_and_only_sent_assistant_history(monkeypatch, tg):
    _, ids, digests = seed()
    captured = model_reply(monkeypatch, reply="Задача по отчёту в работе")

    assert conversations.accept(send_update("101", 11, "Как продвигается?", digests["101"]))
    assert conversations.accept(send_update("101", 12, "Есть вопросы?"))

    assert len(captured) == 2
    first_payload = json.loads(captured[0][1][1]["content"])
    assert [task["id"] for task in first_payload["tasks"]] == ids["101"]
    assert all("бюджет" not in task["title"] for task in first_payload["tasks"])
    second_payload = json.loads(captured[1][1][1]["content"])
    assert [row["role"] for row in second_payload["history"][-2:]] == ["user", "assistant"]
    with SessionLocal() as s:
        assert s.query(ConversationMessage).filter_by(chat_id="101", role="assistant", status="done").count() == 2
        assert s.query(ConversationMessage).filter_by(chat_id="202").count() == 0
    sent_payloads = [call[1] for call in tg if call[0] == "sendMessage"]
    assert sent_payloads[0]["reply_parameters"]["message_id"] == 11


def test_scope_includes_all_employee_tasks_but_not_other_employees(monkeypatch):
    _, ids, _ = seed({"101": [("Подготовить квартальный отчет", None), ("Согласовать бюджет", None)],
                      "202": [("Обновить план найма", None)], "303": [("Рассчитать маржу", None)]})
    captured = model_reply(monkeypatch)

    conversations.accept(send_update("101", 13, "Какие у меня задачи?"))

    payload = json.loads(captured[0][1][1]["content"])
    assert {task["id"] for task in payload["tasks"]} == set(ids["101"])
    assert all(task["title"] != "Рассчитать маржу" for task in payload["tasks"])


def test_deadline_proposal_requires_unambiguous_task_and_exact_parsed_date(monkeypatch):
    meeting_id, ids, _ = seed({"101": [("Подготовить отчет", None), ("Согласовать бюджет", None)]})
    monkeypatch.setattr(deadlines, "today", lambda: dt.date(2026, 10, 7))
    model_reply(monkeypatch, action="set_deadline", task_id=ids["101"][0], deadline="2026-10-09")

    conversations.accept(send_update("101", 21, "До пятницы"))

    with SessionLocal() as s:
        assert all(s.get(Task, task_id).deadline is None for task_id in ids["101"])
        inbound = s.query(ConversationMessage).filter_by(chat_id="101", message_id=21).one()
        assert inbound.result["action"] == "reply"
        assert "уточните" in inbound.result["reply"].lower()

    # A title mention uniquely identifies the intended task; the user date must still match the parser.
    model_reply(monkeypatch, action="set_deadline", task_id=ids["101"][0], deadline="2026-10-09")
    conversations.accept(send_update("101", 22, "До пятницы для отчета"))
    with SessionLocal() as s:
        task = s.get(Task, ids["101"][0])
        assert task.deadline == dt.date(2026, 10, 9) and task.deadline_source == "asked"
        assert s.get(Task, ids["101"][1]).deadline is None
        inbound = s.query(ConversationMessage).filter_by(chat_id="101", message_id=22).one()
        assert inbound.action_applied and inbound.result["action"] == "set_deadline"


def test_foreign_task_action_is_rejected_without_mutation(monkeypatch):
    _, ids, _ = seed()
    model_reply(monkeypatch, action="set_deadline", task_id=ids["202"][0], deadline="2026-10-09")
    monkeypatch.setattr(deadlines, "today", lambda: dt.date(2026, 10, 7))

    conversations.accept(send_update("101", 31, "Проверь чужую задачу"))

    with SessionLocal() as s:
        assert s.get(Task, ids["202"][0]).deadline is None
        inbound = s.query(ConversationMessage).filter_by(chat_id="101", message_id=31).one()
        assert inbound.result["action"] == "reply"
        assert s.query(ConversationEscalation).count() == 0


def test_existing_deadline_escalates_without_changing_it(monkeypatch, tg):
    _, ids, _ = seed({"101": [("Подготовить отчет", dt.date(2026, 10, 20))]})
    model_reply(monkeypatch, action="set_deadline", task_id=ids["101"][0], deadline="2026-10-09")
    monkeypatch.setattr(deadlines, "today", lambda: dt.date(2026, 10, 7))
    settings_store.set_many({"secretary_chat_ids": ["900"]})

    conversations.accept(send_update("101", 41, "До пятницы"))

    with SessionLocal() as s:
        task = s.get(Task, ids["101"][0])
        assert task.deadline == dt.date(2026, 10, 20)
        record = s.query(ConversationEscalation).one()
        assert record.task_id == task.id and record.status == "open"
        assert s.query(Delivery).filter_by(phase="dialogue", kind="escalation", chat_id="900", status="sent").count() == 1
    assert any(call[1].get("chat_id") == "900" for call in tg)


def test_explicit_escalation_is_recorded_even_without_a_secretary(monkeypatch):
    _, ids, _ = seed()
    model_reply(monkeypatch, action="escalate", task_id=ids["101"][0], reason="Нужна проверка")

    conversations.accept(send_update("101", 51, "Нужна помощь руководителя"))

    with SessionLocal() as s:
        record = s.query(ConversationEscalation).one()
        assert record.reason == "Нужна проверка"
        assert s.query(Delivery).filter_by(phase="dialogue", kind="escalation").count() == 0
        inbound = s.query(ConversationMessage).filter_by(message_id=51, role="user").one()
        assert "руководителем" in inbound.result["reply"]


def test_deduplicates_inbound_and_persists_action_result(monkeypatch, tg):
    _, ids, _ = seed()
    captured = model_reply(monkeypatch, reply="Зафиксировал")
    message = send_update("101", 61, "Вопрос")

    assert conversations.accept(message)
    assert conversations.accept(message)

    with SessionLocal() as s:
        inbound = s.query(ConversationMessage).filter_by(chat_id="101", message_id=61, role="user").one()
        assert inbound.status == "done" and inbound.result["action"] == "reply"
        assert inbound.action_applied is True
        assert s.query(Delivery).filter_by(phase="dialogue", kind="reply", chat_id="101").count() == 1
    assert len(captured) == 1
    assert sum(1 for call in tg if call[0] == "sendMessage") == 1


def test_paid_or_non_free_conversation_model_uses_local_fallback(monkeypatch, tg):
    seed()
    settings_store.set_many({"conversation_model": "provider/expensive-model"})
    monkeypatch.setattr(llm, "chat", lambda *args, **kwargs: pytest.fail("must not call a paid model"))

    conversations.accept(send_update("101", 71, "Привет"))

    with SessionLocal() as s:
        inbound = s.query(ConversationMessage).filter_by(message_id=71, role="user").one()
        assert inbound.status == "done" and inbound.result["action"] == "reply"
        assert "только бесплатные" in inbound.result["reply"]
    assert any(call[0] == "sendMessage" for call in tg)


def test_disabled_dialogue_and_unknown_or_taskless_chats_are_not_persisted(monkeypatch):
    _, _, _ = seed()
    settings_store.set_many({"dialogue_enabled": False})
    assert conversations.accept(send_update("101", 81, "Не принимать")) is False
    settings_store.set_many({"dialogue_enabled": True})
    assert conversations.accept(send_update("999", 82, "Неизвестный чат")) is False
    assert conversations.accept(send_update("101", 83, "Нет текста", reply_to=999)) is True
    assert conversations.accept({"message_id": 84, "chat": {"id": 101}}) is False
    with SessionLocal() as s:
        assert s.query(ConversationMessage).count() == 1


def test_rejects_taskless_known_employee(monkeypatch):
    with SessionLocal() as s:
        s.add(Employee(name="No tasks", telegram_chat_id="404"))
        s.commit()
    assert conversations.accept(send_update("404", 91, "Привет")) is False
    with SessionLocal() as s:
        assert s.query(ConversationMessage).count() == 0


def test_parseable_deadline_uses_deterministic_path_and_resolves_request(monkeypatch, tg):
    _, ids, _ = seed({"101": [("Подготовить отчет", None)]})
    monkeypatch.setattr(deadlines, "today", lambda: dt.date(2026, 10, 7))
    monkeypatch.setattr(llm, "chat", lambda *args, **kwargs: pytest.fail("explicit date should not call LLM"))
    with SessionLocal() as s:
        task = s.get(Task, ids["101"][0])
        task.status = "awaiting_deadline"
        from app.db import DeadlineRequest
        request = DeadlineRequest(task_id=task.id, chat_id="101", message_id=880)
        s.add(request)
        s.commit()

    assert conversations.accept(send_update("101", 111, "до 20.10", reply_to=880))

    with SessionLocal() as s:
        task = s.get(Task, ids["101"][0])
        request = s.query(DeadlineRequest).one()
        inbound = s.query(ConversationMessage).filter_by(chat_id="101", message_id=111).one()
        assert task.deadline == dt.date(2026, 10, 20) and task.deadline_source == "asked"
        assert task.status == "sent" and request.resolved
        assert inbound.result["action"] == "set_deadline" and inbound.status == "done"
    assert any("20.10.2026" in call[1].get("text", "") for call in tg)


def test_periodic_pending_does_not_reset_live_processing_but_recovery_does():
    seed()
    with SessionLocal() as s:
        row = ConversationMessage(chat_id="101", message_id=120, role="user", status="processing", text="x",
                                  meeting_id=1)
        s.add(row)
        s.commit()
        row_id = row.id

    conversations.run_pending()
    with SessionLocal() as s:
        assert s.get(ConversationMessage, row_id).status == "processing"

    conversations.recover()
    with SessionLocal() as s:
        assert s.get(ConversationMessage, row_id).status == "pending"


def test_different_chats_process_in_parallel(monkeypatch, tg):
    seed()
    monkeypatch.setattr(config, "TESTING", False)
    started = threading.Event()
    lock = threading.Lock()
    arrivals = []
    release = threading.Event()

    def slow_chat(*args, **kwargs):
        with lock:
            arrivals.append(True)
            if len(arrivals) == 2:
                started.set()
        release.wait(2)
        return json.dumps({"action": "reply", "reply": "Ок"})

    monkeypatch.setattr(llm, "chat", slow_chat)
    conversations.accept(send_update("101", 101, "Первый"))
    conversations.accept(send_update("202", 102, "Второй"))
    try:
        assert started.wait(2), "different chats should be processed concurrently"
    finally:
        release.set()
    import time
    end = time.monotonic() + 2
    while time.monotonic() < end:
        with SessionLocal() as s:
            if s.query(ConversationMessage).filter_by(role="user", status="done").count() == 2:
                break
        time.sleep(0.01)
    with SessionLocal() as s:
        assert s.query(ConversationMessage).filter_by(role="user", status="done").count() == 2


def test_single_missing_deadline_does_not_override_named_existing_task(monkeypatch, tg):
    _, ids, _ = seed({"101": [("Подготовить отчет", dt.date(2026, 10, 9)), ("Согласовать бюджет", None)]})
    monkeypatch.setattr(deadlines, "today", lambda: dt.date(2026, 10, 7))
    model_reply(monkeypatch, action="set_deadline", task_id=ids["101"][0], deadline="2026-10-16")
    conversations.accept(send_update("101", 88, "Отчет сделаю через неделю"))
    with SessionLocal() as s:
        assert s.get(Task, ids["101"][0]).deadline == dt.date(2026, 10, 9)
        assert s.get(Task, ids["101"][1]).deadline is None
        assert s.query(ConversationEscalation).count() == 1


def test_current_message_does_not_include_future_queued_message(monkeypatch, tg):
    mid, _, _ = seed()
    with SessionLocal() as s:
        current = ConversationMessage(chat_id="101", message_id=91, role="user", status="pending",
                                      text="Что нужно сделать?", meeting_id=mid)
        future = ConversationMessage(chat_id="101", message_id=92, role="user", status="pending",
                                     text="Более позднее сообщение", meeting_id=mid)
        s.add(current)
        s.flush()
        cid = current.id
        s.add(future)
        s.commit()
    captured = model_reply(monkeypatch)
    conversations._process(cid)
    payload = json.loads(captured[0][1][1]["content"])
    assert not any("Более позднее" in item["text"] for item in payload["history"])


def test_deadline_timeout_cannot_overwrite_agreed_date():
    from app import pipeline
    _, ids, _ = seed({"101": [("Отчет", dt.date(2026, 10, 9))]})
    assert pipeline.set_deadline(ids["101"][0], dt.date(2026, 10, 16), "default") is None
    with SessionLocal() as s:
        assert s.get(Task, ids["101"][0]).deadline == dt.date(2026, 10, 9)


def test_temporary_telegram_failure_keeps_reply_order_and_does_not_repeat_model(monkeypatch, tg):
    from app import telegram
    from app.db import now
    seed()
    captured = model_reply(monkeypatch, reply="Первый ответ")
    original = telegram.call
    failed_once = []

    def rate_limited(method, data=None, files=None, timeout=60):
        if method == "sendMessage" and not failed_once:
            failed_once.append(True)
            raise telegram.TelegramError("429", retryable=True, retry_after=60)
        return original(method, data, files, timeout)

    monkeypatch.setattr(telegram, "call", rate_limited)
    conversations.accept(send_update("101", 1011, "Первый вопрос"))
    conversations.accept(send_update("101", 1012, "Второй вопрос"))
    with SessionLocal() as s:
        replies = s.query(Delivery).filter_by(phase="dialogue", kind="reply").order_by(Delivery.id).all()
        assert len(replies) == 2
        assert replies[0].status == replies[1].status == "pending"
        assert replies[1].depends_on == replies[0].id
        replies[0].next_attempt_at = now() - dt.timedelta(seconds=1)
        s.commit()
    assert len(captured) == 2
    conversations.run_pending()
    with SessionLocal() as s:
        assert all(row.status == "sent" for row in s.query(Delivery).filter_by(phase="dialogue", kind="reply"))
    assert len(captured) == 2
    sends = [call[1]["reply_parameters"]["message_id"] for call in tg if call[0] == "sendMessage"]
    assert sends == [1011, 1012]


def test_reply_to_previous_bot_response_keeps_original_meeting_scope(monkeypatch, tg):
    mid, ids, _ = seed({"101": [("Подготовить отчет", None)]})
    captured = model_reply(monkeypatch)
    conversations.accept(send_update("101", 1101, "Что требуется по отчету?"))
    with SessionLocal() as s:
        old_reply = s.query(Delivery).filter_by(phase="dialogue", kind="reply", meeting_id=mid).one()
        old_message_id = old_reply.message_id
        person = s.query(Employee).filter_by(telegram_chat_id="101").one()
        newer = Meeting(title="Более новая встреча", status="done", options={"deadline_mode": "ask"})
        task = Task(title="Проверить бюджет", assignee=person, status="sent")
        newer.tasks = [task]
        s.add(newer)
        s.flush()
        digest = delivery.enqueue(s, newer.id, "final", "tasks", "101", {"text": "Бюджет"}, task_ids=[task.id])
        digest.status, digest.message_id = "sent", 9999
        s.commit()
    conversations.accept(send_update("101", 1102, "Уточните детали", old_message_id))
    payload = json.loads(captured[-1][1][1]["content"])
    assert {item["id"] for item in payload["tasks"]} == set(ids["101"])
    with SessionLocal() as s:
        inbound = s.query(ConversationMessage).filter_by(chat_id="101", message_id=1102).one()
        assert inbound.meeting_id == mid


def test_deactivation_while_model_runs_cancels_response_and_action(monkeypatch, tg):
    _, ids, _ = seed()

    def deactivate(*args, **kwargs):
        with SessionLocal() as s:
            person = s.query(Employee).filter_by(telegram_chat_id="101").one()
            person.active = False
            s.commit()
        return json.dumps({"action": "escalate", "task_id": ids["101"][0], "reply": "Ответ", "reason": "Вопрос"})

    monkeypatch.setattr(llm, "chat", deactivate)
    conversations.accept(send_update("101", 1201, "Есть вопрос"))
    with SessionLocal() as s:
        inbound = s.query(ConversationMessage).filter_by(chat_id="101", message_id=1201).one()
        assert inbound.status == "done" and "отменён" in inbound.error
        assert s.query(ConversationEscalation).count() == 0
        assert s.query(Delivery).filter_by(phase="dialogue").count() == 0
    assert not any(call[0] == "sendMessage" for call in tg)
