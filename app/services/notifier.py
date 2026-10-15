"""In-app notifications and optional e-mail alerts."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from html import escape

from ..extensions import db
from ..models import Notification, Violation
from ..utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-app notifications
# ---------------------------------------------------------------------------
def notify(user, title: str, body: str = "", kind: str = "info", vehicle=None,
           url: str = "", commit: bool = False) -> Notification | None:
    """Create an in-app notification for ``user`` (respecting preferences)."""
    if user is None:
        return None
    if kind == "violation" and not getattr(user, "notify_new_violation", True):
        return None
    entry = Notification(
        user_id=user.id,
        vehicle_id=getattr(vehicle, "id", None),
        kind=kind,
        title=title[:160],
        body=body or "",
        url=url or "",
        created_at=utcnow(),
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry


def notify_new_violation(user, vehicle, violations, settings: dict | None = None,
                         commit: bool = False) -> Notification | None:
    """Alert about one *or more* newly discovered violations on a vehicle.

    Accepts a single violation or a list: a scheduled pass can hit five portals
    and the owner should receive one summary, not five separate alerts.
    """
    if isinstance(violations, Violation):
        violations = [violations]
    violations = [v for v in (violations or []) if v is not None]
    if not violations:
        return None

    count = len(violations)
    first = violations[0]
    amount = f"₱{first.amount:,.2f}" if first.amount is not None else "amount pending"
    when = first.violation_date.strftime("%d %b %Y") if first.violation_date else "unknown date"
    plural = "s" if count > 1 else ""
    title = f"{count} new NCAP violation{plural} for {vehicle.display_name}"
    body = (f"{first.offense or 'Violation'} recorded on {when} at "
            f"{first.location or 'an undisclosed location'}. "
            f"Reference {first.reference}, {amount}.")
    if count > 1:
        ports = sorted({(v.source.name if v.source else "portal") for v in violations})
        body += f" Plus {count - 1} more across {', '.join(ports)}."

    entry = notify(user, title, body, kind="violation", vehicle=vehicle,
                   url=f"/vehicles/{vehicle.id}", commit=False)
    if entry is not None and getattr(user, "notify_email", True) and (settings or {}).get("smtp_enabled"):
        ok, message = send_violation_email(user, vehicle, violations, settings)
        entry.emailed = ok
        if not ok:
            logger.info("Violation e-mail not sent to %s: %s", user.email, message)
    if commit:
        db.session.commit()
    return entry


def notify_failure(user, vehicle, source_name: str, reason: str,
                   settings: dict | None = None) -> Notification | None:
    if not getattr(user, "notify_check_failure", False):
        return None
    return notify(user, f"{source_name} check failed",
                  f"Automatic check for {vehicle.display_name} did not complete: {reason}",
                  kind="warning", vehicle=vehicle, url=f"/vehicles/{vehicle.id}",
                  commit=False)


# ---------------------------------------------------------------------------
# E-mail
# ---------------------------------------------------------------------------
def _violation_rows(violations) -> str:
    rows = []
    for violation in violations:
        amount = f"₱{violation.amount:,.2f}" if violation.amount is not None else "-"
        when = violation.violation_date.strftime("%d %b %Y") if violation.violation_date else "-"
        rows.append(
            "<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{escape(when)}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{escape(violation.offense or '-')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{escape(violation.location or '-')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{escape(violation.reference)}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:right'>{escape(amount)}</td>"
            "</tr>"
        )
    return "".join(rows)


def render_violation_email(app_name: str, vehicle, violations) -> tuple[str, str]:
    """Render the alert e-mail for one or many new violations."""
    if isinstance(violations, Violation):
        violations = [violations]
    violations = list(violations or [])
    first = violations[0] if violations else None
    count = len(violations)
    plural = "s" if count > 1 else ""
    prefix = f"{count} new" if count > 1 else "New"
    subject = f"[{app_name}] {prefix} NCAP violation{plural}: {vehicle.display_name}"

    if count <= 1:
        amount = f"₱{first.amount:,.2f}" if first and first.amount is not None else "not yet assessed"
        when = first.violation_date.strftime("%d %B %Y") if first and first.violation_date else "-"
        text = (
            f"A new NCAP violation was found for {vehicle.display_name} "
            f"({vehicle.plate_number}).\n\n"
            f"Date: {when}\n"
            f"Offense: {first.offense if first else '-'}\n"
            f"Location: {first.location if first else '-'}\n"
            f"Reference: {first.reference if first else '-'}\n"
            f"Amount: {amount}\n"
            f"Portal: {first.source.name if first and first.source else '-'}\n"
        )
    else:
        lines = [f"{count} new NCAP violations were found for {vehicle.display_name} "
                 f"({vehicle.plate_number}):\n"]
        for violation in violations:
            amount = (f"₱{violation.amount:,.2f}" if violation.amount is not None
                      else "not yet assessed")
            when = (violation.violation_date.strftime("%d %B %Y")
                    if violation.violation_date else "-")
            lines.append(f"- {when} | {violation.offense or '-'} | "
                         f"{violation.location or '-'} | {violation.reference} | {amount} "
                         f"({violation.source.name if violation.source else '-'})")
        text = "\n".join(lines) + "\n"

    rows = _violation_rows(violations)
    html = f"""<div style="font-family:Segoe UI,Arial,sans-serif;color:#1b1f24">
      <h2 style="margin:0 0 12px">{count} new NCAP violation{plural} detected</h2>
      <p>We found new violation{plural} for <strong>{escape(vehicle.display_name)}</strong>
      ({escape(vehicle.plate_number)}).</p>
      <table style="border-collapse:collapse;font-size:14px">
        <thead><tr>
          <th style="padding:6px 10px;text-align:left;color:#666">Date</th>
          <th style="padding:6px 10px;text-align:left;color:#666">Offense</th>
          <th style="padding:6px 10px;text-align:left;color:#666">Location</th>
          <th style="padding:6px 10px;text-align:left;color:#666">Reference</th>
          <th style="padding:6px 10px;text-align:right;color:#666">Amount</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="margin-top:18px;color:#666;font-size:12px">Sent by {escape(app_name)}.
      Always confirm on the official LGU portal before paying.</p>
    </div>"""
    return subject, (text, html)


def send_email(to_address: str, subject: str, text_body: str, html_body: str = "",
               settings: dict | None = None) -> tuple[bool, str]:
    """Send one message. Returns ``(ok, detail)`` and never raises."""
    config = settings or {}
    if not config.get("enabled") or not config.get("host"):
        return False, "SMTP is not configured"
    if not to_address:
        return False, "No recipient address"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{config.get('sender_name') or 'NCAP Checker'} <{config.get('sender')}>"
    message["To"] = to_address
    message.set_content(text_body or "")
    if html_body:
        message.add_alternative(html_body, subtype="html")

    host = config.get("host")
    port = int(config.get("port") or 587)
    try:
        if config.get("ssl"):
            server = smtplib.SMTP_SSL(host, port, timeout=20,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        with server:
            server.ehlo()
            if config.get("starttls") and not config.get("ssl"):
                try:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                except smtplib.SMTPException as exc:
                    logger.warning("STARTTLS failed: %s", exc)
            if config.get("user"):
                server.login(str(config.get("user")), str(config.get("password") or ""))
            server.send_message(message)
        return True, "sent"
    except Exception as exc:  # noqa: BLE001 - surface any SMTP problem
        logger.warning("E-mail delivery failed: %s", exc)
        return False, f"{type(exc).__name__}: {exc}"[:300]


def send_violation_email(user, vehicle, violation: Violation,
                         settings: dict | None = None) -> tuple[bool, str]:
    subject, (text, html) = render_violation_email(
        (settings or {}).get("app_name", "NCAP Checker"), vehicle, violation)
    return send_email(user.email, subject, text, html, settings)


def send_test_email(to_address: str, settings: dict | None = None) -> tuple[bool, str]:
    config = settings or {}
    subject = f"[{config.get('app_name', 'NCAP Checker')}] Test message"
    text = "This is a test message from your NCAP Checker installation. Delivery works."
    html = ("<div style=\"font-family:Segoe UI,Arial,sans-serif\">"
            "<h3>Test message</h3>"
            "<p>This is a test message from your NCAP Checker installation. "
            "E-mail delivery works.</p></div>")
    return send_email(to_address, subject, text, html, config)
