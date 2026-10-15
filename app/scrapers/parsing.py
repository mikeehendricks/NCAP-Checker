"""HTML / JSON extraction helpers.

The NCAP portals ship three different front-ends (a shared Vite app for
Quezon City / Parañaque / San Juan, the Go!Manila portal and MMDA's May Huli
Ka).  Rather than hard-coding one brittle selector per site, extraction is
driven by a synonym table plus an optional per-source override configured in
the admin panel.  Every helper is pure and side-effect free so it can be
unit-tested against recorded fixtures.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Any, Iterable

from bs4 import BeautifulSoup

from ..utils import parse_amount, parse_date, parse_time

# ---------------------------------------------------------------------------
# Field vocabulary
# ---------------------------------------------------------------------------
FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "ticket_no": (
        "ticket no", "ticket number", "ticket #", "ticket", "citation no",
        "citation number", "transaction no", "transaction number", "reference no",
        "reference number", "reference", "or no", "tvr no", "tvr",
    ),
    "nov_no": (
        "nov no", "nov number", "nov #", "nov", "notice of violation no",
        "notice of violation number", "notice no", "notice number",
        "violation no", "violation number", "apprehension no",
    ),
    "plate": (
        "plate no", "plate number", "plate #", "plate", "license plate",
        "conduction sticker", "conduction no", "conduction sticker no",
        "vehicle plate",
    ),
    "violation_date": (
        "violation date", "date of violation", "date", "apprehension date",
        "date & time", "date and time", "datetime", "date/time", "date issued",
    ),
    "violation_time": ("time", "violation time", "time of violation", "hour"),
    "location": (
        "location", "place", "street", "where", "area", "intersection",
        "place of violation", "violation location", "venue", "road",
    ),
    "offense": (
        "violation", "offense", "violation type", "type of violation",
        "nature of violation", "description", "particulars", "offence",
        "violation description", "code", "violation code",
    ),
    "amount": (
        "amount", "fine", "penalty", "total", "amount due", "total amount",
        "fee", "penalty amount", "fine amount", "settlement amount",
    ),
    "status": ("status", "state", "payment status", "remarks", "flag"),
    "due_date": ("due date", "deadline", "payment deadline", "settle before"),
}

EMPTY_MARKERS = (
    "no violation", "no violations", "no record", "no records", "no result",
    "no results", "no data", "no notices", "no outstanding", "no apprehension",
    "no traffic violation", "walang nahanap", "walang nalabag", "no matching",
    "no entry found", "nothing found", "no pending violation", "clean record",
    "no violation found", "no violations found", "no record found",
    "no records found", "no result found", "no results found",
)

BLOCK_MARKERS = (
    "cf-turnstile",
    "cf_chl_opt",
    "checking your browser",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "just a moment",
    "verify you are human",
    "verify you are not a robot",
    "g-recaptcha",
    "recaptcha",
    "hcaptcha",
    "unusual traffic from your",
    "access denied",
    "request blocked",
    "ddos protection by",
    "please enable cookies",
    "are you a robot",
    "captcha",
)

RECORD_KEYS_HINT = (
    "plate", "violation", "ticket", "nov", "amount", "fine", "location", "date",
)


@dataclass
class ViolationRecord:
    """One normalised violation as understood by the checker service."""

    ticket_no: str = ""
    nov_no: str = ""
    plate: str = ""
    violation_date: Any = None
    violation_time: str | None = None
    location: str = ""
    offense: str = ""
    amount: Decimal | None = None
    currency: str = "PHP"
    status: str = ""
    due_date: Any = None
    raw: dict | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.ticket_no = (self.ticket_no or "").strip()
        self.nov_no = (self.nov_no or "").strip()
        self.plate = (self.plate or "").strip().upper()
        self.location = (self.location or "").strip()
        self.offense = (self.offense or "").strip()
        self.status = (self.status or "").strip()
        self.violation_date = parse_date(self.violation_date)
        self.violation_time = parse_time(self.violation_time)
        self.due_date = parse_date(self.due_date)
        if self.amount is not None and not isinstance(self.amount, Decimal):
            self.amount = parse_amount(self.amount)

    @property
    def reference(self) -> str:
        return self.ticket_no or self.nov_no or "(no reference)"

    def is_meaningful(self) -> bool:
        """Guard against parsing a stray table row into a phantom violation."""
        return bool(
            self.ticket_no
            or self.nov_no
            or self.offense
            or self.violation_date
            or (self.amount is not None and self.amount > 0)
        )

    def fingerprint(self, source_key: str) -> str:
        from ..utils import fingerprint

        return fingerprint(source_key, self.ticket_no, self.nov_no,
                           self.violation_date, self.offense, self.location)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["violation_date"] = self.violation_date.isoformat() if self.violation_date else None
        data["due_date"] = self.due_date.isoformat() if self.due_date else None
        data["amount"] = float(self.amount) if self.amount is not None else None
        return data


# ---------------------------------------------------------------------------
# Header matching
# ---------------------------------------------------------------------------
def canonical_field(label: str, extra_map: dict[str, str] | None = None) -> str | None:
    """Map an arbitrary column header / JSON key onto a canonical field name."""
    if label is None:
        return None
    text = re.sub(r"[^a-z0-9]+", " ", str(label).lower()).strip()
    if not text:
        return None
    if extra_map:
        for pattern, target in extra_map.items():
            if not target:
                continue
            if re.search(r"[^a-z0-9]+", pattern.lower()):
                norm = re.sub(r"[^a-z0-9]+", " ", pattern.lower()).strip()
            else:
                norm = pattern.lower().strip()
            if text == norm or norm in text:
                return target
    for canonical, synonyms in FIELD_SYNONYMS.items():
        for synonym in synonyms:
            if text == synonym:
                return canonical
    for canonical, synonyms in FIELD_SYNONYMS.items():
        for synonym in synonyms:
            if synonym in text:
                return canonical
    return None


def _clean_cell(text: str) -> str:
    text = (text or "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# HTML table extraction
# ---------------------------------------------------------------------------
def _score_headers(headers: Iterable[str], extra_map: dict | None = None) -> int:
    return sum(1 for h in headers if canonical_field(h, extra_map))


def extract_table_records(html: str, table_selector: str | None = None,
                          column_map: dict[str, str] | None = None,
                          min_score: int = 2) -> list[dict]:
    """Return a list of dicts (canonical field -> raw text) from HTML tables."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    tables = list(soup.select(table_selector)) if table_selector else soup.find_all("table")
    if not tables:
        return []

    scored: list[tuple[int, Any]] = []
    for table in tables:
        headers = [ _clean_cell(th.get_text(" ", strip=True))
                    for th in table.find_all("th") ]
        rows = table.find_all("tr")
        if not headers:
            # Headerless tables: use the first row as the header if it looks textual.
            first = rows[0] if rows else None
            if first is None:
                continue
            cells = first.find_all(["td", "th"])
            candidate = [_clean_cell(c.get_text(" ", strip=True)) for c in cells]
            if _score_headers(candidate) >= min_score:
                headers = candidate
                rows = rows[1:]
            else:
                headers = []
        scored.append((_score_headers(headers, column_map), table))

    best = max(score for score, _ in scored) if scored else 0
    if best < min_score and not table_selector:
        # Fall back to the most promising table even if only one header matched.
        if best < 1:
            return []
        threshold = best
    else:
        threshold = max(min_score, best) if table_selector else best

    records: list[dict] = []
    for score, table in scored:
        if score < threshold or score == 0:
            continue
        headers = []
        rows = table.find_all("tr")
        header_row_index = 0
        for index, row in enumerate(rows):
            cells = row.find_all(["td", "th"])
            candidate = [_clean_cell(c.get_text(" ", strip=True)) for c in cells]
            if _score_headers(candidate, column_map) >= 1:
                headers = [canonical_field(h, column_map) or h for h in candidate]
                header_row_index = index
                break
        for row in rows[header_row_index + 1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            values = [_clean_cell(c.get_text(" ", strip=True)) for c in cells]
            if not any(values):
                continue
            record: dict[str, Any] = {}
            for position, value in enumerate(values):
                key = headers[position] if position < len(headers) else f"col_{position}"
                if key in record and record[key]:
                    record[f"col_{position}"] = value
                else:
                    record[key] = value
            if record:
                record["_raw"] = values
                records.append(record)
    return records


def _label_value_pairs(container) -> list[tuple[str, str]]:
    """Extract ``(label, value)`` pairs from a card made of label/value nodes."""
    pairs: list[tuple[str, str]] = []
    candidates = []
    for node in container.find_all(["span", "div", "dt", "dd", "th", "td", "p", "li"]):
        classes = " ".join(node.get("class") or []).lower()
        if "label" in classes or "key" in classes or node.name in ("dt", "th"):
            candidates.append(node)
    if not candidates:
        return pairs
    for label_node in candidates:
        label = _clean_cell(label_node.get_text(" ", strip=True))
        if not label:
            continue
        value = ""
        parent = label_node.parent
        if parent is not None:
            for sibling in parent.find_all(recursive=False):
                if sibling is label_node:
                    continue
                classes = " ".join(sibling.get("class") or []).lower()
                if "value" in classes or "val" in classes:
                    value = _clean_cell(sibling.get_text(" ", strip=True))
                    break
            if not value:
                row_text = _clean_cell(parent.get_text(" ", strip=True))
                if row_text.lower().startswith(label.lower()):
                    value = row_text[len(label):].strip()
        if value:
            pairs.append((label, value))
    return pairs


def extract_card_records(html: str, container_selector: str,
                         field_selectors: dict[str, str] | None = None) -> list[dict]:
    """Extract repeating card/div blocks (used by the JS-driven portals)."""
    if not html or not container_selector:
        return []
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []
    for container in soup.select(container_selector):
        record: dict[str, Any] = {}
        values: list[str] = []
        if field_selectors:
            for name, selector in field_selectors.items():
                node = container.select_one(selector)
                value = _clean_cell(node.get_text(" ", strip=True)) if node else ""
                record[name] = value
                values.append(value)
        elif False:
            pass
        else:
            # 1) structured ".label" / ".value" pairs (the common card layout)
            pairs = _label_value_pairs(container)
            if not pairs:
                # 2) "Label: value" text inside the container
                text = _clean_cell(container.get_text(" ", strip=True))
                pairs = [(match.group(1), match.group(2).strip()) for match in re.finditer(
                    r"([A-Za-z][A-Za-z /#\-()]{2,40})\s*[:：]\s*"
                    r"([^:]{1,120}?)(?=\s+[A-Za-z][A-Za-z /#\-()]{2,40}\s*[:：]|$)",
                    text)]
            for label, value in pairs:
                key = canonical_field(label, field_selectors) or str(label).strip().lower()
                record.setdefault(key, value)
                values.append(value)
        if record:
            record["_raw"] = values
            records.append(record)
    return records


def extract_records_from_html(html: str, table_selector: str | None = None,
                              column_map: dict[str, str] | None = None,
                              container_selector: str | None = None,
                              field_selectors: dict[str, str] | None = None) -> list[dict]:
    """Try the configured strategy first, then fall back to the others."""
    records: list[dict] = []
    if container_selector:
        records = extract_card_records(html, container_selector, field_selectors)
    if not records:
        records = extract_table_records(html, table_selector, column_map)
    if not records and table_selector:
        records = extract_table_records(html, None, column_map)
    if not records and container_selector:
        records = extract_card_records(html, container_selector, field_selectors)
    return records


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
def _walk_to_records(node: Any) -> list[dict] | None:
    """Find the first plausible list-of-dicts inside a decoded JSON payload."""
    if isinstance(node, list):
        dicts = [item for item in node if isinstance(item, dict)]
        if dicts:
            return dicts
        for item in node:
            found = _walk_to_records(item)
            if found:
                return found
        return None
    if isinstance(node, dict):
        for preferred in ("data", "records", "results", "items", "violations", "rows",
                          "result", "payload"):
            value = node.get(preferred)
            if value is not None:
                found = _walk_to_records(value)
                if found:
                    return found
        for value in node.values():
            found = _walk_to_records(value)
            if found:
                return found
    return None


def resolve_path(node: Any, path: str) -> Any:
    """Resolve ``data.violations[0].items`` style paths."""
    current = node
    for token in re.findall(r"[^.\[\]]+", path or ""):
        if isinstance(current, dict):
            current = current.get(token)
        elif isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


def extract_records_from_json(payload: Any, records_path: str | None = None,
                              column_map: dict[str, str] | None = None) -> list[dict]:
    if payload is None:
        return []
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return []
    if records_path:
        resolved = resolve_path(payload, records_path)
        candidates = resolved if isinstance(resolved, list) else [resolved]
    else:
        candidates = _walk_to_records(payload) or []
    records: list[dict] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        record: dict[str, Any] = {}
        for key, value in item.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            target = canonical_field(str(key), column_map) or str(key)
            record.setdefault(target, "" if value is None else str(value).strip())
        if record:
            record["_raw"] = list(item.values())
            records.append(record)
    return records


def records_from_response(text: str, content_type: str = "",
                          config: dict | None = None) -> list[dict]:
    """Parse either a JSON body or an HTML document into raw records."""
    config = config or {}
    body = (text or "").strip()
    if not body:
        return []
    looks_json = "json" in (content_type or "").lower() or body[:1] in "[{"
    if looks_json:
        try:
            records = extract_records_from_json(
                body,
                records_path=config.get("records_path"),
                column_map=config.get("column_map"),
            )
            if records:
                return records
        except ValueError:
            pass
    return extract_records_from_html(
        body,
        table_selector=config.get("table_selector"),
        column_map=config.get("column_map"),
        container_selector=config.get("container_selector"),
        field_selectors=config.get("field_selectors"),
    )


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------
def detect_empty_result(html: str) -> bool:
    """True when a portal explicitly reported "no violations"."""
    if not html:
        return False
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).lower()
    return any(marker in text for marker in EMPTY_MARKERS)


def detect_block(html: str, status_code: int | None = None) -> str | None:
    """Return a reason string when the response looks like a bot challenge."""
    if status_code in (403, 429, 503) and (not html or len(html) < 40000):
        lowered = (html or "").lower()
        if "captcha" in lowered or "turnstile" in lowered or "cloudflare" in lowered \
                or "access denied" in lowered or status_code == 429:
            return f"HTTP {status_code}: rate limited or blocked"
    if not html:
        return None
    if extract_table_records(html):
        # A results table is present: the widget markup may still be on the
        # page (the portals keep the Turnstile div after a successful search),
        # so the response is a real answer, not a challenge.
        return None
    lowered = html.lower()
    for marker in BLOCK_MARKERS:
        if marker in lowered:
            if marker == "captcha" and "g-recaptcha" not in lowered \
                    and "hcaptcha" not in lowered and "turnstile" not in lowered:
                return "CAPTCHA challenge detected"
            label = {
                "cf-turnstile": "Cloudflare Turnstile challenge",
                "g-recaptcha": "reCAPTCHA challenge",
                "recaptcha": "reCAPTCHA challenge",
                "hcaptcha": "hCaptcha challenge",
                "checking your browser": "Cloudflare browser check",
                "just a moment": "Cloudflare interstitial",
                "access denied": "Access denied (HTTP 403)",
            }.get(marker, f"Bot protection detected ({marker})")
            return label
    return None


def extract_turnstile_token(html: str) -> str | None:
    """Pull a solved Turnstile token out of a rendered page (if any)."""
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    field = soup.find("input", {"name": re.compile(r"cf-turnstile-response", re.I)})
    if field and field.get("value"):
        return field["value"].strip() or None
    match = re.search(r'cf-turnstile-response["\']?\s*[:=]\s*["\']([^"\']{20,})', html)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def record_to_violation(record: dict, source_key: str = "",
                        default_plate: str = "") -> ViolationRecord:
    """Convert a raw parsed record into a :class:`ViolationRecord`."""
    def pick(*names: str) -> str:
        for name in names:
            value = record.get(name)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    date_value = parse_date(pick("violation_date", "date", "col_0"))
    time_value = parse_time(pick("violation_time", "time"))
    if not time_value:
        combined = pick("violation_date")
        if combined and any(ch.isdigit() for ch in combined) and ":" in combined:
            time_value = parse_time(combined)

    return ViolationRecord(
        ticket_no=pick("ticket_no", "ticket", "tvr", "reference"),
        nov_no=pick("nov_no", "nov", "notice"),
        plate=(pick("plate") or default_plate or "").upper(),
        violation_date=date_value,
        violation_time=time_value,
        location=pick("location"),
        offense=pick("offense", "violation", "description"),
        amount=parse_amount(pick("amount", "fine", "penalty")),
        currency="PHP",
        status=pick("status"),
        due_date=parse_date(pick("due_date", "deadline")),
        raw={k: v for k, v in record.items() if k != "_raw"},
    )


def normalize_records(records: Iterable[dict], source_key: str = "",
                      default_plate: str = "") -> list[ViolationRecord]:
    results: list[ViolationRecord] = []
    seen: set[str] = set()
    for record in records:
        violation = record_to_violation(record, source_key, default_plate)
        if not violation.is_meaningful():
            continue
        key = violation.fingerprint(source_key)
        if key in seen:
            continue
        seen.add(key)
        results.append(violation)
    return results
