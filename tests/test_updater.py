"""Self-update helpers, exercised against throwaway local git repositories."""
from __future__ import annotations

import subprocess

import pytest

from app.services import updater


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Point the updater at a cloned copy of a throwaway local repository."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(["init", "--bare", "-b", "main"], origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "README.md").write_text("one\n", encoding="utf-8")
    git(["init", "-b", "main"], seed)
    git(["config", "user.email", "dev@example.com"], seed)
    git(["config", "user.name", "Dev"], seed)
    git(["add", "."], seed)
    git(["commit", "-m", "first"], seed)
    git(["remote", "add", "origin", str(origin)], seed)
    git(["push", "-u", "origin", "main"], seed)

    clone = tmp_path / "app"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True,
                   capture_output=True, text=True)
    git(["config", "user.email", "dev@example.com"], clone)
    git(["config", "user.name", "Dev"], clone)

    monkeypatch.setattr(updater, "BASE_DIR", clone)
    monkeypatch.setattr(updater, "UPDATE_MODE", "git")
    return {"origin": origin, "seed": seed, "clone": clone}


def test_git_info_reports_the_current_commit(repo):
    info = updater.git_info()
    assert info["is_repo"] is True
    assert info["branch"] == "main"
    assert len(info["commit"]) == 40
    assert info["commit_short"]
    assert info["dirty"] is False


def test_git_info_detects_local_modifications(repo):
    (repo["clone"] / "README.md").write_text("changed\n", encoding="utf-8")
    assert updater.git_info()["dirty"] is True


def test_no_updates_when_up_to_date(repo):
    result = updater.check_for_updates()
    assert result["error"] == ""
    assert result["available"] is False
    assert result["behind"] == 0


def test_detects_and_applies_an_incoming_commit(repo):
    (repo["seed"] / "feature.txt").write_text("new\n", encoding="utf-8")
    git(["add", "."], repo["seed"])
    git(["commit", "-m", "second commit"], repo["seed"])
    git(["push", "origin", "main"], repo["seed"])

    pending = updater.check_for_updates()
    assert pending["available"] is True
    assert pending["behind"] == 1
    assert any("second commit" in line for line in pending["commits"])

    result = updater.perform_update(restart=False)
    assert result["ok"] is True
    names = [name for name, _ok, _out in result["steps"]]
    assert any("git pull" in name for name in names)
    assert (repo["clone"] / "feature.txt").is_file()


def test_update_is_refused_outside_a_git_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "BASE_DIR", tmp_path)
    result = updater.perform_update(restart=False)
    assert result["ok"] is False
    assert "Not a git clone" in result["steps"][0][2]


def test_updates_can_be_disabled(repo, monkeypatch):
    monkeypatch.setattr(updater, "UPDATE_MODE", "disabled")
    result = updater.check_for_updates()
    assert "disabled" in result["error"]
    assert updater.perform_update(restart=False)["ok"] is False


def test_restart_reports_failure_without_sudo(repo, monkeypatch):
    monkeypatch.setattr(updater, "_run", lambda *a, **k: (1, "sudo: a password is required"))
    result = updater.restart_services()
    assert result["ok"] is False
    assert "password" in result["detail"]


def test_service_status_is_best_effort():
    statuses = updater.service_status()
    assert len(statuses) == 2
    assert {"ncap-web", "ncap-scheduler"} == {item["name"] for item in statuses}
