"""Tests for `mv3dt_installer.app` (doc 00 §3, §12.3).

Run from installer/: `python3 -m pytest tests/test_app.py -v`

Never touches the real `/var/lib/mv3dt-installer/state.json`,
`/var/lib/mv3dt-installer/logs/`, or `/opt/mv3dt` -- every test passes
`state_path=` (a test-only override on `app.main()`, see its docstring) and
`--install-dir`/`--log-dir` pointing under `tmp_path`. `--status` is the one
flow that deliberately runs before `privilege.require_root()` (see
`app.main()`'s docstring for why); every other flow in these tests
monkeypatches `privilege.require_root` to a no-op, since these tests exist
to exercise app.py's own logic, not re-prove doc 00 §9.1's "must run as
root" requirement (a dedicated test below does confirm that gate exists).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mv3dt_installer import __version__  # noqa: E402
from mv3dt_installer import app  # noqa: E402
from mv3dt_installer import config as config_mod  # noqa: E402
from mv3dt_installer.privilege import InvokingUser  # noqa: E402
from mv3dt_installer.state import STEP_IDS, StateMachine  # noqa: E402
from mv3dt_installer.steps import (  # noqa: E402
    StepResult,
    StepStatus,
    UserAction,
)


# ---------------------------------------------------------------------------
# Test double satisfying the `Step` protocol (doc 00 §12.1)
# ---------------------------------------------------------------------------


class _DummyStep:
    def __init__(
        self,
        step_id: str,
        title: str,
        order: int,
        *,
        preflight: StepResult | None = None,
        run: StepResult | None = None,
        verify: StepResult | None = None,
    ) -> None:
        self.id = step_id
        self.title = title
        self.order = order
        self._preflight_result = preflight or StepResult(status=StepStatus.COMPLETE)
        self._run_result = run or StepResult(status=StepStatus.COMPLETE)
        self._verify_result = verify or StepResult(status=StepStatus.COMPLETE)
        self.report_calls = 0

    def preflight(self, ctx):
        return self._preflight_result

    def run(self, ctx):
        return self._run_result

    def verify(self, ctx):
        return self._verify_result

    def report(self, ctx):
        self.report_calls += 1


def _minimal_ctx(
    tmp_path: Path,
    *,
    webapp_integration: str = "off",
    remote_supervision: str = "off",
):
    """A `Context` + `Config` pair for direct `_dispatch()` unit tests,
    never touching a real install dir or a real invoking user."""
    install_dir = tmp_path / "install"
    install_dir.mkdir(exist_ok=True)
    cfg = config_mod.Config(
        install_dir=install_dir,
        remote_supervision=remote_supervision,
        webapp_integration=webapp_integration,
        values={},
    )
    user = InvokingUser(
        name="tester", home=tmp_path, uid=os.getuid(), gid=os.getgid()
    )
    ctx = app.build_context(cfg, user)
    return ctx, cfg


# ---------------------------------------------------------------------------
# doc 00 §3.3 -- CLI flag parsing
# ---------------------------------------------------------------------------


def test_parse_args_defaults():
    args = app.parse_args([])
    assert args.install_dir is None
    assert args.resume is False
    assert args.status is False
    assert args.reset_state is False
    assert args.reset_step is None
    assert args.non_interactive is False
    assert args.no_pause is False
    assert args.log_dir is None


def test_parse_args_install_dir():
    args = app.parse_args(["--install-dir", "/opt/custom"])
    assert args.install_dir == "/opt/custom"


def test_parse_args_resume():
    assert app.parse_args(["--resume"]).resume is True


def test_parse_args_status():
    assert app.parse_args(["--status"]).status is True


def test_parse_args_reset_state():
    assert app.parse_args(["--reset-state"]).reset_state is True


def test_parse_args_reset_step():
    assert app.parse_args(["--reset-step", "3"]).reset_step == 3


def test_parse_args_non_interactive():
    assert app.parse_args(["--non-interactive"]).non_interactive is True


def test_parse_args_no_pause():
    assert app.parse_args(["--no-pause"]).no_pause is True


def test_parse_args_log_dir():
    assert app.parse_args(["--log-dir", "/tmp/mv3dt-logs"]).log_dir == "/tmp/mv3dt-logs"


def test_parse_args_version_prints_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        app.parse_args(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_parse_args_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        app.parse_args(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "mv3dt-installer" in captured.out


# ---------------------------------------------------------------------------
# --status (doc 00 §3.3) -- smoke test case, never requires root
# ---------------------------------------------------------------------------


def test_status_fresh_state_prints_and_exits_zero(tmp_path, capsys):
    state_path = tmp_path / "var" / "state.json"

    rc = app.main(["--status"], state_path=state_path)

    assert rc == 0
    captured = capsys.readouterr()
    assert "install_dir:" in captured.out
    assert "all complete: False" in captured.out
    for step_id in STEP_IDS:
        assert f"{step_id}: PENDING" in captured.out
    # --status performs no writes -- a fresh/missing state.json is never
    # created just by inspecting it.
    assert not state_path.exists()


def test_status_does_not_require_root(tmp_path):
    """Confirms the deliberate design choice: --status runs before
    privilege.require_root(), so it works for a non-root test process
    without any monkeypatching."""
    state_path = tmp_path / "var" / "state.json"
    assert app.main(["--status"], state_path=state_path) == 0


def test_main_requires_root_for_non_status_flow(tmp_path):
    """Every flow other than --status runs after privilege.require_root()
    (doc 00 §9.1). This process is not root, so main() must die() (exit 1)
    rather than silently proceeding."""
    state_path = tmp_path / "state.json"
    with pytest.raises(SystemExit) as exc_info:
        app.main(["--non-interactive"], state_path=state_path)
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# --reset-state (doc 00 §3.3)
# ---------------------------------------------------------------------------


def test_reset_state_wipes_and_reinitializes(tmp_path, monkeypatch):
    monkeypatch.setattr(app.privilege, "require_root", lambda: None)
    monkeypatch.setattr(app, "STEP_REGISTRY", [])

    state_path = tmp_path / "var" / "state.json"
    sm = StateMachine(path=state_path)
    sm.mark_complete("step1_prerequisites")
    assert sm.load().steps["step1_prerequisites"].status == StepStatus.COMPLETE

    install_dir = tmp_path / "install"
    log_dir = tmp_path / "logs"
    rc = app.main(
        [
            "--reset-state",
            "--non-interactive",
            "--install-dir",
            str(install_dir),
            "--log-dir",
            str(log_dir),
        ],
        state_path=state_path,
    )

    assert rc == 0
    after = sm.load()
    assert after.steps["step1_prerequisites"].status == StepStatus.PENDING


# ---------------------------------------------------------------------------
# --reset-step (doc 00 §3.3)
# ---------------------------------------------------------------------------


def test_reset_step_no_match_prints_message_and_exits_cleanly(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(app.privilege, "require_root", lambda: None)
    monkeypatch.setattr(app, "STEP_REGISTRY", [])

    state_path = tmp_path / "var" / "state.json"
    rc = app.main(["--reset-step", "3"], state_path=state_path)

    assert rc == 0
    captured = capsys.readouterr()
    assert "no step registered with order 3" in captured.err


def test_reset_step_match_clears_status(tmp_path, monkeypatch):
    monkeypatch.setattr(app.privilege, "require_root", lambda: None)

    state_path = tmp_path / "var" / "state.json"
    sm = StateMachine(path=state_path)
    sm.mark_complete("step2_deepstream_sdk")

    step = _DummyStep("step2_deepstream_sdk", "DeepStream SDK", 2)
    monkeypatch.setattr(app, "STEP_REGISTRY", [step])

    rc = app.main(["--reset-step", "2"], state_path=state_path)

    assert rc == 0
    assert sm.load().steps["step2_deepstream_sdk"].status == StepStatus.PENDING


# ---------------------------------------------------------------------------
# Ordering fix: state loaded (and StateMachine passed to config.load())
# before config resolution, end-to-end through app.main().
# ---------------------------------------------------------------------------


def test_main_config_precedence_reads_back_from_state_json(tmp_path, monkeypatch):
    monkeypatch.setattr(app.privilege, "require_root", lambda: None)
    monkeypatch.setattr(app, "STEP_REGISTRY", [])

    state_path = tmp_path / "var" / "state.json"
    log_dir = tmp_path / "logs"
    chosen_install_dir = tmp_path / "chosen-install-dir"

    rc1 = app.main(
        [
            "--install-dir",
            str(chosen_install_dir),
            "--non-interactive",
            "--log-dir",
            str(log_dir),
        ],
        state_path=state_path,
    )
    assert rc1 == 0

    sm = StateMachine(path=state_path)
    assert sm.load().install_dir == str(chosen_install_dir)

    # Second run: no --install-dir override at all. If config.load() had
    # NOT received the real, already-populated StateMachine (the ordering
    # bug the doc's five-line §3.2 summary would produce if taken
    # literally -- config loaded before state), this would fall through to
    # the hardcoded /opt/mv3dt default instead of state.json's recorded
    # value.
    rc2 = app.main(
        ["--non-interactive", "--log-dir", str(log_dir)],
        state_path=state_path,
    )
    assert rc2 == 0
    assert sm.load().install_dir == str(chosen_install_dir)


# ---------------------------------------------------------------------------
# doc 00 §12.3 -- Context construction
# ---------------------------------------------------------------------------


def test_build_context_bindings_work(tmp_path, capsys):
    ctx, cfg = _minimal_ctx(tmp_path)

    assert ctx.install_dir == cfg.install_dir
    assert ctx.conf is cfg.values
    assert ctx.user.name == "tester"

    ctx.report_installed("foo", "1.2.3")
    ctx.report_already_installed("bar", "4.5.6")
    captured = capsys.readouterr()
    assert "installed foo version 1.2.3" in captured.err
    assert "already installed bar version 4.5.6" in captured.err

    assert ctx.verify_pinned("label", "1.0", "1.0") is True
    assert ctx.verify_pinned("label", "1.0", "2.0") is False

    asset = ctx.asset_path("scripts", "example.sh")
    assert asset.parts[-2:] == ("scripts", "example.sh")

    # ngc handle bound to this install_dir; no key ever stored here.
    assert ctx.ngc.load_key() is None
    assert ctx.ngc.manual_fallback is True

    # webapp handle bound to gate "off" -> never enabled regardless of
    # whatever credentials might exist.
    assert ctx.webapp.enabled() is False

    assert ctx.reboot.request() is StepStatus.REBOOT_REQUIRED

    result = ctx.run_root(sys.executable, "-c", "pass")
    assert result.returncode == 0


def test_context_webapp_enabled_true_with_gate_on_and_stored_credentials(tmp_path):
    from mv3dt_installer import webapp as webapp_mod

    ctx, _cfg = _minimal_ctx(tmp_path, webapp_integration="on")
    webapp_mod.store_credentials(
        webapp_mod.Credentials(api_key="k", endpoint="https://example.test"),
        ctx.install_dir,
    )

    assert ctx.webapp.enabled() is True
    assert ctx.webapp.load_credentials().api_key == "k"


def test_context_run_as_user_delegates_to_privilege(tmp_path, monkeypatch):
    calls = []

    def fake_run_as_user(*args, **kwargs):
        calls.append((args, kwargs))
        return "sentinel"

    monkeypatch.setattr(app.privilege, "run_as_user", fake_run_as_user)
    ctx, _cfg = _minimal_ctx(tmp_path)

    result = ctx.run_as_user("echo", "hi", check=True)

    assert result == "sentinel"
    assert calls == [(("echo", "hi"), {"check": True})]


# ---------------------------------------------------------------------------
# doc 00 §3.2 -- dispatch loop (direct _dispatch() unit tests)
# ---------------------------------------------------------------------------


def test_dispatch_empty_registry_completes_trivially(tmp_path, monkeypatch):
    sm = StateMachine(path=tmp_path / "state.json")
    ctx, cfg = _minimal_ctx(tmp_path)
    monkeypatch.setattr(app, "STEP_REGISTRY", [])

    assert app._dispatch(sm, ctx, cfg) == 0


def test_dispatch_completes_step_and_marks_state(tmp_path, monkeypatch):
    sm = StateMachine(path=tmp_path / "state.json")
    ctx, cfg = _minimal_ctx(tmp_path)
    step = _DummyStep("step1_prerequisites", "Prerequisites", 1)
    monkeypatch.setattr(app, "STEP_REGISTRY", [step])

    rc = app._dispatch(sm, ctx, cfg)

    assert rc == 0
    assert step.report_calls == 1
    assert sm.status("step1_prerequisites") is StepStatus.COMPLETE


def test_dispatch_skips_already_complete_step(tmp_path, monkeypatch):
    sm = StateMachine(path=tmp_path / "state.json")
    sm.mark_complete("step1_prerequisites")
    ctx, cfg = _minimal_ctx(tmp_path)
    step = _DummyStep("step1_prerequisites", "Prerequisites", 1)
    monkeypatch.setattr(app, "STEP_REGISTRY", [step])

    rc = app._dispatch(sm, ctx, cfg)

    assert rc == 0
    assert step.report_calls == 0  # skipped, never re-run


def test_dispatch_halts_on_reboot_required(tmp_path, monkeypatch):
    sm = StateMachine(path=tmp_path / "state.json")
    ctx, cfg = _minimal_ctx(tmp_path)
    step = _DummyStep(
        "step1_prerequisites",
        "Prerequisites",
        1,
        verify=StepResult(status=StepStatus.REBOOT_REQUIRED, message="reboot pls"),
    )
    monkeypatch.setattr(app, "STEP_REGISTRY", [step])
    monkeypatch.setattr(app.reboot_mod, "current_boot_id", lambda: "boot-abc")

    rc = app._dispatch(sm, ctx, cfg)

    assert rc == 0
    pending = sm.load().reboot_pending
    assert pending is not None
    assert pending.requested_by == "step1_prerequisites"
    assert pending.boot_id_at_request == "boot-abc"
    # A step that only got as far as verify() before requesting a reboot
    # is not (yet) marked COMPLETE.
    assert sm.status("step1_prerequisites") is not StepStatus.COMPLETE


def test_dispatch_halts_on_user_action_required(tmp_path, monkeypatch, capsys):
    sm = StateMachine(path=tmp_path / "state.json")
    ctx, cfg = _minimal_ctx(tmp_path)
    action = UserAction(text="do the thing", command="do-it --now")
    step = _DummyStep(
        "step2_deepstream_sdk",
        "DeepStream SDK",
        2,
        verify=StepResult(
            status=StepStatus.USER_ACTION_REQUIRED,
            message="needs manual input",
            user_actions=[action],
        ),
    )
    monkeypatch.setattr(app, "STEP_REGISTRY", [step])

    rc = app._dispatch(sm, ctx, cfg)

    assert rc == 0
    captured = capsys.readouterr()
    assert "ACTION REQUIRED" in captured.err
    assert "do the thing" in captured.err
    assert "do-it --now" in captured.err
    assert "Then run the installer again to continue." in captured.err
    assert sm.status("step2_deepstream_sdk") is not StepStatus.COMPLETE


def test_dispatch_halts_on_failed(tmp_path, monkeypatch, capsys):
    sm = StateMachine(path=tmp_path / "state.json")
    ctx, cfg = _minimal_ctx(tmp_path)
    step = _DummyStep(
        "step1_prerequisites",
        "Prerequisites",
        1,
        run=StepResult(status=StepStatus.FAILED, message="boom"),
    )
    monkeypatch.setattr(app, "STEP_REGISTRY", [step])

    rc = app._dispatch(sm, ctx, cfg)

    assert rc == 1
    captured = capsys.readouterr()
    assert "boom" in captured.err
    assert sm.status("step1_prerequisites") is not StepStatus.COMPLETE


# ---------------------------------------------------------------------------
# doc 00 §3.4 -- opt-in step gates
# ---------------------------------------------------------------------------


def test_dispatch_gate_off_autocompletes_and_never_runs_lifecycle(tmp_path, monkeypatch):
    sm = StateMachine(path=tmp_path / "state.json")
    ctx, cfg = _minimal_ctx(tmp_path)  # remote_supervision defaults to "off"
    step = _DummyStep("step6_remote_supervision", "Remote Supervision", 6)
    monkeypatch.setattr(app, "STEP_REGISTRY", [step])

    rc = app._dispatch(sm, ctx, cfg)

    assert rc == 0
    assert sm.status("step6_remote_supervision") is StepStatus.COMPLETE
    # Gate-off skip never invokes the lifecycle, so report() is never
    # called (mirrors the "already complete" skip discipline exactly).
    assert step.report_calls == 0


def test_dispatch_gate_on_runs_step7_normally(tmp_path, monkeypatch):
    sm = StateMachine(path=tmp_path / "state.json")
    ctx, cfg = _minimal_ctx(tmp_path, webapp_integration="on")
    step = _DummyStep("step7_webapp_integration", "Web-app Integration", 7)
    monkeypatch.setattr(app, "STEP_REGISTRY", [step])

    rc = app._dispatch(sm, ctx, cfg)

    assert rc == 0
    assert step.report_calls == 1
    assert sm.status("step7_webapp_integration") is StepStatus.COMPLETE


@pytest.mark.parametrize("gate_value", ["local", "remote"])
def test_dispatch_gate_three_valued_non_off_runs_step6_normally(
    tmp_path, monkeypatch, gate_value
):
    """Regression test: doc 00 §3.4's table gives Step 6's
    `MV3DT_REMOTE_SUPERVISION` gate three values (`off`/`local`/`remote`),
    unlike Step 7's binary `off`/`on`. An earlier version of `_dispatch`'s
    gate check tested `gate_value != "on"`, which incorrectly auto-skipped
    Step 6 for *both* `local` and `remote` (neither equals the literal
    string "on"). Only `"off"` may skip a gated step; every other value
    (however many a given gate defines) must run it normally."""
    sm = StateMachine(path=tmp_path / "state.json")
    ctx, cfg = _minimal_ctx(tmp_path, remote_supervision=gate_value)
    step = _DummyStep("step6_remote_supervision", "Remote Supervision", 6)
    monkeypatch.setattr(app, "STEP_REGISTRY", [step])

    rc = app._dispatch(sm, ctx, cfg)

    assert rc == 0
    assert step.report_calls == 1
    assert sm.status("step6_remote_supervision") is StepStatus.COMPLETE


def test_dispatch_gate_off_skips_step6_explicitly(tmp_path, monkeypatch):
    """Companion to the three-valued regression test above: "off" must
    still skip Step 6, exactly as it does for Step 7's binary gate."""
    sm = StateMachine(path=tmp_path / "state.json")
    ctx, cfg = _minimal_ctx(tmp_path, remote_supervision="off")
    step = _DummyStep("step6_remote_supervision", "Remote Supervision", 6)
    monkeypatch.setattr(app, "STEP_REGISTRY", [step])

    rc = app._dispatch(sm, ctx, cfg)

    assert rc == 0
    assert step.report_calls == 0
    assert sm.status("step6_remote_supervision") is StepStatus.COMPLETE


# ---------------------------------------------------------------------------
# doc 00 §7 -- reboot reconciliation wired into main()
# ---------------------------------------------------------------------------


def test_main_reboot_still_pending_blocks_dispatch(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(app.privilege, "require_root", lambda: None)
    monkeypatch.setattr(app, "STEP_REGISTRY", [])

    state_path = tmp_path / "state.json"
    sm = StateMachine(path=state_path)
    sm.set_reboot_pending("step1_prerequisites", boot_id="boot-xyz")
    monkeypatch.setattr(app.reboot_mod, "current_boot_id", lambda: "boot-xyz")

    install_dir = tmp_path / "install"
    log_dir = tmp_path / "logs"
    rc = app.main(
        [
            "--non-interactive",
            "--install-dir",
            str(install_dir),
            "--log-dir",
            str(log_dir),
        ],
        state_path=state_path,
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert "ACTION REQUIRED" in captured.err
    assert "reboot is required" in captured.err
    assert sm.load().reboot_pending is not None


def test_main_reboot_confirmed_advances_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(app.privilege, "require_root", lambda: None)
    monkeypatch.setattr(app, "STEP_REGISTRY", [])

    state_path = tmp_path / "state.json"
    sm = StateMachine(path=state_path)
    sm.set_reboot_pending("step1_prerequisites", boot_id="boot-old")
    monkeypatch.setattr(app.reboot_mod, "current_boot_id", lambda: "boot-new")

    install_dir = tmp_path / "install"
    log_dir = tmp_path / "logs"
    rc = app.main(
        [
            "--non-interactive",
            "--install-dir",
            str(install_dir),
            "--log-dir",
            str(log_dir),
        ],
        state_path=state_path,
    )

    assert rc == 0
    after = sm.load()
    assert after.reboot_pending is None
    assert after.steps["step1_prerequisites"].status is StepStatus.COMPLETE
