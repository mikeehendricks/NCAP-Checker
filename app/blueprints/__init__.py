"""Blueprint registration helper."""
from .admin import admin_bp
from .api import api_bp
from .auth import auth_bp
from .main import main_bp
from .vehicles import vehicles_bp

BLUEPRINTS = (auth_bp, main_bp, vehicles_bp, admin_bp, api_bp)
