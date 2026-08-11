"""Tests for `mv3dt_installer.shellout` (doc 00 §4.2).

Run with:
    cd installer && python3 -m pytest tests/test_shellout.py -v
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mv3dt_installer import shellout


def test_asset_path_dev_mode_resolves_relative_to_module_dir(monkeypatch):
    """No `sys._MEIPASS` set -> assets resolve under the module's own dir."""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    result = shellout.asset_path("scripts", "foo.sh")

    expected = (
        pathlib.Path(shellout.__file__).parent / "assets" / "scripts" / "foo.sh"
    )
    assert result == expected


def test_asset_path_frozen_mode_resolves_under_meipass(monkeypatch):
    """`sys._MEIPASS` set -> assets resolve under `<_MEIPASS>/assets/...`."""
    monkeypatch.setattr(sys, "_MEIPASS", "/fake/base", raising=False)

    result = shellout.asset_path("scripts", "foo.sh")

    assert result == pathlib.Path("/fake/base", "assets", "scripts", "foo.sh")


def test_run_bundled_script_executes_copy_and_returns_output(tmp_path, monkeypatch):
    """The fragment is copied out (never run from _MEIPASS), chmod +x'd,
    executed, and its stdout/exit code come back on the CompletedProcess."""
    fixture = tmp_path / "greet.sh"
    fixture.write_text("#!/bin/sh\necho hello-from-fixture\nexit 0\n")
    fixture.chmod(0o644)  # deliberately not executable yet; run_bundled_script must fix this

    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)

    result = shellout.run_bundled_script("scripts", "greet.sh")

    assert result.returncode == 0
    assert "hello-from-fixture" in result.stdout


def test_run_bundled_script_passes_custom_env_through(tmp_path, monkeypatch):
    """A custom `env` dict is actually threaded through to the subprocess."""
    fixture = tmp_path / "echo_env.sh"
    fixture.write_text('#!/bin/sh\necho "$MY_VAR"\n')
    fixture.chmod(0o644)

    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)

    result = shellout.run_bundled_script(
        "scripts", "echo_env.sh", env={"MY_VAR": "shellout-env-value", "PATH": "/usr/bin:/bin"}
    )

    assert "shellout-env-value" in result.stdout


def test_run_bundled_script_copies_out_before_executing(tmp_path, monkeypatch):
    """The script that actually runs is a copy in a fresh temp dir, not the
    original source path (guards the "never exec straight out of _MEIPASS"
    rule when the fragment would write next to itself)."""
    fixture = tmp_path / "print_self.sh"
    fixture.write_text('#!/bin/sh\necho "$0"\n')
    fixture.chmod(0o644)

    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)

    result = shellout.run_bundled_script("scripts", "print_self.sh")

    executed_path = result.stdout.strip()
    assert executed_path != str(fixture)
    assert pathlib.Path(executed_path).name == fixture.name


def test_run_bundled_script_forwards_args(tmp_path, monkeypatch):
    fixture = tmp_path / "echo_args.sh"
    fixture.write_text('#!/bin/sh\necho "$1" "$2"\n')
    fixture.chmod(0o644)

    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)

    result = shellout.run_bundled_script("scripts", "echo_args.sh", args=["one", "two"])

    assert result.stdout.strip() == "one two"


@pytest.mark.parametrize("bad_exit", [1, 2, 42])
def test_run_bundled_script_returns_nonzero_exit_code(tmp_path, monkeypatch, bad_exit):
    fixture = tmp_path / "fail.sh"
    fixture.write_text(f"#!/bin/sh\nexit {bad_exit}\n")
    fixture.chmod(0o644)

    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)

    result = shellout.run_bundled_script("scripts", "fail.sh")

    assert result.returncode == bad_exit
