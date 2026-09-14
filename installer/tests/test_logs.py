"""Tests for mv3dt_installer.logs (doc 00 §8.1-8.2).

Run from installer/: `python3 -m pytest tests/test_logs.py -v`
"""

from __future__ import annotations

import io
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from mv3dt_installer import logs  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_transcript_state():
    """Every test starts with no transcript open, and leaves none dangling."""
    logs._transcript_path = None
    yield
    logs._transcript_path = None


@pytest.fixture(autouse=True)
def _force_no_colour(monkeypatch):
    """Default all tests to a non-tty stderr so plain text is asserted on;
    individual tests override this to exercise the colour path."""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)


@pytest.fixture(autouse=True)
def _reset_module_switches(monkeypatch):
    """The colour override and the live-writer claim are module state: no
    test may inherit one from the test before it, and argv decides the
    non-interactive half of the colour question."""
    logs.set_colour(None)
    logs.clear_live_writer()
    monkeypatch.setattr(sys, "argv", ["mv3dt-installer", "install"])
    yield
    logs.set_colour(None)
    logs.clear_live_writer()


# ---------------------------------------------------------------------------
# log.info / log.warn / log.error -> stderr, level-prefixed
# ---------------------------------------------------------------------------


def test_log_info_writes_to_stderr_with_info_tag(capsys):
    logs.log.info("hello world")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[info ]" in captured.err
    assert "hello world" in captured.err


def test_log_warn_writes_to_stderr_with_warn_tag(capsys):
    logs.log.warn("careful now")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[warn ]" in captured.err
    assert "careful now" in captured.err


def test_log_error_writes_to_stderr_with_error_tag(capsys):
    logs.log.error("boom")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[error]" in captured.err
    assert "boom" in captured.err


def test_stdout_is_never_touched(capsys):
    logs.log.info("i")
    logs.log.warn("w")
    logs.log.error("e")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_colour_applied_when_stderr_is_a_tty(capsys, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    logs.log.info("coloured")
    captured = capsys.readouterr()
    assert "\033[32m" in captured.err  # green
    assert "\033[0m" in captured.err


def test_no_colour_when_stderr_is_not_a_tty(capsys):
    logs.log.info("plain")
    captured = capsys.readouterr()
    assert "\033[" not in captured.err


# ---------------------------------------------------------------------------
# die()
# ---------------------------------------------------------------------------


def test_die_logs_error_and_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        logs.die("fatal problem")
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "[error]" in captured.err
    assert "fatal problem" in captured.err


# ---------------------------------------------------------------------------
# open_transcript()
# ---------------------------------------------------------------------------


def test_open_transcript_creates_dir_and_run_file(tmp_path):
    log_dir = tmp_path / "logs"
    run_file = logs.open_transcript(log_dir=log_dir)

    assert log_dir.is_dir()
    assert run_file.parent == log_dir
    assert run_file.exists()
    assert run_file.name.startswith("install-")
    assert run_file.name.endswith(".log")


def test_open_transcript_creates_latest_symlink(tmp_path):
    log_dir = tmp_path / "logs"
    run_file = logs.open_transcript(log_dir=log_dir)

    latest = log_dir / "latest.log"
    assert latest.is_symlink()
    assert latest.resolve() == run_file.resolve()


def test_open_transcript_updates_symlink_on_reopen(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"

    monkeypatch.setattr(logs, "_timestamp", lambda: "20260101-000000")
    first = logs.open_transcript(log_dir=log_dir)
    latest = log_dir / "latest.log"
    assert latest.resolve() == first.resolve()

    # A later run (distinct timestamp) must repoint latest.log at the new
    # file, not leave it pointing at the first one.
    monkeypatch.setattr(logs, "_timestamp", lambda: "20260101-000100")
    second = logs.open_transcript(log_dir=log_dir)
    assert second != first
    assert latest.resolve() == second.resolve()


def test_open_transcript_wires_subsequent_log_calls_to_append(tmp_path):
    log_dir = tmp_path / "logs"
    run_file = logs.open_transcript(log_dir=log_dir)

    logs.log.info("first line")
    logs.log.warn("second line")
    logs.log.error("third line")

    contents = run_file.read_text(encoding="utf-8")
    assert "[info ] first line" in contents
    assert "[warn ] second line" in contents
    assert "[error] third line" in contents


def test_transcript_never_contains_ansi_even_when_stderr_is_coloured(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    log_dir = tmp_path / "logs"
    run_file = logs.open_transcript(log_dir=log_dir)

    logs.log.info("coloured on screen, plain in transcript")

    contents = run_file.read_text(encoding="utf-8")
    assert "\033[" not in contents
    assert "[info ] coloured on screen, plain in transcript" in contents


def test_log_calls_before_open_transcript_do_not_error(capsys):
    # No transcript open (autouse fixture resets state) -- log.* must still
    # work, just without a transcript sink.
    logs.log.info("no transcript yet")
    captured = capsys.readouterr()
    assert "no transcript yet" in captured.err


def test_open_transcript_returns_path_object(tmp_path):
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    import pathlib

    assert isinstance(run_file, pathlib.Path)


# ---------------------------------------------------------------------------
# transcript() -- the transcript-only sink (doc 08 §7.1)
# ---------------------------------------------------------------------------


def test_transcript_writes_the_line_to_the_transcript_only(tmp_path, capsys):
    """The whole point of the sink: the record gets the line, the screen does
    not. A caller reaches for it when something else already owns the
    terminal, so a print here would be the duplicate writer it exists to
    avoid."""
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")

    logs.transcript("info", "drawn by the renderer, not by logs")

    assert run_file.read_text(encoding="utf-8") == (
        "[info ] drawn by the renderer, not by logs\n"
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_transcript_labels_every_level_the_way_log_does(tmp_path):
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")

    logs.transcript("info", "an info line")
    logs.transcript("warn", "a warn line")
    logs.transcript("error", "an error line")

    assert run_file.read_text(encoding="utf-8").splitlines() == [
        "[info ] an info line",
        "[warn ] a warn line",
        "[error] an error line",
    ]


def test_transcript_and_log_append_to_one_file_in_order(tmp_path):
    """A run mixes the two sinks line by line, so a post-mortem reads the
    sequence the installer actually produced rather than two interleaved
    halves."""
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")

    logs.log.info("printed and recorded")
    logs.transcript("info", "recorded only")
    logs.log.warn("printed and recorded again")

    assert run_file.read_text(encoding="utf-8").splitlines() == [
        "[info ] printed and recorded",
        "[info ] recorded only",
        "[warn ] printed and recorded again",
    ]


def test_transcript_strips_ansi_from_a_line_it_did_not_write(tmp_path):
    """Section 7's no-escape rule holds on this path too. Command output is
    exactly what reaches it -- apt and curl colour their own lines -- and an
    escape in the transcript corrupts the one artifact a failed install
    leaves behind."""
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")

    logs.transcript("warn", "\033[32mSetting up\033[0m libnvinfer10")

    contents = run_file.read_text(encoding="utf-8")
    assert "\033" not in contents
    assert contents == "[warn ] Setting up libnvinfer10\n"


def test_transcript_before_open_transcript_is_a_no_op(capsys):
    """No transcript open (autouse fixture resets state): the sink is silent
    rather than an error, exactly as log.* is."""
    logs.transcript("info", "nowhere to put this yet")

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_transcript_is_unaffected_by_a_coloured_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")

    logs.transcript("info", "still plain")

    assert run_file.read_text(encoding="utf-8") == "[info ] still plain\n"


# ---------------------------------------------------------------------------
# Colour is a decision about the context, not only about the terminal
# (doc 08 §7, §12.2 defect 4)
# ---------------------------------------------------------------------------


def test_non_interactive_suppresses_colour_on_a_real_tty(capsys, monkeypatch):
    """Section 7 is REQUIRED and admits no escape sequence at all in a
    --non-interactive run, whatever stderr happens to be attached to. An
    operator running the installer non-interactively from a terminal was
    still getting a green level label on every line."""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(
        sys, "argv", ["mv3dt-installer", "--non-interactive", "install"]
    )

    logs.log.info("plain please")
    logs.log.warn("this too")
    logs.log.error("and this")

    assert "\033" not in capsys.readouterr().err


def test_colour_survives_an_interactive_run_with_other_flags(
    capsys, monkeypatch
):
    """The check is for the flag itself, not for any flag."""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(
        sys, "argv", ["mv3dt-installer", "install", "--verbose"]
    )

    logs.log.info("coloured")

    assert "\033[32m" in capsys.readouterr().err


@pytest.mark.parametrize(
    "spelling",
    ["--non-interactive", "--non-interacti", "--non-int", "--non", "--no"],
)
def test_every_abbreviation_argparse_accepts_suppresses_colour(
    capsys, monkeypatch, spelling
):
    """`build_parser()` takes argparse's defaults, so `allow_abbrev` is on
    and any unambiguous prefix is a real spelling of the flag. The
    pre-parse default has to recognise all of them, because emitting an
    escape into a run that forbids one is the failure section 7 names."""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys, "argv", ["mv3dt-installer", spelling, "install"])

    logs.log.info("plain please")

    assert "\033" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "spelling", ["--nothing-like-it", "--verbose", "--install-dir", "-n"]
)
def test_a_flag_that_is_not_the_one_leaves_colour_alone(
    capsys, monkeypatch, spelling
):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys, "argv", ["mv3dt-installer", spelling, "install"])

    logs.log.info("coloured")

    assert "\033[32m" in capsys.readouterr().err


def test_the_default_stops_reading_options_at_a_bare_double_dash(
    capsys, monkeypatch
):
    """argparse stops treating tokens as options after `--`, so a positional
    that happens to look like the flag is not the flag."""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(
        sys, "argv", ["mv3dt-installer", "run", "--", "--non-interactive"]
    )

    logs.log.info("coloured")

    assert "\033[32m" in capsys.readouterr().err


def test_the_parsed_value_overrides_the_pre_parse_default(capsys, monkeypatch):
    """argv is the default until the arguments are parsed, and no longer.

    The unit that owns `app.py` calls `set_colour(not
    args.non_interactive)` straight after `parse_args`, and from then on
    that answer governs, whatever argv looked like.
    """
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(
        sys, "argv", ["mv3dt-installer", "--non", "install"]
    )
    logs.set_colour(True)

    logs.log.info("the parsed value said colour")

    assert "\033[32m" in capsys.readouterr().err


def test_set_colour_forces_the_decision_both_ways(capsys, monkeypatch):
    """The override for a caller holding a parsed flag it trusts more than
    argv, and the way a test states its intent outright."""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    logs.set_colour(False)
    logs.log.info("forced plain")
    assert "\033" not in capsys.readouterr().err

    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    logs.set_colour(True)
    logs.log.info("forced coloured")
    assert "\033[32m" in capsys.readouterr().err


def test_set_colour_none_restores_auto_detection(capsys, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    logs.set_colour(False)
    logs.set_colour(None)

    logs.log.info("back to the terminal's answer")

    assert "\033[32m" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The transcript strip is as wide as the one `progress` applies
# (doc 08 §12.2 defect 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("\033[32mgreen\033[0m", "green"),
        ("\033]0;title\a", ""),
        ("lone \033 escape", "lone  escape"),
        ("C1 CSI \x9b0m here", "C1 CSI 0m here"),
        ("C1 OSC \x9dtitle here", "C1 OSC title here"),
        ("bell \a here", "bell  here"),
        ("back\bspace", "backspace"),
        ("nul \x00 here", "nul  here"),
        ("del \x7f here", "del  here"),
        ("Get:1 ... 40%\rGet:1 ... 100%", "Get:1 ... 100%"),
    ],
)
def test_the_transcript_strip_covers_every_control_form(
    tmp_path, raw, expected
):
    """Command output reaches `log.info` raw from two step call sites
    (nvidia-smi in step 1, an stderr tail in step 2), so every one of these
    was a real way to get a control character into the one artifact a
    failed install leaves behind."""
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")

    logs.log.info(raw)

    assert run_file.read_text(encoding="utf-8") == f"[info ] {expected}\n"


def test_the_transcript_strip_keeps_tabs_and_newlines(tmp_path):
    """The one place this is deliberately narrower than `progress.sanitise`.
    A log message may be indented and may span lines: the transcript is a
    file, not a region whose redraw arithmetic needs one row per line."""
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")

    logs.log.info("GPU 0:\n\tNVIDIA RTX 5000\n\tDriver 580.65.06")

    assert run_file.read_text(encoding="utf-8") == (
        "[info ] GPU 0:\n\tNVIDIA RTX 5000\n\tDriver 580.65.06\n"
    )


def test_a_coloured_screen_line_is_still_recorded_plain(tmp_path, monkeypatch):
    """The colour this module adds and the escapes a caller's text carries
    are stripped by the same pass, on the recorded copy only."""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")

    logs.log.warn("\x9b31mfailed\x9b0m to fetch\rfetched")

    assert run_file.read_text(encoding="utf-8") == "[warn ] fetched\n"


# ---------------------------------------------------------------------------
# A live renderer owns the cursor on the stream it draws to
# (doc 08 §12.2 defect 3)
# ---------------------------------------------------------------------------


class Recorder:
    """A writer with the shape a renderer registers: a bound method.

    Deliberately not `list.append`. A bound method is rebuilt on every
    attribute access, which is the whole reason `clear_live_writer`
    compares with `==`, so the tests below have to register the kind of
    callable that actually exhibits it.
    """

    def __init__(self) -> None:
        self.taken: list[tuple[str, str]] = []

    def write(self, level: str, line: str) -> None:
        self.taken.append((level, line))


def test_a_registered_writer_takes_the_printed_copy(capsys, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    recorder = Recorder()
    logs.set_live_writer(sys.stderr, recorder.write)

    logs.log.info("through the renderer")

    assert recorder.taken == [
        ("info", "\033[32m[info ]\033[0m through the renderer")
    ]
    assert capsys.readouterr().err == ""


def test_a_registered_writer_is_told_the_level(capsys):
    """The level decides what the region does after the line (blocker 2)."""
    recorder = Recorder()
    logs.set_live_writer(sys.stderr, recorder.write)

    logs.log.info("i")
    logs.log.warn("w")
    logs.log.error("e")

    assert [level for level, _ in recorder.taken] == ["info", "warn", "error"]
    assert capsys.readouterr().err == ""


def test_a_registered_writer_does_not_take_the_recorded_copy(
    tmp_path, monkeypatch
):
    """Section 7.1: routing the screen copy must never cost the record."""
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    logs.set_live_writer(sys.stderr, Recorder().write)

    logs.log.info("drawn, and recorded")

    assert run_file.read_text(encoding="utf-8") == "[info ] drawn, and recorded\n"


def test_a_writer_registered_for_another_stream_is_not_used(capsys):
    """A renderer drawing somewhere else shares no cursor with this module,
    so there is nothing for it to protect."""
    recorder = Recorder()
    logs.set_live_writer(io.StringIO(), recorder.write)

    logs.log.info("straight to stderr")

    assert recorder.taken == []
    assert "straight to stderr" in capsys.readouterr().err


def test_clear_live_writer_gives_the_stream_back(capsys):
    recorder = Recorder()
    logs.set_live_writer(sys.stderr, recorder.write)
    logs.clear_live_writer()

    logs.log.info("printed again")

    assert recorder.taken == []
    assert "printed again" in capsys.readouterr().err


def test_clearing_the_current_writer_gives_the_stream_back(capsys):
    """The named form of the un-claim, with the callable production uses.

    `recorder.write` is a different object every time it is read, so an
    identity comparison inside `clear_live_writer` would leave the claim
    standing here and this test would fail on the line below.
    """
    recorder = Recorder()
    logs.set_live_writer(sys.stderr, recorder.write)

    logs.clear_live_writer(recorder.write)
    logs.log.info("printed again")

    assert recorder.taken == []
    assert "printed again" in capsys.readouterr().err


def test_clearing_a_superseded_writer_leaves_the_current_one_alone(capsys):
    """A renderer that has already been replaced must not be able to unhook
    the one that replaced it -- and the current one must still be able to."""
    stale, current = Recorder(), Recorder()
    logs.set_live_writer(sys.stderr, stale.write)
    logs.set_live_writer(sys.stderr, current.write)

    logs.clear_live_writer(stale.write)
    logs.log.info("still routed")

    assert current.taken == [("info", "[info ] still routed")]
    assert stale.taken == []
    assert capsys.readouterr().err == ""

    # The same call, with the writer that is actually registered, does
    # clear it. Without this the test passes whether or not clearing works.
    logs.clear_live_writer(current.write)
    logs.log.info("printed again")

    assert len(current.taken) == 1
    assert "printed again" in capsys.readouterr().err


def test_die_reaches_the_screen_through_a_registered_writer():
    """`die` is the last thing an operator sees; it may not be the one line
    the region eats."""
    recorder = Recorder()
    logs.set_live_writer(sys.stderr, recorder.write)

    with pytest.raises(SystemExit):
        logs.die("fatal problem")

    assert recorder.taken == [("error", "[error] fatal problem")]
