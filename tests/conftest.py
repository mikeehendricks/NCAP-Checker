"""Shared pytest fixtures."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("NCAP_ENV", "testing")
os.environ.setdefault("NCAP_ROLE", "none")          # never start a scheduler in tests
os.environ.setdefault("NCAP_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("NCAP_PLAYWRIGHT_ENABLED", "true")

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Source, User, Vehicle  # noqa: E402
from app.utils import normalize_mv, normalize_plate  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def app(tmp_path):
    """A fresh application backed by an isolated in-memory database."""
    application = create_app("testing")
    application.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SERVER_NAME="localhost.localdomain",
        INSTANCE_DIR=tmp_path / "instance",
        DATA_DIR=tmp_path / "data",
        SCREENSHOT_DIR=tmp_path / "data" / "screenshots",
        PLAYWRIGHT_STORAGE_DIR=tmp_path / "data" / "browser-profiles",
        LOG_FILE=str(tmp_path / "ncap.log"),
        MAINTENANCE_MODE=False,
    )
    for directory in (application.config["INSTANCE_DIR"], application.config["DATA_DIR"],
                      application.config["SCREENSHOT_DIR"],
                      application.config["PLAYWRIGHT_STORAGE_DIR"]):
        directory.mkdir(parents=True, exist_ok=True)

    # The fixture keeps one app context open for the whole test, and Flask
    # reuses it for every test-client request. Flask-Login caches the loaded
    # user on `g`, which would then leak between requests (and between the
    # different clients a test uses). Drop the cache so each request resolves
    # the user from its own session cookie, exactly like in production.
    def _reset_login_cache():
        from flask import g

        g.pop("_login_user", None)

    # Must run *before* the app's own before_request handlers, so it is
    # inserted at the head of the chain rather than appended.
    application.before_request_funcs.setdefault(None, []).insert(0, _reset_login_cache)

    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def cli_runner(app):
    return app.test_cli_runner()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def make_user(username="tester", email="tester@example.com", password="Str0ngPassw0rd!",
              is_admin=False, **kwargs):
    import secrets

    kwargs.setdefault("api_key", secrets.token_hex(24))
    user = User(username=username, email=email, is_admin=is_admin, **kwargs)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def admin(app):
    return make_user(username="root", email="root@example.com", is_admin=True,
                     full_name="Root Admin")


@pytest.fixture
def user(app, admin):
    """A regular user. Depends on ``admin`` so the app is past first-run setup."""
    return make_user(username="tester", email="tester@example.com")


@pytest.fixture
def second_user(app, admin):
    return make_user(username="other", email="other@example.com")


def login(client, username="tester", password="Str0ngPassw0rd!"):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


@pytest.fixture
def logged_in_client(app, user):
    """Own test client so sessions never leak between fixtures."""
    session = app.test_client()
    login(session, user.username)
    return session


@pytest.fixture
def admin_client(app, admin):
    session = app.test_client()
    login(session, admin.username)
    return session


# ---------------------------------------------------------------------------
# Vehicles & sources
# ---------------------------------------------------------------------------
@pytest.fixture
def vehicle(app, user):
    item = Vehicle(user_id=user.id, label="Test car")
    item.set_identifiers("ABC 1234", "1234-5678901234")
    db.session.add(item)
    db.session.commit()
    return item


@pytest.fixture
def sources(app):
    """Ensure the five built-in sources exist and return them keyed by slug."""
    from app.scrapers import seed_default_sources

    seed_default_sources(update_existing=False)
    return {source.key: source for source in Source.query.all()}


@pytest.fixture
def clean_source(app):
    """A single synthetic source used by the scraper unit tests."""
    source = Source(
        key="test_portal",
        name="Test Portal",
        agency="Test LGU",
        base_url="https://portal.test",
        check_url="https://portal.test/check",
        engine="http",
        enabled=True,
        timeout_seconds=10,
        delay_seconds=0,
        config={},
    )
    db.session.add(source)
    db.session.commit()
    return source


@pytest.fixture
def freeze_time(monkeypatch):
    """Deterministic clock for scheduler tests."""
    from datetime import datetime, timedelta

    current = {"now": datetime(2026, 10, 15, 6, 0, 0)}

    def fake_utcnow():
        return current["now"]

    monkeypatch.setattr("app.utils.utcnow", fake_utcnow)
    monkeypatch.setattr("app.models.utcnow", fake_utcnow)
    monkeypatch.setattr("app.services.checker.utcnow", fake_utcnow)

    def advance(minutes: int):
        current["now"] += timedelta(minutes=minutes)
        return current["now"]

    return advance
