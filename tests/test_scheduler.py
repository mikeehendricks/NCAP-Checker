"""Scheduler wiring and the due-check job."""
from __future__ import annotations

from app.extensions import db
from app.models import CheckRun, CheckStatus
from app.services import scheduler as scheduler_service
from app.services.scheduler import (_job_check_due, init_scheduler, reschedule,
                                    scheduler_state, shutdown_scheduler)


def test_scheduler_is_off_by_default_in_tests(app):
    assert app.config.get("ENABLE_SCHEDULER") is False


def test_init_and_shutdown(app, monkeypatch):
    app.config["ENABLE_SCHEDULER"] = True
    calls = []
    monkeypatch.setattr(scheduler_service, "run_checks_for_all_due",
                        lambda **kw: calls.append(kw) or {"vehicles": 0, "sources": 0,
                                                          "found": 0, "new": 0,
                                                          "blocked": 0, "errors": 0,
                                                          "unknown": 0, "clean": 0,
                                                          "results": []})
    scheduler = init_scheduler(app)
    assert scheduler is not None
    state = scheduler_state()
    assert state["running"] is True
    assert {job["id"] for job in state["jobs"]} >= {"check_due", "cleanup"}

    _job_check_due(app)
    assert calls and calls[0]["triggered_by"] == "scheduler"

    shutdown_scheduler()
    assert scheduler_state()["running"] is False
    app.config["ENABLE_SCHEDULER"] = False


def test_disabled_scheduler_skips_work(app, monkeypatch):
    from app.services import settings as settings_service

    settings_service.set_setting("scheduler_enabled", False)
    called = []
    monkeypatch.setattr(scheduler_service, "run_checks_for_all_due",
                        lambda **kw: called.append(kw))
    _job_check_due(app)
    assert called == []
    settings_service.set_setting("scheduler_enabled", True)


def test_job_records_state(app, vehicle, monkeypatch):
    monkeypatch.setattr(
        scheduler_service, "run_checks_for_all_due",
        lambda **kw: {"vehicles": 2, "sources": 5, "found": 1, "new": 1,
                      "blocked": 0, "errors": 0, "unknown": 0, "clean": 4,
                      "results": []})
    _job_check_due(app)
    state = scheduler_state()
    assert state["last_result"]["vehicles"] == 2
    assert state["last_run_at"] is not None
    assert state["busy"] is False


def test_job_survives_an_exception(app, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("scheduler blew up")

    monkeypatch.setattr(scheduler_service, "run_checks_for_all_due", boom)
    _job_check_due(app)          # must not raise
    assert "error" in (scheduler_state()["last_result"] or {})
    assert scheduler_state()["busy"] is False


def test_reschedule_applies_new_interval(app, monkeypatch):
    app.config["ENABLE_SCHEDULER"] = True
    from app.services import settings as settings_service

    settings_service.set_setting("scheduler_tick_seconds", 900)
    scheduler = init_scheduler(app)
    assert reschedule(app) is True
    trigger = str(scheduler.get_job("check_due").trigger)
    assert "15:00:00" in trigger or "900" in trigger or "interval" in trigger
    shutdown_scheduler()
    app.config["ENABLE_SCHEDULER"] = False


def test_scheduled_run_persists_results(app, vehicle, sources, monkeypatch):
    """The job end-to-end: due vehicle -> adapter -> rows in the database."""
    from app.scrapers.base import CheckOutcome

    monkeypatch.setattr(
        "app.services.checker.get_adapter",
        lambda source, settings=None, **kw: type(
            "A", (), {"check": lambda self, plate, mv, engine=None,
                      capture_screenshot=False: CheckOutcome(
                          status=CheckStatus.CLEAN, violations=[],
                          engine="http", message="Portal reported no violations")})())
    from app.services.scheduler import _job_check_due

    _job_check_due(app)
    assert CheckRun.query.filter_by(vehicle_id=vehicle.id,
                                    triggered_by="scheduler").count() == len(sources)
    db.session.refresh(vehicle)
    assert vehicle.last_status == "clean"
