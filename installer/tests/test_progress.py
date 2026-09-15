"""Tests for `mv3dt_installer.progress` (the live progress renderer).

Every test injects `clock` and writes to a fake stream, so elapsed times are
exact rather than wall-clock-dependent and no test touches a real terminal.
The autouse `_forbid_real_sleep` fixture makes an accidental real sleep fail
loudly -- the renderer is supposed to never sleep at all.

Run with:
    cd installer && python3 -m pytest tests/test_progress.py -v
"""

from __future__ import annotations

import io
import os
import pathlib
import re
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mv3dt_installer import logs, progress  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_transcript_state():
    """Every test starts with no transcript open, and leaves none dangling."""
    logs._transcript_path = None
    yield
    logs._transcript_path = None


@pytest.fixture(autouse=True)
def _force_no_colour(monkeypatch):
    """Default all tests to a non-tty stderr so plain text is asserted on."""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)


@pytest.fixture(autouse=True)
def _reset_live_writer():
    """A live renderer registers itself with `logs`; none may outlive its
    test. The registration is scoped to the stream it was made for, so a
    stale one is inert anyway, but nothing here depends on that."""
    logs.clear_live_writer()
    yield
    logs.clear_live_writer()


@pytest.fixture(autouse=True)
def _forbid_real_sleep(monkeypatch):
    """No test in this file may block on the real clock."""

    def _boom(seconds):  # pragma: no cover -- only runs on a test bug
        raise AssertionError(f"test called the real time.sleep({seconds!r})")

    monkeypatch.setattr(time, "sleep", _boom)


class FakeClock:
    """An injected `clock` the test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeTty(io.StringIO):
    """A StringIO that claims to be an interactive terminal."""

    def isatty(self) -> bool:
        return True


# The live region is erased with one cursor-up + clear-below pair, so
# splitting on it yields the successive frames the renderer drew.
_ERASE = re.compile(r"\r\033\[\d+A\033\[J")
_ANSI = re.compile(r"\033\[[0-9;]*[A-Za-z]")

_PHASES = ("base packages", "CUDA repo and toolkit", "TensorRT and cuDNN")

# Forced terminal width for every test, so the live region's truncation is
# asserted against a known number of columns instead of the real terminal.
_COLUMNS = 80


def _tty_progress(clock: FakeClock, out: FakeTty, **kwargs) -> progress.Progress:
    kwargs.setdefault("terminal_width", lambda: _COLUMNS)
    return progress.Progress(
        total_steps=7, out=out, clock=clock, **kwargs
    )


def _last_frame(out: FakeTty) -> list[str]:
    """The live region exactly as it stands after the final redraw.

    The tail after the last erase can also carry static output the
    renderer scrolled out first (a collapsed phase line, a verbose command
    line), so the frame proper starts at the active-phase row.
    """
    tail = _ERASE.split(out.getvalue())[-1]
    lines = tail.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("  ▸ "):
            return lines[index:]
    return lines


# Cursor-up and clear-below, the two escapes the renderer erases with.
_CSI = re.compile(r"\033\[(\d*)([A-Za-z])")


def _screen(raw: str) -> list[str]:
    """Replay a byte stream the way a terminal would, and return its rows.

    A StringIO accumulates everything ever written to it, including text
    the renderer went on to erase, so it cannot answer "is this line still
    on screen". This applies the cursor moves instead: `ESC[nA` walks up n
    rows, `ESC[J` clears from the cursor to the end of the screen, and
    everything else is written at the cursor. Colour is ignored -- it does
    not move the cursor.
    """
    rows = [""]
    row = col = 0
    index = 0
    while index < len(raw):
        match = _CSI.match(raw, index)
        if match:
            count = int(match.group(1) or 1)
            code = match.group(2)
            if code == "A":
                row = max(row - count, 0)
            elif code == "J":
                rows[row] = rows[row][:col]
                del rows[row + 1 :]
            index = match.end()
            continue
        char = raw[index]
        index += 1
        if char == "\n":
            row += 1
            col = 0
            while len(rows) <= row:
                rows.append("")
        elif char == "\r":
            col = 0
        else:
            line = rows[row].ljust(col)
            rows[row] = line[:col] + char + line[col + 1 :]
            col += 1
    return rows


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def test_format_duration_matches_the_banner_examples():
    assert progress.format_duration(0) == "0s"
    assert progress.format_duration(12) == "12s"
    assert progress.format_duration(12.9) == "12s"
    assert progress.format_duration(4) == "4s"
    assert progress.format_duration(60) == "1m00s"
    assert progress.format_duration(221) == "3m41s"
    assert progress.format_duration(362) == "6m02s"
    assert progress.format_duration(3725) == "1h02m"
    assert progress.format_duration(-5) == "0s"


def test_format_clock_rounds_to_the_nearest_second():
    assert progress.format_clock(13.66) == "0:14"
    assert progress.format_clock(0) == "0:00"
    assert progress.format_clock(725) == "12:05"
    assert progress.format_clock(3723) == "1:02:03"
    assert progress.format_clock(-1) == "0:00"


def test_format_bytes_loses_precision_as_the_value_grows():
    assert progress.format_bytes(0) == "0 B"
    assert progress.format_bytes(512) == "512 B"
    assert progress.format_bytes(1500) == "1.50 KB"
    assert progress.format_bytes(14_200_000) == "14.2 MB"
    assert progress.format_bytes(412_000_000) == "412 MB"
    assert progress.format_bytes(1_050_000_000) == "1.05 GB"
    assert progress.format_bytes(-1) == "0 B"


def test_format_rate_is_a_humanised_size_per_second():
    assert progress.format_rate(14_200_000) == "14.2 MB/s"


# ---------------------------------------------------------------------------
# sanitise -- caller-supplied output is never trusted (section 7)
# ---------------------------------------------------------------------------


# Real shapes: apt colours its progress line, curl redraws its meter with a
# carriage return, dpkg emits both.
_COLOURED = "\033[1;32mSetting up\033[0m libnvinfer10 \033[K"
_CARRIAGE_RETURNED = "  0 412M    0 1024k    0     0  [ 42%]\r partial"


def test_sanitise_strips_colour_and_leaves_the_text():
    assert progress.sanitise(_COLOURED) == "Setting up libnvinfer10 "


def test_sanitise_keeps_only_what_survived_a_carriage_return():
    assert progress.sanitise(_CARRIAGE_RETURNED) == " partial"


def test_sanitise_removes_every_remaining_control_character():
    dirty = "a\x00b\x07c\x1bd\x9fe\nf"

    cleaned = progress.sanitise(dirty)

    assert cleaned == "abcdef"
    assert not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in cleaned)


def test_sanitise_expands_tabs_so_row_widths_stay_predictable():
    assert progress.sanitise("a\tb") == "a b"


def test_sanitise_leaves_ordinary_output_alone():
    plain = "Setting up tensorrt-dev (10.16.0.72-1+cuda13.2)"

    assert progress.sanitise(plain) == plain


def test_dirty_lines_reach_a_non_tty_stream_clean():
    out, clock = io.StringIO(), FakeClock()
    bar = progress.Progress(total_steps=7, out=out, clock=clock)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)

    bar.line(_COLOURED)
    bar.line(_CARRIAGE_RETURNED)

    rendered = out.getvalue()
    assert rendered == "Setting up libnvinfer10\n partial\n"
    assert "\033" not in rendered
    assert "\r" not in rendered


def test_dirty_lines_reach_the_window_and_the_spinner_note_clean():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)
    bar.task("installing")

    bar.line(_COLOURED)

    frame = _last_frame(out)
    assert frame[2] == "        Setting up libnvinfer10"
    # The most recent line is also the spinner's note, so it has to be
    # clean there too.
    assert "[ Setting up libnvinfer10 ]" in frame[1]
    for row in frame:
        assert "\033" not in row and "\r" not in row


def test_a_dirty_line_still_occupies_exactly_one_row():
    """An escape in the text would desynchronise the redraw arithmetic."""
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)

    bar.line(_COLOURED + "\r" + _COLOURED)

    assert len(_last_frame(out)) == 2 + progress.WINDOW_LINES


def test_a_dirty_task_name_is_cleaned_too():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)

    bar.task("\033[31minstalling\033[0m")

    assert "installing" in _last_frame(out)[1]
    # The renderer's own cursor moves are the only escapes on the stream.
    assert "\033[31m" not in out.getvalue()
    assert "\033[0m" not in out.getvalue()


# ---------------------------------------------------------------------------
# Line renderers
# ---------------------------------------------------------------------------


def test_render_step_banner_states_position_in_the_install():
    assert (
        progress.render_step_banner(1, 7, "Prerequisites")
        == "[ 1/7 ] Prerequisites"
    )
    # Unknown total: still say which step, never invent a denominator.
    assert progress.render_step_banner(3, 0, "AMC") == "[ 3 ] AMC"


def test_render_phase_done_matches_the_locked_collapsed_line():
    assert (
        progress.render_phase_done("base packages", 12)
        == "  ✓ base packages                        12s"
    )
    assert progress.render_phase_done("CUDA repo and toolkit", 221).endswith(
        " 3m41s"
    )
    assert progress.render_phase_done("x", 1).startswith("  ✓ x")


def test_render_phase_active_marks_the_one_running_phase():
    assert progress.render_phase_active("TensorRT and cuDNN") == (
        "  ▸ TensorRT and cuDNN"
    )


def test_render_bar_fills_proportionally_and_clamps():
    assert progress.render_bar(0.0) == progress.BAR_EMPTY * progress.BAR_WIDTH
    assert progress.render_bar(1.0) == progress.BAR_FILLED * progress.BAR_WIDTH
    assert progress.render_bar(1.9) == progress.BAR_FILLED * progress.BAR_WIDTH
    assert progress.render_bar(-1.0) == progress.BAR_EMPTY * progress.BAR_WIDTH

    bar = progress.render_bar(412_000_000 / 606_000_000)
    assert bar.count(progress.BAR_FILLED) == 12
    assert bar.count(progress.BAR_EMPTY) == 5
    assert len(bar) == progress.BAR_WIDTH


def test_render_bytes_line_reproduces_the_documented_example():
    line = progress.render_bytes_line(
        412_000_000, 606_000_000, 14_200_000, 13.66
    )

    assert line == (
        "████████████░░░░░  68%   412 MB / 606 MB   14.2 MB/s   0:14"
    )


def test_render_bytes_line_omits_rate_and_eta_until_they_are_known():
    line = progress.render_bytes_line(0, 606_000_000)

    assert line.endswith("0 B / 606 MB")
    assert "/s" not in line
    assert "0:" not in line


def test_percent_never_claims_completion_before_the_last_byte():
    almost = progress.render_bytes_line(605_999_999, 606_000_000)
    done = progress.render_bytes_line(606_000_000, 606_000_000)

    assert " 99%" in almost
    assert "100%" in done
    # The bar holds back with the number: a full bar next to 99% is the
    # same broken promise.
    assert almost.count(progress.BAR_EMPTY) == 1
    assert done.count(progress.BAR_EMPTY) == 0


def test_render_bytes_line_refuses_to_draw_a_bar_without_a_denominator():
    """Section 5.3 is enforced by the renderer, not by its callers."""
    for absent in (0, -1, -606_000_000, None):
        with pytest.raises(ValueError):
            progress.render_bytes_line(5, absent)


def test_render_spinner_line_reports_elapsed_and_never_a_bar():
    line = progress.render_spinner_line(
        "⠹", "installing NVIDIA driver 595.58.03", 252,
        "building kernel module",
    )

    assert line == (
        "⠹ installing NVIDIA driver 595.58.03   4m12s   "
        "[ building kernel module ]"
    )
    assert progress.BAR_FILLED not in line
    assert "%" not in line


def test_render_spinner_line_omits_the_note_when_there_is_none():
    assert progress.render_spinner_line("⠋", "dpkg --configure -a", 5) == (
        "⠋ dpkg --configure -a   5s"
    )


# ---------------------------------------------------------------------------
# Progress -- banner and phases on a tty
# ---------------------------------------------------------------------------


def test_step_banner_is_written_once_with_its_position():
    out, clock = FakeTty(), FakeClock()
    _tty_progress(clock, out).begin_step(3, "AutoMagicCalib launcher")

    assert out.getvalue().count("[ 3/7 ] AutoMagicCalib launcher") == 1


def test_completed_phases_collapse_to_one_line_with_their_duration():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)

    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)
    clock.advance(12)
    bar.phase(2)
    clock.advance(221)
    bar.phase(3)

    rendered = _ANSI.sub("", out.getvalue())
    assert "  ✓ base packages                        12s" in rendered
    assert "  ✓ CUDA repo and toolkit" in rendered
    assert " 3m41s" in rendered
    # Two phases have collapsed; only the phase now running is marked, and
    # only it carries an activity row and a window.
    assert rendered.count("✓") == 2
    frame = _last_frame(out)
    assert frame[0] == "  ▸ TensorRT and cuDNN"
    assert sum(row.count("▸") for row in frame) == 1


def test_active_phase_row_sits_above_the_window():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)

    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)
    bar.line("Setting up libnvinfer10 (10.16.0.72-1+cuda13.2)")

    frame = _last_frame(out)
    assert frame[0] == "  ▸ TensorRT and cuDNN"
    assert frame[1].startswith("      ")  # the activity row
    assert frame[2] == (
        "        Setting up libnvinfer10 (10.16.0.72-1+cuda13.2)"
    )


def test_phase_out_of_range_raises_rather_than_rendering_a_wrong_position():
    bar = _tty_progress(FakeClock(), FakeTty())
    bar.begin_step(1, "Prerequisites", _PHASES)

    for bad in (0, -1, 4, 99):
        with pytest.raises(IndexError):
            bar.phase(bad)


def test_phase_on_a_step_that_declared_none_raises():
    bar = _tty_progress(FakeClock(), FakeTty())
    bar.begin_step(1, "Prerequisites")

    with pytest.raises(IndexError):
        bar.phase(1)


def test_a_step_without_declared_phases_still_renders_and_ends_cleanly():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)

    bar.begin_step(4, "Calibration output wiring")
    bar.line("watching for exports")
    clock.advance(9)
    bar.end_step()

    rendered = _ANSI.sub("", out.getvalue())
    assert "[ 4/7 ] Calibration output wiring" in rendered
    assert "  ▸ Calibration output wiring" in rendered
    # Nothing to collapse: the unnamed phase has no label to tick off.
    assert "✓" not in rendered


def test_phase_transitions_are_recorded_even_while_they_are_drawn(
    tmp_path, capsys
):
    """Section 3.3: the record carries the phase sequence regardless.

    On a tty the region draws every one of these lines, so the copy that
    keeps the record complete is the transcript-only one and nothing is
    printed a second time (section 4.2, section 12.2 defect 1). This test
    asserted `log.info`'s screen copy before that defect was fixed.
    """
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)

    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)
    clock.advance(12)
    bar.phase(2)

    recorded = run_file.read_text(encoding="utf-8")
    assert "[ 1/7 ] Prerequisites" in recorded
    assert "step 1/7 phase 1/3: base packages" in recorded
    assert "step 1/7 phase 1/3 done: base packages (12s)" in recorded
    assert "step 1/7 phase 2/3: CUDA repo and toolkit" in recorded
    assert capsys.readouterr().err == ""


def test_phase_lines_reach_the_transcript_without_control_characters(tmp_path):
    logs.open_transcript(tmp_path)
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)

    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)
    clock.advance(12)
    bar.end_step()

    transcript = (tmp_path / "latest.log").read_text(encoding="utf-8")
    assert "\033" not in transcript
    assert "\r" not in transcript
    assert "step 1/7 phase 1/3 done: base packages (12s)" in transcript


# ---------------------------------------------------------------------------
# Progress -- one writer per line, one owner of the cursor (section 12.2)
#
# Every test here puts the renderer and `logs` on the *same* stream, which
# is the installer's real arrangement (stderr) and the one the two defects
# need: with two different streams nothing can double up and nothing can
# move a cursor the other is counting rows on.
# ---------------------------------------------------------------------------


def _stderr_progress(monkeypatch, screen, clock, **kwargs) -> progress.Progress:
    """A renderer drawing to the same stream `logs` prints to."""
    monkeypatch.setattr(sys, "stderr", screen)
    kwargs.setdefault("terminal_width", lambda: _COLUMNS)
    return progress.Progress(
        total_steps=7, out=sys.stderr, clock=clock, **kwargs
    )


def test_the_step_banner_reaches_a_tty_exactly_once(tmp_path, monkeypatch):
    """Section 12.2 defect 1, counted rather than inferred.

    `begin_step` used to `log.info` the banner and then draw the same text
    to its own stream. Both are stderr in a real run, so the operator read
    the banner twice; the transcript copy now goes through the
    transcript-only sink instead (section 7.1).
    """
    screen, clock = FakeTty(), FakeClock()
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    bar = _stderr_progress(monkeypatch, screen, clock)

    bar.begin_step(3, "DeepStream SDK", _PHASES)

    banner = progress.render_step_banner(3, 7, "DeepStream SDK")
    assert screen.getvalue().count(banner) == 1
    assert banner in run_file.read_text(encoding="utf-8")


def test_a_phase_transition_is_drawn_on_a_tty_and_not_printed_as_well(
    tmp_path, monkeypatch
):
    """The same defect in `_start_phase` and `_close_phase`.

    Their text is the position line, which the region states in its own
    form (an active row, then a collapsed one), so on a tty the printed
    copy was pure duplication. It has to stay in the record, though:
    section 3.3 wants every transition in the transcript.
    """
    screen, clock = FakeTty(), FakeClock()
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    bar = _stderr_progress(monkeypatch, screen, clock)

    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)
    clock.advance(12)
    bar.phase(2)

    written = screen.getvalue()
    recorded = run_file.read_text(encoding="utf-8")
    for line in (
        "step 1/7 phase 1/3: base packages",
        "step 1/7 phase 1/3 done: base packages (12s)",
        "step 1/7 phase 2/3: CUDA repo and toolkit",
    ):
        assert written.count(line) == 0
        assert line in recorded
    # The region still says where the run is, in its own vocabulary.
    assert "✓ base packages" in _ANSI.sub("", written)


def test_a_foreign_log_line_is_not_eaten_by_the_next_erase(monkeypatch):
    """Section 12.2 defect 3, measured on an emulated screen.

    A step's own `log.info` goes to stderr, which is where the region is
    drawn. Before the fix that write moved the cursor down one row without
    the renderer knowing, so the next erase moved up one row too few and
    the clear-below consumed the log line itself. Step 5 alone makes 45 of
    these calls.
    """
    screen, clock = FakeTty(), FakeClock()
    bar = _stderr_progress(monkeypatch, screen, clock)

    bar.begin_step(5, "Per-project executables", _PHASES)
    bar.phase(1)
    logs.log.info("created /opt/mv3dt/projects/demo")
    bar.tick()
    bar.tick()

    rows = [_ANSI.sub("", row) for row in _screen(screen.getvalue())]
    assert any("created /opt/mv3dt/projects/demo" in row for row in rows)


def test_a_foreign_log_line_leaves_the_live_region_below_it(monkeypatch):
    """The line scrolls above the region; the region is redrawn under it.

    The `line()` verbose path's sandwich, so the operator keeps both the
    permanent line and a region that still tracks the run.
    """
    screen, clock = FakeTty(), FakeClock()
    bar = _stderr_progress(monkeypatch, screen, clock)

    bar.begin_step(5, "Per-project executables", _PHASES)
    bar.phase(1)
    logs.log.warn("config/demo.yml already exists, leaving it alone")
    bar.tick()

    rows = [_ANSI.sub("", row) for row in _screen(screen.getvalue())]
    warned = next(
        index
        for index, row in enumerate(rows)
        if "config/demo.yml already exists" in row
    )
    active = next(
        index for index, row in enumerate(rows) if row.startswith("  ▸ ")
    )
    assert active > warned


def test_a_foreign_log_line_still_reaches_the_transcript(tmp_path, monkeypatch):
    """Routing the printed copy must not cost the recorded one (section 7.1)."""
    screen, clock = FakeTty(), FakeClock()
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    bar = _stderr_progress(monkeypatch, screen, clock)

    bar.begin_step(5, "Per-project executables", _PHASES)
    logs.log.error("no such project: demo")

    assert "[error] no such project: demo" in run_file.read_text(
        encoding="utf-8"
    )


def _visible(screen: FakeTty) -> list[str]:
    """The rows still on screen, stripped of colour and trailing blanks."""
    rows = [_ANSI.sub("", row).rstrip() for row in _screen(screen.getvalue())]
    while rows and not rows[-1]:
        rows.pop()
    return rows


def test_a_fatal_error_is_the_last_thing_on_the_screen(monkeypatch):
    """An error takes the region down with it.

    `die` logs and exits, so anything drawn after the error line is the
    last thing the operator ever sees: a bar and a spinner frozen at
    whatever frame they happened to be on, under the message explaining
    that the install is over. The region comes down instead.
    """
    screen, clock = FakeTty(), FakeClock()
    bar = _stderr_progress(monkeypatch, screen, clock)
    bar.begin_step(2, "DeepStream SDK", _PHASES)
    bar.phase(1)
    bar.task("apt-get install deepstream-7.1")

    with pytest.raises(SystemExit):
        logs.die("apt-get returned 100")

    rows = _visible(screen)
    assert rows[-1] == "[error] apt-get returned 100"
    assert not any(
        frame in row for row in rows[-3:] for frame in progress.SPINNER_FRAMES
    )


def test_a_failed_step_does_not_leave_a_frozen_region_below_it(monkeypatch):
    """The dispatch loop's FAILED branch has the same shape as `die`.

    It logs the failure and returns without an `end_step()`, so the same
    rule has to hold for a plain `log.error` and not only for `die`.
    """
    screen, clock = FakeTty(), FakeClock()
    bar = _stderr_progress(monkeypatch, screen, clock)
    bar.begin_step(2, "DeepStream SDK", _PHASES)
    bar.phase(1)

    logs.log.error("step 2 failed: NGC login rejected the API key")

    rows = _visible(screen)
    assert rows[-1] == "[error] step 2 failed: NGC login rejected the API key"


def test_a_warning_still_leaves_the_region_drawing_below_it(monkeypatch):
    """Only an error ends the run, so only an error ends the region."""
    screen, clock = FakeTty(), FakeClock()
    bar = _stderr_progress(monkeypatch, screen, clock)
    bar.begin_step(2, "DeepStream SDK", _PHASES)
    bar.phase(1)

    logs.log.warn("no NGC key configured, skipping the login check")

    rows = _visible(screen)
    assert rows[-1] != "[warn ] no NGC key configured, skipping the login check"
    assert any(row.startswith("  ▸ ") for row in rows)


def test_the_region_comes_back_when_the_run_carries_on_after_an_error(
    monkeypatch
):
    """Down is not gone: a step that logs an error and keeps working gets
    its region back on the next redraw."""
    screen, clock = FakeTty(), FakeClock()
    bar = _stderr_progress(monkeypatch, screen, clock)
    bar.begin_step(2, "DeepStream SDK", _PHASES)
    bar.phase(1)

    logs.log.error("retrying the download")
    bar.tick()

    rows = _visible(screen)
    error_at = rows.index("[error] retrying the download")
    assert any(row.startswith("  ▸ ") for row in rows[error_at:])


def test_the_stream_can_be_given_back_by_naming_the_renderer_s_writer(
    monkeypatch
):
    """The un-claim path, with the callable a renderer actually registers.

    `Progress` binds its writer once, because a bound method read twice is
    two objects: registering the attribute directly would leave nothing
    able to name the registration afterwards.
    """
    screen, clock = FakeTty(), FakeClock()
    bar = _stderr_progress(monkeypatch, screen, clock)
    bar.begin_step(2, "DeepStream SDK", _PHASES)

    logs.clear_live_writer(bar._writer)
    screen.truncate(0)
    screen.seek(0)
    logs.log.info("straight to the stream now")

    assert _ANSI.sub("", screen.getvalue()) == (
        "[info ] straight to the stream now\n"
    )


def test_a_renderer_off_a_tty_leaves_logs_printing_for_itself(monkeypatch):
    """Nothing is drawn, so there is no cursor arithmetic to protect."""
    screen, clock = FakeTty(), FakeClock()
    bar = _stderr_progress(monkeypatch, screen, clock, non_interactive=True)
    bar.begin_step(1, "Prerequisites", _PHASES)
    screen.truncate(0)
    screen.seek(0)

    logs.log.info("plain line")

    # Colour is `logs`' own business (and argv's, section 12.2 defect 4);
    # what matters here is that no erase or redraw wrapped the line.
    assert _ANSI.sub("", screen.getvalue()) == "[info ] plain line\n"


def test_a_renderer_drawing_elsewhere_does_not_capture_the_log_stream(capsys):
    """The claim is scoped to the stream it was made for.

    A renderer drawing to a file, a pipe or a test double shares no cursor
    with `logs`, so log lines keep going to stderr untouched.
    """
    out, clock = FakeTty(), FakeClock()
    _tty_progress(clock, out).begin_step(1, "Prerequisites", _PHASES)
    capsys.readouterr()

    logs.log.info("goes to stderr, not into the region")

    assert "goes to stderr, not into the region" in capsys.readouterr().err
    assert "goes to stderr" not in out.getvalue()


# ---------------------------------------------------------------------------
# Progress -- the rolling window (section 8, LOCKED at 8 lines)
# ---------------------------------------------------------------------------


def test_window_keeps_the_last_eight_lines_in_order():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    for n in range(12):
        bar.line(f"line {n}")

    window = [row.strip() for row in _last_frame(out)[2:] if row.strip()]
    assert window == [f"line {n}" for n in range(4, 12)]


def test_window_height_is_fixed_at_eight_rows_in_every_phase():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)

    heights = []
    for index, feed in ((1, 0), (2, 3), (3, 25)):
        bar.phase(index)
        for n in range(feed):
            bar.line(f"line {n}")
        heights.append(len(_last_frame(out)))

    # Phase row + activity row + exactly WINDOW_LINES rows, every time, so
    # the layout does not shift as the install moves between phases.
    assert heights == [2 + progress.WINDOW_LINES] * 3


def test_window_is_cleared_when_the_phase_changes():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)

    bar.phase(1)
    bar.line("apt output from the first phase")
    bar.phase(2)

    assert all("apt output" not in row for row in _last_frame(out))


def test_long_output_lines_are_truncated_to_the_terminal_width():
    """A wrapped row would leave the cursor-up count short, every redraw."""
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out, terminal_width=lambda: 40)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)

    bar.line(
        "Get:14 http://archive.ubuntu.com/ubuntu noble-updates/main amd64 "
        "libnvinfer10 amd64 10.16.0.72-1+cuda13.2 [412 MB]"
    )

    frame = _last_frame(out)
    # One logical row is one visual row: nothing reaches the last column,
    # so nothing wraps onto a row the redraw does not know about.
    assert all(len(row) < 40 for row in frame)
    assert len(frame) == 2 + progress.WINDOW_LINES


def test_the_live_region_follows_a_resized_terminal():
    out, clock = FakeTty(), FakeClock()
    columns = [100]
    bar = _tty_progress(clock, out, terminal_width=lambda: columns[0])
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)
    bar.line("y" * 200)

    assert max(len(row) for row in _last_frame(out)) == 99

    columns[0] = 40
    bar.tick()
    assert max(len(row) for row in _last_frame(out)) == 39


def test_terminal_width_falls_back_when_there_is_no_terminal(monkeypatch):
    monkeypatch.setattr(
        progress.shutil,
        "get_terminal_size",
        lambda fallback=None: os.terminal_size((fallback[0], fallback[1])),
    )

    assert progress._terminal_width() == progress._FALLBACK_COLUMNS


def test_window_size_is_configurable_for_callers_that_need_it():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out, window_lines=3)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)

    for n in range(6):
        bar.line(f"line {n}")

    assert len(_last_frame(out)) == 5
    assert [row.strip() for row in _last_frame(out)[2:]] == [
        "line 3", "line 4", "line 5",
    ]


def test_window_lines_zero_leaves_no_window_at_all():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out, window_lines=0)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)

    for n in range(4):
        bar.line(f"line {n}")

    frame = _last_frame(out)
    assert len(frame) == 2
    # The last line is still the spinner's note even with no window.
    assert "[ line 3 ]" in frame[1]


# ---------------------------------------------------------------------------
# Progress -- bars only where a denominator exists (section 5.3)
# ---------------------------------------------------------------------------


def _activity_row(out: FakeTty) -> str:
    return _last_frame(out)[1]


def test_no_bar_is_drawn_without_a_denominator():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)
    bar.task("installing NVIDIA driver 595.58.03")
    clock.advance(252)
    bar.line("building kernel module")

    for absent in (None, 0, -1):
        bar.bytes(412_000_000, absent)
        row = _activity_row(out)
        assert progress.BAR_FILLED not in row
        assert progress.BAR_EMPTY not in row
        assert "%" not in row
        assert "installing NVIDIA driver 595.58.03   4m12s" in row
        assert "[ building kernel module ]" in row


def test_a_task_with_no_bytes_reports_honest_elapsed_time():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(2)
    bar.task("dpkg --configure -a")
    clock.advance(75)
    bar.tick()

    row = _activity_row(out)
    assert "dpkg --configure -a   1m15s" in row
    assert "%" not in row


def test_a_real_denominator_draws_the_bar():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)
    bar.task("downloading TensorRT")
    bar.bytes(412_000_000, 606_000_000)

    row = _activity_row(out)
    assert row.startswith("      ")
    assert "68%" in row
    assert "412 MB / 606 MB" in row


def test_rate_and_eta_stay_hidden_until_the_samples_span_enough_time():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    # First sample: a rate here would be divided by nothing at all.
    bar.bytes(0, 606_000_000)
    assert "/s" not in _activity_row(out)

    # Second sample, but far too soon after the first to be meaningful.
    clock.advance(0.2)
    bar.bytes(60_000_000, 606_000_000)
    assert "/s" not in _activity_row(out)


def test_rate_and_eta_appear_once_the_samples_support_them():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    bar.bytes(0, 606_000_000)
    clock.advance(2.0)
    bar.bytes(28_400_000, 606_000_000)  # 14.2 MB/s over two seconds

    row = _activity_row(out)
    assert "14.2 MB/s" in row
    # 577.6 MB left at 14.2 MB/s is 40.68s.
    assert row.endswith("0:41")


def test_a_stalled_transfer_reports_no_rate_rather_than_zero():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    bar.bytes(28_400_000, 606_000_000)
    clock.advance(30.0)
    bar.bytes(28_400_000, 606_000_000)

    assert "/s" not in _activity_row(out)


def test_tick_advances_the_spinner_frame():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)
    bar.task("update-initramfs")

    seen = []
    for _ in range(3):
        seen.append(_activity_row(out).strip()[0])
        bar.tick()

    assert seen == list(progress.SPINNER_FRAMES[:3])


# ---------------------------------------------------------------------------
# Progress -- off a tty (section 7)
# ---------------------------------------------------------------------------


def _run_everything(bar: progress.Progress, clock: FakeClock) -> None:
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)
    bar.task("downloading TensorRT")
    bar.bytes(0, 606_000_000)
    clock.advance(2.0)
    bar.bytes(28_400_000, 606_000_000)
    bar.line("Setting up libnvinfer10 (10.16.0.72-1+cuda13.2)")
    clock.advance(10)
    bar.phase(2)
    bar.tick()
    bar.end_step()


def test_off_a_tty_nothing_live_and_no_escape_is_written(capsys):
    out, clock = io.StringIO(), FakeClock()
    bar = progress.Progress(total_steps=7, out=out, clock=clock)

    _run_everything(bar, clock)

    rendered = out.getvalue()
    err = capsys.readouterr().err
    for stream in (rendered, err):
        assert "\033" not in stream
        assert "\r" not in stream
        assert progress.BAR_FILLED not in stream
        assert progress.BAR_EMPTY not in stream
        assert not any(frame in stream for frame in progress.SPINNER_FRAMES)


def test_off_a_tty_command_output_is_still_emitted_plainly():
    out, clock = io.StringIO(), FakeClock()
    bar = progress.Progress(total_steps=7, out=out, clock=clock)

    _run_everything(bar, clock)

    assert out.getvalue() == (
        "Setting up libnvinfer10 (10.16.0.72-1+cuda13.2)\n"
    )


def test_off_a_tty_position_still_reaches_the_operator(capsys):
    out, clock = io.StringIO(), FakeClock()
    bar = progress.Progress(total_steps=7, out=out, clock=clock)

    _run_everything(bar, clock)

    err = capsys.readouterr().err
    assert "[ 1/7 ] Prerequisites" in err
    assert "step 1/7 phase 1/3: base packages" in err
    assert "step 1/7 phase 1/3 done: base packages (12s)" in err
    assert "step 1/7 phase 2/3: CUDA repo and toolkit" in err


def test_non_interactive_on_a_real_tty_is_plain_too(capsys):
    out, clock = FakeTty(), FakeClock()
    bar = progress.Progress(
        total_steps=7, out=out, clock=clock, non_interactive=True
    )

    _run_everything(bar, clock)

    assert "\033" not in out.getvalue()
    assert "\r" not in out.getvalue()
    assert progress.BAR_FILLED not in out.getvalue()
    assert "step 1/7 phase 2/3" in capsys.readouterr().err


def test_a_stream_that_cannot_report_tty_status_is_treated_as_a_pipe():
    class Odd:
        def __init__(self) -> None:
            self.text = ""

        def isatty(self):
            raise OSError("no")

        def write(self, text):
            self.text += text

    out = Odd()
    bar = progress.Progress(total_steps=7, out=out, clock=FakeClock())
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)
    bar.line("plain")

    assert out.text == "plain\n"


# ---------------------------------------------------------------------------
# Progress -- verbose (section 8)
# ---------------------------------------------------------------------------


def test_verbose_scrolls_every_line_verbatim_above_the_live_region():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out, verbose=True)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)

    for n in range(12):
        bar.line(f"line {n}")

    rendered = _ANSI.sub("", out.getvalue())
    for n in range(12):
        assert f"line {n}" in rendered
    # No rolling window underneath: the scrollback is the record instead.
    assert len(_last_frame(out)) == 2


# ---------------------------------------------------------------------------
# Download byte-progress adapter (section 5.1)
# ---------------------------------------------------------------------------


class FakeTransfer:
    """A scripted download: one size per poll, then it stops.

    `follow_download` samples "is it still running" *before* it reads the
    size, so `running()` answers about the sample that has not been taken
    yet -- it stays true until the next `size()` call would return the last
    scripted value. That is exactly the ordering the adapter relies on to
    guarantee its final frame shows the final size.
    """

    def __init__(self, sizes) -> None:
        self.sizes = list(sizes)
        self.index = -1
        self.sleeps: list[float] = []

    def running(self) -> bool:
        return self.index + 1 < len(self.sizes) - 1

    def size(self, path) -> int:
        self.index = min(self.index + 1, len(self.sizes) - 1)
        return self.sizes[self.index]

    def sleeper(self, clock: FakeClock):
        def _sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            clock.advance(seconds)

        return _sleep


def _percents(text: str) -> list[int]:
    return [int(match) for match in re.findall(r"(\d+)%", text)]


# -- part_size --------------------------------------------------------------


def test_part_size_is_zero_before_the_transfer_creates_the_file(tmp_path):
    assert progress.part_size(tmp_path / "driver.run.part") == 0


def test_part_size_reports_the_bytes_on_disk(tmp_path):
    part = tmp_path / "driver.run.part"
    part.write_bytes(b"x" * 4096)
    assert progress.part_size(part) == 4096


def test_part_size_survives_a_path_it_cannot_stat(tmp_path):
    # A directory where a file is expected, i.e. something is wrong -- but
    # a polled measurement must never be the thing that fails an install.
    (tmp_path / "driver.run.part").mkdir()
    assert progress.part_size(tmp_path / "driver.run.part" / "x" / "y") == 0


# -- content_length ---------------------------------------------------------


def test_content_length_parses_the_declared_size():
    assert progress.content_length("https://example/d", probe=lambda url: "606000000") == 606_000_000
    assert progress.content_length("https://example/d", probe=lambda url: " 42 ") == 42
    assert progress.content_length("https://example/d", probe=lambda url: 42) == 42


def test_content_length_passes_the_url_to_the_injected_probe():
    seen = []

    def probe(url):
        seen.append(url)
        return "10"

    progress.content_length("https://example/driver.run", probe=probe)
    assert seen == ["https://example/driver.run"]


@pytest.mark.parametrize(
    "value", [None, "", "   ", "unknown", "12abc", "0", "-1", "1.5", []]
)
def test_content_length_degrades_to_no_denominator(value):
    assert progress.content_length("https://example/d", probe=lambda url: value) is None


def test_content_length_degrades_when_the_probe_itself_fails():
    def probe(url):
        raise ConnectionError("no route to host")

    # A download that would have succeeded must not fail because a HEAD
    # request did not.
    assert progress.content_length("https://example/d", probe=probe) is None


# -- the off-tty line -------------------------------------------------------


def test_render_download_log_line_states_a_percentage_when_it_has_one():
    line = progress.render_download_log_line(
        "driver.run.part", 412_000_000, 606_000_000, 252
    )
    assert line == "driver.run.part: 412 MB / 606 MB (68%) after 4m12s"


def test_render_download_log_line_omits_the_percentage_without_a_denominator():
    for absent in (None, 0, -1):
        line = progress.render_download_log_line("sdk.tar", 412_000_000, absent, 252)
        assert line == "sdk.tar: 412 MB after 4m12s"


def test_render_download_log_line_never_exceeds_one_hundred_percent():
    line = progress.render_download_log_line("d.part", 620_000_000, 606_000_000, 10)
    assert _percents(line) == [100]


# -- follow_download, the normal case ---------------------------------------


def test_follow_download_drives_the_bar_as_the_part_file_grows():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    transfer = FakeTransfer([0, 121_200_000, 412_000_000, 606_000_000])
    outcome = progress.follow_download(
        "/var/cache/driver.run.part",
        606_000_000,
        is_running=transfer.running,
        renderer=bar,
        poll_s=2.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )

    rendered = _ANSI.sub("", out.getvalue())
    assert "20%" in rendered
    assert "68%" in rendered
    assert "412 MB / 606 MB" in rendered

    row = _activity_row(out)
    assert "100%" in row
    assert "606 MB / 606 MB" in row

    assert outcome.bytes_done == 606_000_000
    assert outcome.total == 606_000_000
    assert outcome.polls == 4
    assert outcome.complete is True
    # One sleep between polls and none after the transfer stopped.
    assert transfer.sleeps == [2.0, 2.0, 2.0]
    assert outcome.elapsed_s == pytest.approx(6.0)


def test_follow_download_reports_rate_and_eta_from_the_polled_sizes():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    # 14.2 MB per two-second poll.
    transfer = FakeTransfer([0, 28_400_000, 56_800_000, 85_200_000])
    progress.follow_download(
        "/var/cache/driver.run.part",
        606_000_000,
        is_running=transfer.running,
        renderer=bar,
        poll_s=2.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )

    row = _activity_row(out)
    assert "14.2 MB/s" in row
    # 520.8 MB left at 14.2 MB/s is 36.6s.
    assert row.endswith("0:37")


def test_follow_download_returns_the_final_size_without_a_renderer():
    clock = FakeClock()
    transfer = FakeTransfer([0, 500, 1000])
    outcome = progress.follow_download(
        "/var/cache/driver.run.part",
        1000,
        is_running=transfer.running,
        poll_s=1.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )
    assert outcome.bytes_done == 1000
    assert outcome.polls == 3
    assert outcome.complete is True


def test_follow_download_floors_the_poll_interval():
    clock = FakeClock()
    transfer = FakeTransfer([0, 1])
    progress.follow_download(
        "/var/cache/driver.run.part",
        10,
        is_running=transfer.running,
        poll_s=0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )
    # A caller passing 0 must not turn this into a busy loop.
    assert transfer.sleeps == [progress._MIN_DOWNLOAD_POLL_S]


def test_follow_download_names_the_task_on_the_renderer():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    transfer = FakeTransfer([0, 10])
    progress.follow_download(
        "/var/cache/driver.run.part",
        None,
        is_running=transfer.running,
        renderer=bar,
        task="downloading NVIDIA driver 595.58.03",
        poll_s=1.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )
    assert "downloading NVIDIA driver 595.58.03" in _activity_row(out)


# -- follow_download, the awkward cases -------------------------------------


def test_follow_download_renders_zero_percent_before_the_part_file_exists(tmp_path):
    """The transfer has been started but has not created its file yet."""
    part = tmp_path / "driver.run.part"
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    polls = []
    sizes = iter([500, 1000])

    def running():
        polls.append(1)
        return len(polls) < 3

    def sleep(seconds):
        clock.advance(seconds)
        part.write_bytes(b"x" * next(sizes))

    outcome = progress.follow_download(
        part,
        1000,
        is_running=running,
        renderer=bar,
        poll_s=1.0,
        clock=clock,
        sleep=sleep,
    )

    rendered = _ANSI.sub("", out.getvalue())
    # The first frame was drawn against a file that did not exist: 0
    # percent, not a traceback.
    assert "0%" in rendered
    assert "0 B / 1.00 KB" in rendered
    assert "50%" in rendered
    assert outcome.bytes_done == 1000
    assert outcome.complete is True


def test_follow_download_on_a_stalled_transfer_reports_no_rate():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    # Connected, 28.4 MB in, and then nothing moves for a minute.
    transfer = FakeTransfer([28_400_000] * 5)
    progress.follow_download(
        "/var/cache/driver.run.part",
        606_000_000,
        is_running=transfer.running,
        renderer=bar,
        poll_s=15.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )

    row = _activity_row(out)
    # No forward motion means no rate at all -- not 0 B/s, and not an ETA
    # extrapolated from a division that has nothing to divide.
    assert "/s" not in row
    assert ":" not in row
    assert "28.4 MB / 606 MB" in row


def test_follow_download_without_a_total_falls_back_to_the_spinner():
    """Section 5.3: never a bar where no true denominator exists."""
    for absent in (None, 0, -1):
        out, clock = FakeTty(), FakeClock()
        bar = _tty_progress(clock, out)
        bar.begin_step(1, "Prerequisites", _PHASES)
        bar.phase(3)

        transfer = FakeTransfer([0, 5_000_000, 9_000_000])
        outcome = progress.follow_download(
            "/var/cache/sdk.tar.part",
            absent,
            is_running=transfer.running,
            renderer=bar,
            task="fetching SDK tarball",
            poll_s=1.0,
            clock=clock,
            sleep=transfer.sleeper(clock),
            size_of=transfer.size,
        )

        row = _activity_row(out)
        assert progress.BAR_FILLED not in row
        assert progress.BAR_EMPTY not in row
        assert "%" not in row
        assert "fetching SDK tarball" in row
        assert outcome.total is None
        assert outcome.complete is False


def test_follow_download_without_a_total_clears_a_previous_denominator():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)
    # An earlier download left a real bar on screen.
    bar.bytes(50_000_000, 100_000_000)
    assert "50%" in _activity_row(out)

    transfer = FakeTransfer([0, 10])
    progress.follow_download(
        "/var/cache/sdk.tar.part",
        None,
        is_running=transfer.running,
        renderer=bar,
        poll_s=1.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )
    # Not one stale bar frame survives into the spinner-driven transfer.
    assert "%" not in _activity_row(out)


def test_follow_download_animates_the_spinner_while_it_has_no_denominator():
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    transfer = FakeTransfer([0, 1, 2, 3, 4])
    progress.follow_download(
        "/var/cache/sdk.tar.part",
        None,
        is_running=transfer.running,
        renderer=bar,
        task="fetching SDK tarball",
        poll_s=1.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )

    rendered = _ANSI.sub("", out.getvalue())
    frames = {frame for frame in progress.SPINNER_FRAMES if frame in rendered}
    # A frozen spinner reads as a hung installer, which is the whole
    # failure this doc exists to fix.
    assert len(frames) >= 3


def test_follow_download_never_renders_above_one_hundred_percent():
    """A mis-declared Content-Length: more bytes arrived than were promised."""
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    transfer = FakeTransfer([0, 400_000_000, 620_000_000])
    outcome = progress.follow_download(
        "/var/cache/driver.run.part",
        606_000_000,
        is_running=transfer.running,
        renderer=bar,
        poll_s=2.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )

    rendered = _ANSI.sub("", out.getvalue())
    assert _percents(rendered)
    assert max(_percents(rendered)) == 100
    assert "620 MB" not in rendered

    row = _activity_row(out)
    assert "100%" in row
    assert "606 MB / 606 MB" in row

    # The outcome keeps the real number, so a caller can still notice the
    # server sent something other than what it declared.
    assert outcome.bytes_done == 620_000_000
    assert outcome.total == 606_000_000


def test_follow_download_samples_running_before_size_so_the_last_frame_is_final():
    """The final size must land on screen, not one poll short of it."""
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    order = []

    def running():
        order.append("running")
        return len(order) < 3

    def size(path):
        order.append("size")
        return 1000 if len(order) >= 4 else 400

    progress.follow_download(
        "/var/cache/driver.run.part",
        1000,
        is_running=running,
        renderer=bar,
        poll_s=1.0,
        clock=clock,
        sleep=lambda seconds: clock.advance(seconds),
        size_of=size,
    )

    assert order[:4] == ["running", "size", "running", "size"]
    assert "100%" in _activity_row(out)


# -- follow_download, off a tty (section 7) ---------------------------------


def _plain_progress(clock: FakeClock) -> progress.Progress:
    return progress.Progress(total_steps=7, out=io.StringIO(), clock=clock)


def test_follow_download_off_a_tty_logs_a_plain_line_on_an_interval(capsys):
    clock = FakeClock()
    bar = _plain_progress(clock)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)
    capsys.readouterr()

    transfer = FakeTransfer([0, 151_500_000, 303_000_000, 454_500_000, 606_000_000])
    progress.follow_download(
        "/var/cache/driver.run.part",
        606_000_000,
        is_running=transfer.running,
        renderer=bar,
        poll_s=20.0,
        log_interval_s=30.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )

    err = capsys.readouterr().err
    # Nothing is drawn off a tty, so these lines are the only thing that
    # keeps a multi-minute download distinguishable from a hang.
    assert "driver.run.part: 0 B / 606 MB (0%) after 0s" in err
    assert "driver.run.part: 303 MB / 606 MB (50%) after 40s" in err
    assert "driver.run.part: received 606 MB in 1m20s" in err
    assert "\033" not in err
    # Throttled to the interval, not one line per poll.
    assert err.count("after") == 3


def test_follow_download_on_a_tty_leaves_the_live_region_alone(capsys):
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)
    capsys.readouterr()

    transfer = FakeTransfer([0, 500, 1000])
    progress.follow_download(
        "/var/cache/driver.run.part",
        1000,
        is_running=transfer.running,
        renderer=bar,
        poll_s=60.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )

    # A log line here would scroll under the redrawn region and
    # desynchronise its cursor-up arithmetic.
    assert "after" not in capsys.readouterr().err


def test_follow_download_with_no_renderer_still_reaches_the_transcript(capsys):
    clock = FakeClock()
    transfer = FakeTransfer([0, 1000])
    progress.follow_download(
        "/var/cache/sdk.tar.part",
        None,
        is_running=transfer.running,
        poll_s=1.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )
    err = capsys.readouterr().err
    assert "sdk.tar.part: 0 B after 0s" in err
    assert "%" not in err


def test_progress_live_reports_whether_anything_is_being_drawn():
    clock = FakeClock()
    assert _tty_progress(clock, FakeTty()).live is True
    assert _plain_progress(clock).live is False
    assert progress.Progress(out=FakeTty(), non_interactive=True, clock=clock).live is False


# -- composition ------------------------------------------------------------


def test_an_unavailable_content_length_ends_up_as_a_spinner():
    """The end-to-end section 5.3 path: no header, therefore no bar."""
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    total = progress.content_length("https://example/driver.run", probe=lambda url: None)
    assert total is None

    transfer = FakeTransfer([0, 10, 20])
    outcome = progress.follow_download(
        "/var/cache/driver.run.part",
        total,
        is_running=transfer.running,
        renderer=bar,
        task="downloading NVIDIA driver 595.58.03",
        poll_s=1.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )

    assert "%" not in _activity_row(out)
    assert outcome.total is None


def test_render_download_log_line_agrees_with_the_drawn_row():
    drawn = progress.render_bytes_line(412_000_000, 606_000_000)
    logged = progress.render_download_log_line("d.part", 412_000_000, 606_000_000, 1)
    assert _percents(drawn) == _percents(logged) == [68]


def test_render_download_log_line_holds_at_99_until_the_last_byte():
    line = progress.render_download_log_line("d.part", 605_999_999, 606_000_000, 1)
    assert _percents(line) == [99]


# ---------------------------------------------------------------------------
# follow_download -- suppressed on screen is still recorded (section 7.1)
# ---------------------------------------------------------------------------


def test_the_interactive_periodic_line_reaches_the_transcript(tmp_path, capsys):
    """The gap section 7.1 names. On a tty the periodic line stays off the
    screen so it cannot fight the live region -- and lands in the transcript
    anyway, so an interactive install's record is not the task name, nothing
    for six minutes, then the phase-done line."""
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    transfer = FakeTransfer([0, 151_500_000, 303_000_000, 454_500_000, 606_000_000])
    progress.follow_download(
        "/var/cache/driver.run.part",
        606_000_000,
        is_running=transfer.running,
        renderer=bar,
        poll_s=20.0,
        log_interval_s=30.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )

    transcript = run_file.read_text(encoding="utf-8")
    assert "[info ] driver.run.part: 0 B / 606 MB (0%) after 0s" in transcript
    assert "[info ] driver.run.part: 303 MB / 606 MB (50%) after 40s" in transcript
    assert "[info ] driver.run.part: received 606 MB in 1m20s" in transcript
    assert "\033" not in transcript
    # Throttled in the record exactly as it is on a plain stream.
    assert transcript.count("after") == 3
    # And still nothing on the terminal, which the renderer owns.
    assert "after" not in capsys.readouterr().err


def test_the_off_tty_line_is_recorded_once_not_twice(tmp_path, capsys):
    """`log.info` already writes both destinations, so the plain path must
    not also call the transcript-only sink: that would double every line in
    the record."""
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    clock = FakeClock()
    transfer = FakeTransfer([0, 1000])

    progress.follow_download(
        "/var/cache/sdk.tar.part",
        None,
        is_running=transfer.running,
        renderer=_plain_progress(clock),
        poll_s=1.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )

    transcript = run_file.read_text(encoding="utf-8")
    assert transcript.count("sdk.tar.part: 0 B after 0s") == 1
    assert capsys.readouterr().err.count("sdk.tar.part: 0 B after 0s") == 1


def test_the_recorded_overshoot_is_the_true_number_and_says_so(tmp_path):
    """The consistency bug in section 7.1: the periodic lines clamp to the
    declared total, so a final line carrying the raw count with no label left
    the transcript reading 100% of 606 MB and then 620 MB, with nothing to
    say which was real."""
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    transfer = FakeTransfer([0, 400_000_000, 620_000_000])
    outcome = progress.follow_download(
        "/var/cache/driver.run.part",
        606_000_000,
        is_running=transfer.running,
        renderer=bar,
        poll_s=2.0,
        clock=clock,
        sleep=transfer.sleeper(clock),
        size_of=transfer.size,
    )

    transcript = run_file.read_text(encoding="utf-8")
    assert "received 620 MB" in transcript
    assert "(606 MB declared)" in transcript
    assert outcome.bytes_done == 620_000_000
    # The drawn row is still clamped: 115% reads as a bug in the installer
    # rather than in the server.
    assert "620 MB" not in _ANSI.sub("", out.getvalue())


def test_render_download_done_line_names_a_total_it_disagrees_with():
    assert progress.render_download_done_line("d.part", 606_000_000, 606_000_000, 80) == (
        "d.part: received 606 MB in 1m20s"
    )
    assert progress.render_download_done_line("d.part", 620_000_000, 606_000_000, 80) == (
        "d.part: received 620 MB in 1m20s (606 MB declared)"
    )
    # A transfer that stopped short is the same disagreement, and worth the
    # same line: the caller is about to checksum a truncated file.
    assert "(606 MB declared)" in progress.render_download_done_line(
        "d.part", 12_000_000, 606_000_000, 80
    )
    # No denominator, nothing to disagree with.
    assert progress.render_download_done_line("d.part", 12_000_000, None, 80) == (
        "d.part: received 12.0 MB in 1m20s"
    )


# ---------------------------------------------------------------------------
# apt Status-Fd percentage adapter (section 5.2)
# ---------------------------------------------------------------------------


# Real shapes off `apt-get install -o APT::Status-Fd=<fd>`: the item is a
# plain number while apt fetches archives, and an architecture-qualified
# package name while dpkg unpacks and configures them.
_APT_DOWNLOAD = "dlstatus:1:50.0000:Retrieving file 12 of 24"
_APT_INSTALL = (
    "pmstatus:libnvinfer10:68.0000:"
    "Setting up libnvinfer10 (10.16.0.72-1+cuda13.2)"
)


def _apt_stream(lines, clock: FakeClock, seconds_per_line: float = 0.0):
    """The status pipe as an iterable, with the clock moving as it is read."""

    def generate():
        for line in lines:
            clock.advance(seconds_per_line)
            yield line

    return generate()


def _window_rows(out: FakeTty) -> list[str]:
    return [row.strip() for row in _last_frame(out)[2:] if row.strip()]


# -- the option the caller passes apt ---------------------------------------


def test_apt_status_fd_args_names_the_descriptor():
    assert progress.apt_status_fd_args(9) == ("-o", "APT::Status-Fd=9")


# -- parse_apt_status -------------------------------------------------------


def test_parse_apt_status_reads_the_two_documented_shapes():
    download = progress.parse_apt_status(_APT_DOWNLOAD)
    assert (download.kind, download.item, download.percent) == (
        "dlstatus",
        "1",
        50.0,
    )
    assert download.description == "Retrieving file 12 of 24"

    install = progress.parse_apt_status(_APT_INSTALL)
    assert (install.kind, install.item, install.percent) == (
        "pmstatus",
        "libnvinfer10",
        68.0,
    )
    assert install.description == "Setting up libnvinfer10 (10.16.0.72-1+cuda13.2)"
    assert install.drives_bar is True


def test_parse_apt_status_keeps_the_colons_inside_a_description():
    """The split is bounded, not greedy: apt's own descriptions carry
    colons, and a description truncated at the first one loses the package
    the operator is being told about."""
    status = progress.parse_apt_status(
        "pmstatus:libc6:12.0:Unpacking libc6:amd64 over 2.39-0ubuntu8.3"
    )
    assert status.description == "Unpacking libc6:amd64 over 2.39-0ubuntu8.3"
    assert status.percent == 12.0


def test_parse_apt_status_finds_the_percent_past_an_arch_qualified_name():
    """apt qualifies a multi-arch package name, so the percentage is not
    reliably the third field."""
    status = progress.parse_apt_status(
        "pmstatus:libnvinfer10:amd64:68.0000:Setting up libnvinfer10:amd64"
    )
    assert status.item == "libnvinfer10:amd64"
    assert status.percent == 68.0
    assert status.description == "Setting up libnvinfer10:amd64"


def test_parse_apt_status_does_not_mistake_a_numbered_item_for_a_percent():
    """`dlstatus` numbers its items, so a scan that started at field 1
    would read item "1" as one percent and then never move."""
    status = progress.parse_apt_status("dlstatus:1:0.0000:Retrieving file 1 of 24")
    assert (status.item, status.percent) == ("1", 0.0)


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "junk without any colon at all",
        "media-change:/cdrom:/dev/sr0",
        "status: libc6: installed",
        "pmconffile:/etc/nginx/nginx.conf:'a' 'b'",
        "pmstatus",
        "pmstatus:libc6",
        "dlstatus:1",
        ":::",
        "PMSTATUS_BUT_NOT_REALLY:libc6:5:x",
    ],
)
def test_parse_apt_status_ignores_what_it_does_not_understand(line):
    """Other record kinds exist and more will be added. Ignoring one is
    always correct; guessing at it is how a bar starts lying."""
    assert progress.parse_apt_status(line) is None


def test_parse_apt_status_never_raises_on_a_truncated_line():
    """The stream is a pipe a subprocess writes, and it dies mid-line when
    that subprocess is killed."""
    for cut in range(len(_APT_INSTALL) + 1):
        progress.parse_apt_status(_APT_INSTALL[:cut])
    for cut in range(len(_APT_DOWNLOAD) + 1):
        progress.parse_apt_status(_APT_DOWNLOAD[:cut])


@pytest.mark.parametrize("value", ["nan", "-nan", "inf", "-inf", "1e400"])
def test_parse_apt_status_rejects_a_percent_that_is_not_finite(value):
    """`float()` accepts these happily and `round()` then raises, from a
    line that came off a pipe."""
    status = progress.parse_apt_status(f"pmstatus:libc6:{value}:Setting up libc6")
    assert status is not None
    assert status.percent is None
    assert status.drives_bar is False
    assert "Setting up libc6" in status.description


def test_parse_apt_status_keeps_a_description_that_carries_no_percent():
    status = progress.parse_apt_status("pmerror:libc6:Sub-process returned an error")
    assert status.kind == "pmerror"
    assert status.percent is None
    assert status.description == "Sub-process returned an error"
    assert status.drives_bar is False


def test_parse_apt_status_clamps_a_percent_outside_the_range():
    assert progress.parse_apt_status("pmstatus:libc6:140:x").percent == 100.0
    assert progress.parse_apt_status("pmstatus:libc6:-5:x").percent == 0.0


def test_parse_apt_status_cleans_control_characters_out_of_a_record():
    status = progress.parse_apt_status(
        "pmstatus:libc6:12.0:\033[1;32mSetting up\033[0m libc6\r\n"
    )
    assert status.description == "Setting up libc6"


def test_parse_apt_status_survives_an_object_that_is_not_a_string():
    assert progress.parse_apt_status(None) is None
    assert progress.parse_apt_status(17) is None


# -- apt_fraction: the monotonic mapping ------------------------------------


def test_apt_fraction_maps_the_two_sweeps_onto_one_rising_range():
    """apt's download and install percentages each run 0 to 100
    independently, so they are two segments of a single range rather than
    two goes at the same bar."""
    share = progress.APT_DOWNLOAD_SHARE
    assert progress.apt_fraction("dlstatus", 0, has_download_phase=True) == 0.0
    assert progress.apt_fraction("dlstatus", 100, has_download_phase=True) == share
    # The install sweep picks up exactly where the download sweep stopped.
    assert progress.apt_fraction("pmstatus", 0, has_download_phase=True) == share
    assert progress.apt_fraction("pmstatus", 100, has_download_phase=True) == 1.0

    sweep = [
        progress.apt_fraction("dlstatus", p, has_download_phase=True)
        for p in range(0, 101, 10)
    ] + [
        progress.apt_fraction("pmstatus", p, has_download_phase=True)
        for p in range(0, 101, 10)
    ]
    assert sweep == sorted(sweep)


def test_apt_fraction_gives_a_cached_transaction_the_whole_range():
    """No archive to fetch means no `dlstatus` at all, and committing to
    the split anyway would open the bar at 50 percent."""
    assert progress.apt_fraction("pmstatus", 0, has_download_phase=False) == 0.0
    assert progress.apt_fraction("pmstatus", 50, has_download_phase=False) == 0.5
    assert progress.apt_fraction("pmstatus", 100, has_download_phase=False) == 1.0


def test_apt_fraction_has_nothing_to_say_about_a_record_with_no_percent():
    assert progress.apt_fraction("pmstatus", None, has_download_phase=True) is None
    assert progress.apt_fraction("pmerror", 50, has_download_phase=True) is None
    assert progress.apt_fraction("media-change", 50, has_download_phase=True) is None


# -- render_percent_line ----------------------------------------------------


def test_render_percent_line_draws_the_bar_and_what_it_is_working_on():
    line = progress.render_percent_line(68, "libnvinfer10")
    assert line == "████████████░░░░░  68%   libnvinfer10"


def test_render_percent_line_omits_the_note_when_there_is_none():
    assert progress.render_percent_line(0) == "░" * progress.BAR_WIDTH + "   0%"


def test_render_percent_line_clamps_rather_than_overflowing_the_bar():
    assert _percents(progress.render_percent_line(140)) == [100]
    assert _percents(progress.render_percent_line(-5)) == [0]


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "x", []])
def test_render_percent_line_refuses_to_draw_without_a_percentage(value):
    with pytest.raises(ValueError):
        progress.render_percent_line(value)


# -- Progress.percent -------------------------------------------------------


def _apt_progress(clock: FakeClock, out: FakeTty) -> progress.Progress:
    bar = _tty_progress(clock, out)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)
    return bar


def test_percent_drives_the_bar_from_a_percentage():
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)
    bar.percent(68, "libnvinfer10")
    row = _activity_row(out)
    assert " 68%" in row
    assert row.endswith("libnvinfer10")


def test_percent_without_a_usable_value_falls_back_to_the_spinner():
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)
    bar.percent(40)
    for absent in (None, float("nan"), "not a number"):
        bar.percent(absent)
        row = _activity_row(out)
        assert "%" not in row
        assert "TensorRT and cuDNN" in row


def test_percent_and_bytes_never_render_two_bars_at_once():
    """One task is a byte transfer or a percentage-reporting transaction,
    never both."""
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)

    bar.bytes(412_000_000, 606_000_000)
    bar.percent(68, "libnvinfer10")
    row = _activity_row(out)
    assert "MB" not in row
    assert " 68%" in row

    bar.bytes(412_000_000, 606_000_000)
    row = _activity_row(out)
    assert "412 MB / 606 MB" in row
    assert "libnvinfer10" not in row


def test_a_dirty_note_on_the_bar_row_is_cleaned_too():
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)
    bar.percent(68, "\033[1;32mlibnvinfer10\033[0m")
    assert "\033" not in _activity_row(out)
    assert _activity_row(out).endswith("libnvinfer10")


# -- follow_apt -------------------------------------------------------------


# One transaction, as apt reports it: fetch every archive, then unpack and
# configure them, with a record kind this module does not read in between.
_APT_TRANSACTION = [
    "dlstatus:1:0.0000:Retrieving file 1 of 24",
    "dlstatus:1:50.0000:Retrieving file 12 of 24",
    "dlstatus:1:100.0000:Retrieving file 24 of 24",
    "media-change:/cdrom:/dev/sr0",
    "pmstatus:libnvinfer10:amd64:0.0000:Preparing libnvinfer10:amd64",
    "pmstatus:libnvinfer10:amd64:36.0000:"
    "Setting up libnvinfer10 (10.16.0.72-1+cuda13.2)",
    "pmstatus:tensorrt-dev:100.0000:"
    "Setting up tensorrt-dev (10.16.0.72-1+cuda13.2)",
]


def test_follow_apt_never_lets_the_bar_go_backwards():
    """The defect this mapping exists to prevent: both sweeps run 0 to 100,
    so feeding them to one bar naively resets it halfway through the
    transaction, which is worse than showing no bar at all."""
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)

    outcome = progress.follow_apt(
        _apt_stream(_APT_TRANSACTION, clock), renderer=bar, clock=clock
    )

    drawn = _percents(_ANSI.sub("", out.getvalue()))
    assert drawn == sorted(drawn)
    # The download sweep occupies the first segment and stops there; the
    # install sweep carries the bar the rest of the way.
    assert 50 in drawn and 100 in drawn
    assert max(drawn[: drawn.index(50) + 1]) == 50
    assert outcome.had_download_phase is True
    assert outcome.had_denominator is True
    assert outcome.percent == 100
    assert (outcome.lines, outcome.parsed) == (7, 6)


def test_follow_apt_holds_the_bar_when_a_record_arrives_out_of_order():
    """The mapping is monotonic in order; the high-water mark is what makes
    the rendered value monotonic outright."""
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)

    progress.follow_apt(
        _apt_stream(
            [
                "dlstatus:1:100.0000:Retrieving file 24 of 24",
                "pmstatus:libc6:60.0000:Setting up libc6",
                "dlstatus:2:10.0000:Retrieving a straggler",
                "pmstatus:libc6:62.0000:Configuring libc6",
            ],
            clock,
        ),
        renderer=bar,
        clock=clock,
    )

    drawn = _percents(_ANSI.sub("", out.getvalue()))
    assert drawn == sorted(drawn)
    assert drawn[-1] == 81


def test_follow_apt_gives_a_fully_cached_transaction_the_whole_range():
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)

    outcome = progress.follow_apt(
        _apt_stream(
            [
                "pmstatus:libc6:0.0000:Preparing libc6",
                "pmstatus:libc6:50.0000:Unpacking libc6",
                "pmstatus:libc6:100.0000:Setting up libc6",
            ],
            clock,
        ),
        renderer=bar,
        clock=clock,
    )

    assert outcome.had_download_phase is False
    # 50 percent of the install sweep is 50 percent of the bar, not 75.
    assert _percents(_ANSI.sub("", out.getvalue())) == [0, 0, 50, 50, 100]


def test_follow_apt_surfaces_each_description_once():
    """Section 3.2's `Setting up ...` rows. apt restates one description
    across dozens of percentage updates, and eight identical window rows
    show less than one does."""
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)

    first = "Setting up libnvinfer10 (10.16.0.72-1+cuda13.2)"
    second = "Setting up tensorrt-dev (10.16.0.72-1+cuda13.2)"
    restated = [f"pmstatus:libnvinfer10:{p}.0:{first}" for p in (10, 20, 30)]

    progress.follow_apt(
        _apt_stream(restated + [f"pmstatus:tensorrt-dev:40.0:{second}"], clock),
        renderer=bar,
        clock=clock,
    )

    assert _window_rows(out) == [first, second]


def test_follow_apt_collapses_retrieval_countdown_but_keeps_file_transitions():
    """The percentage bar tracks download movement; a volatile ETA is not
    a distinct verbose output event, while moving to another file is."""
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out, verbose=True)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)

    progress.follow_apt(
        _apt_stream(
            [
                "dlstatus:1:10.0:Retrieving file 2 of 6 (15s remaining)",
                "dlstatus:1:12.0:Retrieving file 2 of 6 (14s remaining)",
                "dlstatus:2:20.0:Retrieving file 3 of 6 (8s remaining)",
            ],
            clock,
        ),
        renderer=bar,
        clock=clock,
    )

    rendered = _ANSI.sub("", out.getvalue())
    assert rendered.count("Retrieving file 2 of 6\n") == 1
    assert rendered.count("Retrieving file 3 of 6\n") == 1
    assert "remaining)" not in rendered


@pytest.mark.parametrize(
    "duration",
    ["15s", "2min 15s", "1h 2min 15s", "1d 1h 2min 15s"],
)
def test_follow_apt_collapses_apt_duration_variants(duration):
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)

    outcome = progress.follow_apt(
        _apt_stream(
            [f"dlstatus:1:10.0:Retrieving file 2 of 6 ({duration} remaining)"],
            clock,
        ),
        renderer=bar,
        clock=clock,
    )

    assert _window_rows(out) == ["Retrieving file 2 of 6"]
    assert outcome.description == "Retrieving file 2 of 6"


def test_follow_apt_retains_non_duration_retrieval_parentheses():
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)
    description = "Retrieving file 2 of 6 (checksum verification remaining)"

    outcome = progress.follow_apt(
        _apt_stream([f"dlstatus:1:10.0:{description}"], clock),
        renderer=bar,
        clock=clock,
    )

    assert _window_rows(out) == [description]
    assert outcome.description == description


def test_follow_apt_does_not_normalise_pmstatus_parentheses():
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)
    description = "Setting up libcudnn9 (9.20.0.48-1 remaining)"

    outcome = progress.follow_apt(
        _apt_stream([f"pmstatus:libcudnn9:50.0:{description}"], clock),
        renderer=bar,
        clock=clock,
    )

    assert _window_rows(out) == [description]
    assert outcome.description == description


def test_follow_apt_names_the_task_on_the_renderer():
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)
    progress.follow_apt(
        _apt_stream(["pmstatus:libc6:10.0:Setting up libc6"], clock),
        renderer=bar,
        task="installing TensorRT and cuDNN",
        clock=clock,
    )
    assert "installing TensorRT and cuDNN" in _ANSI.sub("", out.getvalue())


def test_follow_apt_clears_a_denominator_the_previous_task_left_behind():
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)
    bar.bytes(412_000_000, 606_000_000)

    progress.follow_apt(
        _apt_stream(["status: libc6: half-installed"], clock),
        renderer=bar,
        clock=clock,
    )
    assert "%" not in _activity_row(out)


# -- follow_apt with nothing to count (section 5.3, REQUIRED) ---------------


def test_follow_apt_without_a_percentage_shows_a_spinner_and_never_a_bar():
    """A transaction whose stream says nothing this module can count is
    reported as elapsed time and apt's last description, not as a
    fabricated fraction."""
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)

    outcome = progress.follow_apt(
        _apt_stream(
            [
                "pmerror:libc6:Sub-process returned an error",
                "media-change:/cdrom:/dev/sr0",
                "not a status record at all",
                "pmerror:libc6:dpkg was interrupted",
            ],
            clock,
            seconds_per_line=9.0,
        ),
        renderer=bar,
        task="installing TensorRT",
        clock=clock,
    )

    rendered = _ANSI.sub("", out.getvalue())
    assert "%" not in rendered
    assert progress.BAR_FILLED not in rendered
    assert progress.BAR_EMPTY not in rendered

    row = _activity_row(out)
    assert "installing TensorRT" in row
    assert "36s" in row
    assert row.endswith("[ dpkg was interrupted ]")

    assert outcome.had_denominator is False
    assert outcome.percent is None
    assert outcome.description == "dpkg was interrupted"


def test_follow_apt_animates_the_spinner_while_it_has_no_denominator():
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)

    progress.follow_apt(
        _apt_stream(["media-change:/cdrom:/dev/sr0"] * 4, clock),
        renderer=bar,
        clock=clock,
    )

    frames = {
        row[0]
        for row in (
            line.strip() for line in _ANSI.sub("", out.getvalue()).split("\n")
        )
        if row and row[0] in progress.SPINNER_FRAMES
    }
    assert len(frames) > 1


def test_follow_apt_survives_a_stream_that_dies_mid_transaction():
    """apt was killed, so the read end of the pipe raises rather than
    ending. Whatever was drawn stands, and the caller owns the exit
    status."""
    clock = FakeClock()

    def dying():
        yield "pmstatus:libc6:40.0:Setting up libc6"
        raise OSError("Input/output error")

    outcome = progress.follow_apt(dying(), clock=clock)
    assert outcome.percent == 40
    assert outcome.lines == 1


def test_follow_apt_with_no_lines_at_all_reports_nothing_rather_than_zero():
    clock = FakeClock()
    outcome = progress.follow_apt([], clock=clock)
    assert outcome == progress.AptOutcome(
        lines=0,
        parsed=0,
        percent=None,
        description=None,
        had_download_phase=False,
        elapsed_s=0.0,
    )
    assert outcome.had_denominator is False


# -- follow_apt, the recorded line (sections 7 and 7.1) ---------------------


def test_render_apt_log_line_states_a_percentage_only_when_it_has_one():
    assert progress.render_apt_log_line("apt", 68, "Setting up libnvinfer10", 252) == (
        "apt: 68% after 4m12s - Setting up libnvinfer10"
    )
    assert progress.render_apt_log_line("apt", None, "Preparing libc6", 252) == (
        "apt: running for 4m12s - Preparing libc6"
    )
    assert progress.render_apt_log_line("apt", None, None, 252) == (
        "apt: running for 4m12s"
    )


def test_render_apt_done_line_does_not_round_an_interrupted_run_up():
    assert progress.render_apt_done_line("apt", 100, 362) == "apt: finished in 6m02s"
    assert progress.render_apt_done_line("apt", 62, 362) == (
        "apt: finished in 6m02s (apt last reported 62%)"
    )
    assert progress.render_apt_done_line("apt", None, 362) == (
        "apt: finished in 6m02s (apt reported no percentage)"
    )


def test_follow_apt_off_a_tty_logs_a_plain_line_on_an_interval(capsys):
    clock = FakeClock()
    bar = _plain_progress(clock)
    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(3)
    capsys.readouterr()

    progress.follow_apt(
        _apt_stream(_APT_TRANSACTION, clock, seconds_per_line=20.0),
        renderer=bar,
        task="installing TensorRT",
        log_interval_s=30.0,
        clock=clock,
    )

    err = capsys.readouterr().err
    # Nothing is drawn off a tty, so these lines are the only thing that
    # keeps a multi-gigabyte transaction distinguishable from a hang --
    # event 3 in section 2, which an operator interrupted.
    assert "installing TensorRT: 0% after 20s - Retrieving file 1 of 24" in err
    assert "installing TensorRT: 50% after 1m00s - Retrieving file 24 of 24" in err
    assert "installing TensorRT: finished in 2m20s" in err
    assert "\033" not in err
    # Throttled to the interval, not one line per record.
    assert err.count("after") == 4


def test_follow_apt_on_a_tty_records_the_line_instead_of_printing_it(
    tmp_path, capsys
):
    """Section 7.1: the periodic line stays off the screen so it cannot
    fight the live region, and lands in the transcript anyway."""
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)

    progress.follow_apt(
        _apt_stream(_APT_TRANSACTION, clock, seconds_per_line=20.0),
        renderer=bar,
        task="installing TensorRT",
        log_interval_s=30.0,
        clock=clock,
    )

    transcript = run_file.read_text(encoding="utf-8")
    assert "[info ] installing TensorRT: 0% after 20s" in transcript
    assert "[info ] installing TensorRT: finished in 2m20s" in transcript
    assert "\033" not in transcript
    assert "after" not in capsys.readouterr().err


def test_follow_apt_records_stable_retrieval_description_in_transcript(
    tmp_path,
):
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    out, clock = FakeTty(), FakeClock()
    bar = _apt_progress(clock, out)

    progress.follow_apt(
        _apt_stream(
            [
                "dlstatus:1:10.0:Retrieving file 2 of 6 (15s remaining)",
                "dlstatus:1:12.0:Retrieving file 2 of 6 (14s remaining)",
            ],
            clock,
            seconds_per_line=30.0,
        ),
        renderer=bar,
        task="installing cuDNN",
        log_interval_s=30.0,
        clock=clock,
    )

    transcript = run_file.read_text(encoding="utf-8")
    assert "Retrieving file 2 of 6" in transcript
    assert "remaining)" not in transcript


def test_follow_apt_with_no_renderer_still_reaches_the_transcript(capsys):
    clock = FakeClock()
    progress.follow_apt(
        _apt_stream(["pmstatus:libc6:40.0:Setting up libc6"], clock),
        clock=clock,
    )
    err = capsys.readouterr().err
    assert "apt: 40% after 0s - Setting up libc6" in err
    assert "apt: finished in 0s (apt last reported 40%)" in err
