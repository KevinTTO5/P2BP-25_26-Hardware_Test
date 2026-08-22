"""Tests for the bundled bash assets under `mv3dt_installer/assets/scripts/`
(installer/plan unit U12: mosquitto setup + tracking export scripts).

Run from installer/: `python3 -m pytest tests/test_bundled_scripts.py -v`

Never touches apt, systemctl, or mosquitto for real, and never touches the
real `/opt/mv3dt` or `/var/lib` -- syntax checks run `bash -n` against the
committed source files, the smoke test only exercises `--help` (no root
required), and `load_installer_conf`/`asset_root` are driven directly in a
subshell against a `tmp_path` fixture file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mv3dt_installer import shellout  # noqa: E402

ASSETS_SCRIPTS_DIR = Path(shellout.__file__).parent / "assets" / "scripts"
COMMON_SH = ASSETS_SCRIPTS_DIR / "lib" / "common.sh"
MOSQUITTO_SETUP_SH = ASSETS_SCRIPTS_DIR / "10_setup_mosquitto.sh"
RECORD_TRACKING_SH = ASSETS_SCRIPTS_DIR / "60_record_tracking.sh"


# ---------------------------------------------------------------------------
# bash -n syntax checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [MOSQUITTO_SETUP_SH, RECORD_TRACKING_SH, COMMON_SH],
    ids=["10_setup_mosquitto.sh", "60_record_tracking.sh", "lib/common.sh"],
)
def test_bundled_script_parses_with_bash_n(script):
    assert script.is_file(), f"missing bundled asset: {script}"
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# 10_setup_mosquitto.sh --help smoke test (no root required)
# ---------------------------------------------------------------------------


def test_mosquitto_setup_help_exits_zero_without_root():
    result = shellout.run_bundled_script(
        "scripts", "10_setup_mosquitto.sh", args=["--help"], tree=("scripts",)
    )

    assert result.returncode == 0, result.stderr
    assert "Usage: 10_setup_mosquitto.sh" in result.stdout


def test_record_tracking_help_exits_zero_without_root():
    result = shellout.run_bundled_script(
        "scripts", "60_record_tracking.sh", args=["--help"], tree=("scripts",)
    )

    assert result.returncode == 0, result.stderr
    assert "Usage: 60_record_tracking.sh" in result.stdout


# ---------------------------------------------------------------------------
# lib/common.sh -- load_installer_conf
# ---------------------------------------------------------------------------


def _source_common_and_run(shell_body: str, env: dict) -> subprocess.CompletedProcess:
    """Run a bash subshell that sources the real common.sh, with `env` merged
    over the current process environment (so PATH etc. survive)."""
    script = f'source "{COMMON_SH}"\n{shell_body}\n'
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=full_env
    )


def test_load_installer_conf_exports_a_sample_key(tmp_path):
    conf = tmp_path / "installer.conf"
    conf.write_text("LOCATION_ID=gainesville-01\n")

    result = _source_common_and_run(
        'load_installer_conf\necho "LOCATION_ID=$LOCATION_ID"',
        {"MV3DT_INSTALLER_CONF": str(conf)},
    )

    assert result.returncode == 0, result.stderr
    assert "LOCATION_ID=gainesville-01" in result.stdout


def test_load_installer_conf_dies_when_env_var_unset():
    # Unset for this subshell, even if the parent test process had it set.
    env = dict(os.environ)
    env.pop("MV3DT_INSTALLER_CONF", None)
    result = subprocess.run(
        ["bash", "-c", f'source "{COMMON_SH}"\nload_installer_conf'],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "MV3DT_INSTALLER_CONF" in result.stderr


def test_load_installer_conf_dies_when_file_missing(tmp_path):
    missing = tmp_path / "does-not-exist.conf"

    result = _source_common_and_run(
        "load_installer_conf", {"MV3DT_INSTALLER_CONF": str(missing)}
    )

    assert result.returncode != 0
    assert "installer.conf not found" in result.stderr


# ---------------------------------------------------------------------------
# lib/common.sh -- asset_root
# ---------------------------------------------------------------------------


def test_asset_root_dies_when_env_var_unset():
    env = dict(os.environ)
    env.pop("MV3DT_ASSET_ROOT", None)

    result = subprocess.run(
        ["bash", "-c", f'source "{COMMON_SH}"\nasset_root'],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "MV3DT_ASSET_ROOT" in result.stderr


def test_asset_root_prints_the_env_var_when_set(tmp_path):
    result = _source_common_and_run("asset_root", {"MV3DT_ASSET_ROOT": str(tmp_path)})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(tmp_path)
