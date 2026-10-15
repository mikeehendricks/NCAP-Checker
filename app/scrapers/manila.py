"""Adapter for the City of Manila portal (Go!Manila - /nocontact).

The Manila form asks for a plate number plus a "Look Up By" mode and is
guarded by Google reCAPTCHA, so the browser engine runs first.
"""
from __future__ import annotations

from . import browser_engine
from .base import BaseAdapter
from ..models import EngineKind


class ManilaAdapter(BaseAdapter):
    key = "manila"
    name = "Manila NCAP (Go!Manila)"
    default_engine = EngineKind.AUTO
    engine_order = (EngineKind.BROWSER, EngineKind.HTTP)

    http_config = dict(BaseAdapter.http_config, **{
        "path": "",
        "content_type": "form",
        "plate_key": "plate_number",
        "mv_key": "mv_file_number",
        "extra_fields": {"lookup_by": "plate"},
    })

    browser_config = dict(BaseAdapter.browser_config, **{
        "agree_texts": ("I Agree", "Agree", "Accept", "OK"),
        "lookup_selectors": ("select", "select#lookup_by", "select[name*='lookup' i]"),
        "lookup_value": "",                 # e.g. "plate" / "mv" - set per source
        "lookup_label": "",                 # e.g. "Plate Number"
        "plate_keywords": ("plate",),
        "mv_keywords": ("mv file", "mv", "file number"),
        "submit_texts": ("Inquire", "Search", "Submit", "Look Up"),
        "submit_selectors": ("button[type='submit']", "form button", "#inquire"),
        "settle_ms": 2500,
        "captcha_timeout_ms": 30000,
    })

    def browser_prepare(self, page) -> None:
        super().browser_prepare(page)
        value = self.option("lookup_value", "", group="browser")
        label = self.option("lookup_label", "", group="browser")
        selectors = self.option("lookup_selectors", (), group="browser")
        if value or label:
            browser_engine.select_option(page, value=value or None, label=label or None,
                                         selectors=tuple(selectors or ()))
