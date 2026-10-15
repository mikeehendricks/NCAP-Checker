"""Self-update support for the admin panel ("update the app" button).

The application is installed by cloning this repository, so updating means:
``git fetch`` -> ``git pull --ff-only`` -> ``pip install -r requirements.txt``
-> reload schema -> restart the systemd services.  Everything runs through
:func:`subprocess.run` with argument lists (no shell), is logged, and each step
is reported back to the admin UI.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from .. import config as app_config

logger = logging.getLogger(__name__)

BASE_DIR = Path(app_config.BASE_DIR)
UPDATE_MODE = os.environ.get("NCAP_UPDATE_MODE", app_config.Config.UPDATE_MODE)


def _run(args: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            args, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError:
        return 127, f"command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(args)}"
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode, output.strip()


def _git(*args: str, timeout: int = 60) -> tuple[int, str]:
    return _run(["git", *args], timeout=timeout)


def is_git_repo() -> bool:
    return (BASE_DIR / ".git").is_dir()


def git_info() -> dict:
    """Current commit, branch and remote for the "About / Updates" screen."""
    info = {
        "is_repo": is_git_repo(),
        "branch": "",
        "commit": "",
        "commit_short": "",
        "date": "",
        "subject": "",
        "dirty": False,
        "remote_url": "",
        "version": app_config.VERSION,
    }
    if not info["is_repo"]:
        return info
    code, out = _git("rev-parse", "--abbrev-ref", "HEAD")
    if code == 0:
        info["branch"] = out.strip()
    code, out = _git("log", "-1", "--format=%H|%h|%cI|%s")
    if code == 0 and "|" in out:
        commit, short, date, subject = out.split("|", 3)
        info.update(commit=commit, commit_short=short, date=date, subject=subject)
    code, out = _git("status", "--porcelain")
    info["dirty"] = bool(code == 0 and out.strip())
    code, out = _git("config", "--get", f"remote.{app_config.Config.GIT_REMOTE}.url")
    if code == 0:
        info["remote_url"] = out.strip()
    return info


def check_for_updates() -> dict:
    """Fetch the remote and report how far behind we are."""
    result = {"available": False, "behind": 0, "ahead": 0, "error": "",
              "local": "", "remote": "", "branch": "", "commits": []}
    if UPDATE_MODE == "disabled":
        result["error"] = "Updates are disabled for this installation."
        return result
    if not is_git_repo():
        result["error"] = "This installation is not a git clone; update manually."
        return result

    info = git_info()
    result["branch"] = info.get("branch", "")
    result["local"] = info.get("commit_short", "")
    remote = app_config.Config.GIT_REMOTE
    branch = app_config.Config.GIT_BRANCH or info.get("branch") or "main"

    code, out = _git("fetch", remote, "--tags", timeout=120)
    if code != 0:
        result["error"] = out[:300] or "git fetch failed"
        return result

    code, out = _git("rev-list", "--count", f"HEAD..{remote}/{branch}")
    result["behind"] = int(out.strip()) if code == 0 and out.strip().isdigit() else 0
    code, out = _git("rev-list", "--count", f"{remote}/{branch}..HEAD")
    result["ahead"] = int(out.strip()) if code == 0 and out.strip().isdigit() else 0
    result["available"] = result["behind"] > 0

    if result["behind"]:
        code, out = _git("log", "--oneline", "-n", "10", f"HEAD..{remote}/{branch}")
        result["commits"] = out.splitlines() if code == 0 else []
    return result


def perform_update(restart: bool = True) -> dict:
    """Pull, install dependencies, migrate and restart. Returns a step log."""
    if UPDATE_MODE == "disabled":
        return {"ok": False, "steps": [("update", False, "Updates are disabled")]}
    if not is_git_repo():
        return {"ok": False, "steps": [("update", False, "Not a git clone")]}

    steps: list[tuple[str, bool, str]] = []
    remote = app_config.Config.GIT_REMOTE
    branch = app_config.Config.GIT_BRANCH or git_info().get("branch") or "main"

    code, out = _git("fetch", remote, "--tags", timeout=180)
    steps.append(("git fetch", code == 0, out[:400]))
    if code != 0:
        return {"ok": False, "steps": steps}

    code, out = _git("pull", "--ff-only", remote, branch, timeout=300)
    steps.append((f"git pull {remote}/{branch}", code == 0, out[:600]))
    if code != 0:
        return {"ok": False, "steps": steps}

    requirements = BASE_DIR / "requirements.txt"
    if requirements.is_file():
        code, out = _run([sys.executable, "-m", "pip", "install", "-q", "-r",
                          str(requirements)], timeout=900)
        steps.append(("pip install -r requirements.txt", code == 0, out[:600]))
    else:  # pragma: no cover - requirements always ships
        steps.append(("pip install -r requirements.txt", True, "no requirements.txt"))

    ok = all(success for _name, success, _out in steps)
    restart_result = None
    if restart and ok:
        restart_result = restart_services()
        steps.append(("restart services", restart_result.get("ok", False),
                      restart_result.get("detail", "")))
    return {"ok": ok, "steps": steps, "restart": restart_result}


def restart_services() -> dict:
    """Ask systemd to restart the web/scheduler units (needs passwordless sudo)."""
    services = [app_config.Config.SERVICE_WEB, app_config.Config.SERVICE_SCHEDULER]
    results = []
    ok = False
    for service in services:
        code, out = _run(["sudo", "-n", "systemctl", "restart", service], timeout=60)
        results.append((service, code == 0, out[:200]))
        if code == 0:
            ok = True
    if not ok:
        code, out = _run(["systemctl", "--user", "restart", service], timeout=60)
        if code == 0:
            ok = True
            results.append(("user-units", True, out[:200]))
    return {
        "ok": ok,
        "results": results,
        "detail": "; ".join(f"{name}: {'ok' if success else out}"
                            for name, success, out in results)[:400],
    }


def service_status() -> list[dict]:
    """``systemctl is-active`` for the two units (best effort)."""
    services = [app_config.Config.SERVICE_WEB, app_config.Config.SERVICE_SCHEDULER]
    status = []
    for service in services:
        code, out = _run(["systemctl", "is-active", service], timeout=15)
        status.append({"name": service, "active": code == 0 and out.strip() == "active",
                       "state": out.strip() or ("unknown" if code else "inactive")})
    return status
