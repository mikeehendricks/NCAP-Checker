"""JSON API: authentication, lookups and vehicle checks."""
from __future__ import annotations

from decimal import Decimal

from app.models import CheckStatus
from app.scrapers.base import CheckOutcome
from app.scrapers.parsing import ViolationRecord


def test_api_requires_authentication(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/vehicles").status_code == 401
    assert client.post("/api/lookup", json={"plate_number": "ABC1234"}).status_code == 401


def test_status_endpoint(logged_in_client, user):
    response = logged_in_client.get("/api/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["user"] == "tester"
    assert len(payload["sources"]) == 5


def test_api_key_authentication(app, client, user):
    response = client.get("/api/status", headers={"X-API-Key": user.api_key})
    assert response.status_code == 200
    assert response.get_json()["user"] == "tester"


def test_bad_api_key_is_rejected(client):
    assert client.get("/api/status", headers={"X-API-Key": "nope"}).status_code == 401


def test_vehicles_are_scoped_to_the_owner(logged_in_client, vehicle, app, second_user):
    other = type(vehicle)(user_id=second_user.id)
    other.set_identifiers("ZZZ1111", "1111111111")
    from app.extensions import db

    db.session.add(other)
    db.session.commit()

    payload = logged_in_client.get("/api/vehicles").get_json()
    assert [item["plate_number"] for item in payload] == ["ABC 1234"]


def test_lookup_endpoint_aggregates_sources(logged_in_client, sources, monkeypatch):
    record = ViolationRecord(ticket_no="API-9", plate="ABC1234", offense="Speeding",
                             violation_date="2026-09-01", amount=Decimal("1500"))

    def fake_adapter(source, settings=None, **kw):
        if source.key == "quezon_city":
            return type("A", (), {"check": lambda self, plate, mv, engine=None,
                                  capture_screenshot=False: CheckOutcome(
                                      status=CheckStatus.FOUND, violations=[record],
                                      engine="http", message="1 violation(s)")})()
        return type("A", (), {"check": lambda self, plate, mv, engine=None,
                              capture_screenshot=False: CheckOutcome(
                                  status=CheckStatus.CLEAN, violations=[],
                                  engine="http", message="none")})()

    monkeypatch.setattr("app.blueprints.api.get_adapter", fake_adapter)
    response = logged_in_client.post("/api/lookup",
                                     json={"plate_number": "abc1234",
                                           "mv_number": "1234567890"})
    payload = response.get_json()
    assert payload["plate"] == "ABC1234"
    assert payload["overall"] == "found"
    found = [r for r in payload["results"] if r["source"] == "quezon_city"]
    assert found[0]["violations"][0]["ticket_no"] == "API-9"


def test_lookup_requires_a_plate(logged_in_client):
    assert logged_in_client.post("/api/lookup", json={}).status_code == 400


def test_check_endpoint_runs_and_reports(logged_in_client, vehicle, sources, monkeypatch):
    monkeypatch.setattr(
        "app.services.checker.get_adapter",
        lambda source, settings=None, **kw: type(
            "A", (), {"check": lambda self, plate, mv, engine=None,
                      capture_screenshot=False: CheckOutcome(
                          status=CheckStatus.CLEAN, violations=[],
                          engine="http", message="none")})())
    response = logged_in_client.post(f"/api/vehicles/{vehicle.id}/check")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["vehicle"]["id"] == vehicle.id
    assert payload["new"] == 0


def test_check_endpoint_hides_other_users_vehicles(logged_in_client, app, second_user):
    from app.extensions import db
    from app.models import Vehicle

    other = Vehicle(user_id=second_user.id)
    other.set_identifiers("ZZZ1111", "1111111111")
    db.session.add(other)
    db.session.commit()
    assert logged_in_client.post(f"/api/vehicles/{other.id}/check").status_code == 404


def test_violations_endpoint(logged_in_client, vehicle, sources):
    from decimal import Decimal

    from app.extensions import db
    from app.models import Violation

    violation = Violation(vehicle_id=vehicle.id, source_id=sources["quezon_city"].id,
                          fingerprint="fp1", ticket_no="T-1", offense="Speeding",
                          amount=Decimal("2000"), plate="ABC1234")
    db.session.add(violation)
    db.session.commit()

    payload = logged_in_client.get("/api/violations").get_json()
    assert len(payload) == 1
    assert payload[0]["ticket_no"] == "T-1"
    assert payload[0]["amount"] == 2000.0
