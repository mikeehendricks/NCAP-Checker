"""Route decorators."""
from __future__ import annotations

from functools import wraps

from flask import abort, flash, redirect, url_for
from flask_login import current_user


def admin_required(func):
    """Allow administrators only."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=__import__("flask").request.path))
        if not current_user.is_admin:
            abort(403)
        return func(*args, **kwargs)

    return wrapper


def active_required(func):
    """Block deactivated accounts."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        if current_user.is_authenticated and not current_user.is_active:
            flash("This account is deactivated. Contact an administrator.", "danger")
            return redirect(url_for("auth.logout"))
        return func(*args, **kwargs)

    return wrapper


def approved_required(func):
    """Block accounts awaiting administrator approval."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        if current_user.is_authenticated and not current_user.is_approved:
            flash("Your account is still waiting for administrator approval.", "warning")
            return redirect(url_for("main.pending_approval"))
        return func(*args, **kwargs)

    return wrapper
