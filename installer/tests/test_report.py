"""Tests for mv3dt_installer.report (doc 00 §8.3-8.4).

Run from installer/: `python3 -m pytest tests/test_report.py -v`
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from mv3dt_installer import logs, report  # noqa: E402


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


# ---------------------------------------------------------------------------
# report_installed / report_already_installed -> exact required strings
# ---------------------------------------------------------------------------


def test_report_installed_exact_string(capsys):
    report.report_installed("cuda-toolkit-13-2", "13.2")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "installed cuda-toolkit-13-2 version 13.2" in captured.err
    # Must not accidentally match the "already installed" variant.
    assert "already installed cuda-toolkit-13-2 version 13.2" not in captured.err


def test_report_already_installed_exact_string(capsys):
    report.report_already_installed("gstreamer1.0-tools", "1.24.2")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "already installed gstreamer1.0-tools version 1.24.2" in captured.err


def test_report_installed_uses_log_info_tag(capsys):
    report.report_installed("deepstream-9.1", "9.1.0-1")
    captured = capsys.readouterr()
    assert "[info ]" in captured.err


def test_report_already_installed_uses_log_info_tag(capsys):
    report.report_already_installed("deepstream-9.1", "9.1.0-1")
    captured = capsys.readouterr()
    assert "[info ]" in captured.err


def test_report_installed_returns_none():
    assert report.report_installed("pkg", "1.0") is None


def test_report_already_installed_returns_none():
    assert report.report_already_installed("pkg", "1.0") is None


def test_report_helpers_append_to_transcript(tmp_path):
    log_dir = tmp_path / "logs"
    run_file = logs.open_transcript(log_dir=log_dir)

    report.report_installed("cuda-toolkit-13-2", "13.2")
    report.report_already_installed("gstreamer1.0-tools", "1.24.2")

    contents = run_file.read_text(encoding="utf-8")
    assert "installed cuda-toolkit-13-2 version 13.2" in contents
    assert "already installed gstreamer1.0-tools version 1.24.2" in contents


# ---------------------------------------------------------------------------
# verify_pinned
# ---------------------------------------------------------------------------


def test_verify_pinned_match_returns_true_and_logs(capsys):
    result = report.verify_pinned("gstreamer1.0-tools", "1.24.2", "1.24.2")
    captured = capsys.readouterr()
    assert result is True
    assert "Version OK: gstreamer1.0-tools == 1.24.2" in captured.err


def test_verify_pinned_mismatch_returns_false_and_logs_exact_string(capsys):
    result = report.verify_pinned("deepstream-9.1", "9.0.0-1", "9.1.0-1")
    captured = capsys.readouterr()
    assert result is False
    assert (
        "Version check failed: deepstream-9.1 — expected '9.1.0-1', "
        "got '9.0.0-1'" in captured.err
    )


def test_verify_pinned_mismatch_uses_em_dash_not_hyphen(capsys):
    report.verify_pinned("label", "actual", "expected")
    captured = capsys.readouterr()
    assert "—" in captured.err  # em dash
    assert " - expected" not in captured.err  # not a plain hyphen substitute


def test_verify_pinned_never_raises_or_exits_on_mismatch():
    # Must not raise SystemExit (i.e. must not call die()) or any other
    # exception on mismatch -- the caller decides what to do.
    try:
        result = report.verify_pinned("label", "wrong", "right")
    except SystemExit:
        pytest.fail("verify_pinned must not call sys.exit on mismatch")
    assert result is False


def test_verify_pinned_match_uses_log_info_tag(capsys):
    report.verify_pinned("label", "1.0", "1.0")
    captured = capsys.readouterr()
    assert "[info ]" in captured.err


def test_verify_pinned_appends_to_transcript_on_match(tmp_path):
    log_dir = tmp_path / "logs"
    run_file = logs.open_transcript(log_dir=log_dir)

    report.verify_pinned("gstreamer1.0-tools", "1.24.2", "1.24.2")

    contents = run_file.read_text(encoding="utf-8")
    assert "Version OK: gstreamer1.0-tools == 1.24.2" in contents


def test_verify_pinned_appends_to_transcript_on_mismatch(tmp_path):
    log_dir = tmp_path / "logs"
    run_file = logs.open_transcript(log_dir=log_dir)

    report.verify_pinned("deepstream-9.1", "9.0.0-1", "9.1.0-1")

    contents = run_file.read_text(encoding="utf-8")
    assert (
        "Version check failed: deepstream-9.1 — expected '9.1.0-1', "
        "got '9.0.0-1'" in contents
    )
