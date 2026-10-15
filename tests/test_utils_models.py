"""Model and utility behaviour."""
from __future__ import annotations

from app.models import CheckStatus
from app.utils import (cooldown_from_failures, is_valid_mv, is_valid_plate,
                       normalize_mv, normalize_plate)


def test_plate_normalisation():
    assert normalize_plate("abc 1234") == "ABC1234"
    assert normalize_plate("ABC-1234") == "ABC1234"
    assert normalize_plate("  ") == ""
    assert normalize_plate(None) == ""


def test_mv_normalisation():
    assert normalize_mv("1234-5678901234") == "12345678901234"
    assert normalize_mv("MV 12 34") == "1234"


def test_plate_validation():
    assert is_valid_plate("ABC1234") is True
    assert is_valid_plate("abc 1234") is True
    assert is_valid_plate("1234567") is False       # no letters
    assert is_valid_plate("ABCDEFG") is False       # no digits
    assert is_valid_plate("AB") is False            # too short
    assert is_valid_plate("") is False


def test_mv_validation():
    assert is_valid_mv("1234567890") is True
    assert is_valid_mv("1234-5678901234") is True
    assert is_valid_mv("12345") is False
    assert is_valid_mv("abc-def") is False


def test_status_severity_rollup():
    assert CheckStatus.worst("clean", "clean") == "clean"
    assert CheckStatus.worst("clean", "found") == "found"
    assert CheckStatus.worst("blocked", "found") == "found"
    assert CheckStatus.worst("error", "blocked") == "blocked"
    assert CheckStatus.worst("unknown", "clean") == "unknown"
    assert CheckStatus.worst() == "unknown"
    assert CheckStatus.label("found") == "Violation found"


def test_backoff_is_capped():
    assert cooldown_from_failures(0) == 0
    assert cooldown_from_failures(1) == 5
    assert cooldown_from_failures(3) == 20
    assert cooldown_from_failures(50) == 240
