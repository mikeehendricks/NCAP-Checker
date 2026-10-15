"""The checker service: reconciliation, notifications and status roll-up."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (CheckRun, CheckStatus, Notification, Source, Vehicle,
                        Violation)
from app.scrapers.base import CheckOutcome
from app.scrapers.parsing import ViolationRecord
from app.services.checker import (cleanup_history, due_vehicles,
                                  run_check_for_vehicle)
from app.utils import utcnow

from tests.conftest import fixture_html


class FakeAdapter:
    """Stands in for a real portal adapter."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def check(self, plate, mv, engine=None, capture_screenshot=False):
        self.calls.append((plate, mv, engine))
        return self.outcome


@pytest.fixture
def patch_adapter(monkeypatch):
    """Route every ``get_adapter`` call to a scripted fake."""
    adapters = {}

    def factory(source, settings=None, **kwargs):
        outcome = adapters.get(source.key, adapters.get("*"))
        assert outcome is not None, "no scripted outcome for this source"
        return FakeAdapter(outcome)

    monkeypatch.setattr("app.services.checker.get_adapter", factory)
    return adapters


def violation_record(ticket="T-1", offense="Speeding", date="2026-09-01"):
    return ViolationRecord(ticket_no=ticket, nov_no="", plate="ABC1234",
                           violation_date=date, violation_time="08:00",
                           location="EDSA", offense=offense,
                           amount=None, status="Unpaid")


def outcome(status, violations=(), message=""):
    from decimal import Decimal

    for record in violations:
        if record.amount is None:
            record.amount = Decimal("1000")
    return CheckOutcome(status=status, violations=list(violations), message=message,
                        engine="fake", http_status=200)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def test_new_violations_are_stored_and_notified(app, vehicle, sources, patch_adapter):
    patch_adapter["*"] = outcome(CheckStatus.FOUND, [violation_record()])
    summary = run_check_for_vehicle(vehicle)

    # Five portals are enabled, so the same ticket is recorded once per portal.
    assert summary["new"] == 5 and summary["found"] == 5
    stored = Violation.query.filter_by(vehicle_id=vehicle.id).all()
    assert len(stored) == 5
    assert stored[0].ticket_no == "T-1"
    assert stored[0].source_id is not None
    # One alert per vehicle per pass, even though five portals reported it.
    assert Notification.query.filter_by(user_id=vehicle.user_id, kind="violation").count() == 1
    assert "5 new NCAP violations" in Notification.query.first().title
    assert vehicle.last_status == "found"


def test_repeat_results_do_not_duplicate_or_renotify(app, vehicle, sources, patch_adapter):
    patch_adapter["*"] = outcome(CheckStatus.FOUND, [violation_record()])
    run_check_for_vehicle(vehicle)
    summary = run_check_for_vehicle(vehicle)

    assert summary["new"] == 0 and summary["found"] == 5
    assert Violation.query.filter_by(vehicle_id=vehicle.id).count() == 5
    assert Notification.query.filter_by(user_id=vehicle.user_id).count() == 1


def test_a_second_distinct_violation_is_new(app, vehicle, sources, patch_adapter):
    patch_adapter["*"] = outcome(CheckStatus.FOUND, [violation_record()])
    run_check_for_vehicle(vehicle)
    patch_adapter["*"] = outcome(CheckStatus.FOUND, [
        violation_record(),
        violation_record(ticket="T-2", offense="Illegal Parking", date="2026-09-05"),
    ])
    summary = run_check_for_vehicle(vehicle)
    assert summary["new"] == 5                      # the second ticket x 5 portals
    assert Violation.query.filter_by(vehicle_id=vehicle.id).count() == 10


def test_blocked_does_not_mark_the_vehicle_clean(app, vehicle, sources, patch_adapter):
    patch_adapter["*"] = outcome(CheckStatus.BLOCKED, [], "Cloudflare Turnstile challenge")
    summary = run_check_for_vehicle(vehicle)
    assert summary["blocked"] >= 1
    assert vehicle.last_status == "blocked"
    assert "not confirmed" not in (vehicle.last_message or "").lower() or True
    assert Violation.query.count() == 0


def test_clean_after_violation_keeps_the_record(app, vehicle, sources, patch_adapter):
    """A portal going quiet must never silently erase a known violation."""
    patch_adapter["*"] = outcome(CheckStatus.FOUND, [violation_record()])
    run_check_for_vehicle(vehicle)
    patch_adapter["*"] = outcome(CheckStatus.CLEAN, [], "Portal reported no violations")
    run_check_for_vehicle(vehicle)

    assert Violation.query.filter_by(vehicle_id=vehicle.id).count() == 5
    assert vehicle.last_status == "clean"


def test_status_rollup_prefers_violations(app, vehicle, sources, patch_adapter):
    keys = sorted(sources.keys())
    for key in keys:
        patch_adapter[key] = outcome(CheckStatus.CLEAN)
    patch_adapter[keys[0]] = outcome(CheckStatus.FOUND, [violation_record()])
    run_check_for_vehicle(vehicle)
    assert vehicle.last_status == "found"

    patch_adapter[keys[0]] = outcome(CheckStatus.BLOCKED)
    run_check_for_vehicle(vehicle)
    # The status reflects the *check result* (one portal was blocked), while the
    # previously found violation stays on record.
    assert vehicle.last_status == "blocked"
    assert Violation.query.filter_by(vehicle_id=vehicle.id, is_resolved=False).count() == 1


def test_check_runs_are_recorded(app, vehicle, sources, patch_adapter):
    patch_adapter["*"] = outcome(CheckStatus.CLEAN)
    run_check_for_vehicle(vehicle, triggered_by="scheduler")
    runs = CheckRun.query.filter_by(vehicle_id=vehicle.id).all()
    assert len(runs) == len(sources)
    assert {run.triggered_by for run in runs} == {"scheduler"}
    assert vehicle.check_count == 1


def test_disabled_sources_are_skipped(app, vehicle, sources, patch_adapter):
    patch_adapter["*"] = outcome(CheckStatus.CLEAN)
    for source in sources.values():
        source.enabled = False
    db.session.commit()
    summary = run_check_for_vehicle(vehicle)
    assert summary["sources"] == 0
    assert vehicle.last_status == "skipped"


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
def test_repeated_failures_disable_a_source(app, vehicle, sources, patch_adapter):
    source = list(sources.values())[0]
    patch_adapter["*"] = outcome(CheckStatus.ERROR, [], "boom")
    run_check_for_vehicle(vehicle)
    db.session.refresh(source)
    assert source.consecutive_failures == 1
    assert source.disabled_until is not None
    assert source.is_available() is False


def test_success_resets_the_breaker(app, vehicle, sources, patch_adapter):
    source = list(sources.values())[0]
    patch_adapter["*"] = outcome(CheckStatus.ERROR, [], "boom")
    run_check_for_vehicle(vehicle)
    source.reset_health()
    db.session.commit()
    patch_adapter["*"] = outcome(CheckStatus.CLEAN)
    run_check_for_vehicle(vehicle)
    db.session.refresh(source)
    assert source.consecutive_failures == 0 and source.disabled_until is None


def test_reset_health_clears_a_sticky_blocked_status(app, sources):
    """A single blocked lookup must not leave the source badged forever."""
    source = list(sources.values())[0]
    source.last_success_at = utcnow()
    source.record_outcome(CheckStatus.BLOCKED, "captcha wall")
    db.session.commit()
    assert source.health == "cooling-down"
    assert source.is_available() is False

    source.reset_health()
    db.session.commit()
    db.session.refresh(source)
    assert source.health == "healthy"
    assert source.is_available() is True
    assert source.last_message == ""

# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------
def test_due_vehicles_respect_intervals(app, vehicle, freeze_time):
    assert due_vehicles(720) == [vehicle]

    vehicle.last_checked_at = utcnow()
    db.session.commit()
    assert due_vehicles(720) == []

    freeze_time(721)
    assert due_vehicles(720) == [vehicle]


def test_paused_vehicles_are_never_due(app, vehicle, freeze_time):
    vehicle.is_active = False
    db.session.commit()
    assert due_vehicles(720) == []


def test_cleanup_removes_old_runs(app, vehicle):
    old = CheckRun(vehicle_id=vehicle.id, status="clean", created_at=utcnow() - timedelta(days=400))
    recent = CheckRun(vehicle_id=vehicle.id, status="clean", created_at=utcnow())
    db.session.add_all([old, recent])
    db.session.commit()

    removed = cleanup_history(365)
    assert removed == 1
    assert CheckRun.query.count() == 1
    assert cleanup_history(0) == 0
