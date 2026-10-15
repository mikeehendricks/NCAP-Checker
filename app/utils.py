"""Small shared helpers: normalisation, parsing and formatting."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

PLATE_RE = re.compile(r"^[A-Z0-9]{4,10}$")
MV_RE = re.compile(r"^[0-9]{6,20}$")

AMOUNT_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d{1,2})?")

DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %I:%M %p",
)

TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?")


def utcnow() -> datetime:
    """Naive UTC timestamp - the single source of truth for stored times."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_plate(value: str | None) -> str:
    """Uppercase and strip separators: ``abc 1234`` -> ``ABC1234``."""
    if not value:
        return ""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def normalize_mv(value: str | None) -> str:
    """Strip separators from an MV file number: ``1234-5678`` -> ``12345678``."""
    if not value:
        return ""
    return re.sub(r"[^0-9]", "", value or "")


def is_valid_plate(value: str | None) -> bool:
    norm = normalize_plate(value)
    if not (4 <= len(norm) <= 10):
        return False
    if not PLATE_RE.match(norm):
        return False
    return bool(re.search(r"[A-Z]", norm)) and bool(re.search(r"[0-9]", norm))


def is_valid_mv(value: str | None) -> bool:
    norm = normalize_mv(value)
    return bool(MV_RE.match(norm))


def parse_amount(value) -> Decimal | None:
    """Pull a decimal amount out of strings like ``PHP 2,000.00``."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip()
    if not text:
        return None
    match = AMOUNT_RE.search(text.replace("₱", "").replace("PHP", ""))
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def parse_date(value) -> date | None:
    """Best-effort date parsing across the formats these portals emit."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Numeric slash/dash dates are ambiguous: Philippine portals print
    # MM/DD/YYYY, so prefer that, but flip when only one side can be a month.
    numeric = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", text)
    if numeric:
        first, second, year = (int(part) for part in numeric.groups())
        if first > 12 >= second:
            day, month = first, second
        elif second > 12 >= first:
            month, day = first, second
        else:
            month, day = first, second          # default MM/DD/YYYY
        try:
            return date(year, month, day)
        except ValueError:
            return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        try:
            return datetime.strptime(match.group(0), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def parse_time(value) -> str | None:
    """Return a normalised ``HH:MM`` string, or ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = (match.group(4) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def fingerprint(*parts) -> str:
    """Stable identity for a violation record so re-checks de-duplicate."""
    basis = "|".join(
        re.sub(r"\s+", " ", str(part or "")).strip().lower() for part in parts
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:32]


def humanize_delta(value: datetime | None, now: datetime | None = None) -> str:
    """``3 minutes ago`` / ``in 2 hours`` style helper for templates."""
    if not value:
        return "never"
    now = now or utcnow()
    delta = value - now
    seconds = abs(int(delta.total_seconds()))
    if seconds < 60:
        amount, unit = seconds, "second"
    elif seconds < 3600:
        amount, unit = seconds // 60, "minute"
    elif seconds < 86400:
        amount, unit = seconds // 3600, "hour"
    else:
        amount, unit = seconds // 86400, "day"
    if amount != 1:
        unit += "s"
    return f"in {amount} {unit}" if delta.total_seconds() > 0 else f"{amount} {unit} ago"


def truncate(text: str | None, length: int = 120) -> str:
    text = (text or "").strip()
    return text if len(text) <= length else text[: length - 1] + "…"


def mask_identifier(value: str | None, keep: int = 3) -> str:
    """Partially hide an identifier in logs/shared screenshots."""
    value = (value or "").strip()
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


def cooldown_from_failures(failures: int, base: int = 5, cap: int = 240) -> int:
    """Exponential back-off (minutes) used by the source circuit breaker."""
    if failures <= 0:
        return 0
    return min(cap, base * (2 ** min(failures - 1, 6)))


def next_run_time(last_run: datetime | None, interval_minutes: int,
                  now: datetime | None = None) -> datetime | None:
    if not interval_minutes or interval_minutes <= 0:
        return None
    return (last_run or (now or utcnow())) + timedelta(minutes=interval_minutes)


def to_local(value: datetime | None, tz_name: str = "Asia/Manila") -> datetime | None:
    """Convert a stored naive-UTC datetime to the configured display zone."""
    if value is None:
        return None
    try:
        from zoneinfo import ZoneInfo

        return value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
    except Exception:
        # Fall back to a fixed +08:00 (Philippine Time) if tzdata is missing.
        from datetime import timezone as _tz

        return value.replace(tzinfo=timezone.utc).astimezone(_tz(timedelta(hours=8)))


def format_datetime(value: datetime | None, tz_name: str = "Asia/Manila",
                    fmt: str = "%d %b %Y, %I:%M %p") -> str:
    local = to_local(value, tz_name)
    if local is None:
        return "-"
    return local.strftime(fmt).replace(" 0", " ").replace("AM", "am").replace("PM", "pm")


def format_date(value, fmt: str = "%d %b %Y") -> str:
    if value is None:
        return "-"
    if hasattr(value, "strftime"):
        return value.strftime(fmt)
    parsed = parse_date(value)
    return parsed.strftime(fmt) if parsed else str(value)
