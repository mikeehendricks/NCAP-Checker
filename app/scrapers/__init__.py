"""Portal adapter registry.

Sources are rows in the ``sources`` table (editable from the admin panel);
this module maps a source ``key`` onto the adapter class that knows how to
drive that portal, and seeds the five built-in portals on first run.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..extensions import db
from ..models import Source
from .base import BaseAdapter
from .manila import ManilaAdapter
from .mmda import MmdaAdapter
from .platform_nocontact import ParanaqueAdapter, QuezonCityAdapter, SanJuanAdapter

logger = logging.getLogger(__name__)

ADAPTER_CLASSES: dict[str, type[BaseAdapter]] = {
    "quezon_city": QuezonCityAdapter,
    "paranaque": ParanaqueAdapter,
    "san_juan": SanJuanAdapter,
    "manila": ManilaAdapter,
    "mmda": MmdaAdapter,
}

#: Built-in portals seeded into the database on first run.
DEFAULT_SOURCES: list[dict] = [
    {
        "key": "quezon_city",
        "name": "Quezon City NCAP",
        "agency": "Quezon City LGU",
        "base_url": "https://nocontact.quezoncity.gov.ph",
        "check_url": "https://nocontact.quezoncity.gov.ph/check-violations",
        "engine": "auto",
        "delay_seconds": 4,
        "config": {},
    },
    {
        "key": "manila",
        "name": "Manila NCAP (Go!Manila)",
        "agency": "City of Manila",
        "base_url": "https://www.gomanila.com",
        "check_url": "https://www.gomanila.com/nocontact",
        "engine": "auto",
        "delay_seconds": 4,
        "config": {"browser": {"lookup_label": "Plate Number"}},
    },
    {
        "key": "paranaque",
        "name": "Parañaque City NCAP",
        "agency": "Parañaque City LGU",
        "base_url": "https://paranaquecity.nocontact.ph",
        "check_url": "https://paranaquecity.nocontact.ph/check-violations",
        "engine": "auto",
        "delay_seconds": 4,
        "config": {},
    },
    {
        "key": "san_juan",
        "name": "San Juan City NCAP",
        "agency": "San Juan City LGU",
        "base_url": "https://nocontact.sanjuancity.gov.ph",
        "check_url": "https://nocontact.sanjuancity.gov.ph/check-violations",
        "engine": "auto",
        "delay_seconds": 4,
        "config": {},
    },
    {
        "key": "mmda",
        "name": "MMDA May Huli Ka",
        "agency": "Metropolitan Manila Development Authority",
        "base_url": "https://mayhulika.mmda.gov.ph",
        "check_url": "https://mayhulika.mmda.gov.ph/",
        "engine": "auto",
        "delay_seconds": 5,
        "config": {},
    },
]


def get_adapter(source: Source, settings: dict | None = None, **kwargs) -> BaseAdapter:
    """Instantiate the adapter bound to ``source``."""
    adapter_cls = ADAPTER_CLASSES.get(source.key, BaseAdapter)
    return adapter_cls(source=source, settings=settings or {}, **kwargs)


def seed_default_sources(update_existing: bool = False) -> int:
    """Insert the built-in portals. Returns the number of rows created."""
    created = 0
    for spec in DEFAULT_SOURCES:
        source = Source.query.filter_by(key=spec["key"]).first()
        if source is None:
            source = Source(**{k: v for k, v in spec.items()
                               if k in {"key", "name", "agency", "base_url", "check_url",
                                        "engine", "delay_seconds", "config"}})
            db.session.add(source)
            created += 1
        elif update_existing:
            source.name = spec["name"]
            source.agency = spec["agency"]
            source.base_url = spec["base_url"]
            source.check_url = spec["check_url"]
    if created:
        db.session.commit()
        logger.info("Seeded %s default NCAP sources", created)
    return created


def enabled_sources() -> list[Source]:
    return (Source.query.filter_by(enabled=True)
            .order_by(Source.name.asc()).all())


def get_source_by_key(key: str) -> Optional[Source]:
    return Source.query.filter_by(key=key).first()
