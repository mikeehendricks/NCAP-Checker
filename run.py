#!/usr/bin/env python3
"""Development server: ``python run.py`` (use gunicorn in production)."""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(__import__("os").environ.get("PORT", 8000)),
            debug=app.config.get("DEBUG", False))
