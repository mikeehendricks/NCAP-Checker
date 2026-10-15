"""Application factory for NCAP Checker."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.middleware.proxy_fix import ProxyFix

from .blueprints import BLUEPRINTS
from .config import (DATA_DIR, INSTANCE_DIR, VERSION, ProductionConfig,
                      get_config)
from .extensions import csrf, db, login_manager, migrate
from .models import AuditLog, Source, User, load_user, load_user_from_api_key
from .utils import format_date, format_datetime, humanize_delta, to_local

__version__ = VERSION


def _setup_logging(app: Flask) -> None:
    level = getattr(logging, str(app.config.get("LOG_LEVEL", "INFO")).upper(), logging.INFO)
    app.logger.setLevel(level)
    if not app.logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s")
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        app.logger.addHandler(stream)
    log_file = app.config.get("LOG_FILE")
    if log_file and not app.testing:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
            handler.setLevel(level)
            app.logger.addHandler(handler)
        except OSError:  # pragma: no cover - read-only filesystem
            app.logger.warning("Could not open log file %s", log_file)


def _register_template_helpers(app: Flask) -> None:
    @app.template_filter("localtime")
    def _localtime(value, fmt: str = "%d %b %Y, %I:%M %p"):
        return format_datetime(value, app.config.get("TIMEZONE", "Asia/Manila"), fmt)

    @app.template_filter("shortdate")
    def _shortdate(value):
        return format_date(value)

    @app.template_filter("humanize")
    def _humanize(value):
        return humanize_delta(value)

    @app.template_filter("money")
    def _money(value):
        if value is None:
            return "-"
        return f"₱{float(value):,.2f}"

    @app.template_filter("status_class")
    def _status_class(value: str) -> str:
        return {
            "clean": "success",
            "found": "danger",
            "blocked": "warning",
            "error": "secondary",
            "unknown": "info",
            "skipped": "light",
        }.get(value, "secondary")

    @app.context_processor
    def _helpers():
        return {
            "utc_now": __import__("datetime").datetime.utcnow,
            "to_local": lambda v: to_local(v, app.config.get("TIMEZONE", "Asia/Manila")),
        }


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(_error):
        db.session.rollback()
        return render_template("errors/500.html"), 500


def _register_cli(app: Flask) -> None:
    import click

    @app.cli.command("init-db")
    def init_db():
        """Create tables, defaults and the five built-in portals."""
        from .scrapers import seed_default_sources
        from .services import settings as settings_service

        db.create_all()
        settings_service.ensure_defaults()
        created = seed_default_sources(update_existing=False)
        click.echo(f"Database ready. Sources added: {created}")

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.argument("email")
    @click.password_option()
    def create_admin(username: str, email: str, password: str):
        """Create (or promote) an administrator account."""
        import secrets

        user = User.query.filter_by(username=username.lower()).first()
        if user is None:
            user = User(username=username.lower(), email=email.lower(),
                        is_admin=True, is_active=True, is_approved=True,
                        api_key=secrets.token_hex(24))
            db.session.add(user)
        user.is_admin = True
        user.is_approved = True
        user.set_password(password)
        db.session.commit()
        click.echo(f"Administrator ready: {user.username}")

    @app.cli.command("check-all")
    @click.option("--force", is_flag=True, help="Check every vehicle, not just due ones.")
    def check_all(force: bool):
        """Run a check pass now (the same work the scheduler does)."""
        from .services.checker import run_check_for_vehicle, run_checks_for_all_due
        from .services.settings import get_settings

        if force:
            results = {"vehicles": 0, "new": 0, "found": 0, "sources": 0}
            for vehicle in _all_vehicles():
                summary = run_check_for_vehicle(vehicle, triggered_by="cli")
                results["vehicles"] += 1
                results["new"] += summary["new"]
                results["found"] += summary["found"]
                results["sources"] += summary["sources"]
        else:
            results = run_checks_for_all_due(triggered_by="cli")
        click.echo(str(results))

    @app.cli.command("check-vehicle")
    @click.argument("plate")
    def check_vehicle(plate: str):
        """Check one vehicle by plate number."""
        from .services.checker import run_check_for_vehicle

        from .models import Vehicle

        vehicle = Vehicle.query.filter_by(plate_norm=plate.upper().replace(" ", "")).first()
        if vehicle is None:
            raise click.ClickException(f"No vehicle with plate {plate}")
        summary = run_check_for_vehicle(vehicle, triggered_by="cli")
        click.echo(str({k: v for k, v in summary.items() if k != "new_violations"}))

    @app.cli.command("seed-sources")
    def seed_sources_cmd():
        from .scrapers import seed_default_sources

        created = seed_default_sources(update_existing=False)
        click.echo(f"Sources added: {created}")

    def _all_vehicles():
        from .models import Vehicle

        return Vehicle.query.filter_by(is_active=True).all()


def create_app(config_name: str | None = None) -> Flask:
    """Build and configure the Flask application."""
    from .config import _load_dotenv

    _load_dotenv()
    config_obj = get_config(config_name)
    app = Flask(__name__, instance_path=str(INSTANCE_DIR),
                template_folder="templates", static_folder="static")
    app.config.from_object(config_obj)
    app.config["VERSION"] = VERSION
    app.config.setdefault("JSON_SORT_KEYS", False)

    for directory in (INSTANCE_DIR, DATA_DIR, config_obj.SCREENSHOT_DIR,
                      config_obj.PLAYWRIGHT_STORAGE_DIR):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:  # pragma: no cover
            pass

    _setup_logging(app)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.user_loader(load_user)
    login_manager.request_loader(load_user_from_api_key)

    @login_manager.unauthorized_handler
    def _unauthorized():
        if current_user.is_authenticated:
            flash("You do not have permission to open that page.", "warning")
            return redirect(url_for("main.dashboard"))
        flash("Please sign in to continue.", "info")
        return redirect(url_for("auth.login", next=request.path))

    csrf.init_app(app)
    migrate.init_app(app, db)

    # Trust X-Forwarded-* when running behind the nginx reverse proxy.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)

    _register_template_helpers(app)
    _register_error_handlers(app)
    _register_cli(app)

    @app.before_request
    def _first_run_and_maintenance():
        from .services import settings as settings_service

        endpoint = request.endpoint or ""
        public_endpoints = {"main.help", "main.about", "auth.login", "auth.register"}
        if endpoint.startswith(("static", "auth.setup", "api.")) or endpoint in public_endpoints:
            return None
        if app.config.get("MAINTENANCE_MODE") or (
                _db_ready() and settings_service.get_setting("maintenance_mode", False)):
            if current_user.is_authenticated and current_user.is_admin:
                return None
            return render_template("errors/maintenance.html"), 503
        if _db_ready() and User.query.filter_by(is_admin=True).count() == 0:
            return redirect(url_for("auth.setup"))
        return None

    def _db_ready() -> bool:
        try:
            return bool(Source.query.limit(1).all() is not None)
        except Exception:
            db.session.rollback()
            return False

    with app.app_context():
        db.create_all()
        try:
            from .services import settings as settings_service

            settings_service.ensure_defaults()
            from .scrapers import seed_default_sources

            seed_default_sources(update_existing=False)
        except Exception as exc:  # pragma: no cover - DB not reachable yet
            app.logger.warning("Bootstrap step skipped: %s", exc)

        if isinstance(config_obj, ProductionConfig) and not app.config.get(
                "SESSION_COOKIE_SECURE", False):
            app.logger.warning(
                "Session cookies are not marked Secure. Set NCAP_SESSION_SECURE=true "
                "in .env (and restart) once the site is served over HTTPS."
            )

        if app.config.get("ENABLE_SCHEDULER", False):
            try:
                from .services.scheduler import init_scheduler

                init_scheduler(app)
            except Exception as exc:  # pragma: no cover
                app.logger.warning("Scheduler not started: %s", exc)

    return app
