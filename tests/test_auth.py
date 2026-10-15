"""Registration, login, lockout and role separation."""
from __future__ import annotations

from app.extensions import db
from app.models import AuditLog, User
from tests.conftest import login, make_user


def test_setup_creates_the_only_administrator(client):
    response = client.get("/setup")
    assert response.status_code == 200

    response = client.post("/setup", data={
        "username": "boss", "email": "boss@example.com",
        "full_name": "Boss", "password": "Sup3rSecret!!", "confirm": "Sup3rSecret!!",
    }, follow_redirects=True)
    assert response.status_code == 200
    admin = User.query.filter_by(username="boss").first()
    assert admin is not None and admin.is_admin is True
    assert admin.verify_password("Sup3rSecret!!")


def test_setup_route_closes_after_first_admin(client, admin):
    assert client.get("/setup").status_code == 404
    response = client.post("/setup", data={
        "username": "sneaky", "email": "sneaky@example.com",
        "password": "AnotherPass1!", "confirm": "AnotherPass1!",
    }, follow_redirects=True)
    assert response.status_code == 404
    assert User.query.filter_by(username="sneaky").first() is None


def test_registration_creates_a_regular_user(client, admin):
    response = client.post("/register", data={
        "username": "juan", "email": "juan@example.com",
        "password": "Str0ngPassw0rd!", "confirm": "Str0ngPassw0rd!",
    }, follow_redirects=True)
    assert response.status_code == 200
    user = User.query.filter_by(username="juan").first()
    assert user and user.is_admin is False and user.is_approved is True
    assert user.api_key


def test_registration_rejects_duplicates(client, admin, user):
    response = client.post("/register", data={
        "username": "tester", "email": "new@example.com",
        "password": "Str0ngPassw0rd!", "confirm": "Str0ngPassw0rd!",
    })
    assert b"already taken" in response.data
    response = client.post("/register", data={
        "username": "unique", "email": "tester@example.com",
        "password": "Str0ngPassw0rd!", "confirm": "Str0ngPassw0rd!",
    })
    assert b"already registered" in response.data


def test_registration_rejects_short_passwords(client, admin):
    response = client.post("/register", data={
        "username": "shorty", "email": "short@example.com",
        "password": "abc", "confirm": "abc",
    })
    assert response.status_code == 200
    assert User.query.filter_by(username="shorty").first() is None


def test_registration_can_be_closed(app, client, admin):
    from app.services import settings as settings_service

    settings_service.set_setting("registration_open", False)
    response = client.get("/register", follow_redirects=True)
    assert b"Registration is currently closed" in response.data
    assert client.post("/register", data={
        "username": "nope", "email": "nope@example.com",
        "password": "Str0ngPassw0rd!", "confirm": "Str0ngPassw0rd!",
    }, follow_redirects=True).status_code == 200
    assert User.query.filter_by(username="nope").first() is None


def test_login_and_logout(client, user):
    response = login(client, "tester")
    assert response.status_code == 200
    assert b"Welcome back" in response.data

    response = client.get("/logout", follow_redirects=True)
    assert b"signed out" in response.data


def test_login_accepts_email(client, user):
    assert b"Welcome back" in login(client, "tester@example.com").data


def test_login_rejects_bad_password(client, user):
    response = login(client, "tester", "wrong-password")
    assert b"Invalid credentials" in response.data


def test_account_locks_after_repeated_failures(client, user):
    for _ in range(5):
        login(client, "tester", "nope")
    user = User.query.filter_by(username="tester").first()
    assert user.failed_logins == 5
    assert user.is_locked is True
    assert b"locked" in login(client, "tester").data


def test_successful_login_clears_failures(client, user):
    login(client, "tester", "wrong")
    login(client, "tester")
    user = User.query.filter_by(username="tester").first()
    assert user.failed_logins == 0 and user.locked_until is None


def test_unapproved_account_cannot_sign_in(app, client, admin):
    make_user(username="pending", email="pending@example.com", is_approved=False)
    response = login(client, "pending")
    assert b"awaiting administrator approval" in response.data


def test_deactivated_account_is_signed_out(app, client, user):
    """Flask-Login treats an inactive user as anonymous, so say so on login."""
    login(client, "tester")
    user.is_active = False
    db.session.commit()

    response = client.get("/dashboard")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]

    response = login(client, "tester")
    assert b"deactivated" in response.data


def test_dashboard_requires_login(client, admin):
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_admin_area_is_forbidden_for_regular_users(logged_in_client):
    assert logged_in_client.get("/admin/").status_code == 403
    assert logged_in_client.get("/admin/users").status_code == 403
    assert logged_in_client.post("/admin/run-now").status_code == 403


def test_admin_can_reach_admin_pages(admin_client):
    for path in ("/admin/", "/admin/sources", "/admin/users", "/admin/settings",
                 "/admin/updates", "/admin/system", "/admin/audit", "/admin/vehicles"):
        assert admin_client.get(path).status_code == 200, path


def test_login_is_audited(client, user):
    login(client, "tester")
    assert AuditLog.query.filter_by(action="user.login").count() == 1


def test_profile_update_and_password_change(logged_in_client, user):
    response = logged_in_client.post("/profile", data={
        "full_name": "Juan Dela Cruz", "email": "tester@example.com",
        "timezone": "Asia/Manila", "notify_email": "y",
        "notify_new_violation": "y",
    }, follow_redirects=True)
    assert b"Profile updated" in response.data
    assert User.query.get(user.id).full_name == "Juan Dela Cruz"

    response = logged_in_client.post("/profile/password", data={
        "current_password": "Str0ngPassw0rd!",
        "password": "N3wPassword!!", "confirm": "N3wPassword!!",
    }, follow_redirects=True)
    assert b"Password updated" in response.data
    assert User.query.get(user.id).verify_password("N3wPassword!!")

    response = logged_in_client.post("/profile/password", data={
        "current_password": "wrong-one",
        "password": "N3wPassword!!", "confirm": "N3wPassword!!",
    }, follow_redirects=True)
    assert b"current password is incorrect" in response.data


def test_api_key_rotation(logged_in_client, user):
    old_key = User.query.get(user.id).api_key
    logged_in_client.post("/profile/api-key")
    assert User.query.get(user.id).api_key != old_key


def test_next_parameter_is_validated(client, user):
    response = client.post("/login?next=http://evil.example.com",
                           data={"username": "tester", "password": "Str0ngPassw0rd!"})
    assert response.status_code == 302
    assert "evil.example.com" not in response.headers["Location"]
