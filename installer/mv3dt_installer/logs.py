"""Shared logger and transcript writer for mv3dt-installer (doc 00 §8.1-8.2).

Mirrors the *behavior* of laptop/scripts/lib/common.sh's log_info/log_warn/
log_error/die: colour-aware, level-prefixed lines to stderr, so stdout stays
reserved for machine-parsable output. Every emitted line is also appended to
the transcript log file once one is open (see open_transcript()).

This module is a self-contained sibling of laptop/ (doc 00 §3.1) — it does
not import or source anything under laptop/scripts/lib/common.sh or any
other laptop/ path.

Public API:
    log.info(msg)      -- level "info", coloured green when stderr is a tty.
    log.warn(msg)       -- level "warn", coloured yellow when stderr is a tty.
    log.error(msg)      -- level "error", coloured red when stderr is a tty.
    die(msg)             -- log.error(msg) then sys.exit(1); never returns.
    open_transcript(log_dir=None) -- open/create the per-run transcript file
        and wire subsequent log.*() calls to also append to it. Returns the
        path to the per-run file.

Secrets redaction is owned by other modules (ngc.py, webapp.py per doc 00
§10/§14.4) — callers must pass already-redacted strings; this module does no
redaction of its own.
"""

from __future__ import annotations

import datetime
import pathlib
import sys
import threading
from typing import NoReturn

# Default transcript directory (doc 00 §8.2). Overridable via the
# open_transcript(log_dir=...) parameter (installer's --log-dir CLI flag
# plumbs through to this).
DEFAULT_LOG_DIR = pathlib.Path("/var/lib/mv3dt-installer/logs")

_RESET = "\033[0m"
_COLOURS = {
    "info": "\033[32m",  # green -- matches lib/common.sh log_info
    "warn": "\033[33m",  # yellow -- matches lib/common.sh log_warn
    "error": "\033[31m",  # red -- matches lib/common.sh log_error
}
_LABELS = {
    "info": "[info ]",
    "warn": "[warn ]",
    "error": "[error]",
}

# Guards access to _transcript_path and the transcript file append below, so
# concurrent log calls (if the installer ever grows threads) don't interleave
# or race on the "is a transcript open" check.
_lock = threading.Lock()
_transcript_path: pathlib.Path | None = None


def _colour_enabled() -> bool:
    # Bash checks `[[ -t 2 ]]`; sys.stderr.isatty() is the Python equivalent.
    return sys.stderr.isatty()


def _timestamp() -> str:
    # Split out for testability (tests monkeypatch this to force distinct
    # per-run filenames deterministically rather than sleeping across a
    # wall-clock second boundary).
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _append_transcript(plain_line: str) -> None:
    with _lock:
        path = _transcript_path
    if path is None:
        return
    with path.open("a", encoding="utf-8") as fh:
        fh.write(plain_line + "\n")


def _emit(level: str, msg: str) -> None:
    label = _LABELS[level]
    plain_line = f"{label} {msg}"
    if _colour_enabled():
        colour = _COLOURS[level]
        print(f"{colour}{label}{_RESET} {msg}", file=sys.stderr)
    else:
        print(plain_line, file=sys.stderr)
    # The transcript never carries ANSI escapes, regardless of whether stderr
    # itself is coloured.
    _append_transcript(plain_line)


class _Log:
    """Namespace exposing the level-prefixed logging calls."""

    @staticmethod
    def info(msg: str) -> None:
        _emit("info", msg)

    @staticmethod
    def warn(msg: str) -> None:
        _emit("warn", msg)

    @staticmethod
    def error(msg: str) -> None:
        _emit("error", msg)


log = _Log()


def die(msg: str) -> NoReturn:
    """Log msg at error level, then exit the process non-zero.

    Mirrors lib/common.sh's die(): `log_error "$*"; exit 1`.
    """
    log.error(msg)
    sys.exit(1)


def open_transcript(log_dir: pathlib.Path | None = None) -> pathlib.Path:
    """Open (creating if needed) the per-run transcript log file (doc 00 §8.2).

    Creates `<log_dir>/install-<UTC YYYYMMDD-HHMMSS>.log`, (re)points a
    `latest.log` symlink in the same directory at it, and wires subsequent
    log.info/warn/error() calls to also append to it. Returns the path to
    the per-run file.

    log_dir defaults to DEFAULT_LOG_DIR (/var/lib/mv3dt-installer/logs/);
    pass an explicit path (e.g. the installer's --log-dir flag, or a tmp_path
    in tests) to override it.
    """
    global _transcript_path

    directory = pathlib.Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)

    run_file = directory / f"install-{_timestamp()}.log"
    run_file.touch(exist_ok=True)

    latest_link = directory / "latest.log"
    if latest_link.is_symlink() or latest_link.exists():
        latest_link.unlink()
    latest_link.symlink_to(run_file.name)

    with _lock:
        _transcript_path = run_file

    return run_file
