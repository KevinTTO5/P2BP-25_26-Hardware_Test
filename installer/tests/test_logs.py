"""Tests for mv3dt_installer.logs (doc 00 §8.1-8.2).

Run from installer/: `python3 -m pytest tests/test_logs.py -v`
"""

from __future__ import annotations

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
