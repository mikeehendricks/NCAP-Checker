#!/usr/bin/env python3
"""Long-running worker process for the background scheduler.

The web process (gunicorn) serves requests; this process owns the APScheduler
instance that periodically re-checks every due vehicle.  Run it with::

    .venv/bin/python scheduler_worker.py

and it is kept alive by the ``ncap-scheduler`` systemd unit.  Nothing else is
served here, so it can be restarted independently of the website.
"""
from __future__ import annotations

import logging
import signal
import sys
import time

from app import create_app
from app.services.scheduler import init_scheduler, shutdown_scheduler

STOP = False


def _handle_stop(signum, _frame):
    global STOP
    STOP = True
    logging.getLogger(__name__).info("Received signal %s, shutting down", signum)


def main() -> int:
    app = create_app()
    logging.basicConfig(level=getattr(logging, app.config.get("LOG_LEVEL", "INFO"), logging.INFO),
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    scheduler = init_scheduler(app)
    if scheduler is None:
        app.logger.error("Scheduler could not be started - is NCAP_ROLE set to a "
                         "role that enables it (all/scheduler)?")
        return 1

    app.logger.info("NCAP scheduler worker running (pid %s)", __import__("os").getpid())
    try:
        while not STOP:
            time.sleep(1)
    finally:
        shutdown_scheduler()
        app.logger.info("NCAP scheduler worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
