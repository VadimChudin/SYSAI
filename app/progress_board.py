"""Read-only progress projection from persisted delivery and dialogue state."""
import hashlib
import json


def build(s, meeting, settings, statuses):
    groups, unassigned = {}, []
    for task in meeting.tasks:
        employee = task.assignee
        if not employee:
            unassigned.append({"id": task.id, "title": task.title})
            continue
        group = groups.setdefault(employee.id, {"employee": employee, "tasks": []})
        group["tasks"].append(task)
    people = []
    for eid, group in groups.items():
        employee, tasks = group["employee"], group["tasks"]
        cid = employee.telegram_chat_id or ""
        ids = {task.id for task in tasks}
        deliveries = [d for d in meeting.deliveries if d.chat_id == cid and d.status != "cancelled" and
                      ((d.phase == "final" and d.kind in ("tasks", "deadline") and ids.intersection(d.task_ids or [])) or
                       (d.phase == "dialogue" and d.kind == "reply"))]
        messages = [m for m in meeting.dialogue if m.chat_id == cid and m.role == "user"]
        unresolved = [e for e in meeting.escalations if e.chat_id == cid and e.status == "open"]
        digest = [d for d in deliveries if d.phase == "final" and d.kind == "tasks"]
        covered = {tid for d in digest if d.status == "sent" for tid in (d.task_ids or [])}
        delivered = ids.issubset(covered)
        processing = any(m.status in ("pending", "processing") for m in messages)
        sending = any(d.status == "sending" for d in deliveries)
        pending = any(d.status == "pending" for d in deliveries)
        failed = any(d.status in ("failed", "uncertain") for d in deliveries)
        active = any(m.status == "processing" for m in messages) or sending
        attention = bool(unresolved) or failed or any(m.status == "error" or m.error for m in messages)
        if not cid:
            state, label, detail = "no_recipient", "Нет Telegram", "Привяжите Telegram сотрудника в справочнике"
        elif active:
            state, label, detail = "processing", "Обработка ответа" if processing else "Отправка", "Есть активная работа в очереди"
        elif attention:
            state, label, detail = "attention", "Нужно внимание", "Открыт вопрос секретарю" if unresolved else "Отправка не подтверждена — смотрите журнал"
        elif pending or processing:
            state, label, detail = "pending", "В очереди", "Ожидается отправка или повтор; активного запроса сейчас нет"
        elif delivered and messages:
            state, label, detail = "responded", "Ответил", "Ответ получен; это не отметка выполнения задачи"
        elif delivered:
            state, label, detail = "sent", "Доставлено", "Telegram подтвердил доставку; прочтение неизвестно"
        elif not settings.get("send_tasks_to_assignees"):
            state, label, detail = "disabled", "Рассылка выключена", "Поручения этому сотруднику не отправляются"
        else:
            state, label, detail = "waiting", "Ждёт отправки", "Отчёт должен быть утверждён перед рассылкой"
        people.append({"id": str(eid), "name": employee.name, "chat_id": cid, "task_count": len(tasks),
                       "state": state, "label": label, "detail": detail, "active": active,
                       "delivered": delivered, "responded": bool(messages), "attention": attention or not cid,
                       "latest_message": messages[-1].text[:220] if messages else "",
                       "tasks": [{"id": t.id, "title": t.title, "deadline": t.deadline.isoformat() if t.deadline else None,
                                  "status": t.status} for t in tasks],
                       "history_url": f"/meetings/{meeting.id}#dialogue-{cid}" if cid else "/employees"})
    label = statuses.get(meeting.status, (meeting.status, "gray"))[0]
    result = {"meeting": {"id": meeting.id, "title": meeting.title, "status": meeting.status, "label": label,
                          "progress": meeting.progress, "approved": meeting.approved_at is not None},
              "summary": {"recipients": len(people), "delivered": sum(p["delivered"] for p in people),
                          "responded": sum(p["responded"] for p in people), "attention": sum(p["attention"] for p in people) + len(unassigned)},
              "people": people, "unassigned": unassigned}
    result["detail_version"] = {"dialogue": [[m.id, m.status, m.text, m.error] for m in meeting.dialogue],
                                "deliveries": [[d.id, d.status, d.attempts, d.error] for d in meeting.deliveries],
                                "escalations": [[e.id, e.status] for e in meeting.escalations],
                                "reports": [v.id for v in meeting.report_versions]}
    result["version"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result
