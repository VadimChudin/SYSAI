"""OpenRouter client (OpenAI-compatible chat completions)."""
import json
import re
import time

import httpx

from . import config


class LLMError(RuntimeError):
    pass


def chat(model: str, messages: list, json_mode: bool = False, temperature: float = 0.1,
         max_tokens: int | None = None, timeout: float = 600) -> str:
    key = config.openrouter_key()
    if not key:
        raise LLMError("Ключ OpenRouter не задан (Настройки → Подключения)")
    body = {"model": model, "messages": messages, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if max_tokens:
        body["max_tokens"] = max_tokens
    headers = {"Authorization": f"Bearer {key}", "X-Title": "SYSAI",
               "HTTP-Referer": config.PUBLIC_URL or "https://github.com/VadimChudin/SYSAI"}
    last = None
    for attempt in range(3):
        try:
            r = httpx.post(f"{config.OPENROUTER_BASE_URL}/chat/completions", json=body, headers=headers,
                           timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise LLMError(f"OpenRouter HTTP {r.status_code}: {r.text[:500]}")
            data = r.json()
            if "error" in data:
                raise LLMError(f"OpenRouter: {data['error']}")
            return data["choices"][0]["message"]["content"] or ""
        except httpx.HTTPError as e:
            last = str(e)
            time.sleep(5 * (attempt + 1))
    raise LLMError(f"OpenRouter недоступен: {last}")


def parse_json(text: str):
    """Tolerant JSON extraction: strips ```json fences and leading/trailing prose."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        a, b = t.find(open_c), t.rfind(close_c)
        if a != -1 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("Модель вернула не JSON: " + text[:300])


def check_key() -> str:
    """Validates the OpenRouter key; returns a short human description."""
    key = config.openrouter_key()
    if not key:
        raise LLMError("Ключ не задан")
    r = httpx.get(f"{config.OPENROUTER_BASE_URL}/key", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    if r.status_code != 200:
        raise LLMError(f"OpenRouter отклонил ключ (HTTP {r.status_code})")
    d = r.json().get("data", {})
    usage, limit = d.get("usage"), d.get("limit")
    return "ключ принят" + (f", израсходовано ${usage:.2f}" if isinstance(usage, (int, float)) else "") + \
        (f" из ${limit:.2f}" if isinstance(limit, (int, float)) else "")
