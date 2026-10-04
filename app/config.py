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

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'sysai.db'}")
if DATABASE_URL.startswith("postgres://"):  # Render/Heroku style
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "SYSAI")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "SYSAI")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_MODE = os.getenv("TELEGRAM_MODE", "webhook")  # webhook | polling | off
# Telegram allows only [A-Za-z0-9_-] in the secret token, so derive it from SECRET_KEY
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET") or hashlib.sha256(SECRET_KEY.encode()).hexdigest()[:48]
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

BITRIX_WEBHOOK_URL = os.getenv("BITRIX_WEBHOOK_URL", "").rstrip("/")

MIC_INBOX_DIR = pathlib.Path(os.getenv("MIC_INBOX_DIR", DATA_DIR / "inbox"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Minsk")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
WORKERS = int(os.getenv("WORKERS", "2"))
TESTING = _bool("SYSAI_TESTING")


def llm_enabled() -> bool:
    return bool(OPENROUTER_API_KEY)


def telegram_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN) and TELEGRAM_MODE != "off"
