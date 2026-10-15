"""Vehicle management and manual checks."""
from __future__ import annotations

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required
from sqlalchemy import desc

from ..extensions import db
from ..forms import VehicleForm
from ..models import AuditLog, CheckRun, Source, Vehicle, Violation
from ..services import settings as settings_service
from ..services.checker import (is_checking, run_check_for_vehicle,
                                start_background_check)
from ..utils import utcnow

vehicles_bp = Blueprint("vehicles", __name__, url_prefix="/vehicles")


def _owned_vehicle(vehicle_id: int) -> Vehicle:
    vehicle = Vehicle.query.get_or_404(vehicle_id)
    if vehicle.user_id != current_user.id and not current_user.is_admin:
        abort(404)
    return vehicle


@vehicles_bp.route("/")
@login_required
def index():
    vehicles = (Vehicle.query.filter_by(user_id=current_user.id)
                .order_by(desc(Vehicle.created_at)).all())
    default_interval = settings_service.get_setting("default_interval_minutes", 720)
    min_interval = settings_service.get_setting("min_interval_minutes", 60)
    return render_template("vehicles/index.html", vehicles=vehicles,
                           default_interval=default_interval,
                           min_interval=min_interval)


def _duplicate_vehicle(plate: str, mv: str, exclude_id: int | None = None):
    """Return an existing vehicle owned by the current user with the same ids."""
    from ..utils import normalize_mv, normalize_plate

    query = Vehicle.query.filter_by(
        user_id=current_user.id,
        plate_norm=normalize_plate(plate),
        mv_norm=normalize_mv(mv),
    )
    if exclude_id:
        query = query.filter(Vehicle.id != exclude_id)
    return query.first()


@vehicles_bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    form = VehicleForm()
    if form.validate_on_submit():
        duplicate = _duplicate_vehicle(form.plate_number.data, form.mv_number.data)
        if duplicate is not None:
            flash(f"You already saved this vehicle: {duplicate.display_name}.", "warning")
            return render_template("vehicles/form.html", form=form, vehicle=None,
                                   title="Add a vehicle")
        vehicle = Vehicle(user_id=current_user.id)
        vehicle.set_identifiers(form.plate_number.data, form.mv_number.data)
        vehicle.label = (form.label.data or "").strip()
        vehicle.is_active = bool(form.is_active.data)
        vehicle.interval_minutes = form.interval_minutes.data or None
        db.session.add(vehicle)
        db.session.flush()
        AuditLog.record("vehicle.created", target=vehicle.plate_number, user=current_user,
                        ip_address=(request.remote_addr or "")[:64])
        db.session.commit()
        flash(f"{vehicle.display_name} added. Run a check to fetch its status.", "success")
        return redirect(url_for("vehicles.detail", vehicle_id=vehicle.id))
    return render_template("vehicles/form.html", form=form, vehicle=None,
                           title="Add a vehicle")


@vehicles_bp.route("/<int:vehicle_id>")
@login_required
def detail(vehicle_id: int):
    vehicle = _owned_vehicle(vehicle_id)
    runs = (CheckRun.query.filter_by(vehicle_id=vehicle.id)
            .order_by(desc(CheckRun.created_at)).limit(40).all())
    violations = (Violation.query.filter_by(vehicle_id=vehicle.id)
                  .order_by(desc(Violation.is_resolved), desc(Violation.first_seen_at)).all())
    sources = Source.query.order_by(Source.name).all()
    default_interval = settings_service.get_setting("default_interval_minutes", 720)
    return render_template("vehicles/detail.html", vehicle=vehicle, runs=runs,
                           violations=violations, sources=sources,
                           default_interval=default_interval,
                           checking=is_checking(vehicle.id))


@vehicles_bp.route("/<int:vehicle_id>/edit", methods=["GET", "POST"])
@login_required
def edit(vehicle_id: int):
    vehicle = _owned_vehicle(vehicle_id)
    form = VehicleForm(obj=vehicle)
    if form.validate_on_submit():
        duplicate = _duplicate_vehicle(form.plate_number.data, form.mv_number.data,
                                       exclude_id=vehicle.id)
        if duplicate is not None:
            flash("Another vehicle you saved already uses that plate and MV number.",
                  "warning")
            return render_template("vehicles/form.html", form=form, vehicle=vehicle,
                                   title=f"Edit {vehicle.display_name}")
        vehicle.set_identifiers(form.plate_number.data, form.mv_number.data)
        vehicle.label = (form.label.data or "").strip()
        vehicle.is_active = bool(form.is_active.data)
        vehicle.interval_minutes = form.interval_minutes.data or None
        AuditLog.record("vehicle.updated", target=vehicle.plate_number, user=current_user,
                        ip_address=(request.remote_addr or "")[:64])
        db.session.commit()
        flash("Vehicle updated.", "success")
        return redirect(url_for("vehicles.detail", vehicle_id=vehicle.id))
    return render_template("vehicles/form.html", form=form, vehicle=vehicle,
                           title=f"Edit {vehicle.display_name}")


@vehicles_bp.route("/<int:vehicle_id>/delete", methods=["POST"])
@login_required
def delete(vehicle_id: int):
    vehicle = _owned_vehicle(vehicle_id)
    plate = vehicle.plate_number
    AuditLog.record("vehicle.deleted", target=plate, user=current_user,
                    ip_address=(request.remote_addr or "")[:64])
    db.session.delete(vehicle)
    db.session.commit()
    flash(f"{plate} removed.", "info")
    return redirect(url_for("vehicles.index"))


@vehicles_bp.route("/<int:vehicle_id>/toggle", methods=["POST"])
@login_required
def toggle(vehicle_id: int):
    vehicle = _owned_vehicle(vehicle_id)
    vehicle.is_active = not vehicle.is_active
    db.session.commit()
    state = "monitored" if vehicle.is_active else "paused"
    flash(f"{vehicle.display_name} is now {state}.", "success")
    return redirect(request.referrer or url_for("vehicles.detail", vehicle_id=vehicle.id))


@vehicles_bp.route("/<int:vehicle_id>/check", methods=["POST"])
@login_required
def check(vehicle_id: int):
    """Run a check right now against every enabled portal."""
    vehicle = _owned_vehicle(vehicle_id)
    source_key = request.form.get("source_key") or None
    engine = request.form.get("engine") or None
    sources = [Source.query.filter_by(key=source_key).first()] if source_key else None
    sources = [s for s in (sources or []) if s] or None

    # Five portals behind a headless browser can take a couple of minutes, so
    # by default the work runs in a worker thread and the page polls for it.
    # `sync=1` (used by the CLI/API and the tests) waits for the result.
    if request.form.get("sync") != "1" and not sources:
        started = start_background_check(current_app._get_current_object(), vehicle,
                                         engine=engine, triggered_by="manual")
        if not started:
            flash("A check for this vehicle is already running.", "info")
        else:
            flash("Check started - this page refreshes automatically.", "info")
        return redirect(url_for("vehicles.detail", vehicle_id=vehicle.id) + "#runs")

    summary = run_check_for_vehicle(vehicle, sources=sources, triggered_by="manual",
                                    capture_screenshot=True, engine=engine)
    if summary["sources"] == 0:
        flash("No enabled portals were available (they may be in back-off).", "warning")
    elif summary["new"]:
        flash(f"Found {summary['new']} new violation(s) for {vehicle.display_name}.",
              "danger")
    elif summary["found"]:
        flash(f"{summary['found']} known violation(s) - nothing new.", "info")
    elif summary["blocked"] or summary["errors"]:
        flash("Some portals could not be checked (see the run log below). "
              "Status is NOT confirmed clean.", "warning")
    elif summary["unknown"]:
        flash("Portals replied, but the response could not be understood. "
              "Tune this source in Admin → Sources.", "warning")
    else:
        flash(f"No violations found for {vehicle.display_name}.", "success")
    return redirect(url_for("vehicles.detail", vehicle_id=vehicle.id) + "#runs")


@vehicles_bp.route("/<int:vehicle_id>/runs.json")
@login_required
def runs_json(vehicle_id: int):
    """Lightweight polling endpoint used by the detail page."""
    vehicle = _owned_vehicle(vehicle_id)
    runs = (CheckRun.query.filter_by(vehicle_id=vehicle.id)
            .order_by(desc(CheckRun.created_at)).limit(15).all())
    return jsonify([{
        "id": run.id,
        "source": run.source.name if run.source else "-",
        "status": run.status,
        "engine": run.engine,
        "message": run.message,
        "found": run.found_count,
        "new": run.new_count,
        "duration": run.duration_label,
        "created_at": run.created_at.isoformat(timespec="seconds") if run.created_at else None,
    } for run in runs])


@vehicles_bp.route("/violations/<int:violation_id>/resolve", methods=["POST"])
@login_required
def resolve_violation(violation_id: int):
    violation = Violation.query.get_or_404(violation_id)
    if violation.vehicle.user_id != current_user.id and not current_user.is_admin:
        abort(404)
    resolved = request.form.get("resolved", "1") == "1"
    violation.mark_resolved(resolved)
    db.session.commit()
    flash(("Marked as settled." if resolved else "Reopened."), "success")
    return redirect(request.referrer or url_for("main.dashboard"))
