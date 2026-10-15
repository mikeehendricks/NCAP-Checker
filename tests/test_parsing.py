"""Unit tests for the HTML/JSON extraction layer."""
from __future__ import annotations

from decimal import Decimal

from app.scrapers import parsing
from tests.conftest import fixture_html


# ---------------------------------------------------------------------------
# HTML tables
# ---------------------------------------------------------------------------
def test_extracts_table_rows_from_quezon_city_page():
    records = parsing.extract_table_records(fixture_html("qc_violations.html"))
    assert len(records) == 2
    assert records[0]["ticket_no"] == "QC-2026-000123"
    assert records[0]["plate"] == "ABC1234"
    assert records[0]["offense"] == "Disregarding Traffic Sign"
    assert records[0]["location"].startswith("Commonwealth Ave.")


def test_normalises_amounts_dates_and_times():
    records = parsing.extract_table_records(fixture_html("qc_violations.html"))
    violations = parsing.normalize_records(records, "quezon_city", "ABC1234")
    assert len(violations) == 2
    first, second = violations
    assert first.amount == Decimal("2000.00")
    assert first.violation_date.isoformat() == "2026-09-12"
    assert first.violation_time == "08:32"
    assert first.status == "Unpaid"
    assert second.violation_date.isoformat() == "2026-09-14"
    assert second.violation_time == "17:05"
    assert second.amount == Decimal("1500.00")


def test_explicit_table_selector_is_honoured():
    records = parsing.extract_table_records(fixture_html("qc_violations.html"),
                                            table_selector="table.violations-table")
    assert len(records) == 2
    assert parsing.extract_table_records(fixture_html("qc_violations.html"),
                                         table_selector="table.does-not-exist") == []


def test_ignores_navigation_tables():
    html = """
    <table><tr><th>Menu</th><th>Links</th></tr><tr><td>Home</td><td>About</td></tr></table>
    <table><tr><th>Ticket No.</th><th>Violation Date</th><th>Amount</th></tr>
           <tr><td>T-1</td><td>2026-01-02</td><td>500.00</td></tr></table>
    """
    records = parsing.extract_table_records(html)
    assert len(records) == 1
    assert records[0]["ticket_no"] == "T-1"


def test_headerless_table_uses_first_row_as_header():
    html = """
    <table>
      <tr><td>Ticket No.</td><td>Violation Date</td><td>Amount</td></tr>
      <tr><td>T-9</td><td>2026-03-04</td><td>750</td></tr>
    </table>"""
    records = parsing.extract_table_records(html)
    assert records and records[0]["ticket_no"] == "T-9"


def test_extracts_div_cards_from_manila_page():
    html = fixture_html("manila_violations.html")
    records = parsing.extract_card_records(html, ".violation-card")
    assert len(records) == 2
    violations = parsing.normalize_records(records, "manila", "ABC1234")
    assert violations[0].ticket_no == "MNL-00088421"
    assert violations[0].offense == "Beating the Red Light"
    assert violations[0].amount == Decimal("5000.00")
    assert violations[1].violation_time == "18:20"


def test_mmda_column_vocabulary():
    records = parsing.extract_table_records(fixture_html("mmda_violations.html"))
    violations = parsing.normalize_records(records, "mmda", "ABC1234")
    assert len(violations) == 1
    assert violations[0].nov_no == "MMDA-2026-77120"      # "Notice of Violation No."
    assert violations[0].location == "EDSA cor. Ortigas Ave."
    assert violations[0].offense == "Bus Lane Violation"
    assert violations[0].amount == Decimal("5000.00")
    assert violations[0].status == "Unsettled"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def test_json_records_are_discovered_without_an_explicit_path():
    payload = fixture_html("api_violations.json")
    records = parsing.extract_records_from_json(payload)
    assert len(records) == 2
    assert records[0]["ticket_no"] == "API-2026-00001"
    assert records[0]["offense"] == "Over speeding"


def test_json_records_path_is_respected():
    payload = fixture_html("api_violations.json")
    records = parsing.extract_records_from_json(payload, records_path="data.violations")
    assert len(records) == 2
    violations = parsing.normalize_records(records, "test", "ABC1234")
    assert violations[0].amount == Decimal("3000.00")
    assert violations[1].status == "Paid"


def test_json_records_path_can_be_missing():
    records = parsing.extract_records_from_json(fixture_html("api_violations.json"),
                                                records_path="data.nope")
    assert records == []


def test_records_from_response_sniffs_content_type():
    json_body = fixture_html("api_violations.json")
    assert len(parsing.records_from_response(json_body, "application/json")) == 2
    html_body = fixture_html("qc_violations.html")
    assert len(parsing.records_from_response(html_body, "text/html")) == 2


# ---------------------------------------------------------------------------
# Signal detection - the safety critical part
# ---------------------------------------------------------------------------
def test_detects_empty_result_markers():
    assert parsing.detect_empty_result(fixture_html("qc_no_violations.html")) is True
    assert parsing.detect_empty_result(fixture_html("qc_violations.html")) is False
    assert parsing.detect_empty_result("") is False


def test_detects_cloudflare_turnstile_challenge():
    reason = parsing.detect_block(fixture_html("turnstile_challenge.html"))
    assert reason and "cloudflare" in reason.lower()


def test_detects_recaptcha_markers():
    assert parsing.detect_block('<div class="g-recaptcha"></div>')
    assert parsing.detect_block("Please verify you are not a robot")


def test_detects_http_403_and_429():
    assert parsing.detect_block("<html>Access Denied</html>", 403)
    assert parsing.detect_block("", 429)
    assert parsing.detect_block(fixture_html("qc_violations.html"), 200) is None


def test_bare_403_without_markers_is_not_assumed_to_be_a_bot_wall():
    """A plain 403 with unknown content must not be labelled a CAPTCHA block."""
    assert parsing.detect_block("<html><body>small</body></html>", 403) is None


def test_clean_pages_are_not_treated_as_blocked():
    assert parsing.detect_block(fixture_html("qc_no_violations.html")) is None


def test_turnstile_token_extraction():
    token = parsing.extract_turnstile_token(
        '<input type="hidden" name="cf-turnstile-response" value="tok-123">')
    assert token == "tok-123"
    assert parsing.extract_turnstile_token(
        '<input type="hidden" name="cf-turnstile-response" value="">') is None


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
def test_canonical_field_matching():
    assert parsing.canonical_field("Ticket No.") == "ticket_no"
    assert parsing.canonical_field("Notice of Violation No.") == "nov_no"
    assert parsing.canonical_field("Nature of Violation") == "offense"
    assert parsing.canonical_field("Apprehension Date") == "violation_date"
    assert parsing.canonical_field("Fine") == "amount"
    assert parsing.canonical_field("Payment Status") == "status"
    assert parsing.canonical_field("Random Column") is None


def test_column_map_overrides_win():
    mapping = {"Transaction Reference": "ticket_no"}
    assert parsing.canonical_field("Transaction Reference", mapping) == "ticket_no"


def test_meaningless_rows_are_dropped():
    assert parsing.record_to_violation({"status": ""}, "x").is_meaningful() is False
    assert parsing.record_to_violation({"ticket_no": "T-1"}, "x").is_meaningful() is True


def test_fingerprint_is_stable_and_unique():
    first = parsing.record_to_violation(
        {"ticket_no": "T-1", "violation_date": "2026-01-01", "offense": "Speeding"}, "qc")
    same = parsing.record_to_violation(
        {"ticket_no": "T-1", "violation_date": "2026-01-01", "offense": "Speeding"}, "qc")
    other = parsing.record_to_violation(
        {"ticket_no": "T-2", "violation_date": "2026-01-01", "offense": "Speeding"}, "qc")
    assert first.fingerprint("qc") == same.fingerprint("qc")
    assert first.fingerprint("qc") != other.fingerprint("qc")


def test_duplicate_rows_collapse():
    records = [
        {"ticket_no": "T-1", "violation_date": "2026-01-01"},
        {"ticket_no": "T-1", "violation_date": "2026-01-01"},
    ]
    assert len(parsing.normalize_records(records, "qc")) == 1


def test_amount_and_date_parsers():
    assert parsing.parse_amount("PHP 2,000.00") == Decimal("2000")
    assert parsing.parse_amount("₱1,234.50") == Decimal("1234.50")
    assert parsing.parse_amount("") is None
    assert parsing.parse_amount(None) is None
    assert str(parsing.parse_date("14 September 2026")) == "2026-09-14"
    assert str(parsing.parse_date("09/12/2026")) == "2026-09-12"
    assert parsing.parse_date("not a date") is None


def test_time_parser_handles_meridiem():
    assert parsing.parse_time("08:32 AM") == "08:32"
    assert parsing.parse_time("5:07 pm") == "17:07"
    assert parsing.parse_time("12:00 AM") == "00:00"
    assert parsing.parse_time("noon") is None
