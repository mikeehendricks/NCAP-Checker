"""End-to-end tests that drive the REAL Chromium browser against local mock portals.

These prove the whole browser pipeline works - consent gate, JavaScript-rendered
form, fill, submit, parse - without ever touching a live government portal or
needing internet access.

They are skipped automatically when Playwright/Chromium is unavailable, so the
rest of the suite still runs on a bare install.
"""
from __future__ import annotations

import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import CheckStatus, Source  # noqa: E402
from app.scrapers import get_adapter  # noqa: E402
from app.scrapers.browser_engine import is_playwright_available  # noqa: E402

CHROMIUM = os.environ.get("NCAP_PLAYWRIGHT_EXECUTABLE") or None


def _launch_works() -> bool:
    """Can we actually start a browser right now?"""
    if not is_playwright_available():
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            kwargs = {"args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]}
            if CHROMIUM:
                kwargs["executable_path"] = CHROMIUM
            browser = p.chromium.launch(**kwargs)
            browser.close()
        return True
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"browser unavailable: {exc}")
        return False


BROWSER_OK = _launch_works()

pytestmark = pytest.mark.skipif(
    not BROWSER_OK,
    reason="Playwright/Chromium is not available in this environment "
           "(set NCAP_PLAYWRIGHT_EXECUTABLE to a Chromium binary to enable).",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def mock_portal_server():
    from werkzeug.serving import make_server

    from scripts.mock_portals import create_mock_app

    port = _free_port()
    server = make_server("127.0.0.1", port, create_mock_app(), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def make_source(app, key: str, url: str, engine: str = "browser") -> Source:
    source = Source(key=key, name=f"Mock {key}", agency="Mock",
                    base_url=url, check_url=url, engine=engine,
                    enabled=True, timeout_seconds=30, delay_seconds=0, config={})
    from app.extensions import db

    db.session.add(source)
    db.session.commit()
    return source


def build(source, **extra):
    settings = {
        "playwright_enabled": True,
        "playwright_headless": True,
        "playwright_executable": CHROMIUM,
        "playwright_timeout_ms": 30000,
        "user_agent": "",
        **extra,
    }
    return get_adapter(source, settings=settings)


@pytest.mark.parametrize("prefix,label", [("qc", "Quezon City"), ("paranaque", "Parañaque"),
                                          ("sanjuan", "San Juan")])
def test_platform_portal_returns_violations(app, mock_portal_server, prefix, label):
    url = f"{mock_portal_server}/{prefix}/check-violations?mode=violations"
    source = make_source(app, f"mock_{prefix}", url)
    outcome = build(source).check("ABC1234", "1234567890")

    assert outcome.status == CheckStatus.FOUND, outcome.message
    assert outcome.engine == "browser"
    assert len(outcome.violations) == 2
    assert outcome.violations[0].ticket_no == "MOCK-0001"
    assert outcome.violations[0].offense == "Disregarding Traffic Sign"


def test_platform_portal_reports_clean(app, mock_portal_server):
    url = f"{mock_portal_server}/qc/check-violations?mode=clean"
    source = make_source(app, "mock_clean", url)
    outcome = build(source).check("XYZ9999", "9876543210")
    assert outcome.status == CheckStatus.CLEAN, (outcome.status, outcome.message,
                                                 outcome.snippet[:200])


def test_challenge_page_is_blocked_not_clean(app, mock_portal_server):
    url = f"{mock_portal_server}/qc/check-violations?mode=blocked"
    source = make_source(app, "mock_blocked", url)
    outcome = build(source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.BLOCKED
    assert "cloudflare" in outcome.message.lower()


def test_manila_portal(app, mock_portal_server):
    url = f"{mock_portal_server}/manila/nocontact?mode=violations"
    source = make_source(app, "mock_manila", url)
    outcome = build(source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.FOUND, (outcome.status, outcome.message)
    assert outcome.violations[0].ticket_no == "MOCK-0001"


def test_mmda_portal(app, mock_portal_server):
    url = f"{mock_portal_server}/mmda/?mode=violations"
    source = make_source(app, "mock_mmda", url)
    outcome = build(source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.FOUND, (outcome.status, outcome.message)
    assert outcome.violations[0].offense == "Disregarding Traffic Sign"


def test_http_engine_against_json_api(app, mock_portal_server):
    source = Source(key="mock_api", name="Mock API", agency="Mock",
                    base_url=mock_portal_server,
                    check_url=f"{mock_portal_server}/api/violations/search",
                    engine="http", enabled=True, timeout_seconds=20, delay_seconds=0,
                    config={"http": {"path": "api/violations/search",
                                     "content_type": "json"}})
    from app.extensions import db

    db.session.add(source)
    db.session.commit()

    outcome = build(source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.FOUND
    assert outcome.violations[0].ticket_no == "API-1"
    assert outcome.violations[0].amount is not None


def test_screenshot_is_captured_for_browser_runs(app, mock_portal_server):
    url = f"{mock_portal_server}/qc/check-violations?mode=violations"
    source = make_source(app, "mock_shot", url)
    outcome = build(source).check("ABC1234", "1234567890", capture_screenshot=True)
    assert outcome.screenshot
    path = app.config["SCREENSHOT_DIR"] / outcome.screenshot
    assert path.is_file() and path.stat().st_size > 1000
