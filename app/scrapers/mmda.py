"""Adapter for MMDA "May Huli Ka 2.0" (mayhulika.mmda.gov.ph).

May Huli Ka accepts a plate number *or* conduction sticker number together
with the MV file number, then renders results after pressing CHECK.  The site
sits behind a WAF that rejects non-browser clients (plain HTTP requests get a
403), so the browser engine runs first.
"""
from __future__ import annotations

from .base import BaseAdapter
from ..models import EngineKind


class MmdaAdapter(BaseAdapter):
    key = "mmda"
    name = "MMDA May Huli Ka"
    default_engine = EngineKind.AUTO
    engine_order = (EngineKind.BROWSER, EngineKind.HTTP)

    http_config = dict(BaseAdapter.http_config, **{
        "path": "",
        "content_type": "form",
        "plate_key": "plate_number",
        "mv_key": "mv_file_number",
    })

    browser_config = dict(BaseAdapter.browser_config, **{
        "agree_texts": ("I Agree", "Agree", "Accept", "Okay", "OK"),
        "plate_keywords": ("plate", "conduction"),
        "mv_keywords": ("mv file", "mv_file", "mv", "file number"),
        "submit_texts": ("Check", "CHECK", "Search", "Submit", "Mag-check"),
        "submit_selectors": ("button[type='submit']", "form button", "#check"),
        "settle_ms": 2500,
        "captcha_timeout_ms": 30000,
        "wait_until": "domcontentloaded",
    })
