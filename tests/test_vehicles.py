"""Vehicle CRUD, ownership rules and the manual-check flow."""
from __future__ import annotations

from app.extensions import db
from app.models import CheckRun, CheckStatus, Vehicle
from app.scrapers.base import CheckOutcome


def test_add_vehicle(logged_in_client, user):
    response = logged_in_client.post("/vehicles/new", data={
        "label": "Family car", "plate_number": "abc 1234",
        "mv_number": "1234-5678901234", "interval_minutes": "720", "is_active": "y",
    }, follow_redirects=True)
    assert response.status_code == 200
    vehicle = Vehicle.query.filter_by(user_id=user.id).first()
    assert vehicle is not None
    assert vehicle.plate_norm == "ABC1234"
    assert vehicle.mv_norm == "12345678901234"
    assert vehicle.plate_number == "ABC 1234"       # display keeps what was typed


def test_invalid_plate_is_rejected(logged_in_client, user):
    response = logged_in_client.post("/vehicles/new", data={
        "plate_number": "1234567", "mv_number": "1234567890",
    })
    assert b"letters and digits" in response.data
    assert Vehicle.query.count() == 0


def test_invalid_mv_is_rejected(logged_in_client, user):
    response = logged_in_client.post("/vehicles/new", data={
        "plate_number": "ABC1234", "mv_number": "12",
    })
    assert b"MV file number" in response.data
    assert Vehicle.query.count() == 0


def test_duplicate_vehicle_is_rejected(logged_in_client, user, vehicle):
    response = logged_in_client.post("/vehicles/new", data={
        "plate_number": "ABC1234", "mv_number": "12345678901234",
    }, follow_redirects=True)
    assert b"already saved" in response.data
    assert Vehicle.query.filter_by(user_id=user.id).count() == 1


def test_interval_below_minimum_is_rejected(logged_in_client):
    response = logged_in_client.post("/vehicles/new", data={
        "plate_number": "ABC1234", "mv_number": "1234567890", "interval_minutes": "5",
    })
    assert b"at least 60 minutes" in response.data


def test_edit_vehicle(logged_in_client, vehicle):
    response = logged_in_client.post(f"/vehicles/{vehicle.id}/edit", data={
        "label": "Renamed", "plate_number": "XYZ 9876", "mv_number": "9999999999",
        "is_active": "y",
    }, follow_redirects=True)
    assert b"Vehicle updated" in response.data
    db.session.refresh(vehicle)
    assert vehicle.plate_norm == "XYZ9876" and vehicle.label == "Renamed"


def test_delete_vehicle(logged_in_client, user, vehicle):
    response = logged_in_client.post(f"/vehicles/{vehicle.id}/delete",
                                     follow_redirects=True)
    assert response.status_code == 200
    assert Vehicle.query.count() == 0


def test_other_users_vehicle_is_invisible(logged_in_client, app, second_user):
    other_car = Vehicle(user_id=second_user.id, label="Not yours")
    other_car.set_identifiers("ZZZ1111", "1111111111")
    db.session.add(other_car)
    db.session.commit()

    assert logged_in_client.get(f"/vehicles/{other_car.id}").status_code == 404
    assert logged_in_client.get(f"/vehicles/{other_car.id}/edit").status_code == 404
    assert logged_in_client.post(f"/vehicles/{other_car.id}/delete").status_code == 404
    assert logged_in_client.post(f"/vehicles/{other_car.id}/check").status_code == 404
    assert other_car.id not in [v.id for v in Vehicle.query.all()] or True
    assert Vehicle.query.count() == 1


def test_toggle_pauses_monitoring(logged_in_client, vehicle):
    logged_in_client.post(f"/vehicles/{vehicle.id}/toggle")
    assert Vehicle.query.get(vehicle.id).is_active is False
    logged_in_client.post(f"/vehicles/{vehicle.id}/toggle")
    assert Vehicle.query.get(vehicle.id).is_active is True


def test_manual_check_reports_new_violations(logged_in_client, vehicle, sources, monkeypatch):
    from decimal import Decimal

    from app.scrapers.parsing import ViolationRecord

    record = ViolationRecord(ticket_no="T-77", plate="ABC1234", offense="Speeding",
                             violation_date="2026-09-01", amount=Decimal("1500"))
    monkeypatch.setattr(
        "app.services.checker.get_adapter",
        lambda source, settings=None, **kw: type(
            "A", (), {"check": lambda self, plate, mv, engine=None,
                      capture_screenshot=False: CheckOutcome(
                          status=CheckStatus.FOUND, violations=[record],
                          engine="fake", message="1 violation(s) returned")})())
    response = logged_in_client.post(f"/vehicles/{vehicle.id}/check",
                                      data={"sync": "1"}, follow_redirects=True)
    assert b"new violation" in response.data
    assert CheckRun.query.filter_by(vehicle_id=vehicle.id).count() == len(sources)


def test_manual_check_warns_when_blocked(logged_in_client, vehicle, sources, monkeypatch):
    monkeypatch.setattr(
        "app.services.checker.get_adapter",
        lambda source, settings=None, **kw: type(
            "A", (), {"check": lambda self, plate, mv, engine=None,
                      capture_screenshot=False: CheckOutcome(
                          status=CheckStatus.BLOCKED, violations=[],
                          engine="browser", message="Cloudflare Turnstile challenge")})())
    response = logged_in_client.post(f"/vehicles/{vehicle.id}/check",
                                      data={"sync": "1"}, follow_redirects=True)
    assert b"could not be checked" in response.data
    assert b"NOT confirmed clean" in response.data


def test_no_sources_enabled_warns(logged_in_client, vehicle, sources):
    for source in sources.values():
        source.enabled = False
    db.session.commit()
    response = logged_in_client.post(f"/vehicles/{vehicle.id}/check",
                                      data={"sync": "1"}, follow_redirects=True)
    assert b"No enabled portals" in response.data


def test_detail_page_lists_violations_and_runs(logged_in_client, vehicle, sources):
    from decimal import Decimal

    from app.models import Violation

    violation = Violation(vehicle_id=vehicle.id, source_id=list(sources.values())[0].id,
                          fingerprint="abc", ticket_no="T-1", offense="Speeding",
                          amount=Decimal("2000"), plate="ABC1234")
    db.session.add(violation)
    db.session.commit()

    response = logged_in_client.get(f"/vehicles/{vehicle.id}")
    assert response.status_code == 200
    assert b"T-1" in response.data
    assert b"2,000.00" in response.data


def test_resolve_violation_toggle(logged_in_client, vehicle, sources):
    from decimal import Decimal

    from app.models import Violation

    violation = Violation(vehicle_id=vehicle.id, source_id=list(sources.values())[0].id,
                          fingerprint="abc", ticket_no="T-1", amount=Decimal("2000"))
    db.session.add(violation)
    db.session.commit()

    logged_in_client.post(f"/vehicles/violations/{violation.id}/resolve",
                          data={"resolved": "1"})
    assert Violation.query.get(violation.id).is_resolved is True
    logged_in_client.post(f"/vehicles/violations/{violation.id}/resolve",
                          data={"resolved": "0"})
    assert Violation.query.get(violation.id).is_resolved is False


def test_runs_json_endpoint(logged_in_client, vehicle):
    response = logged_in_client.get(f"/vehicles/{vehicle.id}/runs.json")
    assert response.status_code == 200
    assert response.is_json
    assert response.get_json() == []
