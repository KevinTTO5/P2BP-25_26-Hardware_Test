"""Tests for mv3dt_installer.preflight (doc 00 §5.1.1).

Run from installer/: `python3 -m pytest tests/test_preflight.py -v`
"""

from __future__ import annotations

import pwd
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from mv3dt_installer import preflight  # noqa: E402

UBUNTU_2404 = """\
PRETTY_NAME="Ubuntu 24.04.1 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
VERSION="24.04.1 LTS (Noble Numbat)"
VERSION_CODENAME=noble
ID=ubuntu
ID_LIKE=debian
HOME_URL="https://www.ubuntu.com/"
UBUNTU_CODENAME=noble
LOGO=ubuntu-logo
"""

DEBIAN_12 = """\
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
VERSION_ID="12"
ID=debian
"""

UBUNTU_2204 = """\
PRETTY_NAME="Ubuntu 22.04.4 LTS"
NAME="Ubuntu"
VERSION_ID="22.04"
ID=ubuntu
"""


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


@pytest.fixture(autouse=True)
def _no_skip_env(monkeypatch):
    """The escape hatch must never leak in from the developer's own shell."""
    monkeypatch.delenv(preflight.SKIP_ENV, raising=False)


def _write_os_release(tmp_path, text):
    path = tmp_path / "os-release"
    path.write_text(text, encoding="utf-8")
    return path


def _fake_pwnam(db):
    def _lookup(name):
        try:
            return db[name]
        except KeyError:
            raise KeyError(f"getpwnam(): name not found: {name!r}")

    return _lookup


# ---------------------------------------------------------------------------
# Pinned constants
# ---------------------------------------------------------------------------


def test_pinned_platform_constants():
    assert preflight.SUPPORTED_DISTRO == "Ubuntu"
    assert preflight.SUPPORTED_RELEASE == "24.04"
    assert preflight.SUPPORTED_ARCH == "x86_64"
    assert preflight.SKIP_ENV == "MV3DT_SKIP_PLATFORM_CHECK"


# ---------------------------------------------------------------------------
# read_os_release
# ---------------------------------------------------------------------------


def test_read_os_release_parses_keys_and_strips_quotes(tmp_path):
    values = preflight.read_os_release(_write_os_release(tmp_path, UBUNTU_2404))

    assert values["ID"] == "ubuntu"
    assert values["VERSION_ID"] == "24.04"
    assert values["PRETTY_NAME"] == "Ubuntu 24.04.1 LTS"
    # Unquoted values survive untouched.
    assert values["VERSION_CODENAME"] == "noble"


def test_read_os_release_ignores_comments_and_blank_lines(tmp_path):
    path = _write_os_release(
        tmp_path,
        "# a comment\n\n   \nID='ubuntu'\nVERSION_ID=\"24.04\"\nnot-a-pair\n",
    )

    values = preflight.read_os_release(path)

    assert values == {"ID": "ubuntu", "VERSION_ID": "24.04"}


def test_read_os_release_raises_when_file_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        preflight.read_os_release(tmp_path / "nope" / "os-release")


# ---------------------------------------------------------------------------
# require_supported_platform
# ---------------------------------------------------------------------------


def test_supported_platform_accepts_ubuntu_2404_x86_64(tmp_path, capsys):
    preflight.require_supported_platform(
        os_release_path=_write_os_release(tmp_path, UBUNTU_2404),
        machine=lambda: "x86_64",
    )

    err = capsys.readouterr().err
    assert "OS OK: Ubuntu 24.04" in err
    assert "Architecture OK: x86_64" in err


def test_supported_platform_dies_on_wrong_distro(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc_info:
        preflight.require_supported_platform(
            os_release_path=_write_os_release(tmp_path, DEBIAN_12),
            machine=lambda: "x86_64",
        )

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "Ubuntu 24.04 required" in err
    assert "debian" in err


def test_supported_platform_dies_on_wrong_release(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc_info:
        preflight.require_supported_platform(
            os_release_path=_write_os_release(tmp_path, UBUNTU_2204),
            machine=lambda: "x86_64",
        )

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "Ubuntu 24.04 required" in err
    assert "22.04" in err


def test_supported_platform_dies_when_os_release_missing(tmp_path, capsys):
    """The exe must not proceed on a platform it cannot identify."""
    with pytest.raises(SystemExit) as exc_info:
        preflight.require_supported_platform(
            os_release_path=tmp_path / "absent" / "os-release",
            machine=lambda: "x86_64",
        )

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "Cannot read" in err
    assert "os-release" in err


def test_supported_platform_dies_on_wrong_arch(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc_info:
        preflight.require_supported_platform(
            os_release_path=_write_os_release(tmp_path, UBUNTU_2404),
            machine=lambda: "aarch64",
        )

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "x86_64 required" in err
    assert "aarch64" in err
    # The OS check ran and passed before the arch check failed.
    assert "OS OK: Ubuntu 24.04" in err


def test_supported_platform_matches_distro_case_insensitively(tmp_path):
    """os-release reports ID=ubuntu; lsb_release reported "Ubuntu"."""
    path = _write_os_release(tmp_path, 'ID="Ubuntu"\nVERSION_ID="24.04"\n')

    preflight.require_supported_platform(
        os_release_path=path, machine=lambda: "x86_64"
    )


# ---------------------------------------------------------------------------
# MV3DT_SKIP_PLATFORM_CHECK -- development-only escape hatch
# ---------------------------------------------------------------------------


def test_skip_env_downgrades_mismatch_to_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(preflight.SKIP_ENV, "1")

    # Must not raise: both the distro and the arch are wrong.
    preflight.require_supported_platform(
        os_release_path=_write_os_release(tmp_path, DEBIAN_12),
        machine=lambda: "aarch64",
    )

    err = capsys.readouterr().err
    assert "[warn ]" in err
    assert "Ubuntu 24.04 required" in err
    assert "x86_64 required" in err
    assert preflight.SKIP_ENV in err


def test_skip_env_downgrades_missing_os_release(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(preflight.SKIP_ENV, "1")

    preflight.require_supported_platform(
        os_release_path=tmp_path / "absent" / "os-release",
        machine=lambda: "x86_64",
    )

    err = capsys.readouterr().err
    assert "[warn ]" in err
    assert "Cannot read" in err


def test_skip_env_zero_does_not_disable_the_gate(tmp_path, monkeypatch):
    monkeypatch.setenv(preflight.SKIP_ENV, "0")

    with pytest.raises(SystemExit):
        preflight.require_supported_platform(
            os_release_path=_write_os_release(tmp_path, DEBIAN_12),
            machine=lambda: "x86_64",
        )


# ---------------------------------------------------------------------------
# require_invoking_user
# ---------------------------------------------------------------------------


def test_require_invoking_user_returns_real_sudo_user(monkeypatch):
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("HOME", "/root")  # root's $HOME under sudo -- a trap
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        _fake_pwnam(
            {"alice": SimpleNamespace(pw_dir="/home/alice", pw_uid=1001, pw_gid=1001)}
        ),
    )

    user = preflight.require_invoking_user()

    assert user.name == "alice"
    assert str(user.home) == "/home/alice"
    assert user.uid == 1001
    assert user.gid == 1001


def test_require_invoking_user_dies_when_sudo_user_unset_under_root_shell(
    monkeypatch, capsys
):
    """The hole this gate closes: privilege.resolve() falls back to
    getpass.getuser(), which is "root" in a bare `sudo -i` shell."""
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(preflight.privilege.getpass, "getuser", lambda: "root")
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        _fake_pwnam({"root": SimpleNamespace(pw_dir="/root", pw_uid=0, pw_gid=0)}),
    )

    with pytest.raises(SystemExit) as exc_info:
        preflight.require_invoking_user()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "SUDO_USER is unset or 'root'" in err
    assert "/root" in err


def test_require_invoking_user_dies_when_sudo_user_is_root(monkeypatch, capsys):
    monkeypatch.setenv("SUDO_USER", "root")
    monkeypatch.setattr(preflight.privilege.getpass, "getuser", lambda: "root")
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        _fake_pwnam({"root": SimpleNamespace(pw_dir="/root", pw_uid=0, pw_gid=0)}),
    )

    with pytest.raises(SystemExit) as exc_info:
        preflight.require_invoking_user()

    assert exc_info.value.code == 1
    assert "SUDO_USER is unset or 'root'" in capsys.readouterr().err


def test_require_invoking_user_unset_sudo_user_still_ok_for_a_real_login(
    monkeypatch,
):
    """No sudo in play (local dev): the fallback resolves a real non-root
    user, which is fine -- only "root" is fatal."""
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(preflight.privilege.getpass, "getuser", lambda: "bob")
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        _fake_pwnam(
            {"bob": SimpleNamespace(pw_dir="/home/bob", pw_uid=2002, pw_gid=2002)}
        ),
    )

    user = preflight.require_invoking_user()

    assert user.name == "bob"
    assert str(user.home) == "/home/bob"


def test_require_invoking_user_dies_when_sudo_user_is_not_a_login(
    monkeypatch, capsys
):
    monkeypatch.setenv("SUDO_USER", "ghost")
    monkeypatch.setattr(pwd, "getpwnam", _fake_pwnam({}))

    with pytest.raises(SystemExit) as exc_info:
        preflight.require_invoking_user()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "ghost" in err
    assert "not a valid login" in err
