"""Minimal settings UI, partial updates and explicit credential actions."""
from html.parser import HTMLParser

from app import llm, settings_store


class Controls(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.details = []
        self.fields = []
        self.buttons = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "details":
            self.details.append(attrs)
        if tag in ("input", "select", "textarea"):
            self.fields.append((tag, attrs, len(self.details)))
        if tag == "button":
            self.buttons.append(attrs)

    def handle_endtag(self, tag):
        if tag == "details":
            self.details.pop()


def test_rendered_settings_are_minimal_and_never_echo_secrets(client):
    secrets = {k: "private-secret-" + k for k in settings_store.SECRET_KEYS}
    settings_store.set_many(secrets)
    response = client.get("/settings")
    assert response.status_code == 200
    html = response.text
    controls = Controls(html)
    for value in secrets.values():
        assert value not in html
    for tag, attrs, depth in controls.fields:
        name = attrs.get("name", "")
        if attrs.get("type") == "hidden":
            assert name.startswith("present_")
            assert attrs["value"] == "1"
        if name in settings_store.SECRET_KEYS:
            assert attrs["type"] == "password"
            assert not attrs.get("value")
        if name in ("transcribe_model", "report_model", "conversation_model", "report_chat_ids_extra",
                    "telegram_bot_token", "bitrix_webhook_url", "deadline_mode"):
            assert depth > 0
        if name in ("approval_required", "openrouter_api_key"):
            assert depth == 0
    assert "<details" in html and "<details open" not in html
    assert any(b.get("formaction") == "/settings/test-openrouter" for b in controls.buttons)
    assert "Сначала сохраните новый ключ" in html


def test_partial_save_keeps_all_omitted_settings(client):
    before = dict(settings_store.DEFAULTS)
    before.update(auto_ingest=True, chunk_minutes=17, glossary="Редкий термин",
                  transcribe_model="custom/audio", report_model="custom/report",
                  conversation_model="old/paid", conversation_tone="Особый стиль",
                  report_chat_ids=["-100123"], approver_chat_ids=["55"],
                  secretary_chat_ids=["66"], bitrix_enabled=True,
                  openrouter_api_key="saved-key", telegram_bot_token="saved-token",
                  bitrix_webhook_url="https://example.invalid/rest/secret")
    settings_store.set_many(before)
    response = client.post("/settings", data={"present_approval_required": "1"}, follow_redirects=False)
    assert response.status_code == 303
    expected = dict(before, approval_required=False)
    assert settings_store.all_settings() == expected


def test_empty_marked_controls_clear_only_their_settings(client):
    settings_store.set_many({"report_chat_ids": ["11"], "approver_chat_ids": ["22"],
                             "send_tasks_to_assignees": True, "dialogue_enabled": True})
    response = client.post("/settings", data={"present_report_chat_ids": "1",
                                             "present_send_tasks_to_assignees": "1"})
    assert response.status_code == 200
    current = settings_store.all_settings()
    assert current["report_chat_ids"] == []
    assert current["send_tasks_to_assignees"] is False
    assert current["approver_chat_ids"] == ["22"]
    assert current["dialogue_enabled"] is True


def test_legacy_hidden_secret_values_are_not_accepted(client):
    old = {k: "saved-" + k for k in settings_store.SECRET_KEYS}
    settings_store.set_many(old)
    form = {k: "hidden-stale-" + k for k in settings_store.SECRET_KEYS}
    form.update(chunk_minutes="2", auto_ingest="", allow_deadline_proposals="")
    assert client.post("/settings", data={k: form[k] for k in old}).status_code == 200
    assert {k: settings_store.get(k) for k in old} == old


def test_explicit_credential_actions_and_empty_replace(client):
    for key in settings_store.SECRET_KEYS:
        settings_store.set_many({key: "original"})
        response = client.post("/settings", data={key: "new-value", "action_" + key: "replace"}, follow_redirects=False)
        assert response.status_code == 303
        assert settings_store.get(key) == "new-value"
        response = client.post("/settings", data={key: "", "action_" + key: "replace"}, follow_redirects=False)
        assert response.status_code == 400
        assert settings_store.get(key) == "new-value"
        assert client.post("/settings", data={key: "stale", "action_" + key: "keep"}).status_code == 200
        assert settings_store.get(key) == "new-value"
        assert client.post("/settings", data={"action_" + key: "clear"}).status_code == 200
        assert settings_store.get(key) == ""


def test_invalid_form_does_not_partially_save(client):
    before = settings_store.all_settings()
    for form in ({"company_name": "Changed", "default_deadline_days": "not-a-number"},
                 {"company_name": "Changed", "deadline_mode": "unknown"},
                 {"company_name": "Changed", "conversation_model": "paid/model"},
                 {"company_name": "Changed", "action_openrouter_api_key": "unknown"}):
        assert client.post("/settings", data=form, follow_redirects=False).status_code == 400
        assert settings_store.all_settings() == before


def test_key_test_still_uses_saved_key_and_does_not_save_form(client, monkeypatch):
    settings_store.set_many({"openrouter_api_key": "saved-key"})
    seen = []

    def check():
        seen.append(settings_store.get("openrouter_api_key"))
        return "ok"

    monkeypatch.setattr(llm, "check_key", check)
    response = client.post("/settings/test-openrouter", data={"openrouter_api_key": "unsaved-key",
                            "action_openrouter_api_key": "replace"}, follow_redirects=False)
    assert response.status_code == 303
    assert seen == ["saved-key"]
    assert settings_store.get("openrouter_api_key") == "saved-key"


def test_new_meeting_uses_visible_optional_overrides_not_hidden_settings(client):
    response = client.get("/app/new")
    assert response.status_code == 200
    controls = Controls(response.text)
    fields = {attrs.get("name"): (attrs, depth) for tag, attrs, depth in controls.fields}
    for key in ("approval_required", "send_tasks_to_assignees", "include_transcript_in_pdf", "deadline_mode"):
        attrs, depth = fields[key]
        assert attrs.get("type") != "hidden"
        assert depth > 0
    assert "report_chat_ids_extra" in fields
    assert not any(attrs.get("name") in settings_store.SECRET_KEYS for _, attrs, _ in controls.fields)
    assert "Изменения действуют только для этой записи" in response.text


class SuccessfulForm(HTMLParser):
    """Submit real rendered controls, including collapsed details, like a browser."""
    def __init__(self, html):
        super().__init__()
        self.data = {}
        self.select = None
        self.textarea = None
        self.feed(html)

    def add(self, name, value):
        if name in self.data:
            old = self.data[name]
            self.data[name] = old + [value] if isinstance(old, list) else [old, value]
        else:
            self.data[name] = value

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = attrs.get("name")
        if tag == "input" and name and "disabled" not in attrs:
            kind = attrs.get("type", "text")
            if kind in ("checkbox", "radio") and "checked" not in attrs:
                return
            self.add(name, attrs.get("value", "on" if kind == "checkbox" else ""))
        elif tag == "select":
            self.select = name
        elif tag == "option" and self.select:
            if self.select not in self.data or "selected" in attrs:
                self.data[self.select] = attrs.get("value", "")
        elif tag == "textarea":
            self.textarea = name
            if name:
                self.data[name] = ""

    def handle_endtag(self, tag):
        if tag == "select":
            self.select = None
        elif tag == "textarea":
            self.textarea = None

    def handle_data(self, data):
        if self.textarea:
            self.data[self.textarea] += data


def test_full_rendered_form_preserves_collapsed_and_unrendered_settings(client):
    before = dict(settings_store.DEFAULTS)
    before.update(chunk_minutes=19, auto_ingest=True, language="en",
                  glossary='Термин <test> & "имя"', company_name="Особая компания",
                  report_chat_ids=["-10012", "77"], secretary_chat_ids=["88"],
                  openrouter_api_key="private-key", telegram_bot_token="private-token",
                  bitrix_webhook_url="https://example.invalid/rest/private", bitrix_enabled=True)
    settings_store.set_many(before)
    rendered = client.get("/settings")
    assert rendered.status_code == 200
    form = SuccessfulForm(rendered.text).data
    assert form["action_openrouter_api_key"] == "keep"
    assert form["openrouter_api_key"] == ""
    assert "chunk_minutes" not in form and "auto_ingest" not in form
    assert client.post("/settings", data=form, follow_redirects=False).status_code == 303
    assert settings_store.all_settings() == before


def test_rendered_meeting_form_matches_global_options(client):
    from app.main import _options
    from starlette.datastructures import FormData

    for checked in (True, False):
        values = {"approval_required": checked, "send_tasks_to_assignees": checked,
                  "include_transcript_in_pdf": checked, "deadline_mode": "ask",
                  "report_chat_ids": ["-10012", "77"]}
        settings_store.set_many(values)
        for source in ("file", "mic"):
            response = client.get("/app/new", params={"source": source})
            assert response.status_code == 200
            form = SuccessfulForm(response.text).data
            assert _options(FormData(form)) == values
