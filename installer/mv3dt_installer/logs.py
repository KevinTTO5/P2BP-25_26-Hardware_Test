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
    transcript(level, msg) -- append one line to the transcript *only*,
        printing nothing (doc 08 §7.1).
    die(msg)             -- log.error(msg) then sys.exit(1); never returns.
    set_colour(enabled)  -- force the colour decision, or None to go back
        to auto-detection (doc 08 §7).
    set_live_writer(stream, writer) / clear_live_writer(writer) -- hand the
        printed copy of a log line to a live renderer that owns the cursor
        on `stream`, so a log call cannot corrupt its redraw arithmetic
        (doc 08 §12.2 defect 3).
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
import re
import sys
import threading
from typing import Callable, NoReturn

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

# What a caller's message may not carry into the transcript. The colour this
# module adds is applied to the printed copy only, so it never reaches the
# transcript -- but a message that came from somewhere else can carry escapes
# of its own (apt, curl and dpkg all colour their output, and two step call
# sites pass raw command output straight into log.info), and doc 00 §8.2
# admits none of them whatever wrote them.
#
# Two passes, because an escape sequence is a run of printable characters
# and the rest are single bytes:
#
# _ESCAPE_RE  CSI, OSC and the two-character forms, introduced by ESC.
# _CONTROL_RE every remaining control character, which is what catches a
#             lone ESC, the C1 singles (\x9b CSI, \x9d OSC) that a UTF-8
#             decode of a latin-1 stream produces, BEL, backspace, NUL and
#             DEL. TAB and LF survive deliberately: a log message is allowed
#             to be indented and to span lines, and the transcript is a file
#             rather than a redrawn region.
#
# This is `progress.sanitise` minus the whitespace handling that a single
# terminal row needs and a transcript line does not. It is duplicated rather
# than imported because `progress` imports this module, so the dependency
# cannot run the other way.
_ESCAPE_RE = re.compile(
    r"\033(?:\[[0-?]*[ -/]*[@-~]|\][^\a\033]*(?:\a|\033\\)?|[@-Z\\-_])"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# The flag doc 00 §3.3 defines and doc 08 §7 makes REQUIRED here: a
# non-interactive run writes no escape sequence at all, on a real terminal
# as much as down a pipe. It is read off argv rather than plumbed in from a
# parsed `Namespace` because argv is where the flag is from the first
# instruction of the process, so the rule holds for every log line a run
# emits, including any written before or during argument parsing.
# `set_colour` is the explicit override for a caller that knows better.
_NON_INTERACTIVE_FLAG = "--non-interactive"

# Guards access to _transcript_path, the transcript file append below, and
# the live-writer registration, so concurrent log calls (if the installer
# ever grows threads) don't interleave or race on the "is a transcript open"
# check.
_lock = threading.Lock()
_transcript_path: pathlib.Path | None = None

# None means "decide from the terminal and argv"; True/False force it.
_colour_override: bool | None = None

# The renderer that owns the cursor, and the stream it owns it on. See
# set_live_writer().
_live_stream: object | None = None
_live_writer: Callable[[str], None] | None = None


def set_colour(enabled: bool | None) -> None:
    """Force colour on or off; None restores auto-detection.

    Auto-detection is `stderr is a tty and the run is not
    --non-interactive`, which is what doc 08 §7 requires of every context
    in its table. This override exists for a caller holding a parsed
    `--non-interactive` value it trusts more than argv, and for tests.
    """
    global _colour_override
    _colour_override = None if enabled is None else bool(enabled)


def set_live_writer(stream: object, writer: Callable[[str], None]) -> None:
    """Route the *printed* copy of a log line through `writer`.

    A live renderer erases its region by counting the rows it drew and
    moving the cursor up that many, so any write to the same stream that
    does not go through it moves the cursor without it knowing: the count
    is then short by exactly that many rows and the clear-below eats real
    output (doc 08 §12.2 defect 3). The seven step modules hold 121 direct
    log calls, so this is not a corner case.

    `writer` receives the fully composed screen line, without its newline,
    and is expected to write it around the region rather than into it. The
    transcript copy is unaffected and still written here.

    `stream` is what the renderer draws to, and the routing applies only
    while that stream *is* `sys.stderr` at the moment of the call: a
    renderer drawing somewhere else shares no cursor with this module and
    has nothing to protect.
    """
    global _live_stream, _live_writer
    with _lock:
        _live_stream = stream
        _live_writer = writer


def clear_live_writer(writer: Callable[[str], None] | None = None) -> None:
    """Stop routing through the live writer.

    Passing the writer clears only that registration, so a renderer that
    has already been superseded cannot unhook the current one. Passing
    nothing clears whatever is registered.
    """
    global _live_stream, _live_writer
    with _lock:
        if writer is None or writer is _live_writer:
            _live_stream = None
            _live_writer = None


def _screen_writer() -> Callable[[str], None] | None:
    with _lock:
        stream, writer = _live_stream, _live_writer
    if writer is None or stream is not sys.stderr:
        return None
    return writer


def _colour_enabled() -> bool:
    if _colour_override is not None:
        return _colour_override
    # Bash checks `[[ -t 2 ]]`; sys.stderr.isatty() is the Python equivalent.
    return sys.stderr.isatty() and _NON_INTERACTIVE_FLAG not in sys.argv[1:]


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


def _strip_control(msg: str) -> str:
    """Everything the transcript may not carry, removed (doc 00 §8.2).

    A carriage return means the writer overwrote what it had already
    emitted on that row, so what survives is the text after the last one,
    which is what a terminal would have been showing anyway. `progress`
    makes the same choice for the same reason.
    """
    text = _ESCAPE_RE.sub("", msg)
    rows = [
        row.rsplit("\r", 1)[-1] if "\r" in row else row
        for row in text.split("\n")
    ]
    return _CONTROL_RE.sub("", "\n".join(rows))


def _plain_line(level: str, msg: str) -> str:
    """The transcript form of a line: level label, and no escapes ever."""
    return f"{_LABELS[level]} {_strip_control(msg)}"


def _emit(level: str, msg: str) -> None:
    label = _LABELS[level]
    if _colour_enabled():
        line = f"{_COLOURS[level]}{label}{_RESET} {msg}"
    else:
        # Deliberately the caller's own text, not the stripped transcript
        # form: what stderr shows is unchanged by the escape rule below.
        line = f"{label} {msg}"

    writer = _screen_writer()
    if writer is None:
        print(line, file=sys.stderr)
    else:
        # A renderer owns the cursor on this stream; it writes the line
        # around its region instead of through it (see set_live_writer).
        writer(line)

    # The transcript never carries ANSI escapes, regardless of whether stderr
    # itself is coloured.
    _append_transcript(_plain_line(level, msg))


def transcript(level: str, msg: str) -> None:
    """Append one line to the transcript without printing it (doc 08 §7.1).

    The transcript-only half of `_emit`, for a caller that has already shown
    the line another way -- a live renderer drew it -- or that is deliberately
    showing nothing at all. Suppressing a line to protect the redrawn region
    must not also delete it from the auditable record: the screen may show
    less than the transcript, the transcript may never show less than the
    screen.

    Identical to `log.<level>()` in everything but the print: the same level
    label, the same escape stripping, the same lock, and the same no-op when
    no transcript is open.
    """
    _append_transcript(_plain_line(level, msg))


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
