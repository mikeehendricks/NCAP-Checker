"""Orchestrates checks: runs adapters, persists runs and reconciles violations."""
from __future__ import annotations

import logging
from datetime import timedelta

from ..extensions import db
from ..models import (CheckRun, CheckStatus, Notification, Source, Vehicle,
                      Violation)
from ..scrapers import get_adapter
from . import settings as settings_service
from .notifier import notify_new_violation
from ..utils import utcnow

logger = logging.getLogger(__name__)


def _settings_snapshot() -> dict:
    try:
        return settings_service.get_settings(include_secrets=True)
    except Exception:  # pragma: no cover - DB not ready
        return {}


def run_check_for_vehicle(vehicle: Vehicle, sources: list[Source] | None = None,
                          triggered_by: str = "manual",
                          capture_screenshot: bool = False,
                          engine: str | None = None,
                          commit: bool = True) -> dict:
    """Check one vehicle against every enabled source.

    Returns a summary dict. Never raises: individual source failures are
    recorded per source so one broken portal cannot stop the others.
    """
    global_settings = _settings_snapshot()
    now = utcnow()
    sources = sources or Source.query.filter_by(enabled=True).all()

    summary = {
        "vehicle_id": vehicle.id,
        "plate": vehicle.plate_number,
        "sources": 0,
        "found": 0,
        "new": 0,
        "blocked": 0,
        "errors": 0,
        "unknown": 0,
        "clean": 0,
        "statuses": {},
        "new_violations": [],
    }

    for source in sources:
        if not source.is_available(now=now):
            summary["statuses"][source.key] = CheckStatus.SKIPPED
            continue

        adapter = get_adapter(source, settings=global_settings)
        outcome = adapter.check(vehicle.plate_norm or vehicle.plate_number,
                                vehicle.mv_norm or vehicle.mv_number,
                                engine=engine,
                                capture_screenshot=capture_screenshot)
        summary["sources"] += 1
        summary["statuses"][source.key] = outcome.status

        run = CheckRun(
            vehicle_id=vehicle.id,
            source_id=source.id,
            status=outcome.status,
            engine=outcome.engine,
            http_status=outcome.http_status,
            message=(outcome.message or "")[:500],
            found_count=len(outcome.violations),
            new_count=0,
            duration_ms=outcome.duration_ms,
            triggered_by=triggered_by[:32],
            screenshot=outcome.screenshot or "",
            created_at=utcnow(),
        )
        db.session.add(run)

        new_violations = _reconcile(vehicle, source, outcome.violations)
        run.new_count = len(new_violations)
        source.record_outcome(
            outcome.status,
            outcome.message,
            cooldown_base=int(global_settings.get("circuit_breaker_base", 5) or 5),
            cooldown_cap=int(global_settings.get("circuit_breaker_cap", 240) or 240),
        )

        summary["found"] += len(outcome.violations)
        summary["new"] += len(new_violations)
        summary["new_violations"].extend(new_violations)
        if outcome.status == CheckStatus.BLOCKED:
            summary["blocked"] += 1
        elif outcome.status == CheckStatus.ERROR:
            summary["errors"] += 1
        elif outcome.status == CheckStatus.UNKNOWN:
            summary["unknown"] += 1
        elif outcome.status == CheckStatus.CLEAN:
            summary["clean"] += 1

    # Roll the per-source results up to a single vehicle status.
    statuses = [s for s in summary["statuses"].values() if s != CheckStatus.SKIPPED]
    vehicle.last_status = CheckStatus.worst(*statuses) if statuses else CheckStatus.SKIPPED
    vehicle.last_checked_at = utcnow()
    vehicle.check_count = (vehicle.check_count or 0) + 1
    if summary["new"]:
        vehicle.last_message = f"{summary['new']} new violation(s) found"
    elif summary["errors"] or summary["blocked"]:
        vehicle.last_message = (f"{summary['blocked']} blocked / {summary['errors']} failed "
                                f"of {summary['sources']} source(s)")
    elif summary["unknown"]:
        vehicle.last_message = "Some portals returned an unrecognised response"
    else:
        vehicle.last_message = "No violations found" if summary["sources"] else "No sources enabled"

    if commit:
        db.session.commit()
        if summary["new_violations"] and global_settings.get("notify_on_new_violation", True):
            # One alert per vehicle per pass: several portals often report the
            # same ticket, and five e-mails about one fine is just noise.
            notify_new_violation(vehicle.owner, vehicle, summary["new_violations"],
                                 settings=global_settings)
            db.session.commit()
    return summary


def _reconcile(vehicle: Vehicle, source: Source, records) -> list[Violation]:
    """Insert new violations, touch existing ones. Returns the new ones."""
    new_violations: list[Violation] = []
    for record in records:
        ident = record.fingerprint(source.key)
        existing = Violation.query.filter_by(vehicle_id=vehicle.id, source_id=source.id,
                                             fingerprint=ident).first()
        if existing is not None:
            existing.touch()
            # Refresh mutable fields (status may change to "paid", etc.)
            if record.status:
                existing.status = record.status
            if record.amount is not None:
                existing.amount = record.amount
            continue
        violation = Violation(
            vehicle_id=vehicle.id,
            source_id=source.id,
            fingerprint=ident,
            ticket_no=(record.ticket_no or "")[:64],
            nov_no=(record.nov_no or "")[:64],
            plate=(record.plate or vehicle.plate_number or "")[:32],
            violation_date=record.violation_date,
            violation_time=record.violation_time,
            location=(record.location or "")[:255],
            offense=(record.offense or "")[:255],
            amount=record.amount,
            currency=record.currency or "PHP",
            status=(record.status or "")[:64],
            due_date=record.due_date,
            raw=record.raw,
        )
        db.session.add(violation)
        new_violations.append(violation)
    if new_violations:
        db.session.flush()
    return new_violations


# ---------------------------------------------------------------------------
# Background ("check now") execution
# ---------------------------------------------------------------------------
import threading

_ACTIVE_CHECKS: set[int] = set()
_ACTIVE_LOCK = threading.Lock()


def is_checking(vehicle_id: int) -> bool:
    with _ACTIVE_LOCK:
        return vehicle_id in _ACTIVE_CHECKS


def active_check_ids() -> list[int]:
    with _ACTIVE_LOCK:
        return sorted(_ACTIVE_CHECKS)


def start_background_check(app, vehicle: Vehicle, engine: str | None = None,
                           triggered_by: str = "manual") -> bool:
    """Run a check in a worker thread so the page does not block for minutes.

    Returns False when a check for this vehicle is already running.
    """
    with _ACTIVE_LOCK:
        if vehicle.id in _ACTIVE_CHECKS:
            return False
        _ACTIVE_CHECKS.add(vehicle.id)

    def worker() -> None:
        with app.app_context():
            try:
                run_check_for_vehicle(vehicle, triggered_by=triggered_by,
                                      capture_screenshot=True, engine=engine)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Background check failed for vehicle %s", vehicle.id)
                db.session.rollback()
            finally:
                with _ACTIVE_LOCK:
                    _ACTIVE_CHECKS.discard(vehicle.id)
                db.session.remove()

    thread = threading.Thread(target=worker, name=f"ncap-check-{vehicle.id}", daemon=True)
    thread.start()
    return True


def due_vehicles(default_interval: int, grace_seconds: int = 0) -> list[Vehicle]:
    """Vehicles whose next check time has arrived (or never ran)."""
    now = utcnow()
    due: list[Vehicle] = []
    for vehicle in Vehicle.query.filter_by(is_active=True).all():
        if vehicle.is_due(default_interval, grace_seconds=grace_seconds, now=now):
            due.append(vehicle)
    return due


def run_checks_for_all_due(triggered_by: str = "scheduler", limit: int | None = None,
                           delay_seconds: int | None = None) -> dict:
    """Scheduled entry point: check everything that is due, politely."""
    import time

    global_settings = _settings_snapshot()
    default_interval = int(global_settings.get("default_interval_minutes", 720) or 720)
    grace = int(global_settings.get("scheduler_grace_seconds", 120) or 0)
    delay = delay_seconds if delay_seconds is not None else int(
        global_settings.get("request_delay_seconds", 3) or 0)

    vehicles = due_vehicles(default_interval, grace)
    if limit:
        vehicles = vehicles[:limit]

    totals = {"vehicles": len(vehicles), "sources": 0, "found": 0, "new": 0,
              "blocked": 0, "errors": 0, "unknown": 0, "clean": 0, "results": []}
    for index, vehicle in enumerate(vehicles):
        if index and delay:
            time.sleep(min(delay, 15))
        try:
            summary = run_check_for_vehicle(vehicle, triggered_by=triggered_by)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Scheduled check failed for vehicle %s: %s", vehicle.id, exc)
            db.session.rollback()
            totals["errors"] += 1
            continue
        totals["sources"] += summary["sources"]
        totals["found"] += summary["found"]
        totals["new"] += summary["new"]
        totals["blocked"] += summary["blocked"]
        totals["errors"] += summary["errors"]
        totals["unknown"] += summary["unknown"]
        totals["clean"] += summary["clean"]
        totals["results"].append({"vehicle_id": vehicle.id,
                                  "plate": vehicle.plate_number,
                                  "status": vehicle.last_status,
                                  "new": summary["new"]})
    return totals


def cleanup_history(retention_days: int = 365) -> int:
    """Drop old check runs (and their screenshots) to keep the DB small."""
    if not retention_days or retention_days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=retention_days)
    deleted = CheckRun.query.filter(CheckRun.created_at < cutoff).delete(
        synchronize_session=False)
    db.session.commit()
    return deleted


def mark_violation_resolved(violation: Violation, resolved: bool = True) -> Violation:
    violation.mark_resolved(resolved)
    db.session.commit()
    return violation
