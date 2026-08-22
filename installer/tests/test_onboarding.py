"""Tests for mv3dt_installer.onboarding (doc 00 §3.2, §5.2, §10.2, §14.3).

Run from installer/: `python3 -m pytest tests/test_onboarding.py -v`

Never touches a real /opt/mv3dt or a real home dir -- `install_dir` is
always a tmp_path location, and ngc.store_key's os.chown is monkeypatched
out via the `_no_chown` fixture (same pattern test_ngc.py uses).
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from mv3dt_installer import onboarding, ngc, privilege, webapp  # noqa: E402

_FAKE_NGC_KEY = "nvapi-fake-key-do-not-use-1234567890"


@pytest.fixture(autouse=True)
def _force_no_colour(monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)


@pytest.fixture(autouse=True)
def _reset_transcript_state():
    from mv3dt_installer import logs

    logs._transcript_path = None
    yield
    logs._transcript_path = None


@pytest.fixture
def _no_chown(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ngc.os, "chown", lambda path, uid, gid: calls.append((str(path), uid, gid))
    )
    return calls


def _fake_user(home, uid=1001, gid=1001, name="alice"):
    return privilege.InvokingUser(name=name, home=home, uid=uid, gid=gid)


# ---------------------------------------------------------------------------
# run_platform_preflight
# ---------------------------------------------------------------------------


def test_run_platform_preflight_delegates_to_preflight_module(monkeypatch):
    calls = []
    monkeypatch.setattr(
        onboarding.preflight,
        "require_supported_platform",
        lambda: calls.append("platform"),
    )
    fake_user = _fake_user(home="/home/alice")
    monkeypatch.setattr(
        onboarding.preflight, "require_invoking_user", lambda: calls.append("user") or fake_user
    )

    result = onboarding.run_platform_preflight()

    assert calls == ["platform", "user"]
    assert result is fake_user


# ---------------------------------------------------------------------------
# ensure_ngc_key
# ---------------------------------------------------------------------------


def test_ensure_ngc_key_skips_when_secret_file_already_exists(tmp_path, monkeypatch):
    install_dir = tmp_path / "install"
    secrets_dir = install_dir / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "ngc.env").write_text("NGC_API_KEY=already-there\n", encoding="utf-8")

    def _boom(non_interactive):
        raise AssertionError("capture_key must not be called when already asked")

    monkeypatch.setattr(onboarding.ngc, "capture_key", _boom)

    onboarding.ensure_ngc_key(install_dir, non_interactive=False)

    # Untouched -- still the pre-existing content.
    assert (secrets_dir / "ngc.env").read_text(encoding="utf-8") == "NGC_API_KEY=already-there\n"


def test_ensure_ngc_key_honors_pre_set_environment_variable_before_prompting(
    tmp_path, monkeypatch, _no_chown
):
    install_dir = tmp_path / "install"
    monkeypatch.setattr(ngc.privilege, "resolve", lambda: _fake_user(tmp_path / "home"))
    monkeypatch.setenv("NGC_API_KEY", _FAKE_NGC_KEY)

    def _boom(non_interactive):
        raise AssertionError("capture_key must not be called when NGC_API_KEY is set")

    monkeypatch.setattr(onboarding.ngc, "capture_key", _boom)

    onboarding.ensure_ngc_key(install_dir, non_interactive=True)

    assert ngc.load_key(install_dir) == _FAKE_NGC_KEY


def test_ensure_ngc_key_captures_and_stores_when_nothing_exists(
    tmp_path, monkeypatch, _no_chown
):
    install_dir = tmp_path / "install"
    monkeypatch.setattr(ngc.privilege, "resolve", lambda: _fake_user(tmp_path / "home"))
    monkeypatch.delenv("NGC_API_KEY", raising=False)
    monkeypatch.setattr(onboarding.ngc, "capture_key", lambda non_interactive: _FAKE_NGC_KEY)

    onboarding.ensure_ngc_key(install_dir, non_interactive=False)

    assert ngc.load_key(install_dir) == _FAKE_NGC_KEY


def test_ensure_ngc_key_environment_variable_never_reaches_a_log_call(
    tmp_path, monkeypatch, _no_chown, capsys
):
    install_dir = tmp_path / "install"
    monkeypatch.setattr(ngc.privilege, "resolve", lambda: _fake_user(tmp_path / "home"))
    monkeypatch.setenv("NGC_API_KEY", _FAKE_NGC_KEY)

    onboarding.ensure_ngc_key(install_dir, non_interactive=True)

    captured = capsys.readouterr()
    assert _FAKE_NGC_KEY not in captured.err
    assert _FAKE_NGC_KEY not in captured.out


# ---------------------------------------------------------------------------
# ensure_webapp_credentials
# ---------------------------------------------------------------------------


def test_ensure_webapp_credentials_skips_entirely_when_gate_off(tmp_path, monkeypatch):
    def _boom(non_interactive, install_dir=None):
        raise AssertionError("capture_credentials must not be called when the gate is off")

    monkeypatch.setattr(onboarding.webapp, "capture_credentials", _boom)

    onboarding.ensure_webapp_credentials(tmp_path / "install", "off", non_interactive=False)


def test_ensure_webapp_credentials_skips_when_secret_file_already_exists(
    tmp_path, monkeypatch
):
    install_dir = tmp_path / "install"
    secrets_dir = install_dir / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "webapp.env").write_text(
        "API_KEY=already\nENDPOINT=https://old\n", encoding="utf-8"
    )

    def _boom(non_interactive, install_dir=None):
        raise AssertionError("capture_credentials must not be called when already asked")

    monkeypatch.setattr(onboarding.webapp, "capture_credentials", _boom)

    onboarding.ensure_webapp_credentials(install_dir, "on", non_interactive=False)


def test_ensure_webapp_credentials_captures_and_stores_when_gate_on_and_nothing_exists(
    tmp_path, monkeypatch
):
    install_dir = tmp_path / "install"
    monkeypatch.setattr(webapp.privilege, "resolve", lambda: _fake_user(tmp_path / "home"))
    monkeypatch.setattr(webapp.os, "chown", lambda *a, **kw: None)

    captured = {}

    def _fake_capture(non_interactive, install_dir=None):
        captured["install_dir"] = install_dir
        return webapp.Credentials(api_key="k", endpoint="https://host")

    monkeypatch.setattr(onboarding.webapp, "capture_credentials", _fake_capture)

    onboarding.ensure_webapp_credentials(install_dir, "on", non_interactive=False)

    assert captured["install_dir"] == install_dir
    assert webapp.load_credentials(install_dir) == webapp.Credentials(
        api_key="k", endpoint="https://host"
    )


def test_ensure_webapp_credentials_incomplete_answer_stores_nothing_and_warns(
    tmp_path, monkeypatch, capsys
):
    install_dir = tmp_path / "install"
    monkeypatch.setattr(
        onboarding.webapp,
        "capture_credentials",
        lambda non_interactive, install_dir=None: webapp.Credentials(
            api_key=None, endpoint=None
        ),
    )

    onboarding.ensure_webapp_credentials(install_dir, "on", non_interactive=True)

    assert webapp.load_credentials(install_dir) is None
    assert "incomplete" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# onboard
# ---------------------------------------------------------------------------


def test_onboard_runs_both_ngc_and_webapp_flows(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        onboarding,
        "ensure_ngc_key",
        lambda install_dir, *, non_interactive: calls.append(("ngc", install_dir, non_interactive)),
    )
    monkeypatch.setattr(
        onboarding,
        "ensure_webapp_credentials",
        lambda install_dir, gate_value, *, non_interactive: calls.append(
            ("webapp", install_dir, gate_value, non_interactive)
        ),
    )

    onboarding.onboard(tmp_path / "install", "on", non_interactive=True)

    assert calls == [
        ("ngc", tmp_path / "install", True),
        ("webapp", tmp_path / "install", "on", True),
    ]
