"""Background scheduler: periodically re-checks every saved vehicle."""
from __future__ import annotations

import logging
import threading

from ..extensions import db
from ..utils import utcnow
from . import settings as settings_service
from .checker import cleanup_history, run_checks_for_all_due

logger = logging.getLogger(__name__)

_scheduler = None
_lock = threading.Lock()
_state = {
    "started_at": None,
    "last_run_at": None,
    "last_result": None,
    "running": False,
    "errors": 0,
}


def _job_check_due(app) -> None:
    """APScheduler entry point - one process, one job, no overlap."""
    with app.app_context():
        if not settings_service.get_setting("scheduler_enabled", True):
            logger.debug("Scheduler disabled by settings; skipping tick")
            return
        if _state["running"]:
            logger.debug("Previous scheduled run still active; skipping tick")
            return
        _state["running"] = True
        try:
            logger.info("Scheduled NCAP check starting")
            result = run_checks_for_all_due(triggered_by="scheduler")
            _state["last_run_at"] = utcnow()
            _state["last_result"] = {
                "vehicles": result["vehicles"],
                "sources": result["sources"],
                "new": result["new"],
                "blocked": result["blocked"],
                "errors": result["errors"],
                "unknown": result["unknown"],
            }
            logger.info("Scheduled NCAP check finished: %s", _state["last_result"])
        except Exception as exc:  # pragma: no cover - defensive
            db.session.rollback()
            _state["errors"] += 1
            _state["last_result"] = {"error": str(exc)[:300]}
            logger.exception("Scheduled run crashed: %s", exc)
        finally:
            _state["running"] = False
            db.session.remove()


def _job_cleanup(app) -> None:
    with app.app_context():
        try:
            days = int(settings_service.get_setting("retention_days", 365) or 0)
            removed = cleanup_history(days)
            if removed:
                logger.info("Retention cleanup removed %s old check runs", removed)
        except Exception as exc:  # pragma: no cover - defensive
            db.session.rollback()
            logger.warning("Cleanup job failed: %s", exc)
        finally:
            db.session.remove()


def init_scheduler(app):
    """Create and start the APScheduler instance (idempotent)."""
    global _scheduler
    if not app.config.get("ENABLE_SCHEDULER", True):
        logger.info("Scheduler disabled for role=%s", app.config.get("ROLE"))
        return None
    with _lock:
        if _scheduler is not None and getattr(_scheduler, "running", False):
            return _scheduler
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            from apscheduler.triggers.interval import IntervalTrigger
        except ImportError:  # pragma: no cover - optional dependency
            logger.warning("APScheduler not installed; scheduler disabled")
            return None

        with app.app_context():
            tick = int(settings_service.get_setting("scheduler_tick_seconds", 300) or 300)

        scheduler = BackgroundScheduler(
            daemon=True,
            job_defaults={"coalesce": True, "max_instances": 1,
                          "misfire_grace_time": 300},
            timezone=app.config.get("TIMEZONE", "Asia/Manila"),
        )
        scheduler.add_job(
            func=_job_check_due,
            trigger=IntervalTrigger(seconds=max(30, tick)),
            id="check_due",
            name="Check vehicles that are due",
            args=[app],
            replace_existing=True,
        )
        scheduler.add_job(
            func=_job_cleanup,
            trigger=CronTrigger(hour=3, minute=17),
            id="cleanup",
            name="Prune old check history",
            args=[app],
            replace_existing=True,
        )
        try:
            scheduler.start()
        except Exception as exc:  # pragma: no cover
            logger.warning("Could not start scheduler: %s", exc)
            return None
        _scheduler = scheduler
        _state["started_at"] = utcnow()
        logger.info("Scheduler started (tick=%ss, jobs=%s)", tick,
                    [job.id for job in scheduler.get_jobs()])
        return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:  # pragma: no cover
                pass
            _scheduler = None
            logger.info("Scheduler stopped")


def reschedule(app) -> bool:
    """Apply a changed tick interval without restarting the process."""
    if _scheduler is None:
        return False
    with app.app_context():
        tick = int(settings_service.get_setting("scheduler_tick_seconds", 300) or 300)
    try:
        from apscheduler.triggers.interval import IntervalTrigger

        _scheduler.reschedule_job("check_due", trigger=IntervalTrigger(seconds=max(30, tick)))
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not reschedule: %s", exc)
        return False


def scheduler_state() -> dict:
    jobs = []
    if _scheduler is not None:
        for job in _scheduler.get_jobs():
            next_run = getattr(job, "next_run_time", None)
            jobs.append({
                "id": job.id,
                "name": job.name,
                "trigger": str(job.trigger),
                "next_run_at": next_run.replace(tzinfo=None) if next_run else None,
            })
    return {
        "running": bool(_scheduler is not None and getattr(_scheduler, "running", False)),
        "started_at": _state["started_at"],
        "last_run_at": _state["last_run_at"],
        "last_result": _state["last_result"],
        "busy": _state["running"],
        "errors": _state["errors"],
        "jobs": jobs,
    }


def trigger_run_now(app, limit: int | None = None) -> None:
    """Run the due-checks job immediately in a background thread."""
    def runner():
        _job_check_due(app)

    thread = threading.Thread(target=runner, name="ncap-manual-run", daemon=True)
    thread.start()
    return thread



