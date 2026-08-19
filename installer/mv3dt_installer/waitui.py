"""Blocking wait/poll screen for mv3dt-installer.

The operator never types a command to run a script. When a step needs a
human to finish something outside the installer -- most importantly the
AutoMagicCalib browser GUI, whose export files simply appear on disk when
the human is done -- the running installer stays up, shows a live status
line, polls a predicate, and continues by itself the moment the predicate
becomes true.

This module is framework-only: it ships the mechanism, not any call site.
It imports nothing from `steps/` and nothing from `privilege.py`; the only
dependency is `logs.log` for the non-tty transcript fallback.

Public API:
    WaitOutcome -- SATISFIED / TIMEOUT / CANCELLED / SKIPPED.
    dir_has_files(path) -> Callable[[], bool] -- predicate factory: true
        once `path` is a directory containing at least one regular file at
        any depth.
    render_wait_header(description, hint_actions=()) -> str -- pure
        formatter for the one-time header printed above the status line.
    wait_until(predicate, *, description, ...) -> WaitOutcome -- the
        blocking poll loop itself.

`hint_actions` calling convention
---------------------------------

Each element of `hint_actions` must expose, via plain attribute access
(duck typing -- `getattr(action, "text")`, no isinstance/Protocol check is
performed at runtime):

    .text     : str        -- required. The instruction text.
    .command  : str | None -- optional. Rendered verbatim on its own
                              indented continuation line, prefixed "$ ".
    .path     : str | None -- optional. Rendered on its own indented
                              continuation line as "(edit: <path>)".

That is deliberately the same shape `privilege.render_user_action_block`
reads and the same shape `steps.UserAction` provides, so a caller builds
one list of `steps.UserAction` objects, passes it here as the wait screen's
hints, and -- if the wait times out or is cancelled -- hands the *same*
list straight to `privilege.show_user_action_block` for the USER-ACTION
frame. The operator sees identical instructions either way.

Outcome contract for callers
----------------------------

    SATISFIED  -- the predicate came true; fall through and keep going.
    TIMEOUT    -- give up waiting; report USER_ACTION_REQUIRED with the
                  same `hint_actions`, preserving the "run the installer
                  again to continue" path.
    CANCELLED  -- the operator pressed Ctrl-C, i.e. "I will finish this
                  later". Also USER_ACTION_REQUIRED, never a crash:
                  `KeyboardInterrupt` is caught here and never propagates.
    SKIPPED    -- `non_interactive=True`. Returned immediately, before any
                  poll, without ever calling the predicate: an unattended
                  run must never block on a human.
"""

from __future__ import annotations

import pathlib
import sys
import time
from enum import Enum
from typing import Any, Callable, Iterable

from .logs import log

__all__ = [
    "WaitOutcome",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_POLL_S",
    "LOG_INTERVAL_S",
    "dir_has_files",
    "render_wait_header",
    "wait_until",
]


class WaitOutcome(Enum):
    """How a `wait_until()` call ended. See the module docstring."""

    SATISFIED = "SATISFIED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


# One hour: long enough for a human to work through the AutoMagicCalib
# browser GUI without the installer giving up on them.
DEFAULT_TIMEOUT_S = 3600.0

# Two seconds between polls: fast enough that the continuation feels
# immediate, slow enough that a `rglob` over an export tree is free.
DEFAULT_POLL_S = 2.0

# Non-tty fallback cadence. A carriage-returned status line would fill a
# transcript or a CI log with control characters, so when `out` is not a
# terminal the screen degrades to one plain `log.info` line every this many
# seconds of elapsed wait.
LOG_INTERVAL_S = 30.0

# Floor on `poll_s`, so a caller passing 0 cannot turn this into a busy
# loop against a real clock.
_MIN_POLL_S = 0.01

# Indent for an action's command/path continuation lines, aligned under the
# text of a single-digit numbered item ("    1. " is 7 columns) -- the same
# alignment `privilege.render_user_action_block` uses.
_ACTION_CONTINUATION_INDENT = "       "

_HINT_INTRO = "  While this waits, do the following:"
_AUTO_CONTINUE_HINT = (
    "  This screen continues on its own as soon as that is done."
)
_CANCEL_HINT = "  Press Ctrl-C to stop waiting and finish this later."


# ---------------------------------------------------------------------------
# Predicate factories
# ---------------------------------------------------------------------------


def dir_has_files(path: str | pathlib.Path) -> Callable[[], bool]:
    """Return a predicate that is true once `path` holds a regular file.

    Written for "the human finished in the AutoMagicCalib GUI and export
    files landed on disk". The check is recursive -- a file inside a
    subdirectory counts -- because exporters routinely write into a
    per-run subdirectory, but an empty directory tree never counts, which
    is what keeps a pre-created (and still empty) export directory from
    reading as done.

    A missing path, a path that is not a directory, and a directory the
    installer cannot read all return False rather than raising: the
    predicate is polled in a loop and must never be the thing that fails
    the run.
    """
    target = pathlib.Path(path)

    def _predicate() -> bool:
        try:
            if not target.is_dir():
                return False
            return any(entry.is_file() for entry in target.rglob("*"))
        except OSError:
            return False

    return _predicate


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_clock(seconds: float) -> str:
    """Format a duration as MM:SS, with minutes rolling past 60 (60:00)."""
    total = max(int(seconds), 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def _status_line(description: str, elapsed: float, timeout_s: float) -> str:
    return (
        f"{description}  "
        f"[{_format_clock(elapsed)} / {_format_clock(timeout_s)}]"
    )


def _render_hint_lines(actions: Iterable[Any]) -> list[str]:
    """Render `hint_actions` using the duck-typed attribute access above."""
    lines: list[str] = []
    for index, action in enumerate(actions, start=1):
        text = getattr(action, "text")
        command = getattr(action, "command", None)
        path = getattr(action, "path", None)

        lines.append(f"    {index}. {text}")
        if command:
            lines.append(f"{_ACTION_CONTINUATION_INDENT}$ {command}")
        if path:
            lines.append(f"{_ACTION_CONTINUATION_INDENT}(edit: {path})")
    return lines


def render_wait_header(
    description: str, hint_actions: Iterable[Any] = ()
) -> str:
    """Render the one-time header shown above the live status line.

    Pure formatter, emitted once per `wait_until()` call. The numbered
    hint list is omitted entirely when `hint_actions` is empty.
    """
    hint_lines = _render_hint_lines(hint_actions)

    lines = [description]
    if hint_lines:
        lines.append(_HINT_INTRO)
        lines.extend(hint_lines)
    lines.append(_AUTO_CONTINUE_HINT)
    lines.append(_CANCEL_HINT)

    return "\n".join(lines)


def _is_tty(out: Any) -> bool:
    """True when `out` is an interactive terminal, defensively."""
    try:
        return bool(out.isatty())
    except Exception:  # pragma: no cover -- exotic stream objects
        return False


def _write(out: Any, text: str) -> None:
    try:
        out.write(text)
        flush = getattr(out, "flush", None)
        if flush is not None:
            flush()
    except (OSError, ValueError):  # pragma: no cover -- closed/broken stream
        pass


# ---------------------------------------------------------------------------
# The wait loop
# ---------------------------------------------------------------------------


def wait_until(
    predicate: Callable[[], bool],
    *,
    description: str,
    hint_actions: Iterable[Any] = (),
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_s: float = DEFAULT_POLL_S,
    non_interactive: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    out: Any = sys.stderr,
) -> WaitOutcome:
    """Block until `predicate()` is true, it times out, or Ctrl-C.

    The loop checks the predicate first and only then sleeps, so a
    predicate that is already true returns `SATISFIED` without a single
    sleep. Each later poll costs exactly one `sleep` call, and the final
    sleep is shortened so the last check lands exactly on the timeout
    boundary rather than past it.

    `clock` and `sleep` are injected so tests drive elapsed time without
    ever really sleeping; production callers take the `time.monotonic` /
    `time.sleep` defaults. `out` receives the carriage-returned status
    line when it is a tty; when it is not, the screen degrades to a
    periodic `logs.log.info` line so the transcript stays free of control
    characters.

    `KeyboardInterrupt` raised anywhere in the loop -- in `predicate`, in
    `sleep`, or between them -- is caught and mapped to `CANCELLED`. It
    never propagates: Ctrl-C here means "I will finish this later", which
    the calling step turns into `USER_ACTION_REQUIRED`, not a crash.
    """
    if non_interactive:
        # No poll, no predicate call, no sleep: the whole point of
        # --non-interactive is that an unattended run never blocks on a
        # human.
        log.warn(f"non-interactive run: not waiting for {description}")
        return WaitOutcome.SKIPPED

    timeout = max(float(timeout_s), 0.0)
    interval = max(float(poll_s), _MIN_POLL_S)
    tty = _is_tty(out)

    header = render_wait_header(description, hint_actions)
    if tty:
        _write(out, header + "\n")
    else:
        log.info(header)

    started = clock()
    elapsed = 0.0
    status_width = 0
    next_log_at = 0.0
    outcome: WaitOutcome

    try:
        while True:
            elapsed = clock() - started

            status = _status_line(description, elapsed, timeout)
            if tty:
                _write(out, "\r" + status.ljust(status_width))
                status_width = len(status)
            elif elapsed >= next_log_at:
                log.info(status)
                next_log_at = elapsed + LOG_INTERVAL_S

            if predicate():
                outcome = WaitOutcome.SATISFIED
                break

            if elapsed >= timeout:
                outcome = WaitOutcome.TIMEOUT
                break

            sleep(min(interval, timeout - elapsed))
    except KeyboardInterrupt:
        outcome = WaitOutcome.CANCELLED

    if tty:
        _write(out, "\n")

    waited = _format_clock(elapsed)
    if outcome is WaitOutcome.SATISFIED:
        log.info(f"done after {waited}: {description}")
    elif outcome is WaitOutcome.TIMEOUT:
        log.warn(f"timed out after {waited}: {description}")
    else:
        log.warn(f"cancelled after {waited}: {description}")

    return outcome
