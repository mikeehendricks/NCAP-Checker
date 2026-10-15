"""Application configuration.

Values are read from environment variables (see ``.env.example``) so the same
codebase runs under systemd, gunicorn, ``flask run`` or the test-suite.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = Path(os.environ.get("NCAP_INSTANCE_DIR", BASE_DIR / "instance"))
DATA_DIR = Path(os.environ.get("NCAP_DATA_DIR", INSTANCE_DIR / "data"))
VERSION = "1.0.0"


def _load_dotenv() -> None:
    """Lightweight .env loader (avoids a hard dependency on python-dotenv)."""
    env_file = BASE_DIR / ".env"
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _persisted_secret_key() -> str:
    """Return a stable secret key, generating and caching one on first run."""
    env_key = os.environ.get("NCAP_SECRET_KEY") or os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    key_file = INSTANCE_DIR / "secret.key"
    if key_file.is_file():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    generated = secrets.token_hex(32)
    key_file.write_text(generated, encoding="utf-8")
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return generated


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Base configuration shared by every environment."""

    APP_NAME = os.environ.get("NCAP_APP_NAME", "NCAP Checker")
    VERSION = VERSION
    TIMEZONE = os.environ.get("NCAP_TIMEZONE", "Asia/Manila")

    SECRET_KEY = _persisted_secret_key()
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_flag("NCAP_SESSION_SECURE", False)
    PERMANENT_SESSION_LIFETIME = int(os.environ.get("NCAP_SESSION_HOURS", "12")) * 3600
    REMEMBER_COOKIE_DURATION = int(os.environ.get("NCAP_REMEMBER_DAYS", "30")) * 86400
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE

    WTF_CSRF_ENABLED = _env_flag("NCAP_CSRF_ENABLED", True)
    WTF_CSRF_TIME_LIMIT = int(os.environ.get("NCAP_CSRF_TIME_LIMIT", "10800"))

    INSTANCE_DIR = INSTANCE_DIR
    DATA_DIR = DATA_DIR
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "NCAP_DATABASE_URI", f"sqlite:///{INSTANCE_DIR / 'ncap.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Which background services this process starts: web | scheduler | all | none
    ROLE = (os.environ.get("NCAP_ROLE", "all")).strip().lower()
    ENABLE_SCHEDULER = _env_flag("NCAP_ENABLE_SCHEDULER", ROLE in {"all", "scheduler"})
    ENABLE_WEB = ROLE in {"all", "web"}

    # Scheduler defaults (overridable from the admin settings screen)
    SCHEDULER_TICK_SECONDS = int(os.environ.get("NCAP_SCHEDULER_TICK_SECONDS", "300"))
    SCHEDULER_MAX_WORKERS = int(os.environ.get("NCAP_SCHEDULER_MAX_WORKERS", "3"))
    SCHEDULER_GRACE_SECONDS = int(os.environ.get("NCAP_SCHEDULER_GRACE_SECONDS", "120"))

    # Scraper defaults
    DEFAULT_TIMEOUT = int(os.environ.get("NCAP_HTTP_TIMEOUT", "30"))
    DEFAULT_USER_AGENT = os.environ.get(
        "NCAP_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
    PLAYWRIGHT_ENABLED = _env_flag("NCAP_PLAYWRIGHT_ENABLED", True)
    PLAYWRIGHT_HEADLESS = _env_flag("NCAP_PLAYWRIGHT_HEADLESS", True)
    PLAYWRIGHT_EXECUTABLE = os.environ.get("NCAP_PLAYWRIGHT_EXECUTABLE") or None
    PLAYWRIGHT_STORAGE_DIR = Path(
        os.environ.get("NCAP_PLAYWRIGHT_STORAGE_DIR", DATA_DIR / "browser-profiles")
    )
    SCREENSHOT_DIR = Path(os.environ.get("NCAP_SCREENSHOT_DIR", DATA_DIR / "screenshots"))

    # Self-update
    UPDATE_MODE = os.environ.get("NCAP_UPDATE_MODE", "git")  # git | manual | disabled
    GIT_REMOTE = os.environ.get("NCAP_GIT_REMOTE", "origin")
    GIT_BRANCH = os.environ.get("NCAP_GIT_BRANCH", "main")
    SERVICE_WEB = os.environ.get("NCAP_SERVICE_WEB", "ncap-web")
    SERVICE_SCHEDULER = os.environ.get("NCAP_SERVICE_SCHEDULER", "ncap-scheduler")

    MAINTENANCE_MODE = _env_flag("NCAP_MAINTENANCE_MODE", False)

    LOG_LEVEL = os.environ.get("NCAP_LOG_LEVEL", "INFO")
    LOG_FILE = os.environ.get("NCAP_LOG_FILE", str(INSTANCE_DIR / "ncap.log"))

    MAX_CONTENT_LENGTH = 8 * 1024 * 1024


class DevelopmentConfig(Config):
    DEBUG = True
    TESTING = False


class ProductionConfig(Config):
    DEBUG = False
    TESTING = False
    # Defaults to False so a fresh install stays usable over plain HTTP.
    # Set NCAP_SESSION_SECURE=true once HTTPS is in place (the installer and
    # the admin panel's System page both call this out).
    SESSION_COOKIE_SECURE = _env_flag("NCAP_SESSION_SECURE", False)
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE


class TestingConfig(Config):
    TESTING = True
    DEBUG = True
    WTF_CSRF_ENABLED = False
    ENABLE_SCHEDULER = False
    SERVER_NAME = "localhost.localdomain"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    LOGIN_DISABLED = False


config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}


def get_config(name: str | None = None) -> type[Config]:
    return config_by_name.get((name or os.environ.get("NCAP_ENV", "development")).lower(),
                              DevelopmentConfig)
