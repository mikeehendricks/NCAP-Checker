"""E-mail alerts and in-app notifications."""
from __future__ import annotations

import smtplib
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import Notification, Violation
from app.services import notifier
from app.services.settings import as_smtp_config


class FakeSMTP:
    """Records messages instead of talking to a mail server."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host=None, port=None, timeout=None, context=None, **kwargs):
        self.host, self.port = host, port
        self.sent: list = []
        self.logged_in = None
        self.starttls_called = False
        self.closed = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        return False

    def ehlo(self, *args, **kwargs):
        return (250, b"ok")

    def starttls(self, *args, **kwargs):
        self.starttls_called = True
        return (220, b"ready")

    def login(self, user, password):
        self.logged_in = (user, password)
        return (235, b"ok")

    def send_message(self, message, *args, **kwargs):
        self.sent.append(message)
        return {}

    def quit(self):
        return (221, b"bye")


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def build_violation(vehicle, sources, ticket="T-1", offense="Speeding"):
    violation = Violation(vehicle_id=vehicle.id, source_id=sources["quezon_city"].id,
                          fingerprint=f"fp-{ticket}", ticket_no=ticket, offense=offense,
                          location="EDSA", plate="ABC1234", amount=Decimal("2000"),
                          status="Unpaid")
    db.session.add(violation)
    db.session.commit()
    return violation


def test_send_email_uses_starttls_and_login(app, smtp):
    ok, detail = notifier.send_email(
        "to@example.com", "Subject", "body", "<p>body</p>",
        {"enabled": True, "host": "smtp.example.com", "port": 587, "user": "u",
         "password": "p", "starttls": True, "ssl": False, "sender": "from@example.com",
         "sender_name": "NCAP Checker"})
    assert ok is True, detail
    instance = FakeSMTP.instances[0]
    assert instance.host == "smtp.example.com" and instance.port == 587
    assert instance.starttls_called is True
    assert instance.logged_in == ("u", "p")
    assert len(instance.sent) == 1
    assert instance.sent[0]["To"] == "to@example.com"
    assert "NCAP Checker" in instance.sent[0]["From"]


def test_send_email_without_configuration_is_a_noop(app):
    ok, detail = notifier.send_email("to@example.com", "s", "b", "", {"enabled": False})
    assert ok is False and "not configured" in detail


def test_smtp_failure_is_reported_not_raised(app, monkeypatch):
    def boom(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"bad creds")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    ok, detail = notifier.send_email("to@example.com", "s", "b", "",
                                     {"enabled": True, "host": "h", "port": 25})
    assert ok is False and "SMTP" in detail


def test_violation_email_lists_every_new_violation(app, vehicle, sources, smtp):
    first = build_violation(vehicle, sources, "T-1", "Speeding")
    second = build_violation(vehicle, sources, "T-2", "Illegal Parking")
    subject, (text, html) = notifier.render_violation_email(
        "NCAP Checker", vehicle, [first, second])
    assert "2 new NCAP violations" in subject
    assert "T-1" in html and "T-2" in html
    assert "T-1" in text and "T-2" in text

    ok, detail = notifier.send_violation_email(
        vehicle.owner, vehicle, [first, second],
        {"enabled": True, "host": "smtp.example.com", "port": 587,
         "sender": "ncap@example.com", "sender_name": "NCAP Checker"})
    assert ok is True


def test_new_violation_creates_in_app_notification(app, vehicle, sources):
    violation = build_violation(vehicle, sources)
    entry = notifier.notify_new_violation(vehicle.owner, vehicle, [violation],
                                          settings={"smtp_enabled": False})
    assert entry is not None
    assert entry.kind == "violation"
    assert "1 new NCAP violation" in entry.title
    assert "Speeding" in entry.body
    assert entry.url == f"/vehicles/{vehicle.id}"


def test_single_violation_argument_still_works(app, vehicle, sources):
    violation = build_violation(vehicle, sources)
    entry = notifier.notify_new_violation(vehicle.owner, vehicle, violation,
                                          settings={"smtp_enabled": False})
    assert "1 new NCAP violation for" in entry.title


def test_preferences_can_suppress_alerts(app, vehicle, sources):
    vehicle.owner.notify_new_violation = False
    db.session.commit()
    violation = build_violation(vehicle, sources)
    assert notifier.notify_new_violation(vehicle.owner, vehicle, [violation]) is None
    assert Notification.query.count() == 0


def test_email_is_not_sent_when_smtp_is_off(app, vehicle, sources, smtp):
    violation = build_violation(vehicle, sources)
    notifier.notify_new_violation(vehicle.owner, vehicle, [violation],
                                  settings={"smtp_enabled": False})
    assert FakeSMTP.instances == []


def test_test_email_helper(app, smtp):
    ok, detail = notifier.send_test_email(
        "me@example.com", {"enabled": True, "host": "smtp.example.com", "port": 587,
                           "sender": "ncap@example.com", "sender_name": "NCAP Checker",
                           "app_name": "NCAP Checker"})
    assert ok is True
    assert "Test message" in FakeSMTP.instances[0].sent[0]["Subject"]


def test_failure_notifications_respect_preferences(app, vehicle):
    assert notifier.notify_failure(vehicle.owner, vehicle, "QC", "boom") is None
    vehicle.owner.notify_check_failure = True
    db.session.commit()
    entry = notifier.notify_failure(vehicle.owner, vehicle, "QC", "boom")
    assert entry is not None and "failed" in entry.title


def test_smtp_config_is_built_from_settings(app):
    from app.services import settings as settings_service

    settings_service.set_setting("smtp_enabled", True)
    settings_service.set_setting("smtp_host", "mail.example.com")
    settings_service.set_setting("smtp_password", "secret")
    config = as_smtp_config()
    assert config["enabled"] is True
    assert config["host"] == "mail.example.com"
    assert config["password"] == "secret"
