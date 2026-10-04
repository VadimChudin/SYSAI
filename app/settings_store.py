"""Settings editable in the web panel (stored in the database)."""
import json

from .db import SessionLocal, Setting

POSITIONS = ["Директор", "Заместитель директора", "Руководитель отдела", "Менеджер проекта", "Менеджер по продажам",
             "Маркетолог", "Бухгалтер", "Юрист", "HR-менеджер", "Разработчик", "Дизайнер", "Аналитик",
             "Офис-менеджер", "Помощник руководителя"]

DEFAULTS = {
    # processing
    "auto_ingest": False,            # pick up recordings from the microphone automatically (mocked inbox in MVP)
    "approval_required": True,       # draft goes to approvers before anything is sent
    "approver_chat_ids": [],         # telegram chats that approve drafts
    # delivery
    "report_chat_ids": [],           # where the full report goes (people, groups, channels)
    "send_tasks_to_assignees": True,
    "include_transcript_in_pdf": False,
    # deadlines
    "deadline_mode": "default",      # none | default | ask
    "default_deadline_days": 3,      # business days
    "ask_timeout_hours": 24,         # if nobody answers, fall back to the default
    # models (OpenRouter)
    "transcribe_model": "google/gemini-2.5-flash",
    "report_model": "google/gemini-2.5-pro",
    "chunk_minutes": 30,
    "language": "ru",
    "glossary": "SYSAI, Bitrix24, Telegram, OpenRouter, CRM, KPI, roadmap, deadline",  # mocked terms for now
    # look
    "company_name": "Компания",
    "accent_color": "#d97757",
    # integrations
    "bitrix_enabled": False,
}


def get(key):
    with SessionLocal() as s:
        row = s.get(Setting, key)
        return json.loads(row.value) if row else DEFAULTS.get(key)


def all_settings() -> dict:
    out = dict(DEFAULTS)
    with SessionLocal() as s:
        for row in s.query(Setting).all():
            out[row.key] = json.loads(row.value)
    return out


MEETING_OPTIONS = ("approval_required", "deadline_mode", "report_chat_ids", "send_tasks_to_assignees",
                   "include_transcript_in_pdf")


def for_meeting(options: dict | None) -> dict:
    """Global settings with per-meeting overrides chosen on the upload page."""
    out = all_settings()
    for k, v in (options or {}).items():
        if k in MEETING_OPTIONS:
            out[k] = v
    return out


def set_many(values: dict):
    with SessionLocal() as s:
        for k, v in values.items():
            if k not in DEFAULTS:
                continue
            row = s.get(Setting, k)
            if row:
                row.value = json.dumps(v, ensure_ascii=False)
            else:
                s.add(Setting(key=k, value=json.dumps(v, ensure_ascii=False)))
        s.commit()
