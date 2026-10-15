"""JSON API used by the CLI, cron jobs and future mobile clients.

Authenticate with a normal browser session (cookie) or by sending the user's
API key in the ``X-API-Key`` header (shown on the profile page).
"""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request
from flask_login import current_user, login_user
from sqlalchemy import desc

from ..extensions import db
from ..models import CheckRun, Source, User, Vehicle, Violation
from ..scrapers import get_adapter
from ..services import settings as settings_service
from ..services.checker import run_check_for_vehicle
from ..utils import normalize_mv, normalize_plate

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.before_request
def authenticate():
    if current_user.is_authenticated:
        return None
    key = request.headers.get("X-API-Key") or request.args.get("api_key")
    if key:
        user = User.query.filter_by(api_key=key, is_active=True, is_approved=True).first()
        if user is not None:
            login_user(user)
            g.api_authenticated = True
            return None
    return jsonify({"error": "authentication required"}), 401


def _vehicle_payload(vehicle: Vehicle) -> dict:
    return {
        "id": vehicle.id,
        "label": vehicle.label,
        "plate_number": vehicle.plate_number,
        "mv_number": vehicle.mv_number,
        "is_active": vehicle.is_active,
        "interval_minutes": vehicle.interval_minutes,
        "last_checked_at": vehicle.last_checked_at.isoformat(timespec="seconds")
        if vehicle.last_checked_at else None,
        "last_status": vehicle.last_status,
        "last_message": vehicle.last_message,
        "open_violations": vehicle.open_violation_count(),
        "total_due": float(vehicle.total_due() or 0),
    }


@api_bp.route("/status")
def status():
    return jsonify({
        "app": settings_service.get_setting("app_name", "NCAP Checker"),
        "version": current_app.config.get("VERSION"),
        "user": current_user.username if current_user.is_authenticated else None,
        "vehicles": Vehicle.query.filter_by(user_id=current_user.id).count()
        if current_user.is_authenticated else 0,
        "sources": [{"key": s.key, "name": s.name, "enabled": s.enabled,
                     "health": s.health} for s in Source.query.all()],
    })


@api_bp.route("/vehicles")
def vehicles():
    query = Vehicle.query
    if not current_user.is_admin:
        query = query.filter_by(user_id=current_user.id)
    return jsonify([_vehicle_payload(v) for v in query.order_by(desc(Vehicle.created_at)).all()])


@api_bp.route("/violations")
def violations():
    query = (Violation.query.join(Vehicle)
             .filter(Vehicle.user_id == current_user.id) if not current_user.is_admin
             else Violation.query)
    items = query.order_by(desc(Violation.first_seen_at)).limit(200).all()
    return jsonify([{
        "id": v.id,
        "vehicle": v.vehicle.display_name,
        "plate": v.plate or v.vehicle.plate_number,
        "source": v.source.name if v.source else None,
        "ticket_no": v.ticket_no,
        "nov_no": v.nov_no,
        "offense": v.offense,
        "location": v.location,
        "violation_date": v.violation_date.isoformat() if v.violation_date else None,
        "amount": float(v.amount) if v.amount is not None else None,
        "status": v.status,
        "is_resolved": v.is_resolved,
        "first_seen_at": v.first_seen_at.isoformat(timespec="seconds"),
    } for v in items])


@api_bp.route("/vehicles/<int:vehicle_id>/check", methods=["POST"])
def check_vehicle(vehicle_id: int):
    vehicle = Vehicle.query.get_or_404(vehicle_id)
    if vehicle.user_id != current_user.id and not current_user.is_admin:
        return jsonify({"error": "not found"}), 404
    source_keys = request.json.get("sources") if request.is_json else None
    sources = None
    if source_keys:
        sources = Source.query.filter(Source.key.in_(source_keys)).all()
    summary = run_check_for_vehicle(vehicle, sources=sources,
                                    triggered_by=request.headers.get("X-API-Key")
                                    and "api" or "manual")
    return jsonify({
        "vehicle": _vehicle_payload(vehicle),
        "new": summary["new"],
        "found": summary["found"],
        "statuses": summary["statuses"],
    })


@api_bp.route("/lookup", methods=["POST"])
def lookup():
    """Ad-hoc check: no vehicle is stored, nothing is persisted."""
    payload = request.get_json(silent=True) or {}
    plate = normalize_plate(payload.get("plate_number") or payload.get("plate"))
    mv = normalize_mv(payload.get("mv_number") or payload.get("mv"))
    if not plate:
        return jsonify({"error": "plate_number is required"}), 400
    keys = payload.get("sources") or [s.key for s in Source.query.filter_by(enabled=True).all()]

    settings = settings_service.get_settings(include_secrets=True)
    results = []
    for source in Source.query.filter(Source.key.in_(keys)).all():
        adapter = get_adapter(source, settings=settings)
        outcome = adapter.check(plate, mv, engine=payload.get("engine"))
        results.append({
            "source": source.key,
            "name": source.name,
            "status": outcome.status,
            "engine": outcome.engine,
            "message": outcome.message,
            "violations": [v.to_dict() for v in outcome.violations],
        })
    worst = "clean"
    for result in results:
        if result["status"] == "found":
            worst = "found"
        elif worst != "found" and result["status"] in ("blocked", "error", "unknown"):
            worst = result["status"]
    return jsonify({"plate": plate, "overall": worst, "results": results})
