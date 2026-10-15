"""Database models.

Everything lives in SQLite by default so a single-file install works on any
Ubuntu box, but the schema is plain SQLAlchemy so PostgreSQL/MySQL also work.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .utils import fingerprint, normalize_mv, normalize_plate, utcnow


# --------------------------------------------------------------------------
# Status vocabularies
# --------------------------------------------------------------------------
class CheckStatus:
    """Outcome of a single source lookup.

    ``clean`` is only ever reported when the portal positively answered
    "no violations". Anything ambiguous becomes ``unknown``/``blocked`` so the
    dashboard never lulls a user into a false sense of security.
    """

    CLEAN = "clean"
    FOUND = "found"
    BLOCKED = "blocked"
    ERROR = "error"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"

    ALL = (CLEAN, FOUND, BLOCKED, ERROR, UNKNOWN, SKIPPED)

    LABELS = {
        CLEAN: "No violations",
        FOUND: "Violation found",
        BLOCKED: "Blocked (CAPTCHA/bot check)",
        ERROR: "Check failed",
        UNKNOWN: "Unknown",
        SKIPPED: "Skipped",
    }

    # Higher wins when rolling per-source results up to the vehicle.
    SEVERITY = {CLEAN: 0, SKIPPED: 1, UNKNOWN: 2, ERROR: 3, BLOCKED: 4, FOUND: 5}

    @classmethod
    def label(cls, value: str) -> str:
        return cls.LABELS.get(value, "Unknown")

    @classmethod
    def worst(cls, *values: str) -> str:
        clean = [v for v in values if v in cls.SEVERITY]
        if not clean:
            return cls.UNKNOWN
        return max(clean, key=lambda v: cls.SEVERITY[v])


class EngineKind:
    HTTP = "http"
    BROWSER = "browser"
    AUTO = "auto"
    ALL = (HTTP, BROWSER, AUTO)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(128), default="")
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_approved = db.Column(db.Boolean, default=True, nullable=False)

    # Brute-force protection
    failed_logins = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)

    # Notification preferences
    notify_email = db.Column(db.Boolean, default=True, nullable=False)
    notify_new_violation = db.Column(db.Boolean, default=True, nullable=False)
    notify_check_failure = db.Column(db.Boolean, default=False, nullable=False)

    api_key = db.Column(db.String(64), unique=True, nullable=True, index=True)
    timezone = db.Column(db.String(64), default="Asia/Manila", nullable=False)
    last_login_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    vehicles = db.relationship(
        "Vehicle", back_populates="owner", cascade="all, delete-orphan", lazy="dynamic"
    )
    notifications = db.relationship(
        "Notification", back_populates="user", cascade="all, delete-orphan", lazy="dynamic"
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256",
                                                    salt_length=16)

    def verify_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > utcnow())

    def register_failure(self, threshold: int = 5, lock_minutes: int = 15) -> None:
        self.failed_logins = (self.failed_logins or 0) + 1
        if self.failed_logins >= threshold:
            self.locked_until = utcnow() + timedelta(minutes=lock_minutes)

    def clear_failures(self) -> None:
        self.failed_logins = 0
        self.locked_until = None

    def unread_notifications(self) -> int:
        return self.notifications.filter_by(is_read=False).count()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.username}>"


# --------------------------------------------------------------------------
# Configurable data sources (the five NCAP portals)
# --------------------------------------------------------------------------
class Source(db.Model):
    __tablename__ = "sources"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), unique=True, nullable=False)      # quezon_city
    name = db.Column(db.String(128), nullable=False)                 # Quezon City NCAP
    agency = db.Column(db.String(128), default="")                   # Quezon City LGU
    base_url = db.Column(db.String(512), nullable=False)
    check_url = db.Column(db.String(512), nullable=False)
    engine = db.Column(db.String(16), default=EngineKind.AUTO, nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    config = db.Column(db.JSON, default=dict, nullable=False)

    timeout_seconds = db.Column(db.Integer, default=30, nullable=False)
    delay_seconds = db.Column(db.Integer, default=3, nullable=False)

    # Health / circuit breaker
    last_run_at = db.Column(db.DateTime, nullable=True)
    last_success_at = db.Column(db.DateTime, nullable=True)
    last_status = db.Column(db.String(16), nullable=True)
    last_message = db.Column(db.String(512), default="", nullable=False)
    consecutive_failures = db.Column(db.Integer, default=0, nullable=False)
    disabled_until = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    violations = db.relationship("Violation", back_populates="source",
                                 cascade="all, delete-orphan", lazy="dynamic")
    runs = db.relationship("CheckRun", back_populates="source",
                           cascade="all, delete-orphan", lazy="dynamic")

    def is_available(self, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        if self.disabled_until and self.disabled_until > (now or utcnow()):
            return False
        return True

    def get_config(self, name: str, default=None):
        return (self.config or {}).get(name, default)

    def record_outcome(self, status: str, message: str = "", cooldown_base: int = 5,
                       cooldown_cap: int = 240) -> None:
        """Update health counters after a run (caller commits)."""
        from .utils import cooldown_from_failures

        self.last_run_at = utcnow()
        self.last_status = status
        self.last_message = (message or "")[:500]
        if status in (CheckStatus.BLOCKED, CheckStatus.ERROR):
            self.consecutive_failures = (self.consecutive_failures or 0) + 1
            minutes = cooldown_from_failures(self.consecutive_failures, cooldown_base,
                                             cooldown_cap)
            self.disabled_until = utcnow() + timedelta(minutes=minutes) if minutes else None
        else:
            self.consecutive_failures = 0
            self.disabled_until = None
            if status != CheckStatus.SKIPPED:
                self.last_success_at = self.last_run_at

    def reset_health(self) -> None:
        """Clear the circuit breaker so the source is tried again immediately.

        ``last_status`` goes back to ``None`` too, otherwise a single blocked
        or failed lookup would keep the "blocked" badge forever.
        """
        self.consecutive_failures = 0
        self.disabled_until = None
        self.last_status = None
        self.last_message = ""

    @property
    def health(self) -> str:
        if not self.enabled:
            return "disabled"
        if self.disabled_until and self.disabled_until > utcnow():
            return "cooling-down"
        if (self.consecutive_failures or 0) >= 3:
            return "failing"
        if self.last_status == CheckStatus.BLOCKED:
            return "blocked"
        if self.last_success_at:
            return "healthy"
        return "unknown"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Source {self.key}>"


# --------------------------------------------------------------------------
# Vehicles
# --------------------------------------------------------------------------
class Vehicle(db.Model):
    __tablename__ = "vehicles"
    __table_args__ = (
        db.UniqueConstraint("user_id", "plate_norm", "mv_norm", name="uq_user_vehicle"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    label = db.Column(db.String(128), default="", nullable=False)
    plate_number = db.Column(db.String(32), nullable=False)
    plate_norm = db.Column(db.String(32), nullable=False, index=True)
    mv_number = db.Column(db.String(64), nullable=False)
    mv_norm = db.Column(db.String(64), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    interval_minutes = db.Column(db.Integer, nullable=True)  # None -> global default

    last_checked_at = db.Column(db.DateTime, nullable=True)
    last_status = db.Column(db.String(16), nullable=True)
    last_message = db.Column(db.String(512), default="", nullable=False)
    check_count = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    owner = db.relationship("User", back_populates="vehicles")
    violations = db.relationship("Violation", back_populates="vehicle",
                                 cascade="all, delete-orphan", lazy="dynamic")
    runs = db.relationship("CheckRun", back_populates="vehicle",
                           cascade="all, delete-orphan", lazy="dynamic")

    def __init__(self, **kwargs):
        plate = kwargs.get("plate_number", "")
        mv = kwargs.get("mv_number", "")
        kwargs.setdefault("plate_norm", normalize_plate(plate))
        kwargs.setdefault("mv_norm", normalize_mv(mv))
        super().__init__(**kwargs)

    def set_identifiers(self, plate: str, mv: str) -> None:
        self.plate_number = (plate or "").strip().upper()
        self.mv_number = (mv or "").strip()
        self.plate_norm = normalize_plate(plate)
        self.mv_norm = normalize_mv(mv)

    @property
    def display_name(self) -> str:
        return self.label or self.plate_number

    def effective_interval(self, default: int = 720) -> int:
        """Minutes between checks: the vehicle override, else the server default."""
        return self.interval_minutes or default

    def next_check_at(self, default_interval: int = 720) -> datetime | None:
        if not self.is_active or not self.last_checked_at:
            return utcnow()
        return self.last_checked_at + timedelta(minutes=self.effective_interval(default_interval))

    def is_due(self, default_interval: int = 720, grace_seconds: int = 0,
               now: datetime | None = None) -> bool:
        if not self.is_active:
            return False
        if not self.last_checked_at:
            return True
        now = now or utcnow()
        return now >= (self.next_check_at(default_interval) or now) - timedelta(
            seconds=grace_seconds
        )

    def open_violations(self):
        return self.violations.filter_by(is_resolved=False)

    def open_violation_count(self) -> int:
        return self.open_violations().count()

    def total_due(self) -> Decimal:
        total = Decimal("0")
        for violation in self.open_violations():
            if violation.amount is not None:
                total += violation.amount
        return total

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Vehicle {self.plate_number}>"


# --------------------------------------------------------------------------
# Violations
# --------------------------------------------------------------------------
class Violation(db.Model):
    __tablename__ = "violations"
    __table_args__ = (
        db.UniqueConstraint("vehicle_id", "source_id", "fingerprint",
                            name="uq_violation_identity"),
        db.Index("ix_violation_lookup", "vehicle_id", "is_resolved"),
    )

    id = db.Column(db.Integer, primary_key=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=False,
                           index=True)
    source_id = db.Column(db.Integer, db.ForeignKey("sources.id"), nullable=False,
                          index=True)
    fingerprint = db.Column(db.String(32), nullable=False)

    ticket_no = db.Column(db.String(64), default="", nullable=False)
    nov_no = db.Column(db.String(64), default="", nullable=False)
    plate = db.Column(db.String(32), default="", nullable=False)
    violation_date = db.Column(db.Date, nullable=True)
    violation_time = db.Column(db.String(8), nullable=True)
    location = db.Column(db.String(255), default="", nullable=False)
    offense = db.Column(db.String(255), default="", nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=True)
    currency = db.Column(db.String(8), default="PHP", nullable=False)
    status = db.Column(db.String(64), default="", nullable=False)
    due_date = db.Column(db.Date, nullable=True)
    raw = db.Column(db.JSON, nullable=True)

    is_resolved = db.Column(db.Boolean, default=False, nullable=False)
    resolved_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, default="", nullable=False)

    first_seen_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    last_seen_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    vehicle = db.relationship("Vehicle", back_populates="violations")
    source = db.relationship("Source", back_populates="violations")

    @staticmethod
    def make_fingerprint(source_key: str, ticket_no: str, nov_no: str,
                         violation_date, offense: str) -> str:
        return fingerprint(source_key, ticket_no or nov_no, violation_date, offense)

    @property
    def reference(self) -> str:
        """Human-readable identifier for alerts and tables."""
        return self.ticket_no or self.nov_no or "(no reference)"

    def touch(self) -> None:
        self.last_seen_at = utcnow()

    def mark_resolved(self, resolved: bool = True) -> None:
        self.is_resolved = resolved
        self.resolved_at = utcnow() if resolved else None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Violation {self.ticket_no or self.nov_no}>"


# --------------------------------------------------------------------------
# Check runs (audit trail of every lookup)
# --------------------------------------------------------------------------
class CheckRun(db.Model):
    __tablename__ = "check_runs"
    __table_args__ = (db.Index("ix_run_recent", "vehicle_id", "created_at"),)

    id = db.Column(db.Integer, primary_key=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=False,
                           index=True)
    source_id = db.Column(db.Integer, db.ForeignKey("sources.id"), nullable=True,
                          index=True)
    status = db.Column(db.String(16), default=CheckStatus.UNKNOWN, nullable=False)
    engine = db.Column(db.String(16), default="", nullable=False)
    http_status = db.Column(db.Integer, nullable=True)
    message = db.Column(db.String(512), default="", nullable=False)
    found_count = db.Column(db.Integer, default=0, nullable=False)
    new_count = db.Column(db.Integer, default=0, nullable=False)
    duration_ms = db.Column(db.Integer, default=0, nullable=False)
    triggered_by = db.Column(db.String(32), default="manual", nullable=False)
    screenshot = db.Column(db.String(255), default="", nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    vehicle = db.relationship("Vehicle", back_populates="runs")
    source = db.relationship("Source", back_populates="runs")

    @property
    def duration_label(self) -> str:
        if self.duration_ms < 1000:
            return f"{self.duration_ms} ms"
        return f"{self.duration_ms / 1000:.1f} s"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CheckRun {self.id} {self.status}>"


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=True)
    kind = db.Column(db.String(32), default="info", nullable=False)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.Text, default="", nullable=False)
    url = db.Column(db.String(255), default="", nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    emailed = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    user = db.relationship("User", back_populates="notifications")

    def mark_read(self, read: bool = True) -> None:
        self.is_read = read


# --------------------------------------------------------------------------
# Settings and audit trail
# --------------------------------------------------------------------------
class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, default="", nullable=False)
    value_type = db.Column(db.String(16), default="str", nullable=False)
    description = db.Column(db.String(255), default="", nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    action = db.Column(db.String(64), nullable=False, index=True)
    target = db.Column(db.String(128), default="", nullable=False)
    detail = db.Column(db.Text, default="", nullable=False)
    ip_address = db.Column(db.String(64), default="", nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)

    user = db.relationship("User")

    @classmethod
    def record(cls, action: str, target: str = "", detail: str = "", user=None,
               ip_address: str = "") -> "AuditLog":
        entry = cls(
            action=action,
            target=str(target)[:128],
            detail=detail or "",
            user_id=getattr(user, "id", None),
            ip_address=(ip_address or "")[:64],
        )
        db.session.add(entry)
        return entry


# --------------------------------------------------------------------------
# Flask-Login
# --------------------------------------------------------------------------
def load_user(user_id: str):  # pragma: no cover - trivial glue
    return db.session.get(User, int(user_id))


def load_user_from_api_key(request):  # pragma: no cover - trivial glue
    """Flask-Login ``request_loader``: honour the ``X-API-Key`` header."""
    api_key = (request.headers.get("X-API-Key") or request.args.get("api_key") or "").strip()
    if not api_key:
        return None
    return User.query.filter_by(api_key=api_key, is_active=True, is_approved=True).first()
