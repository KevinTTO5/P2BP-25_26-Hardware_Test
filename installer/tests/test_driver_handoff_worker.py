"""Behavior tests for the Step 1 desktop-safe driver worker."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mv3dt_installer import shellout  # noqa: E402


def _write_executable(path: pathlib.Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_worker(
    tmp_path: pathlib.Path, *, runfile_rc: int, restore_ok: bool = True
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl_log = tmp_path / "systemctl.log"
    runfile_log = tmp_path / "runfile.log"

    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
printf '%s\n' "$*" >>"$SYSTEMCTL_LOG"
if [[ "$1 $2 $3" == "is-active --quiet gdm3" ]]; then exit 0; fi
if [[ "$1" == "is-active" ]]; then exit 1; fi
if [[ "$1 $2" == "start gdm3" && "$RESTORE_OK" != "1" ]]; then exit 1; fi
exit 0
""",
    )
    _write_executable(fake_bin / "pkill", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "sync", "#!/usr/bin/env bash\nexit 0\n"
    )

    runfile = tmp_path / "NVIDIA.run"
    _write_executable(
        runfile,
        """#!/usr/bin/env bash
printf '%s\n' "$*" >"$RUNFILE_LOG"
exit "$RUNFILE_RC"
""",
    )
    status = tmp_path / "status"
    log = tmp_path / "driver.log"
    boot_id = tmp_path / "boot-id"
    boot_id.write_text("boot-a\n")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SYSTEMCTL_LOG": str(systemctl_log),
        "RUNFILE_LOG": str(runfile_log),
        "RUNFILE_RC": str(runfile_rc),
        "RESTORE_OK": "1" if restore_ok else "0",
    }
    result = subprocess.run(
        [
            "bash",
            str(shellout.asset_path("systemd", "mv3dt-driver-handoff.sh")),
            str(runfile),
            str(status),
            str(log),
            str(boot_id),
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, status, systemctl_log, runfile_log


def test_worker_reboots_after_a_successful_runfile(tmp_path):
    result, status, systemctl_log, runfile_log = _run_worker(
        tmp_path, runfile_rc=0
    )

    assert result.returncode == 0
    assert status.read_text().strip().startswith("succeeded:")
    assert "stop gdm3" in systemctl_log.read_text()
    assert "--no-block reboot" in systemctl_log.read_text()
    assert runfile_log.read_text().strip() == "--silent --no-cc-version-check"


def test_worker_restores_the_desktop_after_a_runfile_failure(tmp_path):
    result, status, systemctl_log, _ = _run_worker(tmp_path, runfile_rc=7)

    assert result.returncode == 7
    assert status.read_text().strip() == "failed:runfile:7"
    calls = systemctl_log.read_text()
    assert "start gdm3" in calls
    assert "--no-block reboot" not in calls


def test_worker_records_when_desktop_restore_fails(tmp_path):
    result, status, systemctl_log, _ = _run_worker(
        tmp_path, runfile_rc=7, restore_ok=False
    )

    assert result.returncode == 7
    assert status.read_text().strip() == "failed:runfile:7:desktop-restore"
    assert "start gdm3" in systemctl_log.read_text()
