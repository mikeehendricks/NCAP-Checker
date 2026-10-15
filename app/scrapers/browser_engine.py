"""Playwright driven browser engine.

Only used when a portal needs JavaScript (all five NCAP sites do: the shared
"nocontact" platform renders with Vite and gates the form behind Cloudflare
Turnstile, Manila uses reCAPTCHA, and MMDA blocks non-browser clients).

The engine is optional at runtime: if Playwright (or the Chromium download) is
missing, :meth:`BrowserRunner.available` returns ``False`` and the adapters
report ``blocked`` instead of crashing the scheduler.
"""
from __future__ import annotations

import importlib
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

logger = logging.getLogger(__name__)


def is_playwright_available() -> bool:
    """True when the ``playwright`` package can be imported."""
    try:
        importlib.import_module("playwright.sync_api")
        return True
    except Exception:  # pragma: no cover - depends on environment
        return False


class BrowserUnavailable(RuntimeError):
    """Raised when a browser run is requested but Playwright is missing."""


class BrowserRunner:
    """Launches Chromium, keeps cookies per source, and captures screenshots."""

    def __init__(self, headless: bool = True, timeout_ms: int = 45000,
                 storage_dir: Path | str | None = None, user_agent: str | None = None,
                 executable_path: str | None = None, proxy: str | None = None,
                 navigation_timeout_ms: int = 45000, screenshot_dir: Path | str | None = None):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.navigation_timeout_ms = navigation_timeout_ms
        self.storage_dir = Path(storage_dir) if storage_dir else None
        self.user_agent = user_agent
        self.executable_path = executable_path or None
        self.proxy = proxy or None
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        if self.storage_dir:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        if self.screenshot_dir:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)

    @property
    def available(self) -> bool:
        return is_playwright_available()

    def storage_path(self, key: str) -> Path | None:
        if not self.storage_dir:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key or "default")
        return self.storage_dir / f"{safe}.json"

    @contextmanager
    def session(self, storage_key: str = "default") -> Iterator:
        """Yield a ready :class:`playwright.sync_api.Page` and persist cookies."""
        if not self.available:
            raise BrowserUnavailable(
                "Playwright is not installed. Run: pip install playwright && "
                "playwright install chromium"
            )
        from playwright.sync_api import sync_playwright

        state_file = self.storage_path(storage_key)
        playwright = sync_playwright().start()
        browser = None
        try:
            launch_kwargs = {
                "headless": self.headless,
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu" if self.headless else "--start-maximized",
                ],
            }
            if self.executable_path:
                launch_kwargs["executable_path"] = self.executable_path
            if self.proxy:
                launch_kwargs["proxy"] = {"server": self.proxy}
            browser = playwright.chromium.launch(**launch_kwargs)
            context_kwargs = {
                "viewport": {"width": 1366, "height": 900},
                "ignore_https_errors": True,
                "locale": "en-PH",
                "timezone_id": "Asia/Manila",
            }
            if self.user_agent:
                context_kwargs["user_agent"] = self.user_agent
            if state_file and state_file.is_file():
                try:
                    context_kwargs["storage_state"] = str(state_file)
                except Exception:  # pragma: no cover - corrupt state file
                    state_file.unlink(missing_ok=True)
            context = browser.new_context(**context_kwargs)
            context.set_default_timeout(self.timeout_ms)
            page = context.new_page()
            try:
                yield page
            finally:
                if state_file:
                    try:
                        context.storage_state(path=str(state_file))
                    except Exception as exc:  # pragma: no cover
                        logger.debug("Could not persist browser state: %s", exc)
                context.close()
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # pragma: no cover
                    pass
            try:
                playwright.stop()
            except Exception:  # pragma: no cover
                pass

    def screenshot(self, page, name: str, full_page: bool = True) -> str:
        """Save a screenshot for the admin probe view; returns the file name."""
        if not self.screenshot_dir:
            return ""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        path = self.screenshot_dir / f"{safe}.png"
        try:
            page.screenshot(path=str(path), full_page=full_page)
            return path.name
        except Exception as exc:  # pragma: no cover - page may be closing
            logger.debug("Screenshot failed: %s", exc)
            return ""


# ---------------------------------------------------------------------------
# Interaction helpers (shared by the adapters)
# ---------------------------------------------------------------------------
def first_visible(page, selectors, timeout: int = 5000):
    """Return the first locator that exists and is visible."""
    for selector in selectors or []:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible(timeout=timeout):
                return locator
        except Exception:
            continue
    return None


def click_first(page, selectors, texts=(), timeout: int = 5000) -> str | None:
    """Click the first matching selector or visible text; returns what matched."""
    for selector in selectors or []:
        try:
            locator = page.locator(selector).first
            if locator.count():
                locator.click(timeout=timeout)
                return f"selector:{selector}"
        except Exception:
            continue
    for text in texts or []:
        try:
            locator = page.get_by_text(text, exact=False).first
            if locator.count():
                locator.click(timeout=timeout)
                return f"text:{text}"
        except Exception:
            continue
    return None


def dismiss_overlays(page, texts: tuple[str, ...] = (), selectors: tuple[str, ...] = (),
                     timeout: int = 4000) -> str | None:
    """Click consent / privacy / cookie banners that block the form."""
    return click_first(page, selectors, texts, timeout=timeout)


def smart_fill(page, value: str, keywords, negative=(), timeout: int = 5000) -> str | None:
    """Fill the first input whose name/id/placeholder/label matches a keyword.

    Returns the selector-ish description of what was filled, or ``None``.
    """
    if not value:
        return None
    candidates = []
    for field_name in ("input", "textarea"):
        try:
            candidates.extend(page.locator(f"{field_name}:visible").all())
        except Exception:
            continue
    for locator in candidates:
        try:
            if not locator.is_visible(timeout=timeout):
                continue
            descriptor = " ".join([
                locator.get_attribute("name") or "",
                locator.get_attribute("id") or "",
                locator.get_attribute("placeholder") or "",
                locator.get_attribute("aria-label") or "",
                locator.get_attribute("type") or "",
            ]).lower()
            if any(neg.lower() in descriptor for neg in negative or ()):
                continue
            if any(str(keyword).lower() in descriptor for keyword in keywords or ()):
                locator.fill(str(value), timeout=timeout)
                return descriptor.strip()[:80] or "input"
        except Exception:
            continue
    return None


def select_option(page, value: str | None = None, label: str | None = None,
                  selectors=(), timeout: int = 5000) -> str | None:
    """Pick an option in a <select> by value or by visible label."""
    for selector in selectors or ("select",):
        try:
            locator = page.locator(selector).first
            if not locator.count():
                continue
            if value:
                locator.select_option(value=value, timeout=timeout)
                return f"{selector}={value}"
            if label:
                try:
                    locator.select_option(label=label, timeout=timeout)
                    return f"{selector}:{label}"
                except Exception:
                    locator.select_option(index=None, label=label, timeout=timeout)
                    return f"{selector}:{label}"
        except Exception:
            continue
    return None


def fill_by_selector(page, value: str, selectors, timeout: int = 5000) -> str | None:
    """Fill a field using explicit (admin-configurable) selectors first."""
    locator = first_visible(page, selectors, timeout=timeout)
    if locator is None:
        return None
    try:
        locator.fill(str(value), timeout=timeout)
        return "configured-selector"
    except Exception:
        return None


def wait_settled(page, wait_ms: int = 1200, selector: str | None = None,
                 timeout: int = 15000) -> None:
    """Give a JS app time to render; optionally wait for a results node."""
    if selector:
        try:
            page.wait_for_selector(selector, timeout=timeout)
        except Exception:
            pass
    if wait_ms:
        try:
            page.wait_for_timeout(wait_ms)
        except Exception:  # pragma: no cover
            pass


def page_snapshot(page) -> tuple[str, int | None]:
    """Return ``(html, status_code)`` for the current page."""
    html = ""
    try:
        html = page.content() or ""
    except Exception:  # pragma: no cover
        html = ""
    return html, None


def run_flow(runner: BrowserRunner, storage_key: str,
             action: Callable, screenshot_name: str | None = None) -> tuple[str, str]:
    """Open a browser session, run ``action(page)`` and return ``(html, shot)``."""
    with runner.session(storage_key) as page:
        html = action(page) or ""
        if not html:
            try:
                html = page.content() or ""
            except Exception:  # pragma: no cover
                html = ""
        shot = runner.screenshot(page, screenshot_name or storage_key) if screenshot_name else ""
        return html, shot
