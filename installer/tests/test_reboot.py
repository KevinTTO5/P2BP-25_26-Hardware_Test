"""Tests for `mv3dt_installer.reboot` (doc 00 §7).

Run from installer/: `python3 -m pytest tests/test_reboot.py -v`

Never touches the real `/proc/sys/kernel/random/boot_id` or `/proc/stat` --
every test monkeypatches `reboot.BOOT_ID_PATH` / `reboot.PROC_STAT_PATH` to
point at a `tmp_path`-backed fake file instead. Likewise never touches the
real `/var/lib/mv3dt-installer/state.json` -- every test constructs a
`StateMachine` against a `tmp_path`-derived file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mv3dt_installer.reboot import (  # noqa: E402
    ReconcileResult,
    current_boot_id,
    reconcile,
)
from mv3dt_installer.state import StateMachine  # noqa: E402
from mv3dt_installer.steps import StepStatus  # noqa: E402


# ---------------------------------------------------------------------------
# current_boot_id()
# ---------------------------------------------------------------------------


def test_current_boot_id_reads_primary_source(tmp_path, monkeypatch):
    fake_boot_id = tmp_path / "boot_id"
    fake_boot_id.write_text("1234abcd-5678-90ef-1234-567890abcdef\n", encoding="utf-8")
    monkeypatch.setattr("mv3dt_installer.reboot.BOOT_ID_PATH", fake_boot_id)

    assert current_boot_id() == "1234abcd-5678-90ef-1234-567890abcdef"


def test_current_boot_id_falls_back_to_proc_stat_btime(tmp_path, monkeypatch):
    # Primary source unreadable (doesn't exist).
    missing_boot_id = tmp_path / "does_not_exist" / "boot_id"
    monkeypatch.setattr("mv3dt_installer.reboot.BOOT_ID_PATH", missing_boot_id)

    fake_proc_stat = tmp_path / "stat"
    fake_proc_stat.write_text(
        "cpu  100 200 300 400 0 0 0 0 0 0\nbtime 1699999999\nprocesses 42\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("mv3dt_installer.reboot.PROC_STAT_PATH", fake_proc_stat)

    assert current_boot_id() == "1699999999"


def test_current_boot_id_fallback_used_only_when_primary_unreadable(
    tmp_path, monkeypatch
):
    """If the primary source is readable, the fallback must never be
    consulted -- point PROC_STAT_PATH at garbage to prove it's unused."""
    fake_boot_id = tmp_path / "boot_id"
    fake_boot_id.write_text("primary-value\n", encoding="utf-8")
    monkeypatch.setattr("mv3dt_installer.reboot.BOOT_ID_PATH", fake_boot_id)
    monkeypatch.setattr(
        "mv3dt_installer.reboot.PROC_STAT_PATH", tmp_path / "unreadable_stat"
    )

    assert current_boot_id() == "primary-value"


def test_current_boot_id_raises_when_both_sources_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mv3dt_installer.reboot.BOOT_ID_PATH", tmp_path / "missing_boot_id"
    )
    monkeypatch.setattr(
        "mv3dt_installer.reboot.PROC_STAT_PATH", tmp_path / "missing_stat"
    )

    with pytest.raises(OSError):
        current_boot_id()


def test_current_boot_id_raises_when_proc_stat_has_no_btime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mv3dt_installer.reboot.BOOT_ID_PATH", tmp_path / "missing_boot_id"
    )
    fake_proc_stat = tmp_path / "stat"
    fake_proc_stat.write_text("cpu  100 200 300 400\nprocesses 42\n", encoding="utf-8")
    monkeypatch.setattr("mv3dt_installer.reboot.PROC_STAT_PATH", fake_proc_stat)

    with pytest.raises(RuntimeError):
        current_boot_id()


# ---------------------------------------------------------------------------
# reconcile()
# ---------------------------------------------------------------------------


STEP_ID = "step1_prerequisites"


def _machine(tmp_path) -> StateMachine:
    return StateMachine(path=tmp_path / "state.json")


def _fix_boot_id(tmp_path, monkeypatch, value: str) -> None:
    fake_boot_id = tmp_path / "boot_id"
    fake_boot_id.write_text(value + "\n", encoding="utf-8")
    monkeypatch.setattr("mv3dt_installer.reboot.BOOT_ID_PATH", fake_boot_id)


def test_reconcile_nothing_pending_is_a_noop(tmp_path, monkeypatch):
    _fix_boot_id(tmp_path, monkeypatch, "boot-a")
    machine = _machine(tmp_path)
    # Fresh state: reboot_pending is None, no state.json even written yet.
    assert machine.load().reboot_pending is None

    result = reconcile(machine)

    assert result is ReconcileResult.NOTHING_PENDING
    assert machine.load().reboot_pending is None
    assert machine.status(STEP_ID) is StepStatus.PENDING


def test_reconcile_different_boot_id_confirms_and_mutates_state(
    tmp_path, monkeypatch
):
    machine = _machine(tmp_path)
    # Simulate a step having requested a reboot while the machine was on
    # "boot-a".
    _fix_boot_id(tmp_path, monkeypatch, "boot-a")
    machine.set_reboot_pending(STEP_ID, boot_id=current_boot_id())
    machine.set_status(STEP_ID, StepStatus.REBOOT_REQUIRED)

    # The machine actually rebooted -- boot id is now different.
    _fix_boot_id(tmp_path, monkeypatch, "boot-b")

    result = reconcile(machine)

    assert result is ReconcileResult.CONFIRMED
    final = machine.load()
    assert final.reboot_pending is None
    assert machine.status(STEP_ID) is StepStatus.COMPLETE
    assert final.steps[STEP_ID].finished_utc is not None


def test_reconcile_same_boot_id_does_not_mutate_state(tmp_path, monkeypatch):
    machine = _machine(tmp_path)
    _fix_boot_id(tmp_path, monkeypatch, "boot-a")
    machine.set_reboot_pending(STEP_ID, boot_id=current_boot_id())
    machine.set_status(STEP_ID, StepStatus.REBOOT_REQUIRED)

    before = machine.load()

    # No reboot happened -- boot id is unchanged.
    result = reconcile(machine)

    assert result is ReconcileResult.STILL_PENDING
    after = machine.load()
    assert after.reboot_pending is not None
    assert after.reboot_pending.boot_id_at_request == before.reboot_pending.boot_id_at_request
    assert machine.status(STEP_ID) is StepStatus.REBOOT_REQUIRED
    assert after.steps[STEP_ID].finished_utc is None


def test_reconcile_confirmed_logs_reboot_confirmed(tmp_path, monkeypatch, capsys):
    machine = _machine(tmp_path)
    _fix_boot_id(tmp_path, monkeypatch, "boot-a")
    machine.set_reboot_pending(STEP_ID, boot_id=current_boot_id())

    _fix_boot_id(tmp_path, monkeypatch, "boot-b")
    reconcile(machine)

    captured = capsys.readouterr()
    assert "reboot confirmed" in captured.err


def test_reconcile_same_boot_id_does_not_log_confirmed(tmp_path, monkeypatch, capsys):
    machine = _machine(tmp_path)
    _fix_boot_id(tmp_path, monkeypatch, "boot-a")
    machine.set_reboot_pending(STEP_ID, boot_id=current_boot_id())

    reconcile(machine)

    captured = capsys.readouterr()
    assert "reboot confirmed" not in captured.err
