"""Persistent Telegram outbox. Ambiguous sends require explicit human verification."""
import datetime as dt
import hashlib

import httpx

from . import config, telegram
from .db import DeadlineRequest, Delivery, SessionLocal, Task, now

MAX_ATTEMPTS = 3


def enqueue(s, meeting_id, phase, kind, chat, payload, document=None, task_ids=None, depends_on=None, revision=""):
    ids = task_ids or []
    digest = hashlib.sha256((revision + ":" + ",".join(map(str, ids))).encode()).hexdigest()
    key = f"{phase}:{kind}:{chat}:{digest}"
    existing = s.query(Delivery).filter_by(meeting_id=meeting_id, key=key).first()
    if existing:
        return existing
    row = Delivery(meeting_id=meeting_id, key=key, phase=phase, kind=kind, chat_id=str(chat),
                   payload=payload, document=document, task_ids=ids, depends_on=depends_on)
    s.add(row)
    s.flush()
    return row


def flush(meeting_id, phase):
    with SessionLocal() as s:
        ids = [r.id for r in s.query(Delivery).filter_by(meeting_id=meeting_id, phase=phase).order_by(Delivery.id)]
    for row_id in ids:
        send(row_id)


def send(row_id):
    with SessionLocal() as s:
        row = s.get(Delivery, row_id)
        if row.depends_on and s.get(Delivery, row.depends_on).status != "sent":
            return
        changed = s.query(Delivery).filter(
            Delivery.id == row_id, Delivery.status == "pending",
            (Delivery.next_attempt_at.is_(None) | (Delivery.next_attempt_at <= now())),
        ).update({Delivery.status: "sending", Delivery.attempts: Delivery.attempts + 1,
                  Delivery.cycle_attempts: Delivery.cycle_attempts + 1}, synchronize_session=False)
        s.commit()
        if not changed:
            return
        s.refresh(row)
        payload, chat, document = row.payload, row.chat_id, row.document
    try:
        if not config.telegram_enabled():
            raise telegram.TelegramError("Telegram отключён или токен не задан")
        if document is not None:
            result = telegram.send_document(chat, payload["filename"], document, payload.get("caption", ""))
        else:
            result = telegram.send_message(chat, payload["text"], payload.get("buttons"))
        message_id = result["message_id"]
    except Exception as exc:
        # Read timeouts and interrupted sends cannot prove Telegram rejected the message.
        retryable = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)) or (
            isinstance(exc, telegram.TelegramError) and exc.retryable)
        definite = retryable or isinstance(exc, telegram.TelegramError)
        with SessionLocal() as s:
            row = s.get(Delivery, row_id)
            row.status = "pending" if retryable and row.cycle_attempts < MAX_ATTEMPTS else ("failed" if definite else "uncertain")
            row.error = "Доставка не подтверждена; проверьте чат" if not definite else str(exc)[:500]
            delay = max(1, getattr(exc, "retry_after", 30)) * row.cycle_attempts
            row.next_attempt_at = now() + dt.timedelta(seconds=delay) if row.status == "pending" else None
            for tid in row.task_ids:
                task = s.get(Task, tid)
                if task and row.kind == "tasks":
                    task.status = "delivery_failed"
            s.commit()
        return
    with SessionLocal() as s:
        row = s.get(Delivery, row_id)
        row.status, row.message_id, row.sent_at, row.error = "sent", message_id, now(), ""
        row.next_attempt_at = None
        for tid in row.task_ids:
            task = s.get(Task, tid)
            if not task:
                continue
            if row.kind == "tasks" and task.status in ("draft", "delivery_failed"):
                task.status = "sent"
            if row.kind == "deadline" and not task.deadline:
                task.status = "awaiting_deadline"
                if not s.query(DeadlineRequest).filter_by(task_id=tid, message_id=message_id, chat_id=chat).first():
                    s.add(DeadlineRequest(task_id=tid, chat_id=chat, message_id=message_id))
        s.commit()


def recover(meeting_id=None):
    with SessionLocal() as s:
        query = s.query(Delivery).filter_by(status="sending")
        if meeting_id is not None:
            query = query.filter_by(meeting_id=meeting_id, phase="final")
        query.update({
            Delivery.status: "uncertain", Delivery.error: "Отправка прервана перезапуском; проверьте чат",
        }, synchronize_session=False)
        s.commit()
