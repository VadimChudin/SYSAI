"""Bitrix24 tasks via an incoming webhook (REST works only on paid Bitrix24 plans).
Disabled by default: turn on in Settings once BITRIX_WEBHOOK_URL is set."""
import httpx

from . import config


class BitrixError(RuntimeError):
    pass


def call(method: str, params: dict) -> dict:
    url = config.bitrix_url()
    if not url:
        raise BitrixError("Вебхук Bitrix24 не задан (Настройки → Подключения)")
    r = httpx.post(f"{url}/{method}.json", json=params, timeout=30)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "error" in data:
        raise BitrixError(f"{method}: {data.get('error_description') or data.get('error') or r.text[:300]}")
    return data.get("result")


def task_fields(task, meeting, created_by: str = "") -> dict:
    desc = [task.description or "", "", f"Из совещания «{(meeting.report or {}).get('title') or meeting.title}»"]
    if task.source_quote:
        desc.append(f"[{task.source_time}] «{task.source_quote}»")
    fields = {"TITLE": task.title, "DESCRIPTION": "\n".join(desc).strip(),
              "RESPONSIBLE_ID": task.assignee.bitrix_user_id if task.assignee else "",
              "PRIORITY": "2" if task.priority == "high" else "1"}
    if task.deadline:
        fields["DEADLINE"] = task.deadline.isoformat() + "T18:00:00"
    if created_by:
        fields["CREATED_BY"] = created_by
    return fields


def create_task(task, meeting) -> str:
    result = call("tasks.task.add", {"fields": task_fields(task, meeting)})
    return str((result or {}).get("task", {}).get("id", ""))


def update_deadline(task) -> None:
    if task.bitrix_task_id and task.deadline:
        call("tasks.task.update", {"taskId": task.bitrix_task_id,
                                   "fields": {"DEADLINE": task.deadline.isoformat() + "T18:00:00"}})


def check() -> str:
    """Quick connectivity check for the settings page."""
    me = call("user.current", {})
    return f"{me.get('NAME', '')} {me.get('LAST_NAME', '')} (ID {me.get('ID')})"
