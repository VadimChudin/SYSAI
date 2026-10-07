"""Minimal Telegram Bot API client + update handling (/start, groups, approval buttons, deadline answers)."""
import json
import logging
import threading
import time

import httpx

from . import config

log = logging.getLogger("sysai.telegram")


class TelegramError(RuntimeError):
    def __init__(self, message, retryable=False, retry_after=30):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _url(method: str) -> str:
    return f"https://api.telegram.org/bot{config.telegram_token()}/{method}"


def call(method: str, data: dict | None = None, files: dict | None = None, timeout: float = 60):
    if not config.telegram_token():
        raise TelegramError("Токен бота не задан (Настройки → Подключения)")
    if files:
        payload = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))
                   for k, v in (data or {}).items()}
        r = httpx.post(_url(method), data=payload, files=files, timeout=timeout)
    else:
        r = httpx.post(_url(method), json=data or {}, timeout=timeout)
    if r.status_code >= 500:
        # A gateway error cannot prove that the send was rejected upstream.
        raise RuntimeError(f"{method}: Telegram HTTP {r.status_code}")
    body = r.json()
    if not body.get("ok"):
        code = body.get("error_code", r.status_code)
        raise TelegramError(f"{method}: {body.get('description')}", retryable=code == 429,
                            retry_after=body.get("parameters", {}).get("retry_after", 30))
    return body["result"]


def send_message(chat_id, text: str, buttons: list | None = None, reply_to: int | None = None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        data["reply_markup"] = {"inline_keyboard": buttons}
    if reply_to:
        data["reply_parameters"] = {"message_id": reply_to}
    return call("sendMessage", data)


def send_document(chat_id, filename: str, content: bytes, caption: str = ""):
    return call("sendDocument", {"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "HTML"},
                files={"document": (filename, content, "application/pdf")}, timeout=120)


def answer_callback(callback_id: str, text: str = ""):
    try:
        call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})
    except TelegramError as e:
        log.warning("answerCallbackQuery: %s", e)


def edit_buttons(chat_id, message_id, buttons: list | None):
    try:
        call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                        "reply_markup": {"inline_keyboard": buttons or []}})
    except TelegramError as e:
        log.warning("editMessageReplyMarkup: %s", e)


def set_webhook():
    if not config.PUBLIC_URL:
        log.warning("PUBLIC_URL не задан — webhook Telegram не установлен")
        return False
    call("setWebhook", {"url": f"{config.PUBLIC_URL}/telegram/webhook",
                        "secret_token": config.TELEGRAM_WEBHOOK_SECRET,
                        "allowed_updates": ["message", "callback_query", "my_chat_member", "channel_post"]})
    return True


def start_polling(handler):
    """For local development without a public URL."""
    def loop():
        try:
            call("deleteWebhook", {})
        except TelegramError as e:
            log.warning("deleteWebhook: %s", e)
        offset = 0
        while True:
            try:
                updates = call("getUpdates", {"offset": offset, "timeout": 30}, timeout=40)
                for u in updates:
                    offset = u["update_id"] + 1
                    try:
                        handler(u)
                    except Exception:
                        log.exception("update handler failed")
            except Exception as e:  # network hiccups
                log.warning("polling: %s", e)
                time.sleep(5)
    threading.Thread(target=loop, daemon=True, name="tg-polling").start()
