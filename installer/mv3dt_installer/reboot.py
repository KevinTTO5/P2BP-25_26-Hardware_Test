"""Reboot detection / continuation contract for mv3dt-installer (doc 00 §7).

Owns two things:

1. `current_boot_id()` (doc 00 §7.2) — a best-effort, kernel-sourced token
   that changes value across a real reboot and stays stable across a mere
   re-run of the binary. Primary source is
   `/proc/sys/kernel/random/boot_id` (a fresh random UUID minted by the
   kernel every boot); if that's unreadable, falls back to the `btime`
   field of `/proc/stat` (boot wall-clock epoch).

2. `reconcile()` (doc 00 §7.2) — the state-mutation/decision logic that
   `app.main()` (a later, separate PR) runs early in dispatch, *before* it
   consults `STEP_REGISTRY`. This module does **not** implement §7.1's
   "signal a reboot" step (that's the dispatch loop reacting to a step's
   `StepResult(status=REBOOT_REQUIRED, ...)` by calling
   `state.set_reboot_pending(...)` itself — see `state.py`), and it does
   **not** print the §9.4 USER-ACTION reboot block or call `sys.exit` —
   both require `privilege.py`'s block renderer, which lives in a sibling
   PR not in this module's dependency chain. This module only decides
   *what happened* and reports that decision back to the caller.

`reconcile()`'s return contract (load-bearing for `app.py`)
-------------------------------------------------------------
`reconcile(state: StateMachine) -> ReconcileResult`, one of:

- `ReconcileResult.NOTHING_PENDING` — `state.reboot_pending` was `None`.
  No mutation happened. `app.main()` proceeds straight to normal dispatch.
- `ReconcileResult.CONFIRMED` — a real reboot happened (the boot id
  differs from the one recorded at request time). This module already
  did the state mutation itself: marked the requesting step `COMPLETE`
  via `state.mark_complete(...)`, called `state.clear_reboot_pending()`,
  and logged `"reboot confirmed"` via `logs.py`. `app.main()` should let
  dispatch continue to the next step — no further action needed here.
- `ReconcileResult.STILL_PENDING` — the operator re-ran the installer
  without rebooting (boot id unchanged). **No state was mutated** — the
  requesting step is still whatever non-`COMPLETE` status it was, and
  `state.reboot_pending` is still set. `app.main()` must NOT advance
  dispatch in this case: it should re-render the §9.4 USER-ACTION reboot
  block (via `privilege.py`) and `sys.exit(0)` itself. This module
  deliberately leaves both of those actions to the caller.

This module is framework-only, same as `state.py` and `logs.py`: it has no
knowledge of what any given step actually does.
"""

from __future__ import annotations

import pathlib
from enum import Enum

from mv3dt_installer.logs import log
from mv3dt_installer.state import StateMachine

__all__ = [
    "BOOT_ID_PATH",
    "PROC_STAT_PATH",
    "ReconcileResult",
    "current_boot_id",
    "reconcile",
]

# Primary source (doc 00 §7.2 #1): a fresh random UUID minted by the kernel
# on every boot. Module-level so tests can monkeypatch it to a tmp_path file
# instead of touching the real /proc.
BOOT_ID_PATH = pathlib.Path("/proc/sys/kernel/random/boot_id")

# Fallback source (doc 00 §7.2 #2): parse the `btime` line out of
# /proc/stat, used only when BOOT_ID_PATH is unreadable.
PROC_STAT_PATH = pathlib.Path("/proc/stat")


class ReconcileResult(Enum):
    """Outcome of `reconcile()` — see the module docstring for the full
    contract `app.py` depends on."""

    NOTHING_PENDING = "NOTHING_PENDING"
    CONFIRMED = "CONFIRMED"
    STILL_PENDING = "STILL_PENDING"


def _btime_from_proc_stat() -> str:
    """Parse the `btime` field out of `/proc/stat` (doc 00 §7.2 fallback).

    `/proc/stat` has a line like `btime 1699999999` — the boot wall-clock
    epoch in seconds. Returned as a string so both branches of
    `current_boot_id()` share the same (str) return type.
    """
    raw = PROC_STAT_PATH.read_text(encoding="utf-8")
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "btime":
            return parts[1]
    raise RuntimeError(
        f"no 'btime' field found in {PROC_STAT_PATH}; cannot determine boot id"
    )


def current_boot_id() -> str:
    """Return a token identifying the current boot (doc 00 §7.2).

    Priority order:
    1. `/proc/sys/kernel/random/boot_id` (primary) — read and stripped.
    2. `btime` from `/proc/stat` (fallback) — used only if the primary is
       unreadable (e.g. `OSError`, including `FileNotFoundError` and
       `PermissionError`).

    The exact token format is an implementation detail; callers only ever
    compare two calls' results for equality.
    """
    try:
        return BOOT_ID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return _btime_from_proc_stat()


def reconcile(state: StateMachine) -> ReconcileResult:
    """Run early in `app.main()` (doc 00 §3.2 step 5) to resolve a pending
    reboot marker against reality. See the module docstring for the exact
    `ReconcileResult` contract `app.py` depends on.

    Mutates state itself on the CONFIRMED branch only; NOTHING_PENDING and
    STILL_PENDING never touch `state.json`.
    """
    current = state.load()
    pending = current.reboot_pending

    if pending is None:
        return ReconcileResult.NOTHING_PENDING

    if pending.boot_id_at_request != current_boot_id():
        # The machine really rebooted: the requesting step's work is done,
        # the reboot was just its tail.
        state.mark_complete(pending.requested_by)
        state.clear_reboot_pending()
        log.info("reboot confirmed")
        return ReconcileResult.CONFIRMED

    # Same boot id: the operator re-ran the binary without rebooting.
    # Deliberately no mutation here -- see module docstring.
    return ReconcileResult.STILL_PENDING
