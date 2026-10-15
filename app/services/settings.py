"""Runtime settings stored in the database and editable from the admin panel.

Values are typed (``bool`` / ``int`` / ``str`` / ``json``) and cached per
request-ish; defaults are seeded on first use so a fresh install works even
before an administrator touches anything.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..extensions import db
from ..models import Setting

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, tuple[Any, str, str]] = {
    # key: (default, type, description)
    "app_name": ("NCAP Checker", "str", "Shown in the header and e-mails"),
    "tagline": ("Consolidated NCAP violation monitoring", "str", "Subtitle on the dashboard"),
    "registration_open": (True, "bool", "Allow new users to register"),
    "registration_requires_approval": (False, "bool", "Admin must approve new accounts"),

    "default_interval_minutes": (720, "int", "How often a saved vehicle is re-checked"),
    "min_interval_minutes": (60, "int", "Smallest interval a user may choose"),
    "scheduler_enabled": (True, "bool", "Run the background scheduler in this process"),
    "scheduler_tick_seconds": (300, "int", "How often the scheduler wakes up"),
    "scheduler_grace_seconds": (120, "int", "Grace period before a due check runs"),

    "http_retries": (2, "int", "Retries per HTTP request"),
    "request_delay_seconds": (3, "int", "Politeness delay between portal hits"),
    "circuit_breaker_base": (5, "int", "Base back-off minutes after a failure"),
    "circuit_breaker_cap": (240, "int", "Maximum back-off minutes"),

    "playwright_enabled": (True, "bool", "Allow the Playwright browser engine"),
    "playwright_headless": (True, "bool", "Run Chromium headless"),
    "playwright_timeout_ms": (45000, "int", "Browser action timeout"),
    "playwright_executable": ("", "str", "Optional path to a Chromium binary"),
    "user_agent": ("", "str", "Override the default browser User-Agent"),
    "proxy_url": ("", "str", "Optional HTTP(S) proxy, e.g. http://host:3128"),

    "smtp_enabled": (False, "bool", "Send e-mail alerts"),
    "smtp_host": ("", "str", "SMTP server host"),
    "smtp_port": (587, "int", "SMTP server port"),
    "smtp_user": ("", "str", "SMTP username"),
    "smtp_password": ("", "secret", "SMTP password or app password"),
    "smtp_starttls": (True, "bool", "Use STARTTLS"),
    "smtp_ssl": (False, "bool", "Use implicit SSL"),
    "mail_from": ("", "str", "From address (defaults to the SMTP user)"),
    "mail_sender_name": ("NCAP Checker", "str", "Display name on outgoing mail"),

    "notify_on_new_violation": (True, "bool", "Notify when a new violation appears"),
    "notify_on_resolved": (False, "bool", "Notify when a violation disappears"),
    "notify_on_failure": (False, "bool", "Notify when a portal check fails"),

    "retention_days": (365, "int", "Delete check history older than this (0 = keep)"),
    "maintenance_mode": (False, "bool", "Show a maintenance page to non-admins"),
}

SECRET_KEYS = {"smtp_password"}


def _coerce(value: Any, value_type: str) -> Any:
    if value_type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if value_type == "int":
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return 0
    if value_type == "json":
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value or "null")
        except (TypeError, ValueError):
            return None
    return value if value is not None else ""


def _to_storage(value: Any, value_type: str) -> str:
    if value_type == "json":
        return json.dumps(value, ensure_ascii=False)
    if value_type == "bool":
        return "1" if value else "0"
    return "" if value is None else str(value)


def get_setting(key: str, default: Any = None) -> Any:
    default_value, value_type, _ = DEFAULTS.get(key, (default, "str", ""))
    if default is None:
        default = default_value
    row = db.session.get(Setting, key)
    if row is None:
        return _coerce(default, value_type)
    return _coerce(row.value, row.value_type or value_type)


def get_settings(include_secrets: bool = False) -> dict[str, Any]:
    """Return every setting with defaults applied."""
    rows = {row.key: row for row in Setting.query.all()}
    result: dict[str, Any] = {}
    for key, (default, value_type, _desc) in DEFAULTS.items():
        row = rows.get(key)
        if row is not None:
            value = _coerce(row.value, row.value_type or value_type)
        else:
            value = _coerce(default, value_type)
        if key in SECRET_KEYS and not include_secrets:
            value = "•" * 8 if value else ""
        result[key] = value
    return result


def set_setting(key: str, value: Any, value_type: str | None = None) -> None:
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")
    default, default_type, description = DEFAULTS[key]
    value_type = value_type or default_type
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value_type=value_type, description=description)
        db.session.add(row)
    row.value = _to_storage(value, value_type)
    row.value_type = value_type
    db.session.commit()


def set_many(values: dict[str, Any]) -> None:
    for key, value in (values or {}).items():
        if key in DEFAULTS:
            set_setting(key, value)


def ensure_defaults() -> int:
    """Materialise defaults into the table so admins can see/edit everything."""
    created = 0
    for key, (default, value_type, description) in DEFAULTS.items():
        if db.session.get(Setting, key) is None:
            db.session.add(Setting(key=key, value=_to_storage(default, value_type),
                                   value_type=value_type, description=description))
            created += 1
    if created:
        db.session.commit()
    return created


def masked_settings() -> dict[str, Any]:
    return get_settings(include_secrets=False)


def as_smtp_config() -> dict[str, Any]:
    values = get_settings(include_secrets=True)
    return {
        "enabled": bool(values.get("smtp_enabled")),
        "host": values.get("smtp_host", ""),
        "port": int(values.get("smtp_port") or 587),
        "user": values.get("smtp_user", ""),
        "password": values.get("smtp_password", ""),
        "starttls": bool(values.get("smtp_starttls")),
        "ssl": bool(values.get("smtp_ssl")),
        "sender": values.get("mail_from") or values.get("smtp_user") or "noreply@localhost",
        "sender_name": values.get("mail_sender_name") or "NCAP Checker",
        "app_name": values.get("app_name") or "NCAP Checker",
    }
