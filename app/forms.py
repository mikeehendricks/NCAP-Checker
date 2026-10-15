"""WTForms definitions."""
from __future__ import annotations

import re

from flask_wtf import FlaskForm
from wtforms import (BooleanField, EmailField, IntegerField, PasswordField,
                     SelectField, StringField, SubmitField, TextAreaField)
from wtforms.validators import (DataRequired, Email, EqualTo, InputRequired, Length,
                                Optional, Regexp, ValidationError)

from .models import User
from .utils import is_valid_mv, is_valid_plate

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


class UniqueUserMixin:
    """Reject usernames/e-mails that already exist."""

    def validate_username(self, field) -> None:
        query = User.query.filter_by(username=field.data.strip().lower())
        if getattr(self, "current_user_id", None):
            query = query.filter(User.id != self.current_user_id)
        if query.first():
            raise ValidationError("That username is already taken.")

    def validate_email(self, field) -> None:
        query = User.query.filter_by(email=(field.data or "").strip().lower())
        if getattr(self, "current_user_id", None):
            query = query.filter(User.id != self.current_user_id)
        if query.first():
            raise ValidationError("That e-mail address is already registered.")


class SetupForm(UniqueUserMixin, FlaskForm):
    """One-time administrator registration."""

    username = StringField("Username", validators=[
        DataRequired(), Length(min=3, max=32),
        Regexp(USERNAME_RE.message if hasattr(USERNAME_RE, "message") else r"^[A-Za-z0-9_.-]{3,32}$",
               message="Use 3-32 characters: letters, numbers, dot, dash or underscore.")])
    email = EmailField("E-mail", validators=[DataRequired(), Email(), Length(max=255)])
    full_name = StringField("Full name", validators=[Optional(), Length(max=128)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=10, max=128)])
    confirm = PasswordField("Confirm password", validators=[
        DataRequired(), EqualTo("password", message="Passwords must match.")])
    submit = SubmitField("Create administrator")


class RegistrationForm(FlaskForm):
    username = StringField("Username", validators=[
        DataRequired(), Length(min=3, max=32),
        Regexp(r"^[A-Za-z0-9_.-]{3,32}$",
               message="Use 3-32 characters: letters, numbers, dot, dash or underscore.")])
    email = EmailField("E-mail", validators=[DataRequired(), Email(), Length(max=255)])
    full_name = StringField("Full name", validators=[Optional(), Length(max=128)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=10, max=128)])
    confirm = PasswordField("Confirm password", validators=[
        DataRequired(), EqualTo("password", message="Passwords must match.")])
    submit = SubmitField("Create account")

    def validate_username(self, field) -> None:
        if User.query.filter_by(username=field.data.strip().lower()).first():
            raise ValidationError("That username is already taken.")

    def validate_email(self, field) -> None:
        if User.query.filter_by(email=(field.data or "").strip().lower()).first():
            raise ValidationError("That e-mail address is already registered.")


class LoginForm(FlaskForm):
    username = StringField("Username or e-mail", validators=[DataRequired(), Length(max=255)])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember me")
    submit = SubmitField("Sign in")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired()])
    password = PasswordField("New password", validators=[DataRequired(), Length(min=10, max=128)])
    confirm = PasswordField("Confirm new password", validators=[
        DataRequired(), EqualTo("password", message="Passwords must match.")])
    submit = SubmitField("Update password")


class ProfileForm(FlaskForm):
    full_name = StringField("Full name", validators=[Optional(), Length(max=128)])
    email = EmailField("E-mail", validators=[DataRequired(), Email(), Length(max=255)])
    timezone = StringField("Time zone", validators=[Optional(), Length(max=64)])
    notify_email = BooleanField("E-mail me about new violations")
    notify_new_violation = BooleanField("In-app alert for new violations")
    notify_check_failure = BooleanField("Warn me when a portal check fails")
    submit = SubmitField("Save profile")

    def __init__(self, *args, current_user_id: int | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_user_id = current_user_id

    def validate_email(self, field) -> None:
        query = User.query.filter_by(email=(field.data or "").strip().lower())
        if self.current_user_id:
            query = query.filter(User.id != self.current_user_id)
        if query.first():
            raise ValidationError("That e-mail address is already registered.")


class VehicleForm(FlaskForm):
    label = StringField("Nickname", validators=[Optional(), Length(max=128)],
                        description="Optional, e.g. 'Family car'")
    plate_number = StringField("Plate number", validators=[DataRequired(), Length(max=32)])
    mv_number = StringField("MV file number", validators=[DataRequired(), Length(max=64)],
                            description="As printed on your LTO Certificate of Registration")
    interval_minutes = IntegerField("Check every (minutes)", validators=[Optional()])
    is_active = BooleanField("Monitoring enabled", default=True)
    submit = SubmitField("Save vehicle")

    def validate_plate_number(self, field) -> None:
        if not is_valid_plate(field.data):
            raise ValidationError(
                "Enter a plate number with letters and digits, e.g. ABC1234.")

    def validate_mv_number(self, field) -> None:
        if not is_valid_mv(field.data):
            raise ValidationError(
                "The MV file number should be 6-20 digits, e.g. 1234-1234567890.")

    def validate_interval_minutes(self, field) -> None:
        if field.data is not None and field.data and field.data < 60:
            raise ValidationError("Use at least 60 minutes to stay polite to the portals.")


class SourceForm(FlaskForm):
    name = StringField("Display name", validators=[DataRequired(), Length(max=128)])
    agency = StringField("Agency", validators=[Optional(), Length(max=128)])
    base_url = StringField("Base URL", validators=[DataRequired(), Length(max=512)])
    check_url = StringField("Check URL", validators=[DataRequired(), Length(max=512)])
    engine = SelectField("Engine", choices=[("auto", "Auto (browser, then HTTP)"),
                                            ("browser", "Browser (Playwright)"),
                                            ("http", "HTTP only")])
    enabled = BooleanField("Enabled")
    timeout_seconds = IntegerField("Timeout (seconds)", validators=[InputRequired()])
    delay_seconds = IntegerField("Delay between hits (seconds)", validators=[InputRequired()])
    config = TextAreaField("Advanced config (JSON)", validators=[Optional()])
    submit = SubmitField("Save source")

    def validate_config(self, field) -> None:
        import json

        if not (field.data or "").strip():
            return
        try:
            parsed = json.loads(field.data)
        except ValueError as exc:
            raise ValidationError(f"Invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValidationError("Config must be a JSON object.")
