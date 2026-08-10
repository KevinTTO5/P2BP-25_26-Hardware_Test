"""Tests for mv3dt_installer.privilege (doc 00 §9.1-9.4).

Run from installer/: `python3 -m pytest tests/test_privilege.py -v`
"""

from __future__ import annotations

import pwd
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from mv3dt_installer import privilege  # noqa: E402

_BORDER = "+" + "-" * 63 + "+"


@pytest.fixture(autouse=True)
def _force_no_colour(monkeypatch):
    """Match test_logs.py's convention: assert on plain (non-tty) text."""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)


@pytest.fixture(autouse=True)
def _reset_transcript_state():
    from mv3dt_installer import logs

    logs._transcript_path = None
    yield
    logs._transcript_path = None


def _fake_pwnam(db):
    def _lookup(name):
        try:
            return db[name]
        except KeyError:
            raise KeyError(f"getpwnam(): name not found: {name!r}")

    return _lookup


# ---------------------------------------------------------------------------
# 9.1 -- require_root
# ---------------------------------------------------------------------------


def test_require_root_dies_when_not_root(monkeypatch, capsys):
    monkeypatch.setattr(privilege.os, "geteuid", lambda: 1000)

    with pytest.raises(SystemExit) as exc_info:
        privilege.require_root()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "root" in captured.err.lower()
    assert "sudo" in captured.err.lower()


def test_require_root_passes_when_root(monkeypatch):
    monkeypatch.setattr(privilege.os, "geteuid", lambda: 0)

    # Must not raise / must not call die().
    privilege.require_root()


# ---------------------------------------------------------------------------
# 9.2 -- resolve() / InvokingUser
# ---------------------------------------------------------------------------


def test_resolve_uses_sudo_user_when_set(monkeypatch):
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("HOME", "/root")  # root's $HOME under sudo -- a trap
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        _fake_pwnam(
            {
                "alice": SimpleNamespace(
                    pw_dir="/home/alice", pw_uid=1001, pw_gid=1001
                ),
            }
        ),
    )

    result = privilege.resolve()

    assert result.name == "alice"
    assert result.uid == 1001
    assert result.gid == 1001


def test_resolve_home_comes_from_passwd_db_not_home_env(monkeypatch):
    """The core §9.2 trap: $HOME is root's under sudo. home must come from
    getent passwd (here, monkeypatched pwd.getpwnam), never os.environ."""
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("HOME", "/root")  # misleading on purpose
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        _fake_pwnam(
            {
                "alice": SimpleNamespace(
                    pw_dir="/home/alice", pw_uid=1001, pw_gid=1001
                ),
            }
        ),
    )

    result = privilege.resolve()

    assert result.home == privilege.pathlib.Path("/home/alice")
    assert str(result.home) != "/root"


def test_resolve_falls_back_to_current_user_when_sudo_user_unset(monkeypatch):
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(privilege.getpass, "getuser", lambda: "bob")
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        _fake_pwnam(
            {"bob": SimpleNamespace(pw_dir="/home/bob", pw_uid=2002, pw_gid=2002)}
        ),
    )

    result = privilege.resolve()

    assert result.name == "bob"
    assert result.home == privilege.pathlib.Path("/home/bob")
    assert result.uid == 2002
    assert result.gid == 2002


def test_resolve_falls_back_to_current_user_when_sudo_user_is_root(monkeypatch):
    monkeypatch.setenv("SUDO_USER", "root")
    monkeypatch.setattr(privilege.getpass, "getuser", lambda: "bob")
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        _fake_pwnam(
            {"bob": SimpleNamespace(pw_dir="/home/bob", pw_uid=2002, pw_gid=2002)}
        ),
    )

    result = privilege.resolve()

    assert result.name == "bob"


def test_run_as_user_builds_expected_sudo_command(monkeypatch):
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        _fake_pwnam(
            {
                "alice": SimpleNamespace(
                    pw_dir="/home/alice", pw_uid=1001, pw_gid=1001
                ),
            }
        ),
    )

    captured_cmd = {}

    def _fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        captured_cmd["kwargs"] = kwargs
        return "completed-process-sentinel"

    monkeypatch.setattr(privilege.subprocess, "run", _fake_run)

    result = privilege.run_as_user("ngc", "config", "set", capture_output=True, text=True)

    assert captured_cmd["cmd"] == ["sudo", "-u", "alice", "-H", "ngc", "config", "set"]
    assert captured_cmd["kwargs"] == {"capture_output": True, "text": True}
    assert result == "completed-process-sentinel"


# ---------------------------------------------------------------------------
# 9.3 -- USER-ACTION display contract
# ---------------------------------------------------------------------------


def test_render_user_action_block_exact_frame_with_actions():
    action1 = SimpleNamespace(
        text="Add DeepStream to PATH",
        command="echo 'export PATH=/opt/ds:$PATH' >> ~/.bashrc",
    )
    action2 = SimpleNamespace(text="Log out and back in for PATH to take effect")

    block = privilege.render_user_action_block(
        step_title="DeepStream SDK install",
        why="the shell needs the new PATH entry before the next step runs",
        actions=[action1, action2],
    )

    expected = "\n".join(
        [
            _BORDER,
            "|  ACTION REQUIRED" + " " * 46 + "|",
            _BORDER,
            "  Step: DeepStream SDK install",
            "  Why:  the shell needs the new PATH entry before the next step runs",
            "",
            "  Do the following, in order:",
            "    1. Add DeepStream to PATH",
            "       $ echo 'export PATH=/opt/ds:$PATH' >> ~/.bashrc",
            "    2. Log out and back in for PATH to take effect",
            "",
            "  Then run the installer again to continue.",
            _BORDER,
        ]
    )

    assert block == expected
    # The contract's load-bearing phrase, verbatim.
    assert "Then run the installer again to continue." in block


def test_render_user_action_block_border_and_header_widths():
    block = privilege.render_user_action_block("t", "w", actions=[])
    lines = block.splitlines()

    assert lines[0] == _BORDER
    assert lines[0] == lines[2]
    assert lines[-1] == _BORDER
    assert len(lines[0]) == 65
    assert lines[1] == "|  ACTION REQUIRED" + " " * 46 + "|"
    assert len(lines[1]) == 65


def test_render_user_action_block_with_path_attribute():
    action = SimpleNamespace(
        text="Edit the profile script",
        path="/etc/profile.d/deepstream.sh",
    )

    block = privilege.render_user_action_block("Step", "Why", actions=[action])

    assert "    1. Edit the profile script" in block
    assert "       (edit: /etc/profile.d/deepstream.sh)" in block


def test_render_user_action_block_omits_do_the_following_section_when_empty():
    block = privilege.render_user_action_block("Step", "Why reason", actions=[])

    assert "Do the following, in order:" not in block
    lines = block.splitlines()
    # Step/Why followed directly by the blank line and closing line.
    assert lines[3] == "  Step: Step"
    assert lines[4] == "  Why:  Why reason"
    assert lines[5] == ""
    assert lines[6] == "  Then run the installer again to continue."
    assert lines[7] == _BORDER


def test_show_user_action_block_reaches_stderr(capsys):
    action = SimpleNamespace(text="Do a thing", command="do-a-thing --now")

    privilege.show_user_action_block("Step", "Because reasons", actions=[action])

    captured = capsys.readouterr()
    assert "ACTION REQUIRED" in captured.err
    assert "Then run the installer again to continue." in captured.err
    assert "do-a-thing --now" in captured.err


# ---------------------------------------------------------------------------
# 9.4 -- reboot block
# ---------------------------------------------------------------------------


def test_render_reboot_block_uses_fixed_reason_and_closing_line():
    block = privilege.render_reboot_block("Driver install")

    assert "  Why:  a reboot is required before the next step can run" in block
    assert "  Then run the installer again to continue." in block
    assert "Do the following, in order:" not in block

    lines = block.splitlines()
    assert lines[0] == _BORDER
    assert lines[-1] == _BORDER
    assert lines[3] == "  Step: Driver install"


def test_render_reboot_block_matches_render_user_action_block_with_no_actions():
    assert privilege.render_reboot_block("X") == privilege.render_user_action_block(
        "X", privilege.REBOOT_REASON, actions=()
    )


def test_show_reboot_required_reaches_stderr(capsys):
    privilege.show_reboot_required("Driver install")

    captured = capsys.readouterr()
    assert "a reboot is required before the next step can run" in captured.err
    assert "Then run the installer again to continue." in captured.err
