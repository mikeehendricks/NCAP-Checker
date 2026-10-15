"""Authentication and account management."""
from __future__ import annotations

import secrets

from flask import (Blueprint, abort, current_app, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func as sqla_func

from ..extensions import db
from ..forms import (ChangePasswordForm, LoginForm, ProfileForm, RegistrationForm,
                     SetupForm)
from ..models import AuditLog, User
from ..services import settings as settings_service
from ..utils import utcnow

auth_bp = Blueprint("auth", __name__, url_prefix="")


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return (request.remote_addr or "")[:64]


@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    """One-time administrator registration.

    Available only while the installation has no administrator; afterwards the
    route returns 404 so it cannot be used to grab a second admin account.
    """
    if User.query.filter_by(is_admin=True).count() > 0:
        abort(404)
    form = SetupForm()
    if form.validate_on_submit():
        admin = User(
            username=form.username.data.strip().lower(),
            email=form.email.data.strip().lower(),
            full_name=(form.full_name.data or "").strip(),
            is_admin=True,
            is_active=True,
            is_approved=True,
            api_key=secrets.token_hex(24),
        )
        admin.set_password(form.password.data)
        db.session.add(admin)
        db.session.flush()
        AuditLog.record("admin.created", target=admin.username,
                        detail="Initial administrator registered", user=admin,
                        ip_address=_client_ip())
        db.session.commit()
        flash("Administrator account created. Please sign in.", "success")
        current_app.logger.info("Administrator account created: %s", admin.username)
        return redirect(url_for("auth.login"))
    return render_template("auth/setup.html", form=form)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if not settings_service.get_setting("registration_open", True):
        flash("Registration is currently closed. Contact an administrator.", "warning")
        return redirect(url_for("auth.login"))

    form = RegistrationForm()
    needs_approval = settings_service.get_setting("registration_requires_approval", False)
    if form.validate_on_submit():
        user = User(
            username=form.username.data.strip().lower(),
            email=form.email.data.strip().lower(),
            full_name=(form.full_name.data or "").strip(),
            is_admin=False,
            is_approved=not needs_approval,
            api_key=secrets.token_hex(24),
        )
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.flush()
        AuditLog.record("user.registered", target=user.username, user=user,
                        ip_address=_client_ip())
        db.session.commit()
        if needs_approval:
            flash("Account created. An administrator must approve it before you can sign in.",
                  "info")
        else:
            flash("Account created. You can now sign in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/register.html", form=form, needs_approval=needs_approval)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        identifier = (form.username.data or "").strip().lower()
        user = (User.query.filter(sqla_func.lower(User.username) == identifier).first()
                or User.query.filter(sqla_func.lower(User.email) == identifier).first())
        if user is None:
            flash("Invalid credentials.", "danger")
            return render_template("auth/login.html", form=form)

        if user.is_locked:
            flash("Too many failed attempts. This account is locked for a few minutes.",
                  "danger")
            return render_template("auth/login.html", form=form)

        if not user.is_active:
            # Flask-Login treats an inactive user as anonymous, so say so here
            # instead of failing silently with "invalid credentials".
            flash("This account has been deactivated. Contact an administrator.",
                  "warning")
            return render_template("auth/login.html", form=form)

        if not user.verify_password(form.password.data):
            user.register_failure()
            db.session.commit()
            remaining = max(0, 5 - (user.failed_logins or 0))
            flash(f"Invalid credentials. {remaining} attempt(s) left before lockout.",
                  "danger")
            return render_template("auth/login.html", form=form)

        if not user.is_approved:
            flash("Your account is awaiting administrator approval.", "warning")
            return render_template("auth/login.html", form=form)

        user.clear_failures()
        user.last_login_at = utcnow()
        db.session.commit()
        login_user(user, remember=bool(form.remember.data))
        AuditLog.record("user.login", target=user.username, user=user,
                        ip_address=_client_ip())
        db.session.commit()
        flash(f"Welcome back, {user.full_name or user.username}!", "success")
        next_url = request.args.get("next")
        if next_url and next_url.startswith("/"):
            return redirect(next_url)
        return redirect(url_for("main.dashboard"))
    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout")
@login_required
def logout():
    AuditLog.record("user.logout", target=current_user.username, user=current_user,
                    ip_address=_client_ip())
    db.session.commit()
    logout_user()          # clears the session, so flash afterwards
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    form = ProfileForm(current_user_id=current_user.id,
                       obj=current_user)
    password_form = ChangePasswordForm()
    if form.validate_on_submit():
        current_user.full_name = (form.full_name.data or "").strip()
        current_user.email = (form.email.data or "").strip().lower()
        current_user.timezone = (form.timezone.data or "").strip() or "Asia/Manila"
        current_user.notify_email = bool(form.notify_email.data)
        current_user.notify_new_violation = bool(form.notify_new_violation.data)
        current_user.notify_check_failure = bool(form.notify_check_failure.data)
        AuditLog.record("user.profile_updated", target=current_user.username,
                        user=current_user, ip_address=_client_ip())
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("auth.profile"))
    return render_template("auth/profile.html", form=form, password_form=password_form)


@auth_bp.route("/profile/password", methods=["POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.verify_password(form.current_password.data):
            flash("Your current password is incorrect.", "danger")
            return redirect(url_for("auth.profile"))
        current_user.set_password(form.password.data)
        AuditLog.record("user.password_changed", target=current_user.username,
                        user=current_user, ip_address=_client_ip())
        db.session.commit()
        flash("Password updated.", "success")
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"{getattr(form, field).label.text}: {error}", "danger")
    return redirect(url_for("auth.profile"))


@auth_bp.route("/profile/api-key", methods=["POST"])
@login_required
def rotate_api_key():
    current_user.api_key = secrets.token_hex(24)
    db.session.commit()
    flash("API key rotated.", "success")
    return redirect(url_for("auth.profile"))
