"""WSGI entry point used by gunicorn / the systemd unit."""
from app import create_app

app = create_app()

if __name__ == "__main__":  # pragma: no cover - manual smoke test
    app.run(host="0.0.0.0", port=8000)
