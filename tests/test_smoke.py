"""End-to-end usability walkthrough: every screen a user or admin will touch.

This is deliberately written as a "person clicking through the app" script so a
regression in navigation, templates or permissions fails here rather than in
production.
"""
from __future__ import annotations

import re

import pytest

from app.extensions import db
from app.models import Vehicle
from tests.conftest import login

PUBLIC_PAGES = ["/", "/login", "/register", "/help", "/about"]
USER_PAGES = ["/dashboard", "/vehicles/", "/vehicles/new", "/notifications", "/profile"]
ADMIN_PAGES = ["/admin/", "/admin/sources", "/admin/settings", "/admin/users",
               "/admin/vehicles", "/admin/updates", "/admin/system", "/admin/audit"]


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_pages_render(client, admin, path):
    response = client.get(path)
    assert response.status_code == 200, path
    assert b"<!doctype html>" in response.data.lower()
    assert b"Traceback" not in response.data


@pytest.mark.parametrize("path", USER_PAGES)
def test_user_pages_render(logged_in_client, path):
    response = logged_in_client.get(path)
    assert response.status_code == 200, path
    assert b"Traceback" not in response.data


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_render(admin_client, path):
    response = admin_client.get(path)
    assert response.status_code == 200, path
    assert b"Traceback" not in response.data


def test_settings_tabs_render(admin_client):
    for section in ("general", "scheduling", "scraping", "mail"):
        response = admin_client.get(f"/admin/settings?section={section}")
        assert response.status_code == 200, section
        assert b"Save settings" in response.data


def test_full_user_journey(client, app):
    """Register -> add vehicle -> check -> read the alert -> mark it settled."""
    from decimal import Decimal

    from app.models import CheckStatus, CheckRun, Notification, Violation
    from app.scrapers.base import CheckOutcome
    from app.scrapers.parsing import ViolationRecord

    # 1. Administrator exists, so the visitor registers a normal account.
    from tests.conftest import make_user

    with app.app_context():
        make_user(username="root", email="root@example.com", is_admin=True)
        record = ViolationRecord(ticket_no="JOURNEY-1", plate="ABC1234",
                                 offense="Disregarding Traffic Sign",
                                 violation_date="2026-09-12", amount=Decimal("2000"),
                                 location="Commonwealth Ave.")

        import app.services.checker as checker

        original = checker.get_adapter
        checker.get_adapter = lambda source, settings=None, **kw: type(
            "A", (), {"check": lambda self, plate, mv, engine=None,
                      capture_screenshot=False: CheckOutcome(
                          status=CheckStatus.FOUND, violations=[record], engine="http",
                          message="1 violation(s) returned")})()

    response = client.post("/register", data={
        "username": "journey", "email": "journey@example.com",
        "password": "Str0ngPassw0rd!", "confirm": "Str0ngPassw0rd!",
    }, follow_redirects=True)
    assert b"Account created" in response.data

    response = login(client, "journey")
    assert b"Welcome back" in response.data

    response = client.post("/vehicles/new", data={
        "label": "Journey car", "plate_number": "ABC 1234",
        "mv_number": "1234-5678901234", "is_active": "y",
    }, follow_redirects=True)
    assert response.status_code == 200
    vehicle = Vehicle.query.filter_by(plate_norm="ABC1234").first()
    assert vehicle is not None

    response = client.get("/dashboard")
    assert b"Journey car" in response.data

    response = client.post(f"/vehicles/{vehicle.id}/check", data={"sync": "1"},
                            follow_redirects=True)
    assert b"new violation" in response.data

    violation = Violation.query.filter_by(vehicle_id=vehicle.id).first()
    assert violation is not None and violation.ticket_no == "JOURNEY-1"
    assert CheckRun.query.filter_by(vehicle_id=vehicle.id).count() == 5

    response = client.get("/notifications")
    assert b"new NCAP violation" in response.data
    assert Notification.query.filter_by(kind="violation").count() == 1

    response = client.get(f"/vehicles/{vehicle.id}")
    assert b"JOURNEY-1" in response.data
    assert b"2,000.00" in response.data

    client.post(f"/vehicles/violations/{violation.id}/resolve", data={"resolved": "1"})
    assert Violation.query.get(violation.id).is_resolved is True

    checker.get_adapter = original


def test_navigation_links_resolve(logged_in_client, admin_client):
    """Crawl internal links from the dashboard and make sure none 404."""
    visited = set()

    def crawl(client, path, depth=0):
        if depth > 1 or path in visited:
            return
        visited.add(path)
        response = client.get(path)
        if response.status_code != 200:
            return
        for link in re.findall(rb'href="(/[^"#?]*)"', response.data):
            target = link.decode()
            if target.startswith("/static") or target.startswith("/admin/screenshot"):
                continue
            crawl(client, target, depth + 1)

    crawl(logged_in_client, "/dashboard")
    crawl(admin_client, "/admin/")

    for path in sorted(visited):
        response = (admin_client if path.startswith("/admin") else logged_in_client).get(path)
        assert response.status_code < 400, f"{path} -> {response.status_code}"


def test_csrf_protects_post_routes(app):
    """With CSRF enabled (production default), token-less POSTs are rejected."""
    from tests.conftest import make_user

    with app.app_context():
        make_user(username="csrf", email="csrf@example.com", is_admin=True)
    client = app.test_client()
    assert b"Welcome back" in login(client, "csrf").data

    app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post("/vehicles/new", data={"plate_number": "ABC1234",
                                                      "mv_number": "1234567890"})
        assert response.status_code == 400
        assert b"CSRF" in response.data
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_error_pages_render(client, admin):
    assert client.get("/definitely-not-a-page").status_code == 404
    assert b"404" in client.get("/definitely-not-a-page").data
    # anonymous -> login redirect (an administrator exists in this test)
    assert client.get("/admin/").status_code == 302
    assert "/login" in client.get("/admin/").headers["Location"]
