"""Adapter behaviour: engines, fallbacks, block detection and configuration."""
from __future__ import annotations

import pytest
import requests

from app.models import CheckStatus, EngineKind
from app.scrapers import get_adapter
from app.scrapers.base import BaseAdapter
from app.scrapers.http_engine import HttpResponse
from tests.conftest import fixture_html

CHECK_URL = "https://portal.test/check"


@pytest.fixture
def http_mock(requests_mock):
    return requests_mock


def build(source, **settings):
    return get_adapter(source, settings=settings)


# ---------------------------------------------------------------------------
# HTTP engine
# ---------------------------------------------------------------------------
def test_http_engine_finds_violations(app, clean_source, http_mock):
    http_mock.post(CHECK_URL, text=fixture_html("qc_violations.html"), status_code=200)
    outcome = build(clean_source).check("ABC1234", "1234567890")

    assert outcome.status == CheckStatus.FOUND
    assert outcome.engine == EngineKind.HTTP
    assert len(outcome.violations) == 2
    assert outcome.violations[0].ticket_no == "QC-2026-000123"
    assert outcome.http_status == 200


def test_http_engine_detects_clean_result(app, clean_source, http_mock):
    http_mock.post(CHECK_URL, text=fixture_html("qc_no_violations.html"))
    outcome = build(clean_source).check("XYZ9999", "9876543210")
    assert outcome.status == CheckStatus.CLEAN
    assert outcome.violations == []


def test_http_engine_detects_turnstile_block(app, clean_source, http_mock):
    http_mock.post(CHECK_URL, text=fixture_html("turnstile_challenge.html"), status_code=200)
    outcome = build(clean_source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.BLOCKED
    assert "Cloudflare" in outcome.message


def test_http_403_is_blocked_not_clean(app, clean_source, http_mock):
    http_mock.post(CHECK_URL, text="Access Denied", status_code=403)
    outcome = build(clean_source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.BLOCKED
    assert outcome.http_status == 403


def test_network_error_is_reported(app, clean_source, http_mock):
    http_mock.post(CHECK_URL, exc=requests.exceptions.ConnectTimeout)
    outcome = build(clean_source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.ERROR
    assert "Network error" in outcome.message


def test_unknown_response_is_not_a_false_clean(app, clean_source, http_mock):
    http_mock.post(CHECK_URL, text="<html><body><p>Maintenance window</p></body></html>")
    outcome = build(clean_source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.UNKNOWN
    assert outcome.snippet


def test_json_endpoint_is_supported(app, clean_source, http_mock):
    clean_source.config = {
        "http": {"path": "api/violations/search", "content_type": "json",
                 "plate_key": "plate_number", "mv_key": "mv_file_number"},
    }
    http_mock.post("https://portal.test/api/violations/search",
                   text=fixture_html("api_violations.json"),
                   headers={"Content-Type": "application/json"})
    outcome = build(clean_source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.FOUND
    assert outcome.violations[0].ticket_no == "API-2026-00001"
    assert outcome.violations[0].amount is not None


def test_custom_plate_and_mv_field_names(app, clean_source, http_mock):
    clean_source.config = {"http": {"plate_key": "plateNo", "mv_key": "mvNo"}}
    http_mock.post(CHECK_URL, text=fixture_html("qc_no_violations.html"))
    build(clean_source).check("ABC1234", "1234567890")
    sent = http_mock.last_request.text
    assert "plateNo=ABC1234" in sent and "mvNo=1234567890" in sent


def test_get_method_is_supported(app, clean_source, http_mock):
    clean_source.config = {"http": {"method": "GET"}}
    http_mock.get(CHECK_URL, text=fixture_html("qc_violations.html"))
    outcome = build(clean_source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.FOUND
    assert "plate_number=ABC1234" in (http_mock.last_request.url or "")


def test_column_map_overrides_headers(app, clean_source, http_mock):
    clean_source.config = {"parse": {"column_map": {"Reference Code": "ticket_no"}}}
    html = ("<table><tr><th>Reference Code</th><th>Date</th></tr>"
            "<tr><td>REF-1</td><td>2026-01-02</td></tr></table>")
    http_mock.post(CHECK_URL, text=html)
    outcome = build(clean_source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.FOUND
    assert outcome.violations[0].ticket_no == "REF-1"


def test_container_selectors_are_used_when_configured(app, clean_source, http_mock):
    clean_source.config = {"parse": {"container_selector": ".violation-card"}}
    http_mock.post(CHECK_URL, text=fixture_html("manila_violations.html"))
    outcome = build(clean_source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.FOUND
    assert outcome.violations[0].ticket_no == "MNL-00088421"


def test_missing_plate_short_circuits(app, clean_source):
    outcome = build(clean_source).check("", "1234567890")
    assert outcome.status == CheckStatus.ERROR
    assert "plate" in outcome.message.lower()


# ---------------------------------------------------------------------------
# Engine selection & fallbacks
# ---------------------------------------------------------------------------
def test_auto_uses_http_when_playwright_is_disabled(app, clean_source, http_mock):
    clean_source.engine = EngineKind.AUTO
    http_mock.post(CHECK_URL, text=fixture_html("qc_violations.html"))
    outcome = build(clean_source, playwright_enabled=False).check("ABC1234", "1234567890")
    assert outcome.engine == EngineKind.HTTP
    assert outcome.status == CheckStatus.FOUND


def test_browser_engine_unavailable_is_reported_clearly(app, clean_source, monkeypatch):
    clean_source.engine = EngineKind.BROWSER
    monkeypatch.setattr("app.scrapers.browser_engine.is_playwright_available",
                        lambda: False)
    outcome = build(clean_source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.BLOCKED
    assert "Playwright" in outcome.message


def test_auto_falls_back_to_http_when_the_browser_is_missing(app, clean_source,
                                                             http_mock, monkeypatch):
    clean_source.engine = EngineKind.AUTO
    monkeypatch.setattr("app.scrapers.browser_engine.is_playwright_available",
                        lambda: False)
    http_mock.post(CHECK_URL, text=fixture_html("qc_violations.html"))
    outcome = build(clean_source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.FOUND
    assert outcome.engine == EngineKind.HTTP


def test_adapter_exception_becomes_an_error_outcome(app, clean_source, monkeypatch):
    from app.scrapers import base

    monkeypatch.setattr(base.BaseAdapter, "_http_check",
                        lambda self, *a, **kw: (_ for _ in ()).throw(RuntimeError("kaboom")))
    outcome = build(clean_source).check("ABC1234", "1234567890")
    assert outcome.status == CheckStatus.ERROR
    assert "kaboom" in outcome.message


# ---------------------------------------------------------------------------
# Configuration plumbing
# ---------------------------------------------------------------------------
def test_option_prefers_source_config(app, clean_source):
    clean_source.config = {"browser": {"settle_ms": 999}, "http": {"method": "PUT"}}
    adapter = build(clean_source)
    assert adapter.option("settle_ms", group="browser") == 999
    assert adapter.option("method", group="http") == "PUT"
    assert adapter.option("tab_texts", group="browser") == BaseAdapter.browser_config["tab_texts"]


def test_check_url_and_timeout(app, clean_source):
    adapter = build(clean_source)
    assert adapter.check_url == CHECK_URL
    assert adapter.timeout == 10


def test_registry_maps_every_builtin_source(app, sources):
    from app.scrapers import ADAPTER_CLASSES

    for key in sources:
        assert key in ADAPTER_CLASSES, key
    adapter = get_adapter(sources["quezon_city"], settings={})
    assert adapter.__class__.__name__ == "QuezonCityAdapter"
    assert "quezoncity" in adapter.check_url


def test_platform_adapter_defaults_target_the_right_hosts(app, sources):
    mmda = get_adapter(sources["mmda"], settings={})
    assert mmda.check_url.startswith("https://mayhulika.mmda.gov.ph")
    manila = get_adapter(sources["manila"], settings={})
    assert manila.check_url == "https://www.gomanila.com/nocontact"
    assert manila.option("lookup_label", group="browser") == "Plate Number"


def test_seed_is_idempotent(app):
    from app.scrapers import seed_default_sources

    seed_default_sources()
    before = {s.key for s in __import__("app.models", fromlist=["Source"]).Source.query.all()}
    assert seed_default_sources() == 0
    after = {s.key for s in __import__("app.models", fromlist=["Source"]).Source.query.all()}
    assert before == after


def test_http_response_helpers():
    response = HttpResponse(status_code=500, text="x")
    assert response.ok is False and bool(response) is True
    failed = HttpResponse(status_code=0, error="boom")
    assert failed.ok is False and bool(failed) is False
