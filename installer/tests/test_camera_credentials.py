from __future__ import annotations

import pathlib
import stat
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mv3dt_installer import camera_credentials, privilege  # noqa: E402


def _user(tmp_path):
    return privilege.InvokingUser(
        name="alice", home=tmp_path / "home", uid=1001, gid=1001
    )


def test_capture_hides_password_and_retries_blank(monkeypatch):
    usernames = iter(["", "admin"])
    passwords = iter(["", "camera-secret"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(usernames))
    monkeypatch.setattr(
        camera_credentials.getpass, "getpass", lambda _prompt: next(passwords)
    )

    assert camera_credentials.capture_credentials(False) == camera_credentials.Credentials(
        username="admin", password="camera-secret"
    )


def test_non_interactive_capture_fails_without_environment():
    with pytest.raises(SystemExit):
        camera_credentials.capture_credentials(True)


def test_store_and_load_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(camera_credentials.privilege, "resolve", lambda: _user(tmp_path))
    monkeypatch.setattr(camera_credentials.os, "chown", lambda *args: None)
    creds = camera_credentials.Credentials("admin", "camera=secret")

    path = camera_credentials.store_credentials(creds, tmp_path / "install")

    assert camera_credentials.load_credentials(tmp_path / "install") == creds
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_load_rejects_incomplete_file(tmp_path):
    path = tmp_path / "install" / "secrets" / "camera.env"
    path.parent.mkdir(parents=True)
    path.write_text("CAM_USER=admin\n", encoding="utf-8")

    assert camera_credentials.load_credentials(tmp_path / "install") is None
