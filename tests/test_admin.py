"""Admin panel: settings, sources, users, updates and audit trail."""
from __future__ import annotations

import json

from app.extensions import db
from app.models import AuditLog, CheckStatus, Source, User
from app.services import settings as settings_service
from app.services.updater import git_info
from tests.conftest import make_user


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def test_settings_persist(admin_client):
    response = admin_client.post("/admin/settings", data={
        "section": "general", "set_app_name": "Fleet Checker",
        "set_tagline": "Tag", "set_registration_open": "on",
    }, follow_redirects=True)
    assert b"Settings saved" in response.data
    assert settings_service.get_setting("app_name") == "Fleet Checker"
    assert settings_service.get_setting("registration_open") is True


def test_unchecked_boxes_become_false(admin_client, app):
    settings_service.set_setting("registration_open", True)
    admin_client.post("/admin/settings", data={
        "section": "general", "set_app_name": "X", "set_tagline": "Y",
    })
    assert settings_service.get_setting("registration_open") is False


def test_numeric_settings_are_coerced(admin_client):
    admin_client.post("/admin/settings", data={
        "section": "scheduling", "set_default_interval_minutes": "180",
        "set_scheduler_tick_seconds": "600", "set_retention_days": "30",
    })
    assert settings_service.get_setting("default_interval_minutes") == 180
    assert isinstance(settings_service.get_setting("scheduler_tick_seconds"), int)


def test_blank_smtp_password_is_preserved(admin_client):
    settings_service.set_setting("smtp_password", "keep-me")
    admin_client.post("/admin/settings", data={
        "section": "mail", "set_smtp_host": "smtp.example.com", "set_smtp_port": "587",
        "set_smtp_password": "",
    })
    from app.services.settings import get_settings

    assert get_settings(include_secrets=True)["smtp_password"] == "keep-me"


def test_secrets_are_masked_in_the_ui(admin_client):
    settings_service.set_setting("smtp_password", "topsecret")
    response = admin_client.get("/admin/settings?section=mail")
    assert b"topsecret" not in response.data


def test_test_email_reports_failure_when_smtp_is_off(admin_client):
    response = admin_client.post("/admin/settings/test-email", follow_redirects=True)
    assert b"Could not send" in response.data


def test_settings_changes_are_audited(admin_client):
    admin_client.post("/admin/settings", data={"section": "general",
                                               "set_app_name": "Audited"})
    assert AuditLog.query.filter_by(action="settings.updated").count() == 1


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def test_edit_source(admin_client, sources):
    source = sources["quezon_city"]
    response = admin_client.post(f"/admin/sources/{source.id}/edit", data={
        "name": "QC NCAP", "agency": "QC LGU",
        "base_url": "https://nocontact.quezoncity.gov.ph",
        "check_url": "https://nocontact.quezoncity.gov.ph/check-violations",
        "engine": "browser", "timeout_seconds": "45", "delay_seconds": "6",
        "enabled": "y",
        "config": json.dumps({"browser": {"settle_ms": 4000}}),
    }, follow_redirects=True)
    assert b"saved" in response.data
    db.session.refresh(source)
    assert source.name == "QC NCAP" and source.engine == "browser"
    assert source.timeout_seconds == 45
    assert source.config["browser"]["settle_ms"] == 4000


def test_invalid_source_config_json_is_rejected(admin_client, sources):
    source = sources["mmda"]
    response = admin_client.post(f"/admin/sources/{source.id}/edit", data={
        "name": "MMDA", "agency": "MMDA",
        "base_url": "https://mayhulika.mmda.gov.ph",
        "check_url": "https://mayhulika.mmda.gov.ph/",
        "engine": "auto", "timeout_seconds": "30", "delay_seconds": "5",
        "config": "{not json",
    })
    assert b"Invalid JSON" in response.data
    db.session.refresh(source)
    assert source.config == {}


def test_toggle_and_reset_source(admin_client, sources):
    source = sources["manila"]
    admin_client.post(f"/admin/sources/{source.id}/toggle")
    assert Source.query.get(source.id).enabled is False
    admin_client.post(f"/admin/sources/{source.id}/toggle")
    assert Source.query.get(source.id).enabled is True

    source.consecutive_failures = 4
    db.session.commit()
    admin_client.post(f"/admin/sources/{source.id}/reset")
    assert Source.query.get(source.id).consecutive_failures == 0


def test_probe_runs_and_renders_results(admin_client, sources, monkeypatch):
    from decimal import Decimal

    from app.scrapers.base import CheckOutcome
    from app.scrapers.parsing import ViolationRecord

    record = ViolationRecord(ticket_no="PROBE-1", plate="ABC1234",
                             offense="Speeding", violation_date="2026-09-01",
                             amount=Decimal("2500"))
    monkeypatch.setattr(
        "app.blueprints.admin.get_adapter",
        lambda source, settings=None, **kw: type(
            "A", (), {"check": lambda self, plate, mv, engine=None,
                      capture_screenshot=False: CheckOutcome(
                          status=CheckStatus.FOUND, violations=[record],
                          engine="browser", http_status=200,
                          duration_ms=1234, message="1 violation(s) returned")})())

    assert admin_client.get(f"/admin/sources/{sources['quezon_city'].id}/probe").status_code == 200
    response = admin_client.post(
        f"/admin/sources/{sources['quezon_city'].id}/probe",
        data={"plate": "ABC1234", "mv_number": "1234567890", "engine": ""})
    assert response.status_code == 200
    assert b"PROBE-1" in response.data
    assert b"Parsed violations" in response.data
    assert AuditLog.query.filter_by(action="source.probed").count() == 1


def test_sources_page_lists_all_five(admin_client, sources):
    response = admin_client.get("/admin/sources")
    assert response.status_code == 200
    for source in sources.values():
        assert source.name.encode() in response.data


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def test_approve_user(admin_client, app):
    pending = make_user(username="waiter", email="waiter@example.com", is_approved=False)
    admin_client.post(f"/admin/users/{pending.id}/approve")
    assert User.query.get(pending.id).is_approved is True


def test_promote_and_demote_user(admin_client, user):
    admin_client.post(f"/admin/users/{user.id}/toggle-admin")
    assert User.query.get(user.id).is_admin is True
    admin_client.post(f"/admin/users/{user.id}/toggle-admin")
    assert User.query.get(user.id).is_admin is False


def test_last_admin_cannot_be_demoted(admin_client, admin):
    response = admin_client.post(f"/admin/users/{admin.id}/toggle-admin",
                                 follow_redirects=True)
    assert b"cannot remove your own" in response.data
    assert User.query.get(admin.id).is_admin is True


def test_cannot_delete_yourself(admin_client, admin):
    response = admin_client.post(f"/admin/users/{admin.id}/delete", follow_redirects=True)
    assert b"cannot delete your own" in response.data
    assert User.query.get(admin.id) is not None


def test_deactivate_and_delete_user(admin_client, user):
    admin_client.post(f"/admin/users/{user.id}/toggle-active")
    assert User.query.get(user.id).is_active is False
    admin_client.post(f"/admin/users/{user.id}/delete")
    assert User.query.get(user.id) is None


def test_password_reset_issues_a_temporary_password(admin_client, user):
    response = admin_client.post(f"/admin/users/{user.id}/reset-password",
                                 follow_redirects=True)
    assert b"Temporary password" in response.data
    assert User.query.get(user.id).password_hash != ""


# ---------------------------------------------------------------------------
# Updates / system / audit
# ---------------------------------------------------------------------------
def test_update_screen_reports_git_state(admin_client):
    response = admin_client.get("/admin/updates")
    assert response.status_code == 200
    info = git_info()
    assert b"Version" in response.data
    assert info["version"]


def test_check_for_updates_button_is_safe_offline(app, admin_client, monkeypatch):
    monkeypatch.setattr("app.services.updater.is_git_repo", lambda: False)
    response = admin_client.post("/admin/updates", data={"action": "check"},
                                 follow_redirects=True)
    assert response.status_code == 200
    assert b"not a git clone" in response.data.lower()


def test_system_page(admin_client):
    response = admin_client.get("/admin/system")
    assert response.status_code == 200
    assert b"Python" in response.data


def test_audit_page(admin_client, admin):
    AuditLog.record("test.event", target="x", user=admin)
    db.session.commit()
    response = admin_client.get("/admin/audit")
    assert b"test.event" in response.data


def test_admin_sees_all_vehicles(admin_client, vehicle):
    response = admin_client.get("/admin/vehicles")
    assert b"ABC 1234" in response.data


def test_maintenance_mode_blocks_regular_users(app, logged_in_client, admin_client):
    settings_service.set_setting("maintenance_mode", True)
    assert b"maintenance" in logged_in_client.get("/dashboard").data.lower()
    assert admin_client.get("/admin/").status_code == 200
    settings_service.set_setting("maintenance_mode", False)
    assert logged_in_client.get("/dashboard").status_code == 200
