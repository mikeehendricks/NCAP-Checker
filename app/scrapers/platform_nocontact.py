"""Adapter for the shared "No Contact Apprehension" platform.

Used by Quezon City, Parañaque City and San Juan City, which all run the same
Vite front-end:

* a privacy gate ("I Agree & Continue") that must be accepted first,
* two search modes ("Plate + MV File No." / "Plate + NOV No."),
* a Cloudflare Turnstile widget guarding the form,
* results rendered client-side after submitting.

Because the form is Turnstile-protected, the browser engine is tried first;
the HTTP engine remains available for admins who point it at a working JSON
endpoint (configurable per source in the admin panel).
"""
from __future__ import annotations

from ..models import EngineKind
from .base import BaseAdapter


class NoContactPlatformAdapter(BaseAdapter):
    """Generic driver for the shared nocontact.* / *.nocontact.ph platform."""

    default_engine = EngineKind.AUTO
    engine_order = (EngineKind.BROWSER, EngineKind.HTTP)

    http_config = dict(BaseAdapter.http_config, **{
        # Leave the path empty by default: POST straight to the check page.
        # If the portal exposes a JSON API, set `http.path` in the source
        # settings (e.g. "api/violations/search") and `http.content_type=json`.
        "path": "",
        "content_type": "form",
        "plate_key": "plate_number",
        "mv_key": "mv_file_number",
        "search_type": "plate_mv",
    })

    browser_config = dict(BaseAdapter.browser_config, **{
        "agree_texts": ("I Agree & Continue", "I Agree", "Agree & Continue", "Agree",
                        "Accept"),
        "agree_selectors": ("button#agree-continue", ".privacy button.primary",
                            "#privacy-agree"),
        "tab_texts": ("Plate + MV File No.", "Plate + MV File Number",
                      "Plate + MV File No"),
        "tab_selectors": ("[role='tab']", "button.tab", ".MuiTab-root"),
        "plate_keywords": ("plate",),
        "mv_keywords": ("mv file", "mv_file", "mv", "file number"),
        "submit_texts": ("Search", "Submit", "Check", "Maghanap"),
        "submit_selectors": ("button[type='submit']", "form button.primary", "#search"),
        "settle_ms": 2500,
        "captcha_timeout_ms": 30000,
        "wait_until": "domcontentloaded",
    })


class QuezonCityAdapter(NoContactPlatformAdapter):
    key = "quezon_city"
    name = "Quezon City NCAP"


class ParanaqueAdapter(NoContactPlatformAdapter):
    key = "paranaque"
    name = "Parañaque City NCAP"


class SanJuanAdapter(NoContactPlatformAdapter):
    key = "san_juan"
    name = "San Juan City NCAP"
