"""Adapter base class: engine selection, browser flow and result interpretation."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from ..models import CheckStatus, EngineKind, Source
from ..utils import normalize_mv, normalize_plate, truncate
from . import browser_engine, parsing
from .http_engine import HttpEngine, HttpResponse
from .parsing import ViolationRecord

logger = logging.getLogger(__name__)


@dataclass
class CheckOutcome:
    """Result of checking one vehicle against one portal."""

    status: str = CheckStatus.UNKNOWN
    violations: list[ViolationRecord] = field(default_factory=list)
    http_status: int | None = None
    message: str = ""
    engine: str = ""
    duration_ms: int = 0
    snippet: str = ""
    screenshot: str = ""
    records: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (CheckStatus.CLEAN, CheckStatus.FOUND)

    @property
    def found(self) -> int:
        return len(self.violations)


class BaseAdapter:
    """Common machinery shared by every portal adapter.

    Subclasses normally only tweak the ``*_config`` defaults and override the
    small ``browser_*`` hooks; the engine selection, retry order, captcha
    detection and result interpretation live here so behaviour is consistent
    across portals.
    """

    key: str = "generic"
    name: str = "Generic portal"
    default_engine: str = EngineKind.AUTO
    engine_order: tuple[str, ...] = (EngineKind.BROWSER, EngineKind.HTTP)

    #: Default HTTP request config. Overridable per source from the admin panel.
    http_config: dict = {
        "method": "POST",
        "path": "",
        "content_type": "form",           # form | json
        "plate_key": "plate_number",
        "mv_key": "mv_file_number",
        "extra_fields": {},
        "token_field": "cf-turnstile-response",
        "records_path": "",
        "headers": {},
    }

    #: Default browser flow config. Overridable per source from the admin panel.
    browser_config: dict = {
        "agree_texts": ("I Agree & Continue", "I Agree", "Agree", "Accept",
                        "Accept All", "Got it", "Continue"),
        "agree_selectors": ("button#agree", ".privacy-modal button", "#consent-accept"),
        "tab_texts": ("Plate + MV File No.", "Plate + MV File Number"),
        "tab_selectors": (),
        "plate_selectors": ("input[placeholder*='Plate' i]", "input#plate",
                            "input[name*='plate' i]"),
        "mv_selectors": ("input[placeholder*='MV' i]", "input#mv_file_number",
                         "input[name*='mv' i]"),
        "plate_keywords": ("plate",),
        "mv_keywords": ("mv file", "mv", "file number", "mv_number"),
        "submit_texts": ("Search", "Check", "Submit", "Inquire", "Look Up"),
        "submit_selectors": ("button[type='submit']", "form button", "#search-btn"),
        "results_selector": "",
        "empty_selector": "",
        "captcha_timeout_ms": 25000,
        "settle_ms": 1500,
        "wait_until": "domcontentloaded",
    }

    def __init__(self, source: Source, settings: dict | None = None,
                 http_engine: HttpEngine | None = None, runner=None):
        self.source = source
        self.settings = settings or {}
        self.config = dict(source.config or {})
        self._http_engine = http_engine
        self._runner = runner

    # ------------------------------------------------------------------
    # Configuration accessors
    # ------------------------------------------------------------------
    def option(self, name, default=None, group: str | None = None):
        """Read a source config value, falling back to the class default."""
        if group:
            group_cfg = dict(self.config.get(group) or {})
            if name in group_cfg and group_cfg[name] not in (None, ""):
                return group_cfg[name]
            base = getattr(self, f"{group}_config", {}) or {}
            return base.get(name, default)
        if name in self.config and self.config[name] not in (None, ""):
            return self.config[name]
        return default

    @property
    def check_url(self) -> str:
        return self.option("check_url") or self.source.check_url

    @property
    def base_url(self) -> str:
        return self.option("base_url") or self.source.base_url

    @property
    def timeout(self) -> int:
        return int(self.option("timeout", self.source.timeout_seconds or 30))

    @property
    def http_engine(self) -> HttpEngine:
        if self._http_engine is None:
            self._http_engine = HttpEngine(
                timeout=self.timeout,
                user_agent=self.settings.get("user_agent") or None,
                proxy=self.settings.get("proxy_url") or None,
                retries=int(self.settings.get("http_retries", 2) or 0),
            )
        return self._http_engine

    @property
    def runner(self):
        """A :class:`BrowserRunner` built from global settings (lazy)."""
        if self._runner is None:
            from .browser_engine import BrowserRunner
            from .. import config as app_config

            data_dir = Path(app_config.DATA_DIR)
            try:
                from flask import current_app, has_app_context

                if has_app_context():
                    data_dir = Path(current_app.config.get("DATA_DIR", data_dir))
            except Exception:  # pragma: no cover - running outside Flask
                pass
            self._runner = BrowserRunner(
                headless=bool(self.settings.get("playwright_headless", True)),
                timeout_ms=int(self.settings.get("playwright_timeout_ms", 45000)),
                storage_dir=data_dir / "browser-profiles",
                screenshot_dir=data_dir / "screenshots",
                user_agent=self.settings.get("user_agent") or None,
                executable_path=self.settings.get("playwright_executable") or None,
                proxy=self.settings.get("proxy_url") or None,
            )
        return self._runner

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def check(self, plate: str, mv_number: str, engine: str | None = None,
              capture_screenshot: bool = False) -> CheckOutcome:
        """Check one vehicle. Returns a :class:`CheckOutcome`, never raises."""
        started = time.monotonic()
        plate_norm = normalize_plate(plate)
        mv_norm = normalize_mv(mv_number)
        if not plate_norm:
            return CheckOutcome(status=CheckStatus.ERROR,
                                message="Missing plate number", engine=engine or "")

        outcomes: list[CheckOutcome] = []
        for engine_name in self._engines(engine):
            try:
                if engine_name == EngineKind.HTTP:
                    outcome = self._http_check(plate_norm, mv_norm, capture_screenshot)
                else:
                    outcome = self._browser_check(plate_norm, mv_norm, capture_screenshot)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("[%s] %s engine crashed: %s", self.key, engine_name, exc)
                outcome = CheckOutcome(status=CheckStatus.ERROR,
                                       message=f"{engine_name} engine error: {exc}"[:300],
                                       engine=engine_name)
            outcome.engine = engine_name
            outcomes.append(outcome)
            if outcome.ok:
                break

        result = self._best_outcome(outcomes)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        if not result.violations:
            result.violations = []
        for violation in result.violations:
            violation.plate = violation.plate or plate_norm
        return result

    def _engines(self, override: str | None) -> list[str]:
        requested = (override or self.source.engine or self.default_engine or EngineKind.AUTO)
        requested = str(requested).lower()
        if requested == EngineKind.HTTP:
            return [EngineKind.HTTP]
        if requested == EngineKind.BROWSER:
            return [EngineKind.BROWSER]
        if requested == EngineKind.AUTO and not self.settings.get("playwright_enabled", True):
            return [EngineKind.HTTP]
        return [name for name in self.engine_order
                if name in (EngineKind.HTTP, EngineKind.BROWSER)] or [EngineKind.HTTP]

    @staticmethod
    def _best_outcome(outcomes: list[CheckOutcome]) -> CheckOutcome:
        if not outcomes:
            return CheckOutcome(status=CheckStatus.ERROR, message="No engine produced a result")
        for outcome in outcomes:
            if outcome.ok:
                return outcome
        # Prefer the most informative failure: blocked > error > unknown
        priority = {CheckStatus.BLOCKED: 0, CheckStatus.ERROR: 1, CheckStatus.UNKNOWN: 2,
                    CheckStatus.SKIPPED: 3}
        return sorted(outcomes, key=lambda o: priority.get(o.status, 9))[0]

    # ------------------------------------------------------------------
    # HTTP engine
    # ------------------------------------------------------------------
    def _http_url(self) -> str:
        path = self.option("path", "", group="http")
        if not path:
            return self.check_url
        if path.startswith("http"):
            return path
        return urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/"))

    def _http_payload(self, plate: str, mv: str) -> dict:
        plate_key = self.option("plate_key", "plate_number", group="http")
        mv_key = self.option("mv_key", "mv_file_number", group="http")
        payload = {plate_key: plate, mv_key: mv}
        extra = self.option("extra_fields", {}, group="http") or {}
        if isinstance(extra, dict):
            payload.update({k: v for k, v in extra.items() if v is not None})
        token = self.option("token", "", group="http")
        if token:
            payload[self.option("token_field", "cf-turnstile-response", group="http")] = token
        return payload

    def _http_headers(self) -> dict:
        headers = dict(self.option("headers", {}, group="http") or {})
        headers.setdefault("Referer", self.check_url)
        headers.setdefault("X-Requested-With", "XMLHttpRequest")
        return headers

    def _http_check(self, plate: str, mv: str, capture_screenshot: bool = False) -> CheckOutcome:
        method = str(self.option("method", "POST", group="http")).upper()
        url = self._http_url()
        payload = self._http_payload(plate, mv)
        headers = self._http_headers()
        as_json = str(self.option("content_type", "form", group="http")).lower() == "json"

        response: HttpResponse
        if method == "GET":
            response = self.http_engine.get(url, params=payload, headers=headers,
                                            timeout=self.timeout)
        elif as_json:
            response = self.http_engine.post(url, json_body=payload, headers=headers,
                                             timeout=self.timeout)
        else:
            response = self.http_engine.post(url, data=payload, headers=headers,
                                             timeout=self.timeout)
        return self._interpret_response(response, plate, EngineKind.HTTP)

    # ------------------------------------------------------------------
    # Browser engine
    # ------------------------------------------------------------------
    def _browser_check(self, plate: str, mv: str,
                       capture_screenshot: bool = False) -> CheckOutcome:
        runner = self.runner
        if not runner.available:
            return CheckOutcome(
                status=CheckStatus.BLOCKED,
                engine=EngineKind.BROWSER,
                message=("Browser engine unavailable - install Playwright "
                         "(`pip install playwright && playwright install chromium`) "
                         "or switch this source to the HTTP engine."),
            )
        wait_until = self.option("wait_until", "domcontentloaded", group="browser")
        shot_name = f"{self.source.key}-{int(time.time())}" if capture_screenshot else None

        def action(page) -> str:
            try:
                page.goto(self.check_url, wait_until=wait_until,
                          timeout=int(self.option("browser_timeout_ms",
                                                  self.timeout * 1000, group="browser")))
            except Exception as exc:
                raise RuntimeError(f"Navigation failed: {exc}") from exc
            self.browser_prepare(page)
            self._browser_wait_ready(page)
            self.browser_fill(page, plate, mv)
            self.browser_submit(page)
            self.browser_wait_results(page)
            try:
                return page.content() or ""
            except Exception:  # pragma: no cover
                return ""

        try:
            html, shot = browser_engine.run_flow(runner, self.source.key, action, shot_name)
        except Exception as exc:
            return CheckOutcome(status=CheckStatus.ERROR, engine=EngineKind.BROWSER,
                                message=truncate(str(exc), 300))

        outcome = self._interpret_html(html, plate, EngineKind.BROWSER)
        outcome.screenshot = shot
        return outcome

    def _browser_wait_ready(self, page) -> None:
        """Wait until the form is interactive (CAPTCHA widget solved/disabled)."""
        timeout_ms = int(self.option("captcha_timeout_ms", 25000, group="browser"))
        browser_engine.wait_settled(page, wait_ms=600)
        # Consent/agree buttons sometimes reveal the form; try again after a beat.
        self.browser_prepare(page)
        browser_engine.wait_settled(page, wait_ms=timeout_ms // 6 or 400)

    # -- browser hooks (overridden per portal) --------------------------
    def browser_prepare(self, page) -> None:
        """Dismiss consent overlays and pick the right search tab/mode."""
        browser_engine.dismiss_overlays(
            page,
            texts=tuple(self.option("agree_texts", (), group="browser") or ()),
            selectors=tuple(self.option("agree_selectors", (), group="browser") or ()),
        )
        browser_engine.click_first(
            page,
            tuple(self.option("tab_selectors", (), group="browser") or ()),
            tuple(self.option("tab_texts", (), group="browser") or ()),
        )

    def browser_fill(self, page, plate: str, mv: str) -> None:
        """Fill the plate / MV inputs using configured selectors then heuristics."""
        if plate:
            if not browser_engine.fill_by_selector(
                    page, plate, self.option("plate_selectors", (), group="browser")):
                browser_engine.smart_fill(
                    page, plate,
                    self.option("plate_keywords", ("plate",), group="browser"))
        if mv:
            if not browser_engine.fill_by_selector(
                    page, mv, self.option("mv_selectors", (), group="browser")):
                browser_engine.smart_fill(
                    page, mv,
                    self.option("mv_keywords", ("mv", "file"), group="browser"))

    def browser_submit(self, page) -> None:
        browser_engine.click_first(
            page,
            tuple(self.option("submit_selectors", (), group="browser") or ()),
            tuple(self.option("submit_texts", ("Search", "Submit"), group="browser") or ()),
        )

    def browser_wait_results(self, page) -> None:
        browser_engine.wait_settled(
            page,
            wait_ms=int(self.option("settle_ms", 1500, group="browser")),
            selector=self.option("results_selector", "", group="browser") or None,
            timeout=int(self.option("browser_timeout_ms", self.timeout * 1000, group="browser")),
        )

    # ------------------------------------------------------------------
    # Interpretation
    # ------------------------------------------------------------------
    def _interpret_response(self, response: HttpResponse, plate: str,
                            engine: str) -> CheckOutcome:
        if response.error:
            return CheckOutcome(status=CheckStatus.ERROR, engine=engine,
                                http_status=response.status_code or None,
                                message=truncate(f"Network error: {response.error}", 300))
        if response.status_code >= 400:
            block = parsing.detect_block(response.text, response.status_code)
            return CheckOutcome(
                status=CheckStatus.BLOCKED if block else CheckStatus.ERROR,
                engine=engine, http_status=response.status_code,
                message=block or f"HTTP {response.status_code} from portal",
                snippet=truncate(response.text, 400),
            )
        return self._interpret_html_or_json(response.text, response.content_type, plate,
                                            engine, http_status=response.status_code)

    def _interpret_html(self, html: str, plate: str, engine: str) -> CheckOutcome:
        return self._interpret_html_or_json(html, "text/html", plate, engine)

    def _interpret_html_or_json(self, body: str, content_type: str, plate: str,
                                engine: str, http_status: int | None = None) -> CheckOutcome:
        parse_config = {
            "records_path": self.option("records_path", "", group="http"),
            "column_map": self.option("column_map", {}, group="parse") or {},
            "table_selector": self.option("table_selector", "", group="parse") or None,
            "container_selector": self.option("container_selector", "", group="parse") or None,
            "field_selectors": self.option("field_selectors", {}, group="parse") or None,
        }
        records = parsing.records_from_response(body, content_type, parse_config)
        violations = parsing.normalize_records(records, self.source.key, plate)
        block_reason = parsing.detect_block(body, http_status)

        if violations:
            return CheckOutcome(status=CheckStatus.FOUND, violations=violations,
                                engine=engine, http_status=http_status,
                                message=f"{len(violations)} violation(s) returned",
                                records=records)

        empty_selector = self.option("empty_selector", "", group="browser")
        if empty_selector and self._selector_present(body, empty_selector):
            return CheckOutcome(status=CheckStatus.CLEAN, engine=engine,
                                http_status=http_status,
                                message="Portal reported no violations")

        if parsing.detect_empty_result(body):
            return CheckOutcome(status=CheckStatus.CLEAN, engine=engine,
                                http_status=http_status,
                                message="Portal reported no violations")

        if block_reason:
            return CheckOutcome(status=CheckStatus.BLOCKED, engine=engine,
                                http_status=http_status, message=block_reason,
                                snippet=truncate(body, 400))

        if records:
            return CheckOutcome(
                status=CheckStatus.UNKNOWN, engine=engine, http_status=http_status,
                message="Portal returned rows that could not be understood",
                snippet=truncate(body, 400), records=records)

        return CheckOutcome(
            status=CheckStatus.UNKNOWN, engine=engine, http_status=http_status,
            message=("Portal response did not contain a violations table or a "
                     "no-violation notice - check Source settings"),
            snippet=truncate(body, 400))

    @staticmethod
    def _selector_present(html: str, selector: str) -> bool:
        if not html or not selector:
            return False
        try:
            from bs4 import BeautifulSoup
            return bool(BeautifulSoup(html, "lxml").select(selector))
        except Exception:  # pragma: no cover
            return False
