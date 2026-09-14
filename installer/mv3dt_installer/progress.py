"""Live progress rendering for mv3dt-installer (doc 08 sections 3, 5, 7, 8).

The installer is a single non-interactive binary an operator runs once on a
bare workstation, so the terminal is the only channel it has. Silence on
that channel is indistinguishable from failure: during the 0.1.2-0.1.9
workstation runs an operator twice interrupted work that was proceeding
normally, once mid-apt-transaction, because a captured multi-gigabyte
download presents exactly like a hung process. This module owns everything
that makes the difference visible.

Framework-only, following the `waitui.py` precedent: it ships the mechanism
and no call site, imports `logs` and nothing from `steps/` or
`privilege.py`, and is reached by a step through `Context` alone.

Public API:
    Progress -- the renderer. `begin_step` / `phase` / `task` / `bytes` /
        `percent` / `line` / `tick` / `end_step`; `phase`, `task` and
        `bytes` are the step-author contract in doc 08 section 9, and
        `percent` is the adapter-facing sibling of `bytes`.
    sanitise(text) -- strip control characters out of command output.
    format_duration / format_clock / format_bytes / format_rate -- pure
        formatters.
    render_step_banner / render_phase_done / render_phase_active /
        render_bar / render_bytes_line / render_spinner_line -- pure line
        renderers, one per row of the section 3.2 banner.
    content_length / follow_download -- the download byte-progress adapter
        (section 5.1): poll the `.part` file the installer is writing and
        drive the bar from `size(.part) / Content-Length`.
    parse_apt_status / apt_fraction / follow_apt -- the apt percentage
        adapter (section 5.2): read `APT::Status-Fd` and drive one bar that
        only ever rises across apt's two independent 0-100 sweeps.

Three properties this module owes its callers:

1. **No fabricated progress (section 5.3).** A bar is drawn only where a
   true denominator exists. `bytes(done, total)` with no usable total falls
   back to the spinner rather than inventing a percentage, and rate and ETA
   stay hidden until the samples behind them span enough time to mean
   anything. A bar that does not track real work teaches the operator to
   distrust the one signal they have.
2. **Nothing live off a tty (section 7).** When `out` is not a terminal, or
   the run is non-interactive, no bar, no spinner and no escape sequence is
   written at all -- command output goes out as plain lines, and position
   in the install comes from `logs.log`, which the transcript already
   guarantees is escape-free. That holds for text the installer did not
   write either: `apt`, `dpkg` and `curl` colour and carriage-return their
   own output, so everything a caller hands in is sanitised on the way in.
3. **The phase sequence survives in the transcript (section 3.3).** Every
   phase transition emits a `log.info` line whether or not anything is
   being drawn, because a post-mortem of a failed install has only the
   transcript to work from. A line this module suppresses to protect the
   live region goes to `logs.transcript` instead of being dropped (section
   7.1): the screen may show less than the transcript, never the reverse.

The renderer never sleeps and starts no thread, so there is no `sleep` to
inject: the caller drives redraws by calling `tick()`, and `clock` is
injected so tests get exact elapsed times instead of wall-clock ones.
"""

from __future__ import annotations

import http.client
import math
import pathlib
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Iterable, List, Sequence

from .logs import log, transcript

__all__ = [
    "Progress",
    "sanitise",
    "WINDOW_LINES",
    "BAR_WIDTH",
    "SPINNER_FRAMES",
    "format_duration",
    "format_clock",
    "format_bytes",
    "format_rate",
    "render_bar",
    "render_step_banner",
    "render_phase_done",
    "render_phase_active",
    "render_bytes_line",
    "render_percent_line",
    "render_spinner_line",
    "DownloadOutcome",
    "DEFAULT_DOWNLOAD_POLL_S",
    "DOWNLOAD_LOG_INTERVAL_S",
    "part_size",
    "default_content_length_probe",
    "content_length",
    "render_download_log_line",
    "render_download_done_line",
    "follow_download",
    "APT_DOWNLOAD_SHARE",
    "APT_LOG_INTERVAL_S",
    "AptStatus",
    "AptOutcome",
    "apt_status_fd_args",
    "parse_apt_status",
    "apt_fraction",
    "render_apt_log_line",
    "render_apt_done_line",
    "follow_apt",
]


# Rolling command-output window, LOCKED at 8 lines in every phase (section
# 8): a fixed height rather than one that grows for download-heavy phases,
# so the layout does not shift underneath the operator between phases.
WINDOW_LINES = 8

# Bar geometry from the section 3.2 example: 17 cells, so 68% renders as
# the doc's twelve filled cells and five empty ones.
BAR_WIDTH = 17
BAR_FILLED = "█"
BAR_EMPTY = "░"

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_PHASE_INDENT = "  "
_ACTIVITY_INDENT = "      "
_WINDOW_INDENT = "        "
_PHASE_LABEL_WIDTH = 34
_DURATION_WIDTH = 6

# Every live row is truncated to the real terminal width rather than
# wrapped: a wrapped row occupies two terminal rows, so the cursor-up count
# used to redraw the region would be short by one for every row that
# overflowed, and the difference stays on screen as debris that grows with
# each redraw. One column is held back because writing the last column of a
# row is enough to wrap on terminals with deferred auto-wrap.
_FALLBACK_COLUMNS = 80
_MIN_ROW_CHARS = 20

# Control characters in caller-supplied output. apt, dpkg and curl all emit
# colour and carriage returns in normal operation, and neither may reach a
# transcript, a pipe, or the cursor arithmetic above (section 7).
_ANSI_RE = re.compile(
    r"\033(?:\[[0-?]*[ -/]*[@-~]|\][^\a\033]*(?:\a|\033\\)?|[@-Z\\-_])"
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Rate/ETA sampling. Samples older than the window are dropped so the rate
# tracks the current transfer rather than its lifetime average, and nothing
# is shown until the surviving samples span _MIN_RATE_SPAN_S -- the first
# sample pair of a download otherwise reports a rate off by an order of
# magnitude, which is worse than reporting none.
_RATE_WINDOW_S = 5.0
_MIN_RATE_SPAN_S = 1.0

_CURSOR_UP = "\033[{n}A"
_CLEAR_BELOW = "\033[J"


# ---------------------------------------------------------------------------
# Stream helpers (the waitui.py pair, kept local so framework modules stay
# independent of each other)
# ---------------------------------------------------------------------------


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


def _terminal_width() -> int:
    """Columns available to the live region, or 80 when unknowable."""
    try:
        columns = shutil.get_terminal_size(
            fallback=(_FALLBACK_COLUMNS, 24)
        ).columns
    except Exception:  # pragma: no cover -- exotic environments
        columns = _FALLBACK_COLUMNS
    return max(int(columns), _MIN_ROW_CHARS)


def sanitise(text: str) -> str:
    """Strip control characters out of caller-supplied output.

    Two things this protects. The transcript and every non-tty stream must
    stay free of escapes (section 7), and they carry command output that
    the installer did not write: `apt` colours its own lines, `curl` and
    `dpkg` redraw theirs with carriage returns. And a row containing an
    escape or a CR no longer occupies the number of terminal rows the
    redraw arithmetic assumed it did.

    A carriage return means the writer overwrote what it had already
    emitted, so what survives is the text after the last one, which is what
    a terminal would have been showing anyway.
    """
    text = _ANSI_RE.sub("", text)
    text = text.replace("\t", " ")
    if "\r" in text:
        text = text.rsplit("\r", 1)[-1]
    return _CONTROL_RE.sub("", text)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """Wall-clock duration for a collapsed phase line: 12s, 3m41s, 1h02m."""
    total = max(int(seconds), 0)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def format_clock(seconds: float) -> str:
    """Countdown clock for an ETA: 0:14, 12:05, 1:02:03.

    Rounded to the nearest second rather than truncated: this only ever
    renders an estimate, and 13.6 seconds left reads as 0:14, not 0:13.
    """
    total = max(int(seconds + 0.5), 0)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_bytes(count: float) -> str:
    """Humanise a byte count: 512 B, 14.2 MB, 606 MB, 1.05 GB.

    Decimal units, because the numbers this renders are compared against
    `Content-Length` and against vendor-published download sizes, both of
    which are quoted decimal. Precision shrinks as the value grows so the
    field stays about as wide whatever unit it lands in.
    """
    value = float(max(count, 0))
    unit = "B"
    for candidate in ("B", "KB", "MB", "GB", "TB"):
        unit = candidate
        if value < 1000 or candidate == "TB":
            break
        value /= 1000.0

    if unit == "B":
        return f"{int(value)} B"
    if value >= 100:
        return f"{value:.0f} {unit}"
    if value >= 10:
        return f"{value:.1f} {unit}"
    return f"{value:.2f} {unit}"


def format_rate(bytes_per_second: float) -> str:
    """Humanise a transfer rate: 14.2 MB/s."""
    return f"{format_bytes(bytes_per_second)}/s"


# ---------------------------------------------------------------------------
# Line renderers -- one per row of the section 3.2 banner, all pure
# ---------------------------------------------------------------------------


def render_step_banner(index: int, total: int, title: str) -> str:
    """`[ 3/7 ] <title>`, the line event 4 in section 2 had no equivalent of."""
    position = f"{index}/{total}" if total > 0 else f"{index}"
    return f"[ {position} ] {title}"


def render_phase_done(label: str, seconds: float) -> str:
    """A completed phase, collapsed to one line (LOCKED, section 3.2).

    Finished phases stay on screen as a running record of the install, so
    an operator can see where the time went and which phase a later failure
    followed.
    """
    duration = format_duration(seconds)
    return (
        f"{_PHASE_INDENT}✓ {label.ljust(_PHASE_LABEL_WIDTH)}"
        f"{duration.rjust(_DURATION_WIDTH)}"
    )


def render_phase_active(label: str) -> str:
    """The one phase currently running: only this one carries a bar."""
    return f"{_PHASE_INDENT}▸ {label}"


def render_bar(fraction: float, width: int = BAR_WIDTH) -> str:
    """Render a determinate bar; `fraction` is clamped to 0.0-1.0."""
    clamped = min(max(float(fraction), 0.0), 1.0)
    filled = round(clamped * width)
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)


def render_bytes_line(
    done: int,
    total: int,
    rate: float | None = None,
    eta: float | None = None,
) -> str:
    """The determinate row: bar, percent, done/total, and rate/ETA if known.

    `rate` and `eta` are optional because they are not knowable at the
    start of a transfer (section 5.3); their fields are omitted entirely
    rather than rendered as zero or as a guess.

    A missing or non-positive `total` raises: section 5.3 is REQUIRED, and
    a renderer that quietly substitutes a zero denominator produces exactly
    the bar that requirement exists to prevent. Callers that may not have a
    denominator use `render_spinner_line` instead, which is what
    `Progress.bytes` falls back to.
    """
    if not total or total <= 0:
        raise ValueError(
            f"no denominator to render a bar from (total={total!r}); "
            "use render_spinner_line for indeterminate work"
        )

    fraction = done / total
    # Rounded, but held one short of complete until the last byte actually
    # lands, in the bar as well as in the number: a bar that sits full
    # while the transfer is still running is the same broken promise as a
    # bar with no denominator behind it.
    percent = min(round(fraction * 100), 100)
    if done < total:
        percent = min(percent, 99)
        fraction = min(fraction, (BAR_WIDTH - 1) / BAR_WIDTH)

    fields = [
        f"{render_bar(fraction)} {percent:3d}%",
        f"{format_bytes(done)} / {format_bytes(total)}",
    ]
    if rate is not None:
        fields.append(format_rate(rate))
    if eta is not None:
        fields.append(format_clock(eta))
    return "   ".join(fields)


def render_percent_line(percent: float | None, note: str | None = None) -> str:
    """The determinate row for work that reports a percentage, not bytes.

    apt is the one operation in the section 5 table with a true denominator
    and no byte count behind it: `APT::Status-Fd` reports how far through
    the transaction it is, so the row is the bar, the percentage, and what
    apt is working on -- "Bar + percent + current package" in that table.

    `percent` is in 0-100. A missing or non-finite one raises for the same
    reason `render_bytes_line` does: section 5.3 is REQUIRED, and a
    renderer that quietly substitutes a zero produces exactly the bar that
    requirement exists to prevent. Indeterminate work uses
    `render_spinner_line`, which is what `Progress.percent` falls back to.
    """
    try:
        usable = percent is not None and math.isfinite(float(percent))
    except (TypeError, ValueError):
        usable = False
    if not usable:
        raise ValueError(
            f"no percentage to render a bar from (percent={percent!r}); "
            "use render_spinner_line for indeterminate work"
        )

    value = min(max(float(percent), 0.0), 100.0)
    fields = [f"{render_bar(value / 100.0)} {round(value):3d}%"]
    if note:
        fields.append(note)
    return "   ".join(fields)


def render_spinner_line(
    frame: str, name: str, elapsed: float, note: str | None = None
) -> str:
    """The indeterminate row: what is running, for how long, last output.

    This is what an operation with no denominator gets (section 5.3) -- a
    kernel module build, `dpkg --configure`, `update-initramfs`. It reports
    honest elapsed time and never a percentage.
    """
    line = f"{frame} {name}   {format_duration(elapsed)}"
    if note:
        line += f"   [ {note} ]"
    return line


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------


class Progress:
    """Renders where the install is, while it is there.

    Lifecycle, driven by the dispatch loop and by the running step:

        p.begin_step(3, "DeepStream SDK", phases=("download", "install"))
        p.phase(1)                    # section 9: indexes declared phases
        p.task("fetching SDK tarball")
        p.bytes(done, total)          # only with a real denominator
        p.percent(68.0, "libnvinfer10")   # the same, as a percentage
        p.line("...")                 # one line of command output
        p.end_step()

    Every one of those calls is optional beyond `begin_step`: a step that
    makes none of them still gets its banner, which is what lets the seven
    steps adopt phases one at a time (section 3.1).
    """

    def __init__(
        self,
        *,
        total_steps: int = 0,
        out: Any = sys.stderr,
        verbose: bool = False,
        non_interactive: bool = False,
        window_lines: int = WINDOW_LINES,
        clock: Callable[[], float] = time.monotonic,
        terminal_width: Callable[[], int] = _terminal_width,
    ) -> None:
        self._total_steps = int(total_steps)
        self._out = out
        self._verbose = bool(verbose)
        self._window_lines = max(int(window_lines), 0)
        self._clock = clock
        # Queried per redraw rather than cached: an operator resizing the
        # terminal mid-install must not leave the region wrapping.
        self._terminal_width = terminal_width

        # The single switch behind section 7: everything live is gated on
        # it, so a pipe, a CI run, a `tee` and a non-interactive run all
        # get plain text and not one escape sequence.
        self._live = _is_tty(out) and not non_interactive

        self._step_index = 0
        self._step_title = ""
        self._phases: tuple[str, ...] = ()
        self._phase_number = 0
        self._phase_label = ""
        self._phase_started: float | None = None
        self._completed: List[tuple[str, float]] = []

        self._task_name: str | None = None
        self._task_started: float | None = None
        self._done: int | None = None
        self._total: int | None = None
        self._samples: Deque[tuple[float, int]] = deque()
        self._percent: float | None = None
        self._percent_note: str | None = None

        self._window: Deque[str] = deque(maxlen=max(self._window_lines, 1))
        self._last_line: str | None = None
        self._frame = 0
        self._drawn = 0

    @property
    def live(self) -> bool:
        """True when this renderer is actually drawing (section 7).

        Exposed because an adapter driving a long operation has to know
        whether anything is reaching the operator: off a tty nothing is
        drawn, so the adapter owes the transcript a periodic plain line
        instead. Read-only -- the switch is decided once, in `__init__`.
        """
        return self._live

    # -- step and phase ----------------------------------------------------

    def begin_step(
        self, index: int, title: str, phases: Sequence[str] | Iterable[str] = ()
    ) -> None:
        """Announce a step and adopt its declared phase labels (3.1)."""
        self._close_phase()
        self._erase()

        self._step_index = int(index)
        self._step_title = title
        self._phases = tuple(phases)
        self._phase_number = 0
        self._phase_label = ""
        self._phase_started = None
        self._completed = []
        self._reset_task()

        banner = render_step_banner(index, self._total_steps, title)
        log.info(banner)
        if self._live:
            _write(self._out, "\n" + banner + "\n\n")

        # A step that declares no phases, or has not reached its first one
        # yet, still runs inside one unnamed phase (section 3.1) -- that is
        # what gives it a live region without any adoption work.
        self._phase_started = self._clock()
        self._draw()

    def phase(self, number: int) -> None:
        """Advance to declared phase `number` (1-based, section 9).

        An out-of-range index raises rather than rendering: a wrong
        denominator on screen is worse than a traceback, because the
        operator has no way to tell it is wrong.
        """
        if not 1 <= int(number) <= len(self._phases):
            raise IndexError(
                f"phase {number} is out of range for step "
                f"{self._step_index or '?'} with "
                f"{len(self._phases)} declared phase(s)"
            )
        self._start_phase(int(number), self._phases[int(number) - 1])

    def task(self, name: str) -> None:
        """Name the operation now running inside the current phase."""
        self._reset_task()
        name = sanitise(name)
        self._task_name = name
        self._task_started = self._clock()
        log.info(name)
        self._draw()

    def bytes(self, done: int, total: int | None) -> None:
        """Drive a determinate bar from a real denominator (section 5).

        A missing or non-positive `total` is not an error and not a reason
        to guess: the row falls back to the spinner, per section 5.3.
        """
        # A task is a byte transfer or a percentage-reporting transaction,
        # never both, so adopting one denominator drops the other rather
        # than leaving two bars competing for the activity row.
        self._percent = None
        self._percent_note = None

        if not total or int(total) <= 0:
            self._done = None
            self._total = None
            self._samples.clear()
            self._draw()
            return

        self._done = max(int(done), 0)
        self._total = int(total)

        now = self._clock()
        self._samples.append((now, self._done))
        while (
            len(self._samples) > 2
            and now - self._samples[0][0] > _RATE_WINDOW_S
        ):
            self._samples.popleft()

        self._draw()

    def percent(self, value: float | None, note: str | None = None) -> None:
        """Drive a determinate bar from a percentage (section 5).

        The sibling of `bytes()` for an operation that reports how far
        through it is rather than how many bytes it has moved -- apt on
        `APT::Status-Fd` is the one in the section 5 table. `value` is in
        0-100 and `note` is what the operation is working on, rendered
        beside the bar.

        `None`, or a value that is not a finite number, is not an error and
        not a reason to guess: the row falls back to the spinner, per
        section 5.3. The caller stays responsible for the value only ever
        rising -- see `follow_apt`, which is where apt's two independent
        sweeps are mapped onto one bar that cannot go backwards.
        """
        # A task is a byte transfer or a percentage-reporting transaction,
        # never both, so adopting one denominator drops the other rather
        # than leaving two bars competing for the activity row.
        self._done = None
        self._total = None
        self._samples.clear()

        try:
            usable = value is not None and math.isfinite(float(value))
        except (TypeError, ValueError):
            usable = False

        if not usable:
            self._percent = None
            self._percent_note = None
            self._draw()
            return

        self._percent = min(max(float(value), 0.0), 100.0)
        self._percent_note = sanitise(note) if note else None
        self._draw()

    def line(self, text: str) -> None:
        """Feed one line of command output to the window (4.2, 8).

        The line is sanitised on the way in, so no colour or carriage
        return a child process emitted can reach a transcript, a pipe, or
        the redraw arithmetic. It is not truncated here: off a tty the full
        line is the point, and on a tty the live region truncates to the
        terminal width at draw time.

        Redaction is a separate concern and is not re-implemented here: it
        belongs to `shellout.py`, and the caller has already applied it.
        """
        # Trailing whitespace is stripped after sanitising, not before: an
        # erase-to-end-of-line escape leaves its own behind.
        text = sanitise(text).rstrip()
        self._last_line = text

        if not self._live:
            _write(self._out, text + "\n")
            return
        if self._verbose:
            # Verbose keeps the full stream on screen, so the line scrolls
            # above the live region instead of into the window.
            self._erase()
            _write(self._out, text + "\n")
            self._draw()
            return

        self._window.append(text)
        self._draw()

    def tick(self) -> None:
        """Advance the spinner and redraw; a no-op when nothing is live."""
        self._frame += 1
        self._draw()

    def end_step(self) -> None:
        """Collapse the last phase and leave the live region behind."""
        self._close_phase()
        self._erase()
        self._reset_task()
        if self._live:
            _write(self._out, "\n")

    # -- internals ---------------------------------------------------------

    def _reset_task(self) -> None:
        self._task_name = None
        self._task_started = None
        self._done = None
        self._total = None
        self._samples.clear()
        self._percent = None
        self._percent_note = None
        self._window.clear()
        self._last_line = None

    def _start_phase(self, number: int, label: str) -> None:
        self._close_phase()
        self._reset_task()
        self._phase_number = number
        self._phase_label = label
        self._phase_started = self._clock()

        # Section 3.3: drawn or not, the transition is logged, so the
        # transcript carries the full phase sequence of a run.
        log.info(f"{self._position()}: {label}")
        self._draw()

    def _close_phase(self) -> None:
        if self._phase_started is None:
            return
        elapsed = self._clock() - self._phase_started
        label = self._phase_label
        self._phase_started = None
        if not label:
            # The implicit unnamed phase has nothing to collapse to.
            return

        self._completed.append((label, elapsed))
        log.info(f"{self._position()} done: {label} ({format_duration(elapsed)})")
        if self._live:
            self._erase()
            _write(self._out, render_phase_done(label, elapsed) + "\n")

    def _position(self) -> str:
        where = f"phase {self._phase_number}/{len(self._phases)}"
        if not self._step_index:
            return where
        step = (
            f"{self._step_index}/{self._total_steps}"
            if self._total_steps > 0
            else f"{self._step_index}"
        )
        return f"step {step} {where}"

    def _rate(self) -> float | None:
        """Bytes per second, or None while the samples cannot support one."""
        if len(self._samples) < 2:
            return None
        first_at, first_done = self._samples[0]
        last_at, last_done = self._samples[-1]
        span = last_at - first_at
        moved = last_done - first_done
        if span < _MIN_RATE_SPAN_S or moved <= 0:
            return None
        return moved / span

    def _activity_line(self) -> str:
        if self._total and self._done is not None:
            rate = self._rate()
            eta = None
            if rate:
                eta = max(self._total - self._done, 0) / rate
            return _ACTIVITY_INDENT + render_bytes_line(
                self._done, self._total, rate, eta
            )

        if self._percent is not None:
            return _ACTIVITY_INDENT + render_percent_line(
                self._percent, self._percent_note
            )

        started = self._task_started
        if started is None:
            started = self._phase_started
        elapsed = self._clock() - started if started is not None else 0.0
        name = self._task_name or self._phase_label or self._step_title
        frame = SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]
        return _ACTIVITY_INDENT + render_spinner_line(
            frame, name, elapsed, self._last_line
        )

    def _frame_lines(self) -> List[str]:
        if self._phase_started is None:
            return []

        lines = [render_phase_active(self._phase_label or self._step_title)]
        lines.append(self._activity_line())
        if not self._verbose and self._window_lines:
            window = list(self._window)[-self._window_lines :]
            lines.extend(_WINDOW_INDENT + text for text in window)
            # Blank rows keep the region exactly `window_lines` tall in
            # every phase (LOCKED, section 8).
            lines.extend("" for _ in range(self._window_lines - len(window)))

        # Every row is one terminal row, or the cursor-up count below is
        # wrong by however many of them wrapped.
        limit = max(self._terminal_width() - 1, 1)
        return [_truncate(row, limit) for row in lines]

    def _erase(self) -> None:
        if not self._live or not self._drawn:
            return
        _write(self._out, "\r" + _CURSOR_UP.format(n=self._drawn) + _CLEAR_BELOW)
        self._drawn = 0

    def _draw(self) -> None:
        if not self._live:
            return
        lines = self._frame_lines()
        self._erase()
        if lines:
            _write(self._out, "\n".join(lines) + "\n")
        self._drawn = len(lines)


# ---------------------------------------------------------------------------
# Download byte-progress adapter (section 5.1)
# ---------------------------------------------------------------------------


# Half a second: fast enough that a 14 MB/s transfer visibly moves between
# redraws, slow enough that the poll itself is a rounding error next to the
# download. The measurement is one `stat()` on a file the installer is
# already writing, not a parse of curl's output -- so this works unchanged
# for every future download whatever tool fetches it.
DEFAULT_DOWNLOAD_POLL_S = 0.5

# Off a tty nothing is drawn at all (section 7), so this line is the only
# sign of life a multi-minute download gives. One plain line this often
# keeps event 3 in section 2 -- "it has hung, I will interrupt it" -- from
# coming back on a piped or CI run. It paces the transcript at the same rate
# on a tty, where the line is recorded rather than printed (section 7.1).
DOWNLOAD_LOG_INTERVAL_S = 30.0

# Floor on `poll_s`, so a caller passing 0 cannot turn this into a busy loop
# against a real clock (the `waitui.py` precedent).
_MIN_DOWNLOAD_POLL_S = 0.01

# HEAD is cheap and the installer only ever wants the header, but a server
# that is slow to answer must not hold up a download that would have worked
# without a bar.
_CONTENT_LENGTH_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class DownloadOutcome:
    """What the poll loop observed, for the caller that owns the transfer.

    `bytes_done` is the raw final size on disk, deliberately *not* clamped
    to `total`: the renderer must never show more than 100%, but a caller
    verifying the fetch needs the real number to notice that the server
    sent something other than what it declared.
    """

    bytes_done: int
    total: int | None
    elapsed_s: float
    polls: int

    @property
    def complete(self) -> bool:
        """True only when a real denominator existed and was reached."""
        return self.total is not None and self.bytes_done >= self.total


def part_size(path: "pathlib.Path | str") -> int:
    """Bytes currently on disk at `path`; 0 when it is not there yet.

    A missing file is the normal state for the first poll or two -- the
    transfer has been started but has not created its `.part` file yet --
    so it reads as 0 rather than raising. An unreadable or vanished file
    reads as 0 for the same reason: this is polled in a loop and must
    never be the thing that fails an install.
    """
    try:
        return max(pathlib.Path(path).stat().st_size, 0)
    except OSError:
        return 0


def default_content_length_probe(
    url: str, *, timeout: float = _CONTENT_LENGTH_TIMEOUT_S
) -> str | None:
    """Ask `url` for its `Content-Length` header, or None if it will not say.

    Injected as `probe` in `content_length()` so no test reaches the
    network. Failures are swallowed here rather than raised: a denominator
    is a nicety, and a download that would have succeeded must not fail
    because a HEAD request did not.

    The URL is never logged from this module. Some download URLs are
    signed and carry a credential in their query string; redaction lives
    in `webapp.py`, and the simplest way to honour it here is to say
    nothing.
    """
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.headers.get("Content-Length")
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        return None


def content_length(
    url: str,
    *,
    probe: Callable[[str], Any] = default_content_length_probe,
) -> int | None:
    """The declared size of `url`, or None when there is no usable one.

    None is the honest answer for every failure mode -- no header, an
    empty or non-numeric one, a zero or negative one, a probe that raised
    -- and `follow_download` turns None into the spinner. Section 5.3 is
    the reason this never falls back to a guess: a denominator that was
    invented is indistinguishable on screen from one that was measured.
    """
    try:
        raw = probe(url)
    except Exception:
        # Any probe failure at all, including one from an injected probe.
        # See the docstring: no denominator is a supported outcome, an
        # exception escaping into a step is not.
        return None

    if raw is None:
        return None
    try:
        declared = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return declared if declared > 0 else None


def render_download_log_line(
    name: str, done: int, total: int | None, elapsed: float
) -> str:
    """The off-tty line for a download in flight (section 7).

    Same honesty rule as the drawn row: a percentage appears only when
    there is a denominator behind it.
    """
    if total and total > 0:
        shown = min(done, total)
        # The same rounding and the same hold-at-99 as `render_bytes_line`:
        # the drawn row and the logged line describe one transfer, and a
        # transcript that disagrees with the screen by a point is a bug
        # report waiting to happen.
        percent = min(round(shown * 100 / total), 100)
        if percent >= 100 and shown < total:
            percent = 99
        return (
            f"{name}: {format_bytes(shown)} / {format_bytes(total)} "
            f"({percent}%) after {format_duration(elapsed)}"
        )
    return f"{name}: {format_bytes(done)} after {format_duration(elapsed)}"


def render_download_done_line(
    name: str, done: int, total: int | None, elapsed: float
) -> str:
    """The closing line for a finished transfer (section 7.1).

    Unlike the periodic lines this one is **never clamped**, and says so.
    Those clamp to the declared total because a bar reading 115% looks like
    a bug in the installer rather than in the server; a transcript that
    ended "100% of 606 MB" and then "620 MB" would leave an operator with no
    way to tell which of the two numbers was real. So the true byte count is
    what is recorded, it is labelled as received, and a total it disagrees
    with -- short or over -- is named beside it as the declared one.
    """
    line = f"{name}: received {format_bytes(done)} in {format_duration(elapsed)}"
    if total and total > 0 and done != total:
        line += f" ({format_bytes(total)} declared)"
    return line


def _say(renderer: "Progress | None", message: str) -> None:
    """Emit one progress line, to the screen when there is room for it.

    Suppressed for rendering reasons is not the same as dropped (section
    7.1). Off a tty the drawn region does not exist, so this line is the
    only signal a long operation gives and it goes to `log.info`, which
    writes the screen and the transcript together. On a tty it would fight
    the live region's cursor arithmetic, so it is recorded and not printed:
    an interactive install is the one an operator runs by hand and asks
    about afterwards, and without this its transcript reads task name,
    nothing at all for the length of a multi-gigabyte transaction, phase
    done.
    """
    if renderer is None or not renderer.live:
        log.info(message)
    else:
        transcript("info", message)


def follow_download(
    path: "pathlib.Path | str",
    total: int | None,
    *,
    is_running: Callable[[], bool],
    renderer: "Progress | None" = None,
    task: str | None = None,
    poll_s: float = DEFAULT_DOWNLOAD_POLL_S,
    log_interval_s: float = DOWNLOAD_LOG_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    size_of: Callable[[Any], int] = part_size,
) -> DownloadOutcome:
    """Watch a download land at `path` and drive the bar from its size.

    Section 5.1, mechanically: the installer already writes every download
    to a `.part` path (`STEP-1` section 5.2), so percentage is
    `size(.part) / Content-Length` sampled on an interval. Nothing here
    parses a downloader's output -- the file is ours, `stat()` reports it
    identically whatever tool is writing it, and a future download that
    switches from curl to anything else keeps its bar for free.

    This owns no transfer. The caller starts curl (in a thread, or as a
    `Popen`) and hands over `is_running`, a predicate that is true while
    that transfer is still going; the loop returns once it goes false.
    `clock`, `sleep` and `size_of` are injected on the `waitui.py` pattern
    so tests drive a whole download without sleeping or downloading.

    `total` of None, 0 or negative is not an error: it means no true
    denominator exists, and the rendering falls back to the spinner the
    renderer already provides (section 5.3). That is also what an
    unavailable `Content-Length` degrades to, via `content_length()`.
    """
    denominator = int(total) if total and int(total) > 0 else None
    interval = max(float(poll_s), _MIN_DOWNLOAD_POLL_S)
    log_every = max(float(log_interval_s), 0.0)
    # Sanitised for the same reason `task()` sanitises: this name reaches
    # the transcript, which section 7 requires to stay free of escapes,
    # whichever of the two sinks below carries it there.
    name = sanitise(task or pathlib.Path(path).name)

    if renderer is not None:
        if task is not None:
            renderer.task(task)
        if denominator is None:
            # Clear any denominator a previous transfer left behind, so
            # the spinner path is selected from the very first frame
            # rather than after one stale bar.
            renderer.bytes(0, None)

    def say(message: str) -> None:
        _say(renderer, message)

    started = clock()
    next_log_at = 0.0
    polls = 0
    done = 0
    elapsed = 0.0

    while True:
        # Sampled before the size, not after: if the transfer has already
        # stopped, whatever `stat()` reports next cannot grow any further,
        # so this ordering guarantees the final frame shows the final
        # size. The other order can end the loop on a stale sample and
        # leave the bar parked short of 100%.
        running = bool(is_running())
        done = max(int(size_of(path)), 0)
        polls += 1
        elapsed = clock() - started

        if renderer is not None and denominator is not None:
            # Clamped, because a mis-declared Content-Length is a real
            # failure mode and "620 MB / 606 MB  115%" reads as a bug in
            # the installer rather than in the server.
            renderer.bytes(min(done, denominator), denominator)
        elif renderer is not None:
            # No denominator: the spinner is the whole rendering, and it
            # needs a frame advance per poll to look alive.
            renderer.tick()

        if elapsed >= next_log_at:
            say(render_download_log_line(name, done, denominator, elapsed))
            next_log_at = elapsed + log_every

        if not running:
            break
        sleep(interval)

    say(render_download_done_line(name, done, denominator, elapsed))

    return DownloadOutcome(
        bytes_done=done, total=denominator, elapsed_s=elapsed, polls=polls
    )


# ---------------------------------------------------------------------------
# apt Status-Fd percentage adapter (section 5.2)
# ---------------------------------------------------------------------------


# `apt-get install -o APT::Status-Fd=<fd>` writes one machine-readable
# record per line to that descriptor, in parallel with its human output:
#
#     dlstatus:1:12.5000:Retrieving file 3 of 24
#     pmstatus:libnvinfer10:68.0000:Setting up libnvinfer10 (10.16.0.72-1+cuda13.2)
#
# That is the denominator event 3 in section 2 lacked -- the transaction an
# operator interrupted because several gigabytes of captured apt output
# presented exactly like a hung process, and which then needed
# `dpkg --configure -a` to recover.

# The two sweeps are independent: `dlstatus` runs 0 to 100 while apt fetches
# archives, then `pmstatus` runs 0 to 100 again while dpkg unpacks and
# configures them. Feeding both to one bar naively resets it halfway, which
# is worse than no bar at all -- so they are rendered as two segments of a
# single 0-100 range, download first. The share is a convention, not a
# measurement, and it is the only number here that is: both percentages
# inside their segments are apt's own.
APT_DOWNLOAD_SHARE = 0.5

# Same interval and the same reason as `DOWNLOAD_LOG_INTERVAL_S`: off a tty
# nothing is drawn, so this line is all a multi-minute transaction gives,
# and on a tty it paces the transcript instead of the screen (section 7.1).
APT_LOG_INTERVAL_S = 30.0

# Only these two carry a percentage this module will draw. `pmerror` is
# read for its description alone -- an error apt reports mid-transaction is
# the one line an operator most needs to see -- and every other record kind
# (`media-change`, `pmconffile`, dpkg's own `status:`) is ignored rather
# than guessed at, per the parse-defensively rule.
_APT_BAR_KINDS = frozenset({"dlstatus", "pmstatus"})
_APT_NOTE_KINDS = frozenset({"pmerror"})
_APT_KINDS = _APT_BAR_KINDS | _APT_NOTE_KINDS

# How many colon-separated fields the item may occupy before the percentage.
# Normally one, but apt qualifies package names with an architecture
# (`libnvinfer10:amd64`), so the percentage is not reliably field 2.
_APT_MAX_ITEM_FIELDS = 3


@dataclass(frozen=True)
class AptStatus:
    """One parsed `APT::Status-Fd` record.

    `percent` is None whenever the record carried no usable number, which
    is not an error: the description alone is still worth showing, and
    section 5.3 is what governs whether a bar appears.
    """

    kind: str
    item: str
    percent: float | None
    description: str

    @property
    def drives_bar(self) -> bool:
        """True only for a record that can move a real denominator."""
        return self.kind in _APT_BAR_KINDS and self.percent is not None


@dataclass(frozen=True)
class AptOutcome:
    """What the status stream reported, for the caller that owns apt.

    `percent` is the last percentage rendered, so `had_denominator` says
    whether a bar was ever honest to draw. It is deliberately not forced to
    100 at the end: the stream ending means apt closed the descriptor, and
    whether the transaction succeeded is the caller's exit status to read,
    not this adapter's to infer.
    """

    lines: int
    parsed: int
    percent: int | None
    description: str | None
    had_download_phase: bool
    elapsed_s: float

    @property
    def had_denominator(self) -> bool:
        """True when a percentage was reported and a bar was drawn."""
        return self.percent is not None


def apt_status_fd_args(fd: int) -> tuple[str, ...]:
    """The `apt-get` options that turn the stream on, for the caller.

    Kept beside the parser so the descriptor number and the thing that
    reads it cannot drift apart. The caller owns the pipe itself: it makes
    the descriptor inheritable (`pass_fds`) and hands the read end to
    `follow_apt`.
    """
    return ("-o", f"APT::Status-Fd={int(fd)}")


def _apt_percent(field: str) -> float | None:
    """A status field as a percentage, or None when it is not one.

    Non-finite values are rejected as hard as unparseable ones: `float()`
    accepts "nan" and "inf" happily, and either would reach `round()` in
    the renderer and raise, from a line that came off a pipe.
    """
    text = str(field).strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return min(max(value, 0.0), 100.0)


def parse_apt_status(line: Any) -> "AptStatus | None":
    """Parse one `APT::Status-Fd` line, or None for anything unrecognised.

    This never raises. It is fed a pipe that a subprocess writes and that
    dies mid-line when that subprocess is killed, so a truncated, empty or
    entirely foreign line has to read as "nothing to say" rather than as an
    exception surfacing from inside a phase.

    The split is bounded from both ends rather than greedy. A description
    contains colons of its own (`Setting up libnvinfer10:amd64`), so the
    tail is never split; and the item is not reliably one field either,
    because apt qualifies package names with an architecture. What is
    unambiguous is the percentage, so the item ends at the first field
    after it that parses as a number -- and the scan starts at field 2, not
    field 1, because `dlstatus` numbers its items and item "1" would
    otherwise read as "1 percent".
    """
    try:
        # The line terminator comes off first: `sanitise` keeps only what
        # follows the last carriage return, so a CRLF-terminated record
        # would otherwise sanitise away to nothing.
        text = sanitise(str(line).rstrip("\r\n")).strip()
    except Exception:  # pragma: no cover -- an object whose str() raises
        return None
    if not text:
        return None

    fields = text.split(":")
    if len(fields) < 3:
        return None

    kind = fields[0].strip().lower()
    if kind not in _APT_KINDS:
        return None

    limit = min(len(fields), 2 + _APT_MAX_ITEM_FIELDS)
    for index in range(2, limit):
        percent = _apt_percent(fields[index])
        if percent is None:
            continue
        return AptStatus(
            kind=kind,
            item=":".join(fields[1:index]).strip(),
            percent=percent,
            description=":".join(fields[index + 1 :]).strip(),
        )

    # No number anywhere it could be: the record still names something, and
    # the description is the half an operator can use.
    return AptStatus(
        kind=kind,
        item=fields[1].strip(),
        percent=None,
        description=":".join(fields[2:]).strip(),
    )


def apt_fraction(
    kind: str, percent: float | None, *, has_download_phase: bool
) -> float | None:
    """Map one sweep onto the single 0.0-1.0 range the bar renders.

    The mapping, explicitly:

    | transaction | `dlstatus` p | `pmstatus` p |
    |-------------|--------------|--------------|
    | with a download phase | `p * SHARE` | `SHARE + p * (1 - SHARE)` |
    | without one | (not reached) | `p` |

    A transaction whose archives are all cached emits no `dlstatus` at all,
    so committing to the two-segment split unconditionally would start its
    bar at 50 percent. `has_download_phase` is therefore learned from the
    stream -- it is the kind of the *first* record that carried a
    percentage -- and the install sweep owns the whole range when no
    download was ever reported.

    This is monotonic within each segment and across the transition, but
    not across a stream that interleaves the two kinds; `follow_apt` holds
    the high-water mark that makes the rendered value monotonic outright.
    """
    if kind not in _APT_BAR_KINDS or percent is None:
        return None
    fraction = min(max(float(percent), 0.0), 100.0) / 100.0
    if not has_download_phase:
        return fraction
    if kind == "dlstatus":
        return fraction * APT_DOWNLOAD_SHARE
    return APT_DOWNLOAD_SHARE + fraction * (1.0 - APT_DOWNLOAD_SHARE)


def _apt_note(status: AptStatus) -> str | None:
    """What to render beside the bar: the package, when there is one.

    `dlstatus` numbers its items, and "1" on the bar row says nothing the
    description does not already say better.
    """
    item = status.item.strip()
    if not item or item.isdigit():
        return None
    return item


def render_apt_log_line(
    name: str, percent: int | None, description: str | None, elapsed: float
) -> str:
    """The off-tty line for a transaction in flight (section 7).

    Same honesty rule as the drawn row: a percentage appears only when apt
    reported one. Without it the line still says how long this has been
    running and what apt last said it was doing, which is the whole
    difference between a long transaction and a hung one.
    """
    if percent is None:
        line = f"{name}: running for {format_duration(elapsed)}"
    else:
        line = f"{name}: {int(percent)}% after {format_duration(elapsed)}"
    if description:
        line += f" - {description}"
    return line


def render_apt_done_line(name: str, percent: int | None, elapsed: float) -> str:
    """The closing line for a finished status stream (section 7.1).

    "Finished" here means apt closed the descriptor, which is all this
    adapter observed. It never rounds the last percentage up to 100 to make
    the record look tidy: a stream that stopped at 62 percent is a
    transaction that was killed, and the transcript of a failed install is
    the one place that has to say so.
    """
    line = f"{name}: finished in {format_duration(elapsed)}"
    if percent is None:
        line += " (apt reported no percentage)"
    elif percent < 100:
        line += f" (apt last reported {percent}%)"
    return line


def follow_apt(
    lines: Iterable[str],
    *,
    renderer: "Progress | None" = None,
    task: str | None = None,
    log_interval_s: float = APT_LOG_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
) -> AptOutcome:
    """Consume apt's status stream and drive the bar from it (section 5.2).

    This owns no transaction. The caller runs `apt-get` with
    `apt_status_fd_args(fd)`, keeps the write end of a pipe open for it,
    and hands the read end here as any iterable of lines; the loop returns
    when the stream ends. `clock` is injected on the `waitui.py` pattern so
    tests drive a whole transaction without a clock or an apt.

    Two properties it guarantees, and a third it inherits:

    1. **The bar never goes backwards.** apt's download and install sweeps
       each run 0 to 100 independently (`apt_fraction`), and on top of that
       mapping a high-water mark clamps the rendered value, so no ordering
       of records -- including a `dlstatus` arriving after the install
       phase began -- can make it drop.
    2. **No fabricated progress (section 5.3, REQUIRED).** A stream that
       yields no parseable percentage at all drives no bar. It falls back
       to the spinner, which reports honest elapsed time and apt's most
       recent description, and `AptOutcome.had_denominator` says so
       afterwards.
    3. Each new description goes to the rolling window (section 3.2's
       `Setting up libnvinfer10 (10.16.0.72-1+cuda13.2)` rows) via
       `Progress.line`, so it is sanitised, truncated and recorded exactly
       like streamed command output. Consecutive repeats are dropped: apt
       restates the same description across dozens of percentage updates,
       and eight identical window rows show less than one does.
    """
    name = sanitise(task or "apt")
    log_every = max(float(log_interval_s), 0.0)

    if renderer is not None:
        if task is not None:
            renderer.task(task)
        # Clear any denominator the previous task left behind -- a byte
        # bar included -- so the spinner path is selected from the first
        # frame rather than after one stale bar.
        renderer.percent(None)

    started = clock()
    next_log_at = 0.0
    seen = 0
    parsed = 0
    percent: int | None = None
    description: str | None = None
    has_download_phase = False
    committed = False
    high_water: float | None = None

    iterator = iter(lines)
    while True:
        try:
            raw = next(iterator)
        except StopIteration:
            break
        except (OSError, ValueError):
            # The descriptor went away underneath us: apt exited, or the
            # pipe was closed. Whatever was rendered stands, and the
            # caller owns the transaction's exit status.
            break

        seen += 1
        fraction = None
        status = parse_apt_status(raw)
        if status is not None:
            parsed += 1

            if status.description and status.description != description:
                description = status.description
                if renderer is not None:
                    renderer.line(status.description)

            if status.drives_bar:
                if not committed:
                    # The first record carrying a percentage decides
                    # whether this transaction has a download segment at
                    # all -- see `apt_fraction`.
                    committed = True
                    has_download_phase = status.kind == "dlstatus"
                fraction = apt_fraction(
                    status.kind,
                    status.percent,
                    has_download_phase=has_download_phase,
                )

        if fraction is not None:
            high_water = (
                fraction if high_water is None else max(high_water, fraction)
            )
            percent = int(round(high_water * 100))
            if renderer is not None:
                renderer.percent(percent, _apt_note(status))
        elif renderer is not None and high_water is None:
            # No denominator yet, so the spinner is the whole rendering
            # and needs a frame advance to look alive -- including while
            # every line so far has been one this module does not read.
            renderer.tick()

        elapsed = clock() - started
        if elapsed >= next_log_at:
            _say(renderer, render_apt_log_line(name, percent, description, elapsed))
            next_log_at = elapsed + log_every

    elapsed = clock() - started
    _say(renderer, render_apt_done_line(name, percent, elapsed))

    return AptOutcome(
        lines=seen,
        parsed=parsed,
        percent=percent,
        description=description,
        had_download_phase=has_download_phase,
        elapsed_s=elapsed,
    )
