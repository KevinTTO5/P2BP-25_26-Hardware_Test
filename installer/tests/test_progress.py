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


def test_phase_transitions_are_logged_even_while_they_are_drawn(capsys):
    """Section 3.3: the transcript carries the phase sequence regardless."""
    out, clock = FakeTty(), FakeClock()
    bar = _tty_progress(clock, out)

    bar.begin_step(1, "Prerequisites", _PHASES)
    bar.phase(1)
    clock.advance(12)
    bar.phase(2)

    err = capsys.readouterr().err
    assert "[ 1/7 ] Prerequisites" in err
    assert "step 1/7 phase 1/3: base packages" in err
    assert "step 1/7 phase 1/3 done: base packages (12s)" in err
    assert "step 1/7 phase 2/3: CUDA repo and toolkit" in err


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
