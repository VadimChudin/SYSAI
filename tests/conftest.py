import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="sysai_test_")
os.environ.update(SYSAI_TESTING="1", DATA_DIR=_tmp, DATABASE_URL=f"sqlite:///{_tmp}/test.db",
                  ADMIN_USERNAME="SYSAI", ADMIN_PASSWORD="pw", SECRET_KEY="test-secret", OPENROUTER_API_KEY="", TELEGRAM_BOT_TOKEN="",
                  TELEGRAM_WEBHOOK_SECRET="hook")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from app import config, db, telegram  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db.Base.metadata.drop_all(db.engine)
    db.init_db()
    yield


@pytest.fixture
def tg(monkeypatch):
    """Fake Telegram: records every Bot API call."""
    calls = []
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake")

    def fake_call(method, data=None, files=None, timeout=60):
        calls.append((method, data or {}, files))
        return {"message_id": len(calls)}
    monkeypatch.setattr(telegram, "call", fake_call)
    return calls


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "sysai", "password": "pw"})
        yield c
