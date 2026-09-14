"""Tests for mv3dt_installer.report (doc 00 §8.3-8.4).

Run from installer/: `python3 -m pytest tests/test_report.py -v`
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from mv3dt_installer import logs, report  # noqa: E402
from mv3dt_installer.steps import StepStatus, UserAction  # noqa: E402


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


# ---------------------------------------------------------------------------
# doc 08 §6 -- Evidence / with_evidence
# ---------------------------------------------------------------------------


def test_evidence_keyword_labels_match_the_doc_08_worked_example():
    evidence = report.Evidence(
        controlling_terminal="/dev/pts/1",
        sshd_ancestor=False,
        active_display_manager="gdm3",
    )
    assert evidence.render() == (
        "controlling terminal: /dev/pts/1; sshd ancestor: no; "
        "active display manager: gdm3"
    )


def test_with_evidence_reproduces_the_doc_08_worked_example():
    message = (
        "the driver installer must stop the desktop session, which would kill "
        "this installer along with it"
    )
    rendered = report.with_evidence(
        message,
        report.Evidence(
            controlling_terminal="/dev/pts/1",
            sshd_ancestor=False,
            active_display_manager="gdm3",
        ),
    )
    assert rendered == (
        "the driver installer must stop the desktop session, which would kill "
        "this installer along with it (controlling terminal: /dev/pts/1; "
        "sshd ancestor: no; active display manager: gdm3)"
    )


def test_evidence_preserves_insertion_order_not_alphabetical():
    evidence = report.Evidence()
    evidence.add("zulu", "1").add("alpha", "2").add("mike", "3")
    assert evidence.render() == "zulu: 1; alpha: 2; mike: 3"


def test_evidence_add_returns_self_for_chaining():
    evidence = report.Evidence()
    assert evidence.add("a", "b") is evidence


def test_evidence_booleans_render_as_yes_and_no():
    evidence = report.Evidence(a=True, b=False)
    assert evidence.render() == "a: yes; b: no"


def test_evidence_none_and_blank_render_as_unknown():
    evidence = report.Evidence(a=None, b="   ")
    assert evidence.render() == "a: unknown; b: unknown"


def test_evidence_accepts_a_mapping():
    evidence = report.Evidence({"controlling terminal": "/dev/tty3"})
    assert evidence.render() == "controlling terminal: /dev/tty3"


def test_evidence_accepts_an_iterable_of_pairs():
    evidence = report.Evidence([("a", 1), ("b", 2)])
    assert evidence.render() == "a: 1; b: 2"


def test_evidence_is_falsey_when_empty_and_truthy_when_not():
    assert not report.Evidence()
    assert report.Evidence(a="b")
    assert len(report.Evidence(a="b", c="d")) == 2


def test_evidence_str_is_its_render():
    evidence = report.Evidence(a="b")
    assert str(evidence) == evidence.render()


def test_render_evidence_accepts_every_shape():
    assert report.render_evidence(None) == ""
    assert report.render_evidence("already: rendered") == "already: rendered"
    assert report.render_evidence({"a": "b"}) == "a: b"
    assert report.render_evidence([("a", "b")]) == "a: b"
    assert report.render_evidence(report.Evidence(a="b")) == "a: b"


def test_with_evidence_without_evidence_returns_the_message_unchanged():
    # Degrades cleanly: a refusal that inferred nothing must not grow an
    # empty "()" tail.
    assert report.with_evidence("plain message") == "plain message"
    assert report.with_evidence("plain message", None) == "plain message"
    assert report.with_evidence("plain message", report.Evidence()) == "plain message"
    assert report.with_evidence("plain message", {}) == "plain message"
    assert report.with_evidence("plain message", "  ") == "plain message"


def test_with_evidence_accepts_a_prerendered_string():
    # step1_prerequisites.py's session guard returns an evidence *string*;
    # §6 generalises that shape rather than replacing it.
    hazard = "controlling terminal: /dev/pts/1; sshd ancestor: no"
    assert report.with_evidence("refused", hazard) == f"refused ({hazard})"


def test_evidence_values_are_scrubbed_of_a_secret_this_process_holds(monkeypatch):
    # An evidence value is as likely to come from installer.conf or the
    # environment as from a literal, and the message it lands in is printed
    # to stderr and written to the transcript (doc 08 section 4.2).
    monkeypatch.setenv("MQTT_PASSWORD", "hunter2")
    evidence = report.Evidence(password="hunter2")
    assert "hunter2" not in evidence.render()
    assert "<redacted>" in evidence.render()


def test_evidence_values_are_scrubbed_of_a_key_assignment_never_held(monkeypatch):
    monkeypatch.delenv("NGC_API_KEY", raising=False)
    rendered = report.Evidence(source="NGC_API_KEY=from-a-file").render()
    assert "from-a-file" not in rendered
    assert "<redacted>" in rendered


def test_evidence_values_are_stripped_of_control_characters():
    rendered = report.Evidence(probe="\033[31mred\033[0m\x07").render()
    assert "\033" not in rendered
    assert "\x07" not in rendered
    assert "probe: red" in rendered


def test_evidence_value_that_cleans_away_to_nothing_reads_as_unknown():
    assert report.Evidence(probe="\x07\x00").render() == "probe: unknown"


def test_refusal_message_does_not_leak_a_secret_through_its_evidence(monkeypatch):
    # The exact review repro: the message app.py prints must not carry the
    # plaintext an inference happened to be drawn from.
    monkeypatch.setenv("MQTT_PASSWORD", "hunter2")
    result = report.refusal(
        "broker rejected the credentials",
        evidence=report.Evidence(password="hunter2"),
    )
    assert "hunter2" not in result.message
    assert result.message == "broker rejected the credentials (password: <redacted>)"


def test_failure_message_does_not_leak_a_secret_through_its_evidence(monkeypatch):
    monkeypatch.setenv("NGC_API_KEY", "s3cr3t-value")
    result = report.failure(
        "download refused", evidence={"key": "s3cr3t-value"}, show=False
    )
    assert "s3cr3t-value" not in result.message
    assert "<redacted>" in result.message


def test_render_evidence_scrubs_a_prerendered_string_too(monkeypatch):
    # The one evidence shape that never passes through Evidence.add.
    monkeypatch.setenv("MQTT_PASSWORD", "hunter2")
    rendered = report.render_evidence("password: hunter2; broker: localhost")
    assert "hunter2" not in rendered
    assert "broker: localhost" in rendered


def test_evidence_len_and_render_agree_when_a_label_is_blank():
    evidence = report.Evidence().add("  ", "dropped").add("kept", "value")
    assert len(evidence) == 1
    assert evidence.render() == "kept: value"


# ---------------------------------------------------------------------------
# doc 08 §6.1 -- FailureContext.from_completed
# ---------------------------------------------------------------------------


def _completed(args, returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_from_completed_takes_command_exit_code_and_output():
    proc = _completed(["apt-get", "install", "-y", "deepstream-9.1"], 100, "a\n", "b\n")
    ctx = report.FailureContext.from_completed(proc, step="Step 2", phase="2/4 apt")

    assert ctx.step == "Step 2"
    assert ctx.phase == "2/4 apt"
    assert ctx.command == ["apt-get", "install", "-y", "deepstream-9.1"]
    assert ctx.exit_code == 100
    assert "a" in ctx.output and "b" in ctx.output


def test_from_completed_puts_stderr_after_stdout_so_the_tail_favours_it():
    proc = _completed(["x"], 1, stdout="out line\n", stderr="err line\n")
    ctx = report.FailureContext.from_completed(proc)
    assert ctx.output.index("out line") < ctx.output.index("err line")


def test_from_completed_handles_bytes_output():
    proc = _completed(["x"], 1, stdout=b"binary \xff out\n", stderr=b"")
    block = report.render_failure_context(report.FailureContext.from_completed(proc))
    assert "binary" in block


def test_from_completed_handles_no_captured_output():
    proc = _completed(["x"], 1, stdout=None, stderr=None)
    ctx = report.FailureContext.from_completed(proc)
    assert ctx.output is None
    assert "(none captured)" in report.render_failure_context(ctx)


def test_from_completed_on_a_duck_typed_result():
    fake = types.SimpleNamespace(
        args="/opt/run.sh --silent", returncode=9, stdout="boom\n", stderr=""
    )
    ctx = report.FailureContext.from_completed(fake)
    assert ctx.command == "/opt/run.sh --silent"
    assert ctx.exit_code == 9


# ---------------------------------------------------------------------------
# doc 08 §6.1 -- render_failure_context
# ---------------------------------------------------------------------------


def test_failure_block_names_every_required_field(tmp_path):
    ctx = report.FailureContext(
        step="Step 1 of 7: Prerequisites",
        phase="phase 3/5: NVIDIA driver",
        command=["/opt/NVIDIA.run", "--silent"],
        exit_code=1,
        output="first\nsecond\nthird\n",
        transcript=tmp_path / "install-20260101-000000.log",
    )
    block = report.render_failure_context(ctx)

    assert "INSTALL FAILED" in block
    assert "Step 1 of 7: Prerequisites" in block
    assert "phase 3/5: NVIDIA driver" in block
    assert "/opt/NVIDIA.run --silent" in block
    assert "Exit code:  1" in block
    assert "third" in block
    assert str(tmp_path / "install-20260101-000000.log") in block


def test_failure_block_keeps_only_the_last_twenty_lines():
    output = "\n".join(f"line {n}" for n in range(1, 51))
    ctx = report.FailureContext(command=["x"], exit_code=1, output=output)
    block = report.render_failure_context(ctx)

    assert "line 50" in block
    assert "line 31" in block
    assert "line 30" not in block
    assert "line 1\n" not in block
    assert "Last 20 lines of output (50 total):" in block


def test_render_failure_context_takes_no_tail_override():
    # Section 6.1 fixes the number at 20 and nothing asks for another, so
    # the parameter is gone rather than left as untested surface.
    with pytest.raises(TypeError):
        report.render_failure_context(report.FailureContext(output="x"), tail=3)
    assert report.FAILURE_TAIL_LINES == 20


def test_failure_block_shows_short_output_whole():
    ctx = report.FailureContext(output="only line\n")
    block = report.render_failure_context(ctx)
    assert "Output (1 line):" in block
    assert "only line" in block


def test_failure_block_drops_the_trailing_blank_line_of_captured_output():
    # A command's output ends in a newline; that must not spend one of the
    # 20 tail slots on nothing.
    output = "\n".join(f"line {n}" for n in range(1, 25)) + "\n"
    block = report.render_failure_context(report.FailureContext(output=output))
    assert "Last 20 lines of output (24 total):" in block
    assert "line 24" in block
    assert "line 5" in block
    assert "line 4" not in block


def test_failure_block_accepts_output_as_a_sequence_of_lines():
    ctx = report.FailureContext(output=["alpha", "beta"])
    block = report.render_failure_context(ctx)
    assert "alpha" in block and "beta" in block


def test_failure_block_quotes_a_command_so_it_can_be_pasted_back():
    ctx = report.FailureContext(command=["bash", "-c", "echo hello world"])
    block = report.render_failure_context(ctx)
    assert "bash -c 'echo hello world'" in block


def test_failure_block_accepts_a_command_string_verbatim():
    ctx = report.FailureContext(command="apt-get install -y deepstream-9.1")
    assert "apt-get install -y deepstream-9.1" in report.render_failure_context(ctx)


def test_failure_block_with_nothing_recorded_still_renders_coherently():
    block = report.render_failure_context(report.FailureContext())
    assert "INSTALL FAILED" in block
    assert "Step:       (unknown)" in block
    assert "Command:    (not recorded)" in block
    assert "Exit code:  (not recorded)" in block
    assert "Output:     (none captured)" in block
    assert "Transcript: (no transcript open)" in block
    # The frame is intact: top border, header border, bottom border.
    assert block.startswith("+-")
    assert block.rstrip().endswith("-+")


def test_render_failure_context_accepts_no_context_at_all():
    assert "INSTALL FAILED" in report.render_failure_context()
    assert "INSTALL FAILED" in report.render_failure_context(None)


def test_failure_block_omits_the_phase_line_when_there_is_no_phase():
    # A step that declared no phases has no phase to name; an empty
    # "Phase:" line would be noise, unlike a missing exit code.
    block = report.render_failure_context(report.FailureContext(step="Step 4"))
    assert "Phase:" not in block


def test_failure_block_exit_code_zero_is_recorded_not_treated_as_missing():
    block = report.render_failure_context(report.FailureContext(exit_code=0))
    assert "Exit code:  0" in block


def test_failure_block_falls_back_to_the_open_transcript(tmp_path):
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    block = report.render_failure_context(report.FailureContext(exit_code=1))
    assert str(run_file) in block


def test_failure_block_strips_control_characters_from_captured_output():
    ctx = report.FailureContext(output="\033[31mred\033[0m\nplain\x07\n")
    block = report.render_failure_context(ctx)
    assert "\033" not in block
    assert "\x07" not in block
    assert "red" in block


def test_failure_block_keeps_only_the_survivor_of_a_carriage_return_redraw():
    ctx = report.FailureContext(output="10%\r50%\r100%\n")
    block = report.render_failure_context(ctx)
    assert "\r" not in block
    assert "100%" in block
    # The overwritten frames are gone, not merely stripped of their CRs:
    # str.splitlines() splits on \r itself, so a split that used it would
    # keep all three as separate lines and still satisfy the two asserts
    # above.
    assert "10%" not in block
    assert "50%" not in block
    assert "Output (1 line):" in block


def test_failure_block_does_not_spend_its_tail_on_progress_redraw_frames():
    # doc 08 section 2 events 2 and 3: curl, wget and dpkg redraw a single
    # line hundreds of times. Measured against a 101-frame bar, a tail built
    # on str.splitlines() was nineteen percentages and one error line; the
    # block exists to show the error, so that defeated it for its motivating
    # case.
    bar = "".join(f"\r{pct}% [{'#' * (pct // 5)}]" for pct in range(101))
    output = (
        bar
        + "\ncurl: (22) The requested URL returned error: 404\n"
        + "make: *** [download] Error 22\n"
    )
    block = report.render_failure_context(
        report.FailureContext(command=["curl", "-fL", "https://x"], output=output)
    )

    assert "Output (3 lines):" in block
    assert "curl: (22) The requested URL returned error: 404" in block
    assert "make: *** [download] Error 22" in block
    # Exactly one frame survives, and it is the last one drawn.
    frames = [line for line in block.splitlines() if line.strip().endswith("]")]
    assert frames == ["      100% [####################]"]
    for overwritten in ("0%", "37%", "50%", "99%"):
        assert f"\n      {overwritten} [" not in block


def test_failure_block_strips_control_characters_from_the_command():
    # logs' ANSI strip does not touch a CR (doc 08 section 12.2 defect 2),
    # so an unsanitised command line would clobber the frame it sits in and
    # reach the transcript with its escapes intact.
    ctx = report.FailureContext(command="echo \033[31mred\033[0m\rGOTCHA")
    block = report.render_failure_context(ctx)
    assert "\033" not in block
    assert "\r" not in block
    assert "Command:    GOTCHA" in block


def test_failure_block_strips_control_characters_from_an_argv_command():
    ctx = report.FailureContext(command=["/opt/run.sh", "\033[1m--silent\033[0m"])
    block = report.render_failure_context(ctx)
    assert "\033" not in block
    assert "/opt/run.sh --silent" in block


def test_failure_block_redacts_a_secret_in_captured_output(monkeypatch):
    monkeypatch.setenv("NGC_API_KEY", "s3cr3t-value")
    ctx = report.FailureContext(output="curl failed with key s3cr3t-value\n")
    block = report.render_failure_context(ctx)
    assert "s3cr3t-value" not in block
    assert "<redacted>" in block


def test_failure_block_redacts_a_secret_in_the_failed_command(monkeypatch):
    monkeypatch.setenv("MQTT_PASSWORD", "hunter2")
    ctx = report.FailureContext(command=["mosquitto_pub", "-P", "hunter2"])
    block = report.render_failure_context(ctx)
    assert "hunter2" not in block
    assert "<redacted>" in block


def test_failure_block_redacts_a_key_assignment_it_never_held(monkeypatch):
    monkeypatch.delenv("NGC_API_KEY", raising=False)
    ctx = report.FailureContext(output="env: NGC_API_KEY=from-a-file\n")
    block = report.render_failure_context(ctx)
    assert "from-a-file" not in block
    assert "<redacted>" in block


# ---------------------------------------------------------------------------
# doc 08 §6.1 -- show_failure_context
# ---------------------------------------------------------------------------


def test_show_failure_context_logs_at_error_level(capsys):
    report.show_failure_context(
        report.FailureContext(step="Step 2", command=["x"], exit_code=2)
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[error]" in captured.err
    assert "INSTALL FAILED" in captured.err


def test_show_failure_context_reaches_the_transcript(tmp_path):
    run_file = logs.open_transcript(log_dir=tmp_path / "logs")
    report.show_failure_context(
        report.FailureContext(step="Step 2", command=["apt-get", "install"], exit_code=100)
    )
    contents = run_file.read_text(encoding="utf-8")
    assert "INSTALL FAILED" in contents
    assert "apt-get install" in contents
    assert "Exit code:  100" in contents


def test_show_failure_context_with_nothing_recorded_still_prints(capsys):
    report.show_failure_context()
    assert "INSTALL FAILED" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Result constructors
# ---------------------------------------------------------------------------


def test_refusal_is_user_action_required_with_its_evidence():
    result = report.refusal(
        "the driver installer must stop the desktop session",
        evidence=report.Evidence(controlling_terminal="/dev/pts/1"),
        user_actions=[UserAction(text="Switch to a virtual console")],
    )
    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert result.message == (
        "the driver installer must stop the desktop session "
        "(controlling terminal: /dev/pts/1)"
    )
    assert len(result.user_actions) == 1
    assert result.user_actions[0].text == "Switch to a virtual console"


def test_refusal_without_evidence_keeps_its_message_verbatim():
    result = report.refusal("nothing was inferred here")
    assert result.message == "nothing was inferred here"
    assert result.user_actions == []


def test_failure_is_failed_and_prints_its_context_block(capsys):
    proc = _completed(["apt-get", "install"], 100, stderr="E: broken packages\n")
    result = report.failure(
        "the DeepStream package transaction failed",
        context=report.FailureContext.from_completed(proc, step="Step 2"),
    )
    captured = capsys.readouterr()

    assert result.status is StepStatus.FAILED
    assert result.message == "the DeepStream package transaction failed"
    assert "INSTALL FAILED" in captured.err
    assert "E: broken packages" in captured.err
    assert "Exit code:  100" in captured.err


def test_failure_attaches_its_context_to_the_result():
    ctx = report.FailureContext(step="Step 3", exit_code=1)
    result = report.failure("boom", context=ctx, show=False)
    assert getattr(result, report.FAILURE_CONTEXT_ATTR) is ctx


def test_failure_show_false_prints_nothing(capsys):
    report.failure("boom", context=report.FailureContext(exit_code=1), show=False)
    assert capsys.readouterr().err == ""


def test_failure_without_a_context_prints_nothing_and_still_returns_failed(capsys):
    result = report.failure("boom")
    assert result.status is StepStatus.FAILED
    assert capsys.readouterr().err == ""
    assert getattr(result, report.FAILURE_CONTEXT_ATTR) is None


def test_failure_message_carries_its_evidence_too():
    result = report.failure(
        "the driver runfile exited nonzero",
        evidence=report.Evidence(kernel="6.8.0-60", dkms="absent"),
        show=False,
    )
    assert result.message == (
        "the driver runfile exited nonzero (kernel: 6.8.0-60; dkms: absent)"
    )


def test_result_constructors_do_not_share_a_mutable_user_actions_default():
    first = report.refusal("a")
    second = report.refusal("b")
    first.user_actions.append(UserAction(text="x"))
    assert second.user_actions == []


def test_output_lines_keeps_crlf_terminated_lines_whole():
    # The switch away from str.splitlines() (so that sanitise can still see
    # a redraw sequence) leaves a CRLF line ending as a trailing "\r", and
    # sanitise keeps only what follows the last carriage return. Without
    # stripping that one terminator, every CRLF line loses its content
    # rather than its line ending, and a block of real apt errors renders
    # as "(none captured)".
    assert report._output_lines("a\r\nb\r\n") == ["a", "b"]
    assert report._output_lines("E: one\r\nE: two\r\n") == ["E: one", "E: two"]

    # Only the terminator, and only one: a genuine redraw separates its
    # frames with carriage returns *inside* the line, which must still
    # collapse to the last frame.
    assert report._output_lines("  0%\r 50%\r100% [####]\n") == ["100% [####]"]
    assert report._output_lines("a\rb\rc") == ["c"]

    assert report._output_lines("a\nb\n") == ["a", "b"]
    assert report._output_lines("") == []


def test_failure_block_renders_crlf_errors_rather_than_none_captured():
    block = report.render_failure_context(
        report.FailureContext(
            step="Prerequisites",
            command=["apt-get", "install", "tensorrt"],
            exit_code=100,
            output="E: Sub-process returned an error code\r\nE: Unmet dependencies\r\n",
        )
    )
    assert "(none captured)" not in block
    assert "E: Sub-process returned an error code" in block
    assert "E: Unmet dependencies" in block


def test_render_command_reports_an_argv_that_cleans_away_as_not_recorded():
    # Quoting an argv whose every element sanitises to nothing yields a row
    # of '' that reads as though the command really was an empty string.
    assert report._render_command(["\x1b[0m"]) == "(not recorded)"
    # A genuine empty argument beside a real one still renders.
    assert report._render_command(["echo", ""]) == "echo ''"
