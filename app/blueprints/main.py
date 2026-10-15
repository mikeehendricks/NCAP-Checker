"""Landing page, dashboard, notifications and help."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import desc, func

from ..extensions import db
from ..models import CheckRun, Notification, Source, Vehicle, Violation
from ..services import settings as settings_service
from ..services.scheduler import scheduler_state

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    if not current_user.is_authenticated:
        return render_template("landing.html")
    return redirect(url_for("main.dashboard"))


@main_bp.route("/dashboard")
@login_required
def dashboard():
    vehicles = (Vehicle.query.filter_by(user_id=current_user.id)
                .order_by(desc(Vehicle.created_at)).all())
    vehicle_ids = [v.id for v in vehicles]

    open_violations = []
    recent_runs = []
    if vehicle_ids:
        open_violations = (Violation.query.filter(Violation.vehicle_id.in_(vehicle_ids),
                                                  Violation.is_resolved.is_(False))
                           .order_by(desc(Violation.first_seen_at)).limit(8).all())
        recent_runs = (CheckRun.query.filter(CheckRun.vehicle_id.in_(vehicle_ids))
                       .order_by(desc(CheckRun.created_at)).limit(12).all())

    totals = {
        "vehicles": len(vehicles),
        "monitored": sum(1 for v in vehicles if v.is_active),
        "open": sum(v.open_violation_count() for v in vehicles),
        "due": sum(float(v.total_due() or 0) for v in vehicles),
        "attention": sum(1 for v in vehicles
                         if v.last_status in ("blocked", "error", "unknown")),
    }
    sources = Source.query.order_by(Source.name).all()
    return render_template(
        "dashboard.html",
        vehicles=vehicles,
        open_violations=open_violations,
        recent_runs=recent_runs,
        totals=totals,
        sources=sources,
        scheduler=scheduler_state(),
        default_interval=settings_service.get_setting("default_interval_minutes", 720),
    )


@main_bp.route("/notifications")
@login_required
def notifications():
    page = request.args.get("page", 1, type=int)
    query = (Notification.query.filter_by(user_id=current_user.id)
             .order_by(desc(Notification.created_at)))
    pagination = query.paginate(page=page, per_page=25, error_out=False)
    return render_template("notifications.html", pagination=pagination,
                           items=pagination.items)


@main_bp.route("/notifications/<int:notification_id>/read", methods=["POST"])
@login_required
def mark_notification(notification_id: int):
    entry = Notification.query.filter_by(id=notification_id,
                                         user_id=current_user.id).first_or_404()
    entry.mark_read(True)
    db.session.commit()
    return redirect(request.referrer or url_for("main.notifications"))


@main_bp.route("/notifications/read-all", methods=["POST"])
@login_required
def mark_all_notifications():
    (Notification.query.filter_by(user_id=current_user.id, is_read=False)
     .update({Notification.is_read: True}, synchronize_session=False))
    db.session.commit()
    flash("All notifications marked as read.", "success")
    return redirect(url_for("main.notifications"))


@main_bp.route("/pending-approval")
@login_required
def pending_approval():
    if current_user.is_approved:
        return redirect(url_for("main.dashboard"))
    return render_template("pending_approval.html")


@main_bp.route("/help", endpoint="help")
def help_page():
    sources = Source.query.order_by(Source.name).all()
    return render_template("help.html", sources=sources)


@main_bp.route("/about")
def about():
    from ..services.updater import git_info

    return render_template("about.html", git=git_info(),
                           sources=Source.query.order_by(Source.name).all())


@main_bp.app_context_processor
def inject_globals():
    """Make a handful of helpers available to every template."""
    from ..models import CheckStatus
    from ..services import updater

    unread = 0
    if current_user.is_authenticated:
        try:
            unread = current_user.unread_notifications()
        except Exception:  # pragma: no cover - DB may be mid-migration
            unread = 0
    settings = {}
    try:
        settings = settings_service.get_settings()
    except Exception:  # pragma: no cover
        settings = {}
    return {
        "app_name": settings.get("app_name", "NCAP Checker"),
        "tagline": settings.get("tagline", ""),
        "app_version": updater.git_info().get("commit_short") or __import__(
            "app.config", fromlist=["VERSION"]).VERSION,
        "unread_notifications": unread,
        "CheckStatus": CheckStatus,
        "settings": settings,
    }
