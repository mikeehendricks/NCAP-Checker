"""Administration: sources, users, settings, updates and diagnostics."""
from __future__ import annotations

import json
import os
import platform
import shutil
from datetime import timedelta

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_from_directory, url_for)
from flask_login import current_user, login_required
from sqlalchemy import desc, func

from .. import config as app_config
from ..decorators import admin_required
from ..extensions import db
from ..forms import SourceForm
from ..models import (AuditLog, CheckRun, CheckStatus, EngineKind, Notification,
                      Setting, Source, User, Vehicle, Violation)
from ..scrapers import (get_adapter, seed_default_sources)
from ..services import settings as settings_service
from ..services import updater
from ..services.checker import cleanup_history, run_check_for_vehicle
from ..services.notifier import send_test_email
from ..services.scheduler import (reschedule, scheduler_state, shutdown_scheduler,
                                  trigger_run_now)
from ..utils import utcnow

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _audit(action: str, target: str = "", detail: str = "") -> None:
    AuditLog.record(action, target=target, detail=detail, user=current_user,
                    ip_address=(request.remote_addr or "")[:64])


def _settings_form(defaults: dict, submitted: dict) -> dict:
    """Coerce a submitted settings form back into typed values."""
    values = {}
    for key, (default, value_type, _desc) in defaults.items():
        if value_type == "bool":
            values[key] = key in submitted
        elif key in submitted:
            values[key] = submitted.get(key)
        else:
            values[key] = default
    return values


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
@admin_bp.route("/")
@login_required
@admin_required
def overview():
    stats = {
        "users": User.query.count(),
        "admins": User.query.filter_by(is_admin=True).count(),
        "pending_users": User.query.filter_by(is_approved=False).count(),
        "vehicles": Vehicle.query.count(),
        "active_vehicles": Vehicle.query.filter_by(is_active=True).count(),
        "violations": Violation.query.count(),
        "open_violations": Violation.query.filter_by(is_resolved=False).count(),
        "runs": CheckRun.query.count(),
        "runs_24h": CheckRun.query.filter(
            CheckRun.created_at >= utcnow() - timedelta(days=1)).count(),
    }
    statuses = dict(db.session.query(CheckRun.status, func.count(CheckRun.id))
                    .group_by(CheckRun.status).all())
    sources = Source.query.order_by(Source.name).all()
    recent_runs = CheckRun.query.order_by(desc(CheckRun.created_at)).limit(10).all()
    return render_template("admin/overview.html", stats=stats, sources=sources,
                           statuses=statuses, recent_runs=recent_runs,
                           scheduler=scheduler_state(),
                           services=updater.service_status(),
                           CheckStatus=CheckStatus)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@admin_bp.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    defaults = settings_service.DEFAULTS
    if request.method == "POST":
        section = request.form.get("section", "general")
        submitted = {}
        for key in request.form.keys():
            if key.startswith("set_"):
                submitted[key[4:]] = request.form.get(key)
        values = _settings_form(defaults, submitted)
        # Only update the keys belonging to the submitted section.
        sections = {
            "general": ["app_name", "tagline", "registration_open",
                        "registration_requires_approval", "maintenance_mode"],
            "scheduling": ["default_interval_minutes", "min_interval_minutes",
                           "scheduler_enabled", "scheduler_tick_seconds",
                           "scheduler_grace_seconds", "retention_days"],
            "scraping": ["http_retries", "request_delay_seconds",
                         "circuit_breaker_base", "circuit_breaker_cap",
                         "playwright_enabled", "playwright_headless",
                         "playwright_timeout_ms", "playwright_executable",
                         "user_agent", "proxy_url"],
            "mail": ["smtp_enabled", "smtp_host", "smtp_port", "smtp_user",
                     "smtp_password", "smtp_starttls", "smtp_ssl", "mail_from",
                     "mail_sender_name", "notify_on_new_violation",
                     "notify_on_resolved", "notify_on_failure"],
        }
        keys = sections.get(section, list(defaults.keys()))
        for key in keys:
            if key == "smtp_password" and not submitted.get("smtp_password"):
                continue  # leave an unchanged masked password alone
            settings_service.set_setting(key, values.get(key))
        _audit("settings.updated", target=section)
        db.session.commit()
        reschedule(current_app._get_current_object())
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings", section=section))

    section = request.args.get("section", "general")
    values = settings_service.get_settings(include_secrets=False)
    smtp_configured = bool(settings_service.get_settings(include_secrets=True)
                           .get("smtp_password"))
    return render_template("admin/settings.html", defaults=defaults, values=values,
                           section=section, smtp_configured=smtp_configured,
                           scheduler=scheduler_state())


@admin_bp.route("/settings/test-email", methods=["POST"])
@login_required
@admin_required
def settings_test_email():
    recipient = (request.form.get("recipient") or "").strip() or current_user.email
    ok, detail = send_test_email(
        recipient, settings_service.as_smtp_config())
    if ok:
        flash(f"Test message sent to {recipient}.", "success")
    else:
        flash(f"Could not send the test message: {detail}", "danger")
    _audit("settings.test_email", target=recipient, detail=detail)
    db.session.commit()
    return redirect(url_for("admin.settings", section="mail"))


@admin_bp.route("/run-now", methods=["POST"])
@login_required
@admin_required
def run_now():
    trigger_run_now(current_app._get_current_object())
    _audit("scheduler.run_now")
    db.session.commit()
    flash("Due checks started in the background. This page refreshes automatically.",
          "info")
    return redirect(url_for("admin.overview"))


@admin_bp.route("/scheduler/toggle", methods=["POST"])
@login_required
@admin_required
def scheduler_toggle():
    enabled = not settings_service.get_setting("scheduler_enabled", True)
    settings_service.set_setting("scheduler_enabled", enabled)
    _audit("scheduler.toggled", detail="enabled" if enabled else "disabled")
    db.session.commit()
    flash("Scheduler " + ("enabled." if enabled else "disabled."), "success")
    return redirect(url_for("admin.settings", section="scheduling"))


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
@admin_bp.route("/sources")
@login_required
@admin_required
def sources():
    sources = Source.query.order_by(Source.name).all()
    return render_template("admin/sources.html", sources=sources,
                           CheckStatus=CheckStatus, EngineKind=EngineKind)


@admin_bp.route("/sources/seed", methods=["POST"])
@login_required
@admin_required
def seed_sources():
    created = seed_default_sources(update_existing=False)
    _audit("sources.seeded", detail=f"{created} created")
    db.session.commit()
    flash(f"{created} missing portal(s) restored." if created else
          "All built-in portals are already present.", "success")
    return redirect(url_for("admin.sources"))


@admin_bp.route("/sources/<int:source_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def edit_source(source_id: int):
    source = Source.query.get_or_404(source_id)
    form = SourceForm(obj=source)
    if request.method == "GET":
        form.config.data = json.dumps(source.config or {}, indent=2)
    if form.validate_on_submit():
        source.name = form.name.data.strip()
        source.agency = (form.agency.data or "").strip()
        source.base_url = form.base_url.data.strip()
        source.check_url = form.check_url.data.strip()
        source.engine = form.engine.data
        source.enabled = bool(form.enabled.data)
        source.timeout_seconds = int(form.timeout_seconds.data or 30)
        source.delay_seconds = int(form.delay_seconds.data or 3)
        try:
            source.config = json.loads(form.config.data) if (form.config.data or "").strip() else {}
        except ValueError:
            source.config = {}
        _audit("source.updated", target=source.key)
        db.session.commit()
        flash(f"{source.name} saved.", "success")
        return redirect(url_for("admin.sources"))
    return render_template("admin/source_edit.html", form=form, source=source)


@admin_bp.route("/sources/<int:source_id>/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_source(source_id: int):
    source = Source.query.get_or_404(source_id)
    source.enabled = not source.enabled
    source.reset_health()
    _audit("source.toggled", target=source.key,
           detail="enabled" if source.enabled else "disabled")
    db.session.commit()
    flash(f"{source.name} {'enabled' if source.enabled else 'disabled'}.", "success")
    return redirect(url_for("admin.sources"))


@admin_bp.route("/sources/<int:source_id>/reset", methods=["POST"])
@login_required
@admin_required
def reset_source(source_id: int):
    source = Source.query.get_or_404(source_id)
    source.reset_health()
    _audit("source.reset", target=source.key)
    db.session.commit()
    flash(f"Health counters cleared for {source.name}.", "success")
    return redirect(url_for("admin.sources"))


@admin_bp.route("/sources/<int:source_id>/probe", methods=["GET", "POST"])
@login_required
@admin_required
def probe_source(source_id: int):
    """Run one live lookup and show exactly what came back.

    This is the tool an administrator uses to tune selectors: it reports the
    engine used, HTTP status, detected CAPTCHA/blocked state, parsed rows and a
    screenshot of the page as the browser saw it.
    """
    source = Source.query.get_or_404(source_id)
    result = None
    if request.method == "POST":
        plate = (request.form.get("plate") or "ABC1234").strip()
        mv = (request.form.get("mv_number") or "1234567890").strip()
        engine = request.form.get("engine") or None
        settings = settings_service.get_settings(include_secrets=True)
        adapter = get_adapter(source, settings=settings)
        outcome = adapter.check(plate, mv, engine=engine, capture_screenshot=True)
        result = {
            "plate": plate,
            "mv": mv,
            "status": outcome.status,
            "engine": outcome.engine,
            "http_status": outcome.http_status,
            "message": outcome.message,
            "duration_ms": outcome.duration_ms,
            "violations": [v.to_dict() for v in outcome.violations],
            "records": outcome.records[:5],
            "snippet": outcome.snippet,
            "screenshot": outcome.screenshot,
        }
        _audit("source.probed", target=source.key,
               detail=f"{outcome.status} via {outcome.engine}")
        db.session.commit()
    return render_template("admin/probe.html", source=source, result=result)


@admin_bp.route("/screenshot/<path:filename>")
@login_required
@admin_required
def screenshot(filename: str):
    directory = app_config.DATA_DIR / "screenshots"
    if not directory.is_dir():
        abort(404)
    return send_from_directory(str(directory), filename)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
@admin_bp.route("/users")
@login_required
@admin_required
def users():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template("admin/users.html", users=users)


@admin_bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@login_required
@admin_required
def toggle_user_active(user_id: int):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot deactivate your own account.", "warning")
        return redirect(url_for("admin.users"))
    user.is_active = not user.is_active
    _audit("user.toggled_active", target=user.username,
           detail="active" if user.is_active else "inactive")
    db.session.commit()
    flash(f"{user.username} is now {'active' if user.is_active else 'inactive'}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/toggle-admin", methods=["POST"])
@login_required
@admin_required
def toggle_user_admin(user_id: int):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot remove your own administrator rights.", "warning")
        return redirect(url_for("admin.users"))
    if user.is_admin and User.query.filter_by(is_admin=True).count() == 1:
        flash("At least one administrator must remain.", "warning")
        return redirect(url_for("admin.users"))
    user.is_admin = not user.is_admin
    _audit("user.toggled_admin", target=user.username,
           detail="admin" if user.is_admin else "user")
    db.session.commit()
    flash(f"{user.username} is now {'an administrator' if user.is_admin else 'a regular user'}.",
          "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/approve", methods=["POST"])
@login_required
@admin_required
def approve_user(user_id: int):
    user = User.query.get_or_404(user_id)
    user.is_approved = not user.is_approved
    _audit("user.approved" if user.is_approved else "user.unapproved", target=user.username)
    db.session.commit()
    flash(f"{user.username} {'approved' if user.is_approved else 'unapproved'}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@admin_required
def reset_user_password(user_id: int):
    import secrets

    user = User.query.get_or_404(user_id)
    password = secrets.token_urlsafe(12)
    user.set_password(password)
    user.clear_failures()
    _audit("user.password_reset", target=user.username)
    db.session.commit()
    flash(f"Temporary password for {user.username}: {password}", "warning")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_user(user_id: int):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "warning")
        return redirect(url_for("admin.users"))
    if user.is_admin and User.query.filter_by(is_admin=True).count() == 1:
        flash("At least one administrator must remain.", "warning")
        return redirect(url_for("admin.users"))
    username = user.username
    _audit("user.deleted", target=username)
    db.session.delete(user)
    db.session.commit()
    flash(f"{username} deleted.", "info")
    return redirect(url_for("admin.users"))


# ---------------------------------------------------------------------------
# Updates / system
# ---------------------------------------------------------------------------
@admin_bp.route("/updates", methods=["GET", "POST"])
@login_required
@admin_required
def updates():
    status = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "check":
            status = {"type": "check", **updater.check_for_updates()}
            _audit("update.checked")
        elif action == "apply":
            status = {"type": "apply", **updater.perform_update(restart=True)}
            _audit("update.applied")
        elif action == "restart":
            status = {"type": "restart", **updater.restart_services()}
            _audit("services.restart")
        elif action == "cleanup":
            days = int(request.form.get("days", 365) or 0)
            removed = cleanup_history(days)
            status = {"type": "cleanup", "removed": removed}
            _audit("data.cleanup", detail=f"{removed} runs removed")
        db.session.commit()
    return render_template("admin/updates.html", git=updater.git_info(),
                           status=status, services=updater.service_status(),
                           update_mode=updater.UPDATE_MODE,
                           scheduler=scheduler_state())


@admin_bp.route("/system")
@login_required
@admin_required
def system():
    db_path = app_config.INSTANCE_DIR / "ncap.db"
    total, used, free = shutil.disk_usage(str(app_config.BASE_DIR))
    info = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "flask": __import__("flask").__version__,
        "database_uri": current_app.config.get("SQLALCHEMY_DATABASE_URI", ""),
        "instance_dir": str(app_config.INSTANCE_DIR),
        "data_dir": str(app_config.DATA_DIR),
        "db_size_mb": round(db_path.stat().st_size / 1048576, 2) if db_path.is_file() else 0,
        "disk_total_gb": round(total / 2 ** 30, 1),
        "disk_free_gb": round(free / 2 ** 30, 1),
        "playwright": __import__("app.scrapers.browser_engine",
                                 fromlist=["x"]).is_playwright_available(),
        "role": current_app.config.get("ROLE"),
        "timezone": current_app.config.get("TIMEZONE"),
        "env_file": (app_config.BASE_DIR / ".env").is_file(),
    }
    return render_template("admin/system.html", info=info,
                           services=updater.service_status(),
                           scheduler=scheduler_state())


@admin_bp.route("/audit")
@login_required
@admin_required
def audit():
    page = request.args.get("page", 1, type=int)
    pagination = (AuditLog.query.order_by(desc(AuditLog.created_at))
                  .paginate(page=page, per_page=40, error_out=False))
    return render_template("admin/audit.html", pagination=pagination,
                           items=pagination.items)


@admin_bp.route("/vehicles")
@login_required
@admin_required
def all_vehicles():
    vehicles = Vehicle.query.order_by(desc(Vehicle.created_at)).all()
    return render_template("admin/vehicles.html", vehicles=vehicles)
