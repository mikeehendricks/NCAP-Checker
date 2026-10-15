"""Flask extensions instantiated once and bound in :func:`app.create_app`."""
from __future__ import annotations

from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"
login_manager.session_protection = "basic"

csrf = CSRFProtect()
migrate = Migrate()
