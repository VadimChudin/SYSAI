"""Environment configuration. Secrets live only in environment variables (.env on the server)."""
import hashlib
import os
import pathlib


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", BASE_DIR.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or f"sqlite:///{DATA_DIR / 'sysai.db'}"
for prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(prefix):
        DATABASE_URL = DATABASE_URL.replace(prefix, "postgresql+psycopg://", 1)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "SYSAI")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "SYSAI")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_MODE = os.getenv("TELEGRAM_MODE", "webhook")  # webhook | polling | off
# Telegram allows only [A-Za-z0-9_-] in the secret token, so derive it from SECRET_KEY
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET") or hashlib.sha256(SECRET_KEY.encode()).hexdigest()[:48]
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or
              (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}" if os.getenv("RAILWAY_PUBLIC_DOMAIN") else "")).rstrip("/")

BITRIX_WEBHOOK_URL = os.getenv("BITRIX_WEBHOOK_URL", "").rstrip("/")

MIC_INBOX_DIR = pathlib.Path(os.getenv("MIC_INBOX_DIR", DATA_DIR / "inbox"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Minsk")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
WORKERS = int(os.getenv("WORKERS", "2"))
TESTING = _bool("SYSAI_TESTING")


def _panel(key: str) -> str:
    """Value pasted in the web panel (Settings → Подключения); env variable is the fallback."""
    try:
        from . import settings_store
        return (settings_store.get(key) or "").strip()
    except Exception:  # database not ready yet
        return ""


def openrouter_key() -> str:
    return _panel("openrouter_api_key") or OPENROUTER_API_KEY


def telegram_token() -> str:
    return _panel("telegram_bot_token") or TELEGRAM_BOT_TOKEN


def bitrix_url() -> str:
    return (_panel("bitrix_webhook_url") or BITRIX_WEBHOOK_URL).rstrip("/")


def llm_enabled() -> bool:
    return bool(openrouter_key())


def telegram_enabled() -> bool:
    return bool(telegram_token()) and TELEGRAM_MODE != "off"
