"""Tests for mv3dt_installer.steps.step3_amc_launcher
(STEP-3-AMC-LAUNCHER.md).

Run from installer/: `python3 -m pytest tests/test_step3_amc_launcher.py -v`

No test here shells out for real, opens a browser, or touches docker/git --
every `ctx.run_root`/`ctx.run_as_user` call is served by a `ScriptedRunner`
fake (mirroring `test_step2_deepstream_sdk.py`'s convention), and every
subprocess/browser-launch seam (`popen`, `which`, `mkdtemp`, `prompt`) in
the section 5 hold-until-closed logic is injected.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mv3dt_installer import app  # noqa: E402
from mv3dt_installer import logs, report  # noqa: E402
from mv3dt_installer.steps import STEP_REGISTRY, StepStatus  # noqa: E402
from mv3dt_installer.steps import step2_deepstream_sdk as step2  # noqa: E402
from mv3dt_installer.steps import step3_amc_launcher as step3  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_transcript_state():
    logs._transcript_path = None
    yield
    logs._transcript_path = None


@pytest.fixture(autouse=True)
def _force_no_colour(monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)


@pytest.fixture(autouse=True)
def _no_real_repo_root(monkeypatch):
    """Every isolation-guard test drives `check_repo_isolation` directly
    against an injected `repo_root`-like value; tests that don't care about
    the guard should not accidentally trip over *this* checkout's real
    `.git` directory."""
    monkeypatch.setattr(step3, "repo_root", lambda: None)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class ScriptedRunner:
    """Stand-in for `ctx.run_root`/`ctx.run_as_user`. See
    test_step2_deepstream_sdk.py's identical fake for the rationale."""

    def __init__(self, *, default_returncode: int = 0, default_stdout: str = "", default_stderr: str = ""):
        self.calls: list[tuple] = []
        self._rules: list[tuple] = []
        self.default_returncode = default_returncode
        self.default_stdout = default_stdout
        self.default_stderr = default_stderr

    def when(self, matcher, *, returncode=0, stdout="", stderr="", side_effect=None):
        self._rules.append((matcher, returncode, stdout, stderr, side_effect))
        return self

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        for matcher, returncode, stdout, stderr, side_effect in reversed(self._rules):
            if matcher(args):
                if side_effect is not None:
                    side_effect()
                return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)
        return subprocess.CompletedProcess(
            list(args), self.default_returncode, self.default_stdout, self.default_stderr
        )

    def called_with_prefix(self, *prefix) -> bool:
        return any(tuple(call[: len(prefix)]) == prefix for call in self.calls)


class FakeUser:
    name = "op"
    uid = 1000
    gid = 1000

    def __init__(self, home: pathlib.Path):
        self.home = home


class FakeNgc:
    def __init__(self, key="a-fake-ngc-key"):
        self._key = key

    def load_key(self):
        return self._key


class FakeContext:
    def __init__(
        self,
        tmp_path,
        *,
        conf=None,
        runner_root=None,
        runner_user=None,
        non_interactive=True,
    ):
        self.install_dir = tmp_path / "mv3dt"
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self.conf = conf if conf is not None else {step2.CONF_METHOD_KEY: "deb"}
        self.user = FakeUser(home=tmp_path / "home" / "op")
        self.user.home.mkdir(parents=True, exist_ok=True)
        self.log = logs.log
        self.report_installed = report.report_installed
        self.report_already_installed = report.report_already_installed
        self.verify_pinned = report.verify_pinned
        self.ngc = FakeNgc()
        self.runner_root = runner_root if runner_root is not None else ScriptedRunner()
        self.runner_user = runner_user if runner_user is not None else ScriptedRunner()
        self.non_interactive = non_interactive

    def run_root(self, *args, **kwargs):
        return self.runner_root(*args, **kwargs)

    def run_as_user(self, *args, **kwargs):
        return self.runner_user(*args, **kwargs)


def _passing_runner() -> ScriptedRunner:
    """A runner that answers every docker/compose/nvidia-toolkit probe
    successfully."""
    runner = ScriptedRunner()
    runner.when(lambda a: a[:2] == ("docker", "info"), stdout="Runtimes: nvidia runc\n")
    return runner


# ---------------------------------------------------------------------------
# Module identity (doc 00 section 12.1)
# ---------------------------------------------------------------------------


def test_registers_itself_with_the_expected_identity():
    matches = [s for s in STEP_REGISTRY if s.id == "step3_amc_launcher"]
    assert len(matches) == 1
    step = matches[0]
    assert step.order == 3
    assert "AutoMagicCalib" in step.title


def test_registers_the_amc_subcommand():
    assert app.SUBCOMMAND_REGISTRY.get("amc") is step3.handle_amc_subcommand


# ---------------------------------------------------------------------------
# resolve_config() -- section 4.1
# ---------------------------------------------------------------------------


def test_resolve_config_defaults(tmp_path):
    ctx = FakeContext(tmp_path)
    cfg = step3.resolve_config(ctx)
    assert cfg.amc_root == ctx.user.home / "auto-magic-calib"
    assert cfg.host_ip == "127.0.0.1"
    assert cfg.ui_port == "5000"
    assert cfg.ms_port == "8000"
    assert cfg.ms_api_url == ""
    assert cfg.project_name == "default"
    assert cfg.nvidia_visible_devices == "all"


def test_resolve_config_reads_installer_conf_values(tmp_path):
    conf = {
        step2.CONF_METHOD_KEY: "deb",
        step3.CONF_AMC_ROOT_KEY: str(tmp_path / "custom-amc"),
        step3.CONF_HOST_IP_KEY: "10.0.0.5",
        step3.CONF_UI_PORT_KEY: "5050",
        step3.CONF_MS_PORT_KEY: "8080",
        step3.CONF_PROJECT_NAME_KEY: "north-lobby",
    }
    ctx = FakeContext(tmp_path, conf=conf)
    cfg = step3.resolve_config(ctx)
    assert cfg.amc_root == tmp_path / "custom-amc"
    assert cfg.host_ip == "10.0.0.5"
    assert cfg.ui_port == "5050"
    assert cfg.ms_port == "8080"
    assert cfg.project_name == "north-lobby"


def test_resolve_config_project_and_host_ip_overrides_win(tmp_path):
    conf = {
        step2.CONF_METHOD_KEY: "deb",
        step3.CONF_PROJECT_NAME_KEY: "from-conf",
        step3.CONF_HOST_IP_KEY: "10.0.0.5",
    }
    ctx = FakeContext(tmp_path, conf=conf)
    cfg = step3.resolve_config(ctx, project="from-flag", host_ip_override="192.168.1.1")
    assert cfg.project_name == "from-flag"
    assert cfg.host_ip == "192.168.1.1"


def test_resolve_config_auto_detects_host_ip_when_unset(tmp_path):
    runner = ScriptedRunner()
    runner.when(
        lambda a: a[:3] == ("ip", "route", "get"),
        stdout="1.1.1.1 via 10.0.0.1 dev eth0 src 10.0.0.42 uid 0\n",
    )
    ctx = FakeContext(tmp_path, runner_root=runner)
    cfg = step3.resolve_config(ctx)
    assert cfg.host_ip == "10.0.0.42"


def test_resolve_config_falls_back_to_localhost_when_detection_fails(tmp_path):
    runner = ScriptedRunner(default_returncode=1)
    ctx = FakeContext(tmp_path, runner_root=runner)
    cfg = step3.resolve_config(ctx)
    assert cfg.host_ip == "127.0.0.1"


def test_persist_config_seeds_missing_keys_only(tmp_path):
    ctx = FakeContext(tmp_path, conf={step2.CONF_METHOD_KEY: "deb", step3.CONF_HOST_IP_KEY: "10.0.0.9"})
    cfg = step3.AmcConfig(
        amc_root=tmp_path / "amc",
        host_ip="127.0.0.1",  # would differ from the already-persisted value
        ui_port="5000",
        ms_port="8000",
        ms_api_url="",
        project_name="default",
    )
    step3.persist_config(ctx, cfg)
    # Already present in ctx.conf -> left untouched, and never (re)written to
    # installer.conf by this call -- it was never there to begin with in this
    # hand-built FakeContext, exactly mirroring "a key already present is
    # read straight back" (config.py's own gate-seeding discipline).
    assert ctx.conf[step3.CONF_HOST_IP_KEY] == "10.0.0.9"
    assert ctx.conf[step3.CONF_AMC_ROOT_KEY] == str(tmp_path / "amc")  # newly seeded
    conf_text = (ctx.install_dir / "installer.conf").read_text(encoding="utf-8")
    assert f"AMC_ROOT={tmp_path / 'amc'}" in conf_text
    assert "HOST_IP=" not in conf_text


# ---------------------------------------------------------------------------
# check_repo_isolation() -- section 4 step 2
# ---------------------------------------------------------------------------


def test_repo_isolation_passes_when_no_repo_root(monkeypatch, tmp_path):
    monkeypatch.setattr(step3, "repo_root", lambda: None)
    assert step3.check_repo_isolation(tmp_path / "auto-magic-calib") is None


def test_repo_isolation_rejects_the_repo_root_itself(monkeypatch, tmp_path):
    monkeypatch.setattr(step3, "repo_root", lambda: tmp_path)
    message = step3.check_repo_isolation(tmp_path)
    assert message is not None
    assert "must not live under this repo" in message


def test_repo_isolation_rejects_a_child_of_the_repo_root(monkeypatch, tmp_path):
    monkeypatch.setattr(step3, "repo_root", lambda: tmp_path)
    message = step3.check_repo_isolation(tmp_path / "installer" / "auto-magic-calib")
    assert message is not None


def test_repo_isolation_allows_a_sibling_path(monkeypatch, tmp_path):
    monkeypatch.setattr(step3, "repo_root", lambda: tmp_path / "repo")
    assert step3.check_repo_isolation(tmp_path / "home" / "auto-magic-calib") is None


# ---------------------------------------------------------------------------
# locate_compose_dir() -- section 4 step 6 (search-order fallback)
# ---------------------------------------------------------------------------


def test_locate_compose_dir_prefers_the_monorepo_layout(tmp_path):
    amc_root = tmp_path / "amc"
    monorepo_compose = amc_root / "tools" / "auto-magic-calib" / "compose"
    monorepo_compose.mkdir(parents=True)
    # Also create a legacy compose/ to prove the monorepo path wins.
    (amc_root / "compose").mkdir(parents=True)
    assert step3.locate_compose_dir(amc_root) == monorepo_compose


def test_locate_compose_dir_falls_back_to_standalone_compose_dir(tmp_path):
    amc_root = tmp_path / "amc"
    fallback = amc_root / "compose"
    fallback.mkdir(parents=True)
    assert step3.locate_compose_dir(amc_root) == fallback


def test_locate_compose_dir_falls_back_to_repo_root_with_compose_yaml(tmp_path):
    amc_root = tmp_path / "amc"
    amc_root.mkdir(parents=True)
    (amc_root / "compose.yaml").write_text("services: {}\n")
    assert step3.locate_compose_dir(amc_root) == amc_root


def test_locate_compose_dir_falls_back_to_repo_root_with_docker_compose_yml(tmp_path):
    amc_root = tmp_path / "amc"
    amc_root.mkdir(parents=True)
    (amc_root / "docker-compose.yml").write_text("services: {}\n")
    assert step3.locate_compose_dir(amc_root) == amc_root


def test_locate_compose_dir_returns_none_when_layout_unrecognized(tmp_path):
    amc_root = tmp_path / "amc"
    amc_root.mkdir(parents=True)
    assert step3.locate_compose_dir(amc_root) is None


# ---------------------------------------------------------------------------
# check_env_drift() -- section 4.3
# ---------------------------------------------------------------------------


def test_env_drift_empty_when_env_example_missing(tmp_path):
    assert step3.check_env_drift(tmp_path) == []


def test_env_drift_reports_missing_keys(tmp_path):
    (tmp_path / ".env.example").write_text(
        "HOST_IP=\nPROJECT_DIR=\nMODEL_DIR=\nNVIDIA_VISIBLE_DEVICES=\n"
    )
    missing = step3.check_env_drift(tmp_path)
    assert set(missing) == {"AUTO_MAGIC_CALIB_MS_PORT", "AUTO_MAGIC_CALIB_UI_PORT"}


def test_env_drift_empty_when_all_keys_present(tmp_path):
    content = "\n".join(f"{key}=" for key in step3.ENV_KEYS) + "\n"
    (tmp_path / ".env.example").write_text(content)
    assert step3.check_env_drift(tmp_path) == []


# ---------------------------------------------------------------------------
# render_env() / write_env_atomic() -- section 4.2
# ---------------------------------------------------------------------------


def _cfg(tmp_path, **overrides) -> step3.AmcConfig:
    defaults = dict(
        amc_root=tmp_path / "amc",
        host_ip="127.0.0.1",
        ui_port="5000",
        ms_port="8000",
        ms_api_url="",
        project_name="default",
        nvidia_visible_devices="all",
    )
    defaults.update(overrides)
    return step3.AmcConfig(**defaults)


def test_render_env_contents(tmp_path):
    cfg = _cfg(tmp_path)
    content = step3.render_env(cfg)
    assert "HOST_IP=127.0.0.1" in content
    assert "AUTO_MAGIC_CALIB_MS_PORT=8000" in content
    assert "AUTO_MAGIC_CALIB_UI_PORT=5000" in content
    assert f"PROJECT_DIR={tmp_path / 'amc' / 'projects'}" in content
    assert f"MODEL_DIR={tmp_path / 'amc' / 'models'}" in content
    assert "NVIDIA_VISIBLE_DEVICES=all" in content
    assert "PROJECT_NAME=default" in content
    assert "AUTO_MAGIC_CALIB_MS_API_URL" not in content


def test_render_env_includes_optional_ms_api_url_when_set(tmp_path):
    cfg = _cfg(tmp_path, ms_api_url="http://127.0.0.1:8000/v1")
    content = step3.render_env(cfg)
    assert "AUTO_MAGIC_CALIB_MS_API_URL=http://127.0.0.1:8000/v1" in content


def test_write_env_atomic_reports_changed_on_first_write(tmp_path):
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    changed = step3.write_env_atomic(compose_dir, "HOST_IP=127.0.0.1\n")
    assert changed is True
    assert (compose_dir / ".env").read_text() == "HOST_IP=127.0.0.1\n"


def test_write_env_atomic_is_idempotent(tmp_path):
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    step3.write_env_atomic(compose_dir, "HOST_IP=127.0.0.1\n")
    changed = step3.write_env_atomic(compose_dir, "HOST_IP=127.0.0.1\n")
    assert changed is False


def test_write_env_atomic_reports_changed_on_content_change(tmp_path):
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    step3.write_env_atomic(compose_dir, "HOST_IP=127.0.0.1\n")
    changed = step3.write_env_atomic(compose_dir, "HOST_IP=10.0.0.1\n")
    assert changed is True
    assert (compose_dir / ".env").read_text() == "HOST_IP=10.0.0.1\n"


# ---------------------------------------------------------------------------
# clone_amc() -- section 4 step 3
# ---------------------------------------------------------------------------


def test_clone_amc_clones_when_missing(tmp_path):
    ctx = FakeContext(tmp_path)
    amc_root = tmp_path / "home" / "op" / "auto-magic-calib"
    cloned = step3.clone_amc(ctx, amc_root)
    assert cloned is True
    calls = ctx.runner_user.calls
    assert calls[0][:2] == ("git", "clone")
    assert calls[1][3:6] == ("sparse-checkout", "set", step3.AMC_SPARSE_PATH)
    assert calls[2][:4] == ("git", "-C", str(amc_root), "checkout")


def test_clone_amc_skips_when_already_present(tmp_path):
    ctx = FakeContext(tmp_path)
    amc_root = tmp_path / "home" / "op" / "auto-magic-calib"
    amc_root.mkdir(parents=True)
    cloned = step3.clone_amc(ctx, amc_root)
    assert cloned is False
    assert ctx.runner_user.calls == []


# ---------------------------------------------------------------------------
# ensure_projects_and_models() -- section 4 step 4
# ---------------------------------------------------------------------------


def test_ensure_projects_and_models_creates_dirs_and_chowns(tmp_path):
    ctx = FakeContext(tmp_path)
    amc_root = tmp_path / "amc"
    projects_dir, models_dir = step3.ensure_projects_and_models(ctx, amc_root)
    assert projects_dir.is_dir()
    assert models_dir.is_dir()
    assert ctx.runner_root.called_with_prefix("chown", "-R", "1000:1000")


# ---------------------------------------------------------------------------
# decide_hold_strategy() -- section 5.1/5.2 decision table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "has_display,browser_available,non_interactive,expected",
    [
        (True, True, False, step3.HoldStrategy.DEDICATED_WINDOW),
        (True, True, True, step3.HoldStrategy.DEDICATED_WINDOW),
        (True, False, False, step3.HoldStrategy.XDG_OPEN_AND_PROMPT),
        (True, False, True, step3.HoldStrategy.PRINT_URL_AND_LEAVE_UP),
        (False, False, False, step3.HoldStrategy.PRINT_URL_AND_PROMPT),
        (False, False, True, step3.HoldStrategy.PRINT_URL_AND_LEAVE_UP),
        (False, True, False, step3.HoldStrategy.PRINT_URL_AND_PROMPT),
        (False, True, True, step3.HoldStrategy.PRINT_URL_AND_LEAVE_UP),
    ],
)
def test_decide_hold_strategy_matrix(has_display, browser_available, non_interactive, expected):
    assert (
        step3.decide_hold_strategy(
            has_display=has_display,
            browser_available=browser_available,
            non_interactive=non_interactive,
        )
        is expected
    )


def test_find_browser_prefers_chromium_family_order():
    seen = []

    def which(name):
        seen.append(name)
        return "/usr/bin/chromium" if name == "chromium" else None

    assert step3.find_browser(which=which) == "chromium"
    assert seen == ["google-chrome", "chromium"]


def test_find_browser_falls_back_to_firefox():
    def which(name):
        return "/usr/bin/firefox" if name == "firefox" else None

    assert step3.find_browser(which=which) == "firefox"


def test_find_browser_returns_none_when_nothing_available():
    assert step3.find_browser(which=lambda name: None) is None


# ---------------------------------------------------------------------------
# execute_hold() -- section 5.1 dedicated window path + teardown
# ---------------------------------------------------------------------------


class _FakePopen:
    def __init__(self, argv):
        self.argv = argv
        self.waited = False

    def wait(self):
        self.waited = True


def test_execute_hold_dedicated_window_waits_then_tears_down(tmp_path):
    ctx = FakeContext(tmp_path)
    torn_down = {"count": 0}
    fake_proc = _FakePopen(["chromium"])

    def popen(argv):
        return fake_proc

    step3.execute_hold(
        ctx,
        step3.HoldStrategy.DEDICATED_WINDOW,
        "http://localhost:5000",
        teardown=lambda: torn_down.__setitem__("count", torn_down["count"] + 1),
        popen=popen,
        which=lambda name: "/usr/bin/chromium" if name == "chromium" else None,
    )

    assert fake_proc.waited is True
    assert torn_down["count"] == 1


def test_execute_hold_keep_up_never_tears_down(tmp_path):
    ctx = FakeContext(tmp_path)
    torn_down = {"count": 0}

    step3.execute_hold(
        ctx,
        step3.HoldStrategy.DEDICATED_WINDOW,
        "http://localhost:5000",
        teardown=lambda: torn_down.__setitem__("count", torn_down["count"] + 1),
        keep_up=True,
        popen=lambda argv: _FakePopen(argv),
        which=lambda name: "/usr/bin/chromium",
    )

    assert torn_down["count"] == 0


def test_execute_hold_print_url_and_leave_up_never_tears_down(tmp_path):
    ctx = FakeContext(tmp_path)
    torn_down = {"count": 0}

    step3.execute_hold(
        ctx,
        step3.HoldStrategy.PRINT_URL_AND_LEAVE_UP,
        "http://localhost:5000",
        teardown=lambda: torn_down.__setitem__("count", torn_down["count"] + 1),
    )

    assert torn_down["count"] == 0


def test_execute_hold_print_url_and_prompt_tears_down_after_enter(tmp_path):
    ctx = FakeContext(tmp_path)
    torn_down = {"count": 0}

    step3.execute_hold(
        ctx,
        step3.HoldStrategy.PRINT_URL_AND_PROMPT,
        "http://localhost:5000",
        teardown=lambda: torn_down.__setitem__("count", torn_down["count"] + 1),
        prompt=lambda _msg: "",
    )

    assert torn_down["count"] == 1


def test_execute_hold_falls_back_when_dedicated_window_unavailable(tmp_path):
    """A race between decide_hold_strategy's check and execute_hold's own
    open attempt (or a caller passing DEDICATED_WINDOW directly with no
    browser present) must not crash -- it degrades to the prompt path."""
    ctx = FakeContext(tmp_path)
    torn_down = {"count": 0}

    step3.execute_hold(
        ctx,
        step3.HoldStrategy.DEDICATED_WINDOW,
        "http://localhost:5000",
        teardown=lambda: torn_down.__setitem__("count", torn_down["count"] + 1),
        popen=lambda argv: _FakePopen(argv),
        which=lambda name: None,
        prompt=lambda _msg: "",
    )

    assert torn_down["count"] == 1


# ---------------------------------------------------------------------------
# render_amc_wrapper() / write_amc_wrapper() / ensure_installer_binary()
# -- section 6.1
# ---------------------------------------------------------------------------


def test_render_amc_wrapper_execs_the_installer_binary_with_amc():
    content = step3.render_amc_wrapper(pathlib.Path("/opt/mv3dt/bin/mv3dt-installer"))
    assert content.startswith("#!/usr/bin/env bash\n")
    assert 'exec "/opt/mv3dt/bin/mv3dt-installer" amc "$@"' in content


def test_write_amc_wrapper_creates_executable_file(tmp_path):
    ctx = FakeContext(tmp_path)
    installer_bin = ctx.install_dir / "bin" / "mv3dt-installer"
    path, changed = step3.write_amc_wrapper(ctx, installer_bin)
    assert changed is True
    assert path.is_file()
    assert path.stat().st_mode & 0o111  # executable bits set
    assert "amc" in path.read_text()


def test_write_amc_wrapper_is_idempotent(tmp_path):
    ctx = FakeContext(tmp_path)
    installer_bin = ctx.install_dir / "bin" / "mv3dt-installer"
    step3.write_amc_wrapper(ctx, installer_bin)
    _, changed = step3.write_amc_wrapper(ctx, installer_bin)
    assert changed is False


def test_ensure_installer_binary_dev_mode_logs_and_leaves_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    ctx = FakeContext(tmp_path)
    dest = step3.ensure_installer_binary(ctx)
    assert dest == ctx.install_dir / "bin" / "mv3dt-installer"
    assert not dest.exists()


def test_ensure_installer_binary_frozen_copies_from_sys_executable(tmp_path, monkeypatch):
    fake_exe = tmp_path / "fake-mv3dt-installer"
    fake_exe.write_bytes(b"#!/bin/sh\necho fake\n")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    ctx = FakeContext(tmp_path)
    dest = step3.ensure_installer_binary(ctx)

    assert dest.is_file()
    assert dest.read_bytes() == fake_exe.read_bytes()


# ---------------------------------------------------------------------------
# preflight() -- section 7.1
# ---------------------------------------------------------------------------


def test_preflight_fails_when_step2_not_complete(tmp_path):
    ctx = FakeContext(tmp_path, conf={})
    result = step3.Step3AmcLauncher().preflight(ctx)
    assert result.status is StepStatus.FAILED
    assert "Step 2" in result.message


def test_preflight_user_action_required_when_git_missing(tmp_path):
    runner_root = ScriptedRunner()
    runner_root.when(lambda a: a[:1] == ("which",), returncode=1)
    ctx = FakeContext(tmp_path, runner_root=runner_root, runner_user=_passing_runner())
    result = step3.Step3AmcLauncher().preflight(ctx)
    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "git" in result.message.lower()


def test_preflight_user_action_required_when_docker_missing(tmp_path):
    runner_user = ScriptedRunner(default_returncode=1)
    ctx = FakeContext(tmp_path, runner_user=runner_user)
    result = step3.Step3AmcLauncher().preflight(ctx)
    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert any(a.command and "docker.io" in a.command for a in result.user_actions)


def test_preflight_user_action_required_when_nvidia_runtime_absent(tmp_path):
    runner_user = ScriptedRunner()
    runner_user.when(lambda a: a[:2] == ("docker", "info"), stdout="Runtimes: runc\n")
    ctx = FakeContext(tmp_path, runner_user=runner_user)
    result = step3.Step3AmcLauncher().preflight(ctx)
    assert result.status is StepStatus.USER_ACTION_REQUIRED


def test_preflight_complete_when_everything_ready(tmp_path):
    ctx = FakeContext(tmp_path, runner_user=_passing_runner())
    result = step3.Step3AmcLauncher().preflight(ctx)
    assert result.status is StepStatus.COMPLETE


# ---------------------------------------------------------------------------
# launch_amc() -- section 5.1 step 3: teardown guard installed before `up -d`
# ---------------------------------------------------------------------------


def _stub_amc_root_with_compose(ctx) -> pathlib.Path:
    amc_root = ctx.user.home / "auto-magic-calib"
    (amc_root / "tools" / "auto-magic-calib" / "compose").mkdir(parents=True)
    return amc_root


def test_launch_amc_installs_teardown_guard_before_compose_up(tmp_path, monkeypatch):
    """Section 5.1 step 3 (REQUIRED): the SIGINT/SIGTERM/atexit fail-safe
    must be armed before `docker compose up -d` runs, not merely before the
    browser-hold step -- a Ctrl-C during pull/up/the readiness poll must
    still tear AMC down. Assert the actual call order, not just that all
    of these eventually happen."""
    ctx = FakeContext(tmp_path, runner_user=_passing_runner())
    _stub_amc_root_with_compose(ctx)

    order: list[str] = []
    monkeypatch.setattr(
        step3, "_install_teardown_guards", lambda teardown: order.append("guard_installed") or teardown
    )
    monkeypatch.setattr(step3, "compose_pull", lambda ctx, compose_dir: order.append("pull"))
    monkeypatch.setattr(step3, "compose_up", lambda ctx, compose_dir: order.append("up"))
    monkeypatch.setattr(
        step3, "wait_for_ui", lambda ctx, url: order.append("wait") or True
    )
    monkeypatch.setattr(step3, "execute_hold", lambda *a, **k: order.append("hold"))

    result = step3.launch_amc(ctx, non_interactive=True)

    assert result.status is StepStatus.COMPLETE
    assert order == ["guard_installed", "pull", "up", "wait", "hold"]


def test_launch_amc_no_open_never_installs_teardown_guard(tmp_path, monkeypatch):
    """Regression: `--no-open` returns COMPLETE right after bring-up
    without ever reaching `execute_hold`. `atexit.register` fires on *any*
    normal process exit, not just a signal -- so installing the guard here
    and then simply returning would silently tear AMC back down the moment
    this process exits, defeating `--no-open`'s documented purpose ("bring
    up without opening/holding -- for scripting"). Nothing must be armed
    with `atexit` at all in this path.

    `atexit.register` itself is monkeypatched to capture handlers rather
    than really registering them with the interpreter, so this proves the
    guard is never armed without touching the real process-exit machinery
    (or any other test's atexit state).
    """
    ctx = FakeContext(tmp_path, runner_user=_passing_runner())
    _stub_amc_root_with_compose(ctx)

    registered: list = []
    monkeypatch.setattr(step3.atexit, "register", lambda fn: registered.append(fn))

    teardown_calls = {"count": 0}
    monkeypatch.setattr(
        step3,
        "compose_down",
        lambda ctx, compose_dir: teardown_calls.__setitem__(
            "count", teardown_calls["count"] + 1
        ),
    )

    result = step3.launch_amc(ctx, no_open=True, non_interactive=True)

    assert result.status is StepStatus.COMPLETE
    assert registered == []  # nothing armed with atexit at all

    # Simulate every finalizer that *was* registered actually firing (there
    # should be none) -- proves this isn't merely "installed but not yet
    # triggered synchronously" the way the original bug was.
    for fn in registered:
        fn()
    assert teardown_calls["count"] == 0


def test_launch_amc_open_case_atexit_fire_does_trigger_teardown(tmp_path, monkeypatch):
    """Contrast case for the regression above, proving the harness itself
    is meaningful (not just trivially empty): in the ordinary hold path
    (no `--no-open`), the guard IS armed with `atexit`, and firing that
    captured handler -- simulating a normal process exit -- does trigger
    `compose down`."""
    ctx = FakeContext(tmp_path, runner_user=_passing_runner())
    _stub_amc_root_with_compose(ctx)

    registered: list = []
    monkeypatch.setattr(step3.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(step3, "wait_for_ui", lambda ctx, url: True)
    monkeypatch.setattr(step3, "execute_hold", lambda *a, **k: None)  # skip the real hold

    teardown_calls = {"count": 0}
    monkeypatch.setattr(
        step3,
        "compose_down",
        lambda ctx, compose_dir: teardown_calls.__setitem__(
            "count", teardown_calls["count"] + 1
        ),
    )

    result = step3.launch_amc(ctx, non_interactive=True)

    assert result.status is StepStatus.COMPLETE
    assert len(registered) == 1

    registered[0]()
    assert teardown_calls["count"] == 1


def test_launch_amc_skips_teardown_guard_when_keep_up(tmp_path, monkeypatch):
    """`--keep-up` means "never tear down" -- the guard must not be
    installed at all, not merely installed-and-then-ignored."""
    ctx = FakeContext(tmp_path, runner_user=_passing_runner())
    _stub_amc_root_with_compose(ctx)

    guard_calls: list[int] = []
    monkeypatch.setattr(
        step3,
        "_install_teardown_guards",
        lambda teardown: guard_calls.append(1) or teardown,
    )
    monkeypatch.setattr(step3, "compose_pull", lambda ctx, compose_dir: None)
    monkeypatch.setattr(step3, "compose_up", lambda ctx, compose_dir: None)
    monkeypatch.setattr(step3, "wait_for_ui", lambda ctx, url: True)
    monkeypatch.setattr(step3, "execute_hold", lambda *a, **k: None)

    step3.launch_amc(ctx, keep_up=True, non_interactive=True)

    assert guard_calls == []


def test_launch_amc_passes_the_guarded_teardown_to_execute_hold(tmp_path, monkeypatch):
    """`execute_hold` must receive the exact run-once-guarded callable
    `_install_teardown_guards` returns, not a fresh, unguarded closure --
    otherwise a signal delivered during the hold step and a normal
    fall-through would race on two different `state["ran"]` flags."""
    ctx = FakeContext(tmp_path, runner_user=_passing_runner())
    _stub_amc_root_with_compose(ctx)

    sentinel = object()
    monkeypatch.setattr(step3, "_install_teardown_guards", lambda teardown: sentinel)
    monkeypatch.setattr(step3, "compose_pull", lambda ctx, compose_dir: None)
    monkeypatch.setattr(step3, "compose_up", lambda ctx, compose_dir: None)
    monkeypatch.setattr(step3, "wait_for_ui", lambda ctx, url: True)

    captured = {}

    def _fake_execute_hold(ctx, strategy, url, *, teardown, keep_up=False, **kwargs):
        captured["teardown"] = teardown

    monkeypatch.setattr(step3, "execute_hold", _fake_execute_hold)

    step3.launch_amc(ctx, non_interactive=True)

    assert captured["teardown"] is sentinel


# ---------------------------------------------------------------------------
# run() -- section 7.2 (deliverable-vs-launch split, section 2)
# ---------------------------------------------------------------------------


def test_run_drops_exe_and_completes_without_launching_when_declined(tmp_path, monkeypatch):
    ctx = FakeContext(tmp_path, non_interactive=True)
    result = step3.Step3AmcLauncher().run(ctx)
    assert result.status is StepStatus.COMPLETE
    assert (ctx.install_dir / "bin" / "amc").is_file()
    # Non-interactive means _confirm_launch_now() never even asks --
    # nothing docker/git-shaped should have run.
    assert ctx.runner_user.calls == []


def test_run_launches_when_operator_confirms(tmp_path, monkeypatch):
    ctx = FakeContext(tmp_path, non_interactive=False, runner_user=_passing_runner())
    amc_root = ctx.user.home / "auto-magic-calib"

    def _fake_clone():
        (amc_root / "tools" / "auto-magic-calib" / "compose").mkdir(parents=True)

    ctx.runner_user.when(
        lambda a: a[:2] == ("git", "clone"), side_effect=_fake_clone
    )

    monkeypatch.setattr(step3, "_INPUT", lambda _prompt: "y")
    monkeypatch.setattr(
        step3,
        "execute_hold",
        lambda *a, **k: None,  # skip the real hold/teardown path
    )
    result = step3.Step3AmcLauncher().run(ctx)
    assert result.status is StepStatus.COMPLETE
    assert ctx.runner_user.called_with_prefix("git", "clone")


def test_run_fails_on_repo_isolation_violation(tmp_path, monkeypatch):
    monkeypatch.setattr(step3, "repo_root", lambda: tmp_path / "home" / "op")
    ctx = FakeContext(tmp_path)
    result = step3.Step3AmcLauncher().run(ctx)
    assert result.status is StepStatus.FAILED
    assert "must not live under this repo" in result.message


# ---------------------------------------------------------------------------
# verify() -- section 7.3
# ---------------------------------------------------------------------------


def _fully_provisioned_ctx(tmp_path) -> FakeContext:
    conf = {
        step2.CONF_METHOD_KEY: "deb",
        step3.CONF_AMC_ROOT_KEY: str(tmp_path / "amc"),
        step3.CONF_HOST_IP_KEY: "127.0.0.1",
        step3.CONF_UI_PORT_KEY: "5000",
        step3.CONF_MS_PORT_KEY: "8000",
        step3.CONF_PROJECT_NAME_KEY: "default",
        step3.CONF_NVIDIA_VISIBLE_DEVICES_KEY: "all",
    }
    ctx = FakeContext(tmp_path, conf=conf, runner_user=_passing_runner())
    bin_dir = ctx.install_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "mv3dt-installer").write_text("#!/bin/sh\n")
    wrapper = bin_dir / "amc"
    wrapper.write_text("#!/usr/bin/env bash\n")
    wrapper.chmod(0o755)
    return ctx


def test_verify_complete_when_fully_provisioned(tmp_path):
    ctx = _fully_provisioned_ctx(tmp_path)
    result = step3.Step3AmcLauncher().verify(ctx)
    assert result.status is StepStatus.COMPLETE


def test_verify_fails_when_wrapper_missing(tmp_path):
    ctx = _fully_provisioned_ctx(tmp_path)
    (ctx.install_dir / "bin" / "amc").unlink()
    result = step3.Step3AmcLauncher().verify(ctx)
    assert result.status is StepStatus.FAILED
    assert "amc" in result.message


def test_verify_fails_when_conf_key_missing(tmp_path):
    ctx = _fully_provisioned_ctx(tmp_path)
    del ctx.conf[step3.CONF_HOST_IP_KEY]
    result = step3.Step3AmcLauncher().verify(ctx)
    assert result.status is StepStatus.FAILED
    assert step3.CONF_HOST_IP_KEY in result.message


def test_verify_fails_when_nvidia_runtime_missing(tmp_path):
    ctx = _fully_provisioned_ctx(tmp_path)
    ctx.runner_user = ScriptedRunner()  # docker info reports no nvidia runtime
    result = step3.Step3AmcLauncher().verify(ctx)
    assert result.status is StepStatus.FAILED


# ---------------------------------------------------------------------------
# resolved_amc_commit() -- section 7.3 ("verify() records the resolved
# commit for the transcript rather than equality-pinning it")
# ---------------------------------------------------------------------------


def test_resolved_amc_commit_returns_stripped_sha(tmp_path):
    amc_root = tmp_path / "amc"
    sha = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    runner_user = ScriptedRunner()
    runner_user.when(
        lambda a: a[:5] == ("git", "-C", str(amc_root), "rev-parse", "HEAD"),
        stdout=f"{sha}\n",
    )
    ctx = FakeContext(tmp_path, runner_user=runner_user)
    assert step3.resolved_amc_commit(ctx, amc_root) == sha


def test_resolved_amc_commit_none_on_git_failure(tmp_path):
    runner_user = ScriptedRunner(default_returncode=1)
    ctx = FakeContext(tmp_path, runner_user=runner_user)
    assert step3.resolved_amc_commit(ctx, tmp_path / "amc") is None


def test_resolved_amc_commit_none_on_blank_output(tmp_path):
    runner_user = ScriptedRunner(default_returncode=0, default_stdout="\n")
    ctx = FakeContext(tmp_path, runner_user=runner_user)
    assert step3.resolved_amc_commit(ctx, tmp_path / "amc") is None


def test_verify_logs_the_resolved_amc_commit(tmp_path, capsys):
    ctx = _fully_provisioned_ctx(tmp_path)
    sha = "deadbeefcafefeed0000111122223333deadbeef"
    ctx.runner_user.when(
        lambda a: a[:4] == ("git", "-C", str(tmp_path / "amc"), "rev-parse"),
        stdout=f"{sha}\n",
    )

    result = step3.Step3AmcLauncher().verify(ctx)

    assert result.status is StepStatus.COMPLETE
    err = capsys.readouterr().err
    assert sha in err


def test_verify_logs_unknown_commit_when_resolution_fails(tmp_path, capsys):
    ctx = _fully_provisioned_ctx(tmp_path)
    ctx.runner_user.when(lambda a: a[:2] == ("git", "-C"), returncode=1)

    result = step3.Step3AmcLauncher().verify(ctx)

    # An unresolvable commit is informational only -- verify() still passes.
    assert result.status is StepStatus.COMPLETE
    err = capsys.readouterr().err
    assert "AMC resolved commit: unknown" in err
