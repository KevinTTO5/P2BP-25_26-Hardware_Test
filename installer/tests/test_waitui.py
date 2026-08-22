"""Tests for `mv3dt_installer.waitui` (the blocking wait/poll screen).

Every test injects `clock` and `sleep`, so no test ever really sleeps and
elapsed time is exact rather than wall-clock-dependent. The autouse
`_forbid_real_sleep` fixture makes an accidental real sleep fail loudly.

Run with:
    cd installer && python3 -m pytest tests/test_waitui.py -v
"""

from __future__ import annotations

import io
import pathlib
import sys
import time
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mv3dt_installer import logs, privilege, waitui  # noqa: E402
from mv3dt_installer.waitui import WaitOutcome  # noqa: E402


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


class FakeTime:
    """Injected `clock`/`sleep` pair: sleeping advances the fake clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class CountingPredicate:
    """Returns each queued value in turn, then repeats the last one."""

    def __init__(self, *values: bool) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        index = min(self.calls, len(self.values)) - 1
        return self.values[index]


class FakeTty(io.StringIO):
    """A StringIO that claims to be an interactive terminal."""

    def isatty(self) -> bool:
        return True


def _never_called() -> bool:  # pragma: no cover -- asserted never to run
    raise AssertionError("predicate must not be called")


def _action(text, command=None, path=None):
    """A duck-typed stand-in for `steps.UserAction` (no import from steps)."""
    return types.SimpleNamespace(text=text, command=command, path=path)


# ---------------------------------------------------------------------------
# wait_until -- outcomes and poll accounting
# ---------------------------------------------------------------------------


def test_predicate_already_true_returns_satisfied_without_sleeping():
    clock = FakeTime()
    predicate = CountingPredicate(True)

    outcome = waitui.wait_until(
        predicate,
        description="waiting for AMC export",
        clock=clock.clock,
        sleep=clock.sleep,
        out=io.StringIO(),
    )

    assert outcome is WaitOutcome.SATISFIED
    assert clock.sleeps == []
    assert predicate.calls == 1


def test_true_on_third_poll_sleeps_exactly_three_times():
    """One immediate check, then three sleep+check polls; true on the third."""
    clock = FakeTime()
    predicate = CountingPredicate(False, False, False, True)

    outcome = waitui.wait_until(
        predicate,
        description="waiting for AMC export",
        timeout_s=600.0,
        poll_s=2.0,
        clock=clock.clock,
        sleep=clock.sleep,
        out=io.StringIO(),
    )

    assert outcome is WaitOutcome.SATISFIED
    assert clock.sleeps == [2.0, 2.0, 2.0]
    assert predicate.calls == 4
    assert clock.now == pytest.approx(1006.0)


def test_never_true_returns_timeout_at_the_boundary():
    clock = FakeTime()
    predicate = CountingPredicate(False)

    outcome = waitui.wait_until(
        predicate,
        description="waiting for AMC export",
        timeout_s=10.0,
        poll_s=2.0,
        clock=clock.clock,
        sleep=clock.sleep,
        out=io.StringIO(),
    )

    assert outcome is WaitOutcome.TIMEOUT
    # Five sleeps consume exactly the budget; the sixth check sees
    # elapsed == timeout and gives up, so the predicate gets its final
    # chance *on* the boundary rather than one poll early.
    assert clock.sleeps == [2.0, 2.0, 2.0, 2.0, 2.0]
    assert predicate.calls == 6
    assert clock.now - 1000.0 == pytest.approx(10.0)


def test_final_sleep_is_shortened_so_it_never_overruns_the_timeout():
    clock = FakeTime()

    outcome = waitui.wait_until(
        CountingPredicate(False),
        description="waiting for AMC export",
        timeout_s=5.0,
        poll_s=2.0,
        clock=clock.clock,
        sleep=clock.sleep,
        out=io.StringIO(),
    )

    assert outcome is WaitOutcome.TIMEOUT
    assert clock.sleeps == [2.0, 2.0, 1.0]
    assert clock.now - 1000.0 == pytest.approx(5.0)


def test_zero_timeout_checks_once_then_times_out():
    clock = FakeTime()
    predicate = CountingPredicate(False)

    outcome = waitui.wait_until(
        predicate,
        description="waiting for AMC export",
        timeout_s=0.0,
        clock=clock.clock,
        sleep=clock.sleep,
        out=io.StringIO(),
    )

    assert outcome is WaitOutcome.TIMEOUT
    assert clock.sleeps == []
    assert predicate.calls == 1


# ---------------------------------------------------------------------------
# wait_until -- Ctrl-C never propagates
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_from_sleep_returns_cancelled():
    clock = FakeTime()

    def _interrupting_sleep(seconds):
        raise KeyboardInterrupt

    outcome = waitui.wait_until(
        CountingPredicate(False),
        description="waiting for AMC export",
        timeout_s=600.0,
        clock=clock.clock,
        sleep=_interrupting_sleep,
        out=io.StringIO(),
    )

    assert outcome is WaitOutcome.CANCELLED


def test_keyboard_interrupt_from_predicate_returns_cancelled():
    clock = FakeTime()

    def _interrupting_predicate():
        raise KeyboardInterrupt

    outcome = waitui.wait_until(
        _interrupting_predicate,
        description="waiting for AMC export",
        clock=clock.clock,
        sleep=clock.sleep,
        out=io.StringIO(),
    )

    assert outcome is WaitOutcome.CANCELLED
    assert clock.sleeps == []


def test_cancelled_wait_closes_the_status_line_on_a_tty():
    clock = FakeTime()
    out = FakeTty()

    def _interrupting_sleep(seconds):
        raise KeyboardInterrupt

    waitui.wait_until(
        CountingPredicate(False),
        description="waiting for AMC export",
        clock=clock.clock,
        sleep=_interrupting_sleep,
        out=out,
    )

    assert out.getvalue().endswith("\n")


# ---------------------------------------------------------------------------
# wait_until -- non-interactive
# ---------------------------------------------------------------------------


def test_non_interactive_returns_skipped_without_polling():
    clock = FakeTime()
    out = io.StringIO()

    outcome = waitui.wait_until(
        _never_called,
        description="waiting for AMC export",
        non_interactive=True,
        clock=clock.clock,
        sleep=clock.sleep,
        out=out,
    )

    assert outcome is WaitOutcome.SKIPPED
    assert clock.sleeps == []
    assert out.getvalue() == ""


def test_non_interactive_warns_on_stderr(capsys):
    waitui.wait_until(
        _never_called,
        description="waiting for AMC export",
        non_interactive=True,
        out=io.StringIO(),
    )

    captured = capsys.readouterr()
    assert "[warn ]" in captured.err
    assert "non-interactive" in captured.err
    assert captured.out == ""


# ---------------------------------------------------------------------------
# Rendering -- tty status line
# ---------------------------------------------------------------------------


def test_tty_status_line_is_carriage_returned_and_shows_the_clock():
    clock = FakeTime()
    out = FakeTty()

    outcome = waitui.wait_until(
        CountingPredicate(False, True),
        description=(
            "waiting for AMC export: "
            "/home/op/auto-magic-calib/projects/lab/exports"
        ),
        timeout_s=3600.0,
        poll_s=252.0,
        clock=clock.clock,
        sleep=clock.sleep,
        out=out,
    )

    rendered = out.getvalue()
    assert outcome is WaitOutcome.SATISFIED
    assert "\r" in rendered
    assert (
        "waiting for AMC export: "
        "/home/op/auto-magic-calib/projects/lab/exports  [00:00 / 60:00]"
    ) in rendered
    assert "[04:12 / 60:00]" in rendered


def test_tty_status_line_is_rewritten_in_place_once_per_poll():
    """One carriage return per poll, no newlines until the wait ends."""
    clock = FakeTime()
    out = FakeTty()

    waitui.wait_until(
        CountingPredicate(False, False, True),
        description="waiting",
        timeout_s=3600.0,
        poll_s=1.0,
        clock=clock.clock,
        sleep=clock.sleep,
        out=out,
    )

    rendered = out.getvalue()
    header, _, live = rendered.partition("\r")
    segments = live.split("\r")

    # Three polls -> three status writes (the first consumed by partition).
    assert len(segments) == 3
    # Each rewrite is padded to at least the width of the one it overwrites,
    # so a shorter line can never leave a tail of the previous one behind.
    widths = [len(seg.rstrip("\n")) for seg in segments]
    assert widths == sorted(widths)
    # The live region is a single line: only the closing newline at the end.
    assert live.count("\n") == 1
    assert rendered.endswith("\n")
    assert header.endswith("\n")


def test_non_tty_output_uses_log_lines_and_writes_no_control_characters(capsys):
    clock = FakeTime()
    out = io.StringIO()

    waitui.wait_until(
        CountingPredicate(False, True),
        description="waiting for AMC export",
        timeout_s=600.0,
        poll_s=2.0,
        clock=clock.clock,
        sleep=clock.sleep,
        out=out,
    )

    captured = capsys.readouterr()
    assert out.getvalue() == ""
    assert "\r" not in captured.err
    assert "waiting for AMC export  [00:00 / 10:00]" in captured.err
    assert "[info ]" in captured.err


def test_non_tty_status_lines_are_rate_limited(capsys):
    clock = FakeTime()

    waitui.wait_until(
        CountingPredicate(False),
        description="waiting for AMC export",
        timeout_s=120.0,
        poll_s=2.0,
        clock=clock.clock,
        sleep=clock.sleep,
        out=io.StringIO(),
    )

    captured = capsys.readouterr()
    status_lines = [
        line for line in captured.err.splitlines() if " / 02:00]" in line
    ]
    # 120s at one line per LOG_INTERVAL_S (30s), not one line per 2s poll.
    assert len(status_lines) == 5


def test_transcript_records_the_wait_without_control_characters(tmp_path):
    clock = FakeTime()
    logs.open_transcript(tmp_path)

    waitui.wait_until(
        CountingPredicate(False, True),
        description="waiting for AMC export",
        timeout_s=600.0,
        poll_s=2.0,
        clock=clock.clock,
        sleep=clock.sleep,
        out=FakeTty(),
    )

    transcript = (tmp_path / "latest.log").read_text(encoding="utf-8")
    assert "\r" not in transcript
    assert "done after 00:02: waiting for AMC export" in transcript


def test_timeout_and_satisfied_log_distinct_outcomes(capsys):
    clock = FakeTime()
    waitui.wait_until(
        CountingPredicate(False),
        description="waiting for AMC export",
        timeout_s=4.0,
        poll_s=2.0,
        clock=clock.clock,
        sleep=clock.sleep,
        out=io.StringIO(),
    )
    assert "timed out after 00:04" in capsys.readouterr().err

    clock = FakeTime()
    waitui.wait_until(
        CountingPredicate(True),
        description="waiting for AMC export",
        clock=clock.clock,
        sleep=clock.sleep,
        out=io.StringIO(),
    )
    assert "done after 00:00" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Rendering -- the hint header
# ---------------------------------------------------------------------------


def test_render_wait_header_without_hints_omits_the_list():
    header = waitui.render_wait_header("waiting for AMC export")

    assert header.splitlines()[0] == "waiting for AMC export"
    assert "1." not in header
    assert "Ctrl-C" in header


def test_render_wait_header_renders_text_command_and_path():
    header = waitui.render_wait_header(
        "waiting for AMC export",
        [
            _action("Open the AutoMagicCalib GUI", command="firefox :8080"),
            _action("Export the calibration", path="/home/op/amc/lab.yml"),
        ],
    )

    assert "    1. Open the AutoMagicCalib GUI" in header
    assert "       $ firefox :8080" in header
    assert "    2. Export the calibration" in header
    assert "       (edit: /home/op/amc/lab.yml)" in header


def test_hint_actions_are_duck_typed_not_isinstance_checked():
    """A bare object with only `.text` renders; no `.command`/`.path` needed."""

    class OnlyText:
        text = "Finish in the browser"

    header = waitui.render_wait_header("waiting", [OnlyText()])

    assert "    1. Finish in the browser" in header
    assert "$" not in header


def test_header_is_written_once_before_the_status_line():
    clock = FakeTime()
    out = FakeTty()

    waitui.wait_until(
        CountingPredicate(False, False, True),
        description="waiting for AMC export",
        hint_actions=[_action("Finish in the browser")],
        timeout_s=600.0,
        poll_s=2.0,
        clock=clock.clock,
        sleep=clock.sleep,
        out=out,
    )

    rendered = out.getvalue()
    assert rendered.count("Finish in the browser") == 1
    assert rendered.index("Finish in the browser") < rendered.index("\r")


def test_the_same_hint_actions_render_verbatim_in_a_user_action_block():
    """The timeout path hands this exact list to privilege.py, unchanged."""
    actions = [
        _action("Open the AutoMagicCalib GUI", command="firefox :8080"),
        _action("Export the calibration", path="/home/op/amc/lab.yml"),
    ]

    header = waitui.render_wait_header("waiting for AMC export", actions)
    block = privilege.render_user_action_block(
        "Ingest calibration", "the export directory is still empty", actions
    )

    for line in (
        "    1. Open the AutoMagicCalib GUI",
        "       $ firefox :8080",
        "    2. Export the calibration",
        "       (edit: /home/op/amc/lab.yml)",
    ):
        assert line in header
        assert line in block


# ---------------------------------------------------------------------------
# dir_has_files
# ---------------------------------------------------------------------------


def test_dir_has_files_is_false_for_an_empty_directory(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()

    assert waitui.dir_has_files(exports)() is False


def test_dir_has_files_is_true_once_a_file_appears(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    predicate = waitui.dir_has_files(exports)

    assert predicate() is False
    (exports / "calibration.yml").write_text("cameras: []\n", encoding="utf-8")
    assert predicate() is True


def test_dir_has_files_is_false_for_a_missing_path(tmp_path):
    assert waitui.dir_has_files(tmp_path / "nope")() is False


def test_dir_has_files_is_false_for_a_regular_file(tmp_path):
    target = tmp_path / "exports"
    target.write_text("not a directory\n", encoding="utf-8")

    assert waitui.dir_has_files(target)() is False


def test_dir_has_files_finds_a_file_in_a_subdirectory(tmp_path):
    exports = tmp_path / "exports"
    (exports / "run-1").mkdir(parents=True)

    predicate = waitui.dir_has_files(exports)
    assert predicate() is False

    (exports / "run-1" / "cameras.json").write_text("{}\n", encoding="utf-8")
    assert predicate() is True


def test_dir_has_files_accepts_a_string_path(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "export.txt").write_text("x\n", encoding="utf-8")

    assert waitui.dir_has_files(str(exports))() is True


def test_wait_until_drives_dir_has_files_end_to_end(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    clock = FakeTime()

    def _sleep(seconds):
        clock.sleep(seconds)
        if len(clock.sleeps) == 2:
            # The human finishes in the browser between two polls.
            (exports / "calibration.yml").write_text("ok\n", encoding="utf-8")

    outcome = waitui.wait_until(
        waitui.dir_has_files(exports),
        description=f"waiting for AMC export: {exports}",
        timeout_s=600.0,
        poll_s=2.0,
        clock=clock.clock,
        sleep=_sleep,
        out=io.StringIO(),
    )

    assert outcome is WaitOutcome.SATISFIED
    assert clock.sleeps == [2.0, 2.0]
