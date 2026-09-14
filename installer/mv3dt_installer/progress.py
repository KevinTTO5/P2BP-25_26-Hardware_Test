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
        `line` / `tick` / `end_step`; `phase`, `task` and `bytes` are the
        step-author contract in doc 08 section 9.
    sanitise(text) -- strip control characters out of command output.
    format_duration / format_clock / format_bytes / format_rate -- pure
        formatters.
    render_step_banner / render_phase_done / render_phase_active /
        render_bar / render_bytes_line / render_spinner_line -- pure line
        renderers, one per row of the section 3.2 banner.

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
   transcript to work from.

The renderer never sleeps and starts no thread, so there is no `sleep` to
inject: the caller drives redraws by calling `tick()`, and `clock` is
injected so tests get exact elapsed times instead of wall-clock ones.
"""

from __future__ import annotations

import re
import shutil
import sys
import time
from collections import deque
from typing import Any, Callable, Deque, Iterable, List, Sequence

from .logs import log

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
    "render_spinner_line",
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

        self._window: Deque[str] = deque(maxlen=max(self._window_lines, 1))
        self._last_line: str | None = None
        self._frame = 0
        self._drawn = 0

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
