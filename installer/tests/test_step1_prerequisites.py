"""Tests for mv3dt_installer.steps.step1_prerequisites (STEP-1-PREREQUISITES.md).

Run from installer/: `python3 -m pytest tests/test_step1_prerequisites.py -v`

No test here shells out for real, installs a package, or touches a real
system path: every `ctx.run_root` call is routed through `FakeRunner` below,
and every system-config path the step writes directly
(`CUDA_PROFILE_PATH`, `NOUVEAU_BLACKLIST_PATH`, `MOSQUITTO_CONF_DIR`) is
monkeypatched to a `tmp_path` location, exactly like `test_systemd.py`
monkeypatches `systemd.UNIT_DIR`.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mv3dt_installer import logs, shellout  # noqa: E402
from mv3dt_installer.steps import STEP_REGISTRY, StepStatus  # noqa: E402
from mv3dt_installer.steps import step1_prerequisites as s1  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_transcript_state():
    logs._transcript_path = None
    yield
    logs._transcript_path = None


@pytest.fixture(autouse=True)
def _force_no_colour(monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)


@pytest.fixture(autouse=True)
def _safe_system_paths(monkeypatch, tmp_path):
    """Belt and braces: point every real system path this module writes at a
    scratch dir so a bug under test can never reach the real filesystem."""
    monkeypatch.setattr(s1, "CUDA_PROFILE_PATH", tmp_path / "etc-profile.d" / "cuda.sh")
    monkeypatch.setattr(s1, "NOUVEAU_BLACKLIST_PATH", tmp_path / "etc-modprobe.d" / "blacklist-nouveau.conf")
    monkeypatch.setattr(s1, "MOSQUITTO_CONF_DIR", tmp_path / "etc-mosquitto" / "conf.d")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _ok(args, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


def _rc(args, code: int, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, code, stdout=stdout, stderr="")


class FakeRunner:
    """Stand-in for `ctx.run_root`, dispatching by argv[0] with a "healthy
    fully-provisioned Launch B machine" default, overridable per test."""

    def __init__(
        self,
        *,
        os_version: str = "24.04",
        arch: str = "x86_64",
        gpu_present: bool = True,
        secure_boot_enabled: bool = False,
        gdm_stops: bool = True,
        nouveau_loaded: bool = False,
        distro_nvidia_packages: tuple = (),
        driver_run_returncode: int = 0,
        dpkg_versions: Optional[dict] = None,
        driver_version: str = "",
        nvcc_release: str = "",
        gstreamer_version: str = "",
        mosquitto_active: bool = True,
        cudnn_install_result: Optional[str] = s1.CUDNN_VERSION,
        kernel_release: str = "6.8.0-generic",
    ) -> None:
        self.calls: list[tuple] = []
        self.os_version = os_version
        self.arch = arch
        self.gpu_present = gpu_present
        self.secure_boot_enabled = secure_boot_enabled
        self.gdm_stops = gdm_stops
        self.nouveau_loaded = nouveau_loaded
        self.distro_nvidia_packages = list(distro_nvidia_packages)
        self.driver_run_returncode = driver_run_returncode
        self.dpkg_versions: dict[str, str] = dict(dpkg_versions or {})
        self.driver_version = driver_version
        self.nvcc_release = nvcc_release
        self.gstreamer_version = gstreamer_version
        self.mosquitto_active = mosquitto_active
        self.cudnn_install_result = cudnn_install_result
        self.kernel_release = kernel_release

    def __call__(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(args)
        cmd = args[0]

        if cmd == "lsb_release":
            return _ok(args, self.os_version)
        if cmd == "uname" and args[1] == "-m":
            return _ok(args, self.arch)
        if cmd == "uname" and args[1] == "-r":
            return _ok(args, self.kernel_release)
        if cmd == "bash":
            script = args[2]
            if "lspci" in script:
                return _rc(args, 0 if self.gpu_present else 1)
            if "nouveau" in script:
                return _rc(args, 0 if self.nouveau_loaded else 1)
            if "nvidia-*" in script:
                return _ok(args, "\n".join(self.distro_nvidia_packages))
            return _ok(args)
        if cmd == "mokutil":
            state = "enabled" if self.secure_boot_enabled else "disabled"
            return _ok(args, f"SecureBoot {state}")
        if cmd == "service":
            return _rc(args, 0 if self.gdm_stops else 1)
        if cmd == "pkill":
            return _rc(args, 0)
        if cmd == "dpkg-query":
            pkg = args[3]
            version = self.dpkg_versions.get(pkg)
            return _ok(args, version) if version else _rc(args, 1)
        if cmd == "apt-get":
            if args[1] == "install":
                for tok in args[4:]:
                    pkg = tok.split("=")[0]
                    if pkg == s1.CUDNN_APT_GLOB:
                        if self.cudnn_install_result:
                            self.dpkg_versions[s1.CUDNN_QUERY_PACKAGE] = self.cudnn_install_result
                        continue
                    version = tok.split("=")[1] if "=" in tok else self.dpkg_versions.get(pkg, "1.0")
                    self.dpkg_versions[pkg] = version
            return _ok(args)
        if cmd == "update-initramfs":
            return _ok(args)
        if cmd == "nvidia-smi":
            if any("driver_version" in a for a in args):
                return _ok(args, self.driver_version) if self.driver_version else _rc(args, 1)
            return _ok(args, "RTX PRO 4500 Blackwell, 8.9")
        if cmd == "nvcc":
            if not self.nvcc_release:
                return _rc(args, 1)
            return _ok(args, f"Cuda compilation tools, release {self.nvcc_release}, V{self.nvcc_release}.100")
        if cmd == "gst-inspect-1.0":
            if not self.gstreamer_version:
                return _rc(args, 1)
            return _ok(args, f"gst-inspect-1.0 version {self.gstreamer_version}")
        if cmd == "systemctl":
            return _rc(args, 0 if self.mosquitto_active else 1)
        if isinstance(cmd, str) and cmd.endswith(".run"):
            return _rc(args, self.driver_run_returncode)
        return _ok(args)


class FakeLog:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, msg: str) -> None:
        self.lines.append(msg)

    def warn(self, msg: str) -> None:
        self.lines.append(msg)

    def error(self, msg: str) -> None:
        self.lines.append(msg)


class FakeContext:
    """Minimal duck-typed stand-in for `app.Context` (doc 00 section 12.3):
    every field the step actually touches, nothing this module doesn't."""

    def __init__(
        self,
        *,
        install_dir: pathlib.Path,
        runner: FakeRunner,
        non_interactive: Optional[bool] = True,
        asset_path: Optional[Callable[..., pathlib.Path]] = None,
    ) -> None:
        self.install_dir = install_dir
        self.conf: dict = {}
        self.user = SimpleNamespace(name="op")
        self.log = FakeLog()
        self.installed: list[tuple[str, str]] = []
        self.already_installed: list[tuple[str, str]] = []
        self.runner = runner
        self.non_interactive = non_interactive
        self.asset_path = asset_path or shellout.asset_path
        self.reboot = SimpleNamespace(request=lambda: StepStatus.REBOOT_REQUIRED)

    def report_installed(self, dependency: str, version: str) -> None:
        self.installed.append((dependency, version))

    def report_already_installed(self, dependency: str, version: str) -> None:
        self.already_installed.append((dependency, version))

    def verify_pinned(self, label: str, actual: str, expected: str) -> bool:
        return actual == expected

    def run_root(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return self.runner(*args, **kwargs)

    def run_as_user(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:  # pragma: no cover
        return self.runner(*args, **kwargs)


def _make_ctx(tmp_path: pathlib.Path, **runner_kwargs: Any) -> tuple[FakeContext, FakeRunner]:
    runner = FakeRunner(**runner_kwargs)
    ctx = FakeContext(install_dir=tmp_path / "opt" / "mv3dt", runner=runner)
    ctx.install_dir.mkdir(parents=True, exist_ok=True)
    return ctx, runner


def _stage_driver_run(ctx: FakeContext) -> pathlib.Path:
    path = s1._driver_run_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\necho fake driver installer\n")
    return path


# ---------------------------------------------------------------------------
# Module identity (doc 00 section 12.1)
# ---------------------------------------------------------------------------


def test_registers_itself_with_the_expected_identity():
    matches = [s for s in STEP_REGISTRY if s.id == "step1_prerequisites"]
    assert len(matches) == 1
    step = matches[0]
    assert step.order == 1
    assert "Prerequisites" in step.title


# ---------------------------------------------------------------------------
# preflight()
# ---------------------------------------------------------------------------


def test_preflight_complete_on_ubuntu_24_04_x86_64_with_gpu(tmp_path):
    ctx, _ = _make_ctx(tmp_path)
    result = s1.Step1Prerequisites().preflight(ctx)
    assert result.status is StepStatus.COMPLETE


def test_preflight_fails_on_wrong_os_version(tmp_path):
    ctx, _ = _make_ctx(tmp_path, os_version="22.04")
    result = s1.Step1Prerequisites().preflight(ctx)
    assert result.status is StepStatus.FAILED
    assert "22.04" in result.message


def test_preflight_fails_on_wrong_arch(tmp_path):
    ctx, _ = _make_ctx(tmp_path, arch="aarch64")
    result = s1.Step1Prerequisites().preflight(ctx)
    assert result.status is StepStatus.FAILED


def test_preflight_fails_when_no_nvidia_gpu(tmp_path):
    ctx, _ = _make_ctx(tmp_path, gpu_present=False)
    result = s1.Step1Prerequisites().preflight(ctx)
    assert result.status is StepStatus.FAILED
    assert "GPU" in result.message


# ---------------------------------------------------------------------------
# Two-launch / reboot-stage tracking (STEP-1 section 5, section 6.3)
# ---------------------------------------------------------------------------


def test_run_dispatches_to_launch_a_when_driver_not_loaded(tmp_path):
    """No driver_version -> _driver_loaded() is False -> Launch A path,
    which (with the .run staged) ends in USER_ACTION_REQUIRED -- not
    REBOOT_REQUIRED, since the merged reboot.reconcile()/app._dispatch()
    would auto-complete this step on a confirmed reboot before its
    post-reboot Launch B work (TensorRT/cuDNN/verify) ever ran. See this
    module's "Reboot handling" docstring section."""
    ctx, runner = _make_ctx(tmp_path, driver_version="")
    _stage_driver_run(ctx)

    result = s1.Step1Prerequisites().run(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert any(a.command == "sudo reboot" for a in result.user_actions)
    # The driver .run was actually invoked.
    assert any(str(c[0]).endswith(".run") for c in runner.calls)


def test_run_dispatches_to_launch_b_when_driver_already_loaded(tmp_path):
    """Driver already loaded -> Launch B path: no .run invocation, ends
    COMPLETE once TensorRT/cuDNN/mosquitto succeed."""
    ctx, runner = _make_ctx(
        tmp_path,
        driver_version=s1.DRIVER_VERSION,
        gstreamer_version=s1.GSTREAMER_VERSION,
        mosquitto_active=True,
    )
    monkeypatch_ok = _patch_mosquitto_success(ctx)

    result = s1.Step1Prerequisites().run(ctx)

    assert result.status is StepStatus.COMPLETE
    assert not any(str(c[0]).endswith(".run") for c in runner.calls)
    monkeypatch_ok()


def _patch_mosquitto_success(ctx: FakeContext):
    """Monkeypatch shellout.run_bundled_script for the duration of a call,
    returning success. Returns a callable that restores the original."""
    original = shellout.run_bundled_script

    def fake_run_bundled_script(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    shellout.run_bundled_script = fake_run_bundled_script  # type: ignore[assignment]

    def _restore():
        shellout.run_bundled_script = original  # type: ignore[assignment]

    return _restore


def test_launch_a_returns_user_action_required_early_when_nouveau_cleanup_needed(tmp_path):
    """STEP-1 section 5 step 5: nouveau loaded -> USER_ACTION_REQUIRED
    (reboot instructions) *before* the Secure Boot check or the .run
    installer are ever reached. Not REBOOT_REQUIRED -- see this module's
    "Reboot handling" docstring section for why."""
    ctx, runner = _make_ctx(tmp_path, nouveau_loaded=True, secure_boot_enabled=True)
    _stage_driver_run(ctx)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "nouveau" in result.message
    assert any(a.command == "sudo reboot" for a in result.user_actions)
    # Never reached the .run invocation or the mokutil check.
    assert not any(str(c[0]).endswith(".run") for c in runner.calls)
    assert not any(c[0] == "mokutil" for c in runner.calls)


def test_launch_a_returns_user_action_required_early_when_distro_driver_purged(tmp_path):
    ctx, runner = _make_ctx(
        tmp_path, nouveau_loaded=False, distro_nvidia_packages=("nvidia-driver-550",)
    )
    _stage_driver_run(ctx)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert any(c[0] == "apt-get" and c[1] == "purge" for c in runner.calls)


def test_launch_a_ends_in_user_action_required_after_a_successful_driver_run(tmp_path):
    ctx, runner = _make_ctx(tmp_path)
    run_path = _stage_driver_run(ctx)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert any(str(c[0]) == str(run_path) for c in runner.calls)
    # doc 00 section 6.2: three UserAction entries -- driver, CUDA path, reboot cmd.
    assert len(result.user_actions) == 3
    assert result.user_actions[-1].command == "sudo reboot"
    assert str(s1.CUDA_PROFILE_PATH) == result.user_actions[1].path
    ctx.report_installed  # sanity: attribute exists
    assert ("nvidia-driver", s1.DRIVER_VERSION) in ctx.installed


def test_launch_a_fails_when_driver_run_exits_nonzero(tmp_path):
    ctx, _ = _make_ctx(tmp_path, driver_run_returncode=1)
    _stage_driver_run(ctx)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.FAILED


# ---------------------------------------------------------------------------
# Regression: a confirmed reboot must not let the *real* merged
# reboot.reconcile()/app._dispatch() skip Launch B (review PR #44 finding 1).
# ---------------------------------------------------------------------------


def test_confirmed_reboot_still_lets_launch_b_run_on_next_dispatch(tmp_path, monkeypatch):
    """End-to-end against the actual merged `mv3dt_installer.reboot` and
    `mv3dt_installer.app` modules (not a re-derivation of their logic):

    `reboot.reconcile()` marks the *requesting* step COMPLETE in
    `state.json` the instant it confirms a real reboot happened, and
    `app._dispatch()` then skips any step already COMPLETE without
    re-running its lifecycle. If Step 1 returned REBOOT_REQUIRED for its
    own reboot points, that combination would mark step1_prerequisites
    COMPLETE the moment the reboot confirmed -- before TensorRT, cuDNN,
    mosquitto, or verify() ever ran on Launch B. Step 1 instead returns
    USER_ACTION_REQUIRED for both reboot points, so state.json never
    records step1_prerequisites as COMPLETE or as a pending reboot; this
    test proves Launch B genuinely executes on the next dispatch.
    """
    from mv3dt_installer import app as app_mod
    from mv3dt_installer import reboot as reboot_mod
    from mv3dt_installer.state import StateMachine

    boot_id_path = tmp_path / "boot_id"
    boot_id_path.write_text("boot-A\n")
    monkeypatch.setattr(reboot_mod, "BOOT_ID_PATH", boot_id_path)

    sm = StateMachine(path=tmp_path / "state.json")
    step = s1.Step1Prerequisites()

    # Isolate the registry to just this step: `_dispatch()` walks the real
    # module-global `STEP_REGISTRY`, which in a full test-suite run also
    # holds every other step registered by sibling test modules' imports.
    # Without this, Launch B's dispatch would also run step2's verify()
    # against a ctx that was only ever set up to satisfy step1's pins.
    monkeypatch.setattr(app_mod, "STEP_REGISTRY", [step])

    # -- Launch A: driver not loaded, .run staged -> reboot instructions.
    ctx_a, runner_a = _make_ctx(tmp_path, driver_version="")
    _stage_driver_run(ctx_a)

    result_a = app_mod._run_step_lifecycle(step, ctx_a)
    assert result_a.status is StepStatus.USER_ACTION_REQUIRED

    # Mirror what app._dispatch() itself does with a USER_ACTION_REQUIRED
    # result: nothing written to state.json (no mark_complete, no
    # set_reboot_pending -- see app._dispatch()'s branching).
    assert sm.status(step.id) is StepStatus.PENDING

    # -- The operator actually reboots.
    boot_id_path.write_text("boot-B\n")

    # reconcile() must see NOTHING_PENDING (this step never registered a
    # pending reboot with the framework) and must not mutate state.json.
    reconcile_result = reboot_mod.reconcile(sm)
    assert reconcile_result is reboot_mod.ReconcileResult.NOTHING_PENDING
    assert sm.status(step.id) is StepStatus.PENDING

    # -- Launch B, driven through the real dispatch loop: driver now loads.
    # `_dispatch()` runs the full preflight -> run -> verify() lifecycle
    # (unlike calling `_run_launch_b` directly), so every pin verify() will
    # check needs a matching value -- including nvcc release, which
    # `run()`'s own launch-B apt calls don't touch.
    ctx_b, runner_b = _make_ctx(
        tmp_path,
        driver_version=s1.DRIVER_VERSION,
        nvcc_release=s1.CUDA_VERSION,
        gstreamer_version=s1.GSTREAMER_VERSION,
        mosquitto_active=True,
    )
    monkeypatch.setattr(s1, "_mosquitto_conf_matches_bundled", lambda ctx: True)
    restore = _patch_mosquitto_success(ctx_b)
    try:
        exit_code = app_mod._dispatch(sm, ctx_b, cfg=object())
    finally:
        restore()

    assert exit_code == 0
    assert sm.status(step.id) is StepStatus.COMPLETE
    # Launch B's TensorRT apt-install actually ran (not skipped as
    # already-COMPLETE) -- the smoking gun for the bug under regression.
    apt_installs = [
        c for c in runner_b.calls if c[0] == "apt-get" and c[1] == "install"
    ]
    assert any(
        any(f"{s1.TENSORRT_PACKAGES[0]}=" in tok for tok in call)
        for call in apt_installs
    )
    # The driver .run was never re-invoked on Launch B.
    assert not any(str(c[0]).endswith(".run") for c in runner_b.calls)


# ---------------------------------------------------------------------------
# USER-ACTION cases
# ---------------------------------------------------------------------------


def test_launch_a_user_action_when_driver_run_not_staged(tmp_path):
    ctx, _ = _make_ctx(tmp_path)
    # deliberately do not stage the .run file

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert len(result.user_actions) == 1
    action = result.user_actions[0]
    assert s1.DRIVER_RUN_FILENAME in action.path
    assert "nvidia.com" in (action.command or "")


def test_launch_a_user_action_when_secure_boot_enabled(tmp_path):
    ctx, runner = _make_ctx(tmp_path, secure_boot_enabled=True)
    _stage_driver_run(ctx)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "Secure Boot" in result.message
    assert any("MOK" in a.text or "Secure Boot" in a.text for a in result.user_actions)
    # Never reached the .run invocation.
    assert not any(str(c[0]).endswith(".run") for c in runner.calls)


def test_launch_a_user_action_when_gdm_will_not_stop(tmp_path):
    ctx, _ = _make_ctx(tmp_path, gdm_stops=False)
    _stage_driver_run(ctx)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert any("TTY" in a.text for a in result.user_actions)


# ---------------------------------------------------------------------------
# apt install + report_installed / report_already_installed wrapping
# ---------------------------------------------------------------------------


def test_apt_install_reported_reports_installed_for_a_new_package(tmp_path):
    ctx, runner = _make_ctx(tmp_path)
    s1._apt_install_reported(ctx, ["curl"])
    assert ("curl", "1.0") in ctx.installed
    assert ctx.already_installed == []


def test_apt_install_reported_reports_already_installed_when_version_unchanged(tmp_path):
    ctx, runner = _make_ctx(tmp_path, dpkg_versions={"curl": "8.5.0-1"})
    s1._apt_install_reported(ctx, ["curl"])
    assert ("curl", "8.5.0-1") in ctx.already_installed
    assert ctx.installed == []


def test_apt_install_reported_uses_pinned_apt_args_for_tensorrt(tmp_path):
    ctx, runner = _make_ctx(tmp_path)
    apt_args = [f"{pkg}={s1.TENSORRT_VERSION}" for pkg in s1.TENSORRT_PACKAGES]
    s1._apt_install_reported(ctx, s1.TENSORRT_PACKAGES, apt_args=apt_args)
    for pkg in s1.TENSORRT_PACKAGES:
        assert (pkg, s1.TENSORRT_VERSION) in ctx.installed


def test_launch_b_pins_cudnn_version_at_apt_install(tmp_path):
    """Secondary review fix: cuDNN must be apt-pinned like TensorRT, not
    installed via a bare unpinned `libcudnn9*` glob -- so a version
    mismatch fails fast at apt-install time, not only later at verify()."""
    ctx, runner = _make_ctx(
        tmp_path,
        driver_version=s1.DRIVER_VERSION,
        gstreamer_version=s1.GSTREAMER_VERSION,
        mosquitto_active=True,
    )
    restore = _patch_mosquitto_success(ctx)

    s1.Step1Prerequisites()._run_launch_b(ctx)

    restore()

    install_calls = [c for c in runner.calls if c[0] == "apt-get" and c[1] == "install"]
    pinned_cudnn_arg = f"{s1.CUDNN_QUERY_PACKAGE}={s1.CUDNN_VERSION}"
    assert any(pinned_cudnn_arg in call for call in install_calls)
    # The rest of the cuDNN family is still picked up via the glob.
    assert any(s1.CUDNN_APT_GLOB in call for call in install_calls)


# ---------------------------------------------------------------------------
# verify() -- pinned checklist (STEP-1 section 7.3)
# ---------------------------------------------------------------------------


def _fully_pinned_versions() -> dict:
    return {
        "libnvinfer10": s1.TENSORRT_VERSION,
        s1.CUDNN_QUERY_PACKAGE: s1.CUDNN_VERSION,
    }


def test_verify_complete_when_every_pin_matches(tmp_path, monkeypatch):
    ctx, _ = _make_ctx(
        tmp_path,
        driver_version=s1.DRIVER_VERSION,
        nvcc_release=s1.CUDA_VERSION,
        gstreamer_version=s1.GSTREAMER_VERSION,
        dpkg_versions=_fully_pinned_versions(),
        mosquitto_active=True,
    )
    monkeypatch.setattr(s1, "_mosquitto_conf_matches_bundled", lambda ctx: True)

    result = s1.Step1Prerequisites().verify(ctx)

    assert result.status is StepStatus.COMPLETE


def test_verify_user_action_required_when_driver_version_mismatches(tmp_path, monkeypatch):
    ctx, _ = _make_ctx(
        tmp_path,
        driver_version="550.00.00",  # wrong pin
        nvcc_release=s1.CUDA_VERSION,
        gstreamer_version=s1.GSTREAMER_VERSION,
        dpkg_versions=_fully_pinned_versions(),
        mosquitto_active=True,
    )
    monkeypatch.setattr(s1, "_mosquitto_conf_matches_bundled", lambda ctx: True)

    result = s1.Step1Prerequisites().verify(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert result.user_actions


def test_verify_user_action_required_when_mosquitto_not_active(tmp_path, monkeypatch):
    ctx, _ = _make_ctx(
        tmp_path,
        driver_version=s1.DRIVER_VERSION,
        nvcc_release=s1.CUDA_VERSION,
        gstreamer_version=s1.GSTREAMER_VERSION,
        dpkg_versions=_fully_pinned_versions(),
        mosquitto_active=False,
    )
    monkeypatch.setattr(s1, "_mosquitto_conf_matches_bundled", lambda ctx: True)

    result = s1.Step1Prerequisites().verify(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED


def test_verify_never_returns_complete_on_a_single_mismatch(tmp_path, monkeypatch):
    """Every pin must match -- one mismatch (cuDNN here) blocks COMPLETE."""
    versions = _fully_pinned_versions()
    versions[s1.CUDNN_QUERY_PACKAGE] = "9.0.0.1"
    ctx, _ = _make_ctx(
        tmp_path,
        driver_version=s1.DRIVER_VERSION,
        nvcc_release=s1.CUDA_VERSION,
        gstreamer_version=s1.GSTREAMER_VERSION,
        dpkg_versions=versions,
        mosquitto_active=True,
    )
    monkeypatch.setattr(s1, "_mosquitto_conf_matches_bundled", lambda ctx: True)

    result = s1.Step1Prerequisites().verify(ctx)

    assert result.status is not StepStatus.COMPLETE


# ---------------------------------------------------------------------------
# Mosquitto before/after diff-detection reporting (STEP-1 section 3.2)
# ---------------------------------------------------------------------------


def test_mosquitto_reports_installed_when_broker_absent_and_conf_missing(tmp_path, monkeypatch):
    ctx, runner = _make_ctx(tmp_path, dpkg_versions={})  # mosquitto absent

    def fake_run_bundled_script(*args, **kwargs):
        # Simulate the script's own install: mosquitto now present, conf written.
        runner.dpkg_versions["mosquitto"] = "2.0.18-1"
        bundled_bytes = ctx.asset_path("mosquitto", "mv3dt.conf").read_bytes()
        dst = s1._mosquitto_dst_path()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(bundled_bytes)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(shellout, "run_bundled_script", fake_run_bundled_script)

    step = s1.Step1Prerequisites()
    result = step._run_mosquitto(ctx)

    assert result is None
    assert ctx.installed[0][0] == "mosquitto"
    assert any(name == "mv3dt.conf" for name, _ in ctx.installed)
    assert ctx.already_installed == []


def test_mosquitto_reports_already_installed_when_nothing_changed(tmp_path, monkeypatch):
    ctx, runner = _make_ctx(tmp_path, dpkg_versions={"mosquitto": "2.0.18-1"})

    # Pre-seed the drop-in so it already matches the bundled asset exactly.
    bundled_bytes = ctx.asset_path("mosquitto", "mv3dt.conf").read_bytes()
    dst = s1._mosquitto_dst_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(bundled_bytes)

    def fake_run_bundled_script(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(shellout, "run_bundled_script", fake_run_bundled_script)

    step = s1.Step1Prerequisites()
    result = step._run_mosquitto(ctx)

    assert result is None
    assert ("mosquitto", "2.0.18-1") in ctx.already_installed
    assert any(name == "mv3dt.conf" for name, _ in ctx.already_installed)
    assert ctx.installed == []


def test_mosquitto_reports_installed_conf_when_drop_in_differs_from_bundled(tmp_path, monkeypatch):
    ctx, runner = _make_ctx(tmp_path, dpkg_versions={"mosquitto": "2.0.18-1"})

    # A stale drop-in on disk, different from the bundled asset.
    dst = s1._mosquitto_dst_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"# stale, different content\n")

    def fake_run_bundled_script(*args, **kwargs):
        bundled_bytes = ctx.asset_path("mosquitto", "mv3dt.conf").read_bytes()
        dst.write_bytes(bundled_bytes)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(shellout, "run_bundled_script", fake_run_bundled_script)

    step = s1.Step1Prerequisites()
    result = step._run_mosquitto(ctx)

    assert result is None
    # mosquitto itself was already installed (version unchanged)...
    assert ("mosquitto", "2.0.18-1") in ctx.already_installed
    # ...but the drop-in changed, so it is reported as newly installed.
    assert any(name == "mv3dt.conf" for name, _ in ctx.installed)


def test_mosquitto_failure_returns_failed_step_result(tmp_path, monkeypatch):
    ctx, _ = _make_ctx(tmp_path)

    def fake_run_bundled_script(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")

    monkeypatch.setattr(shellout, "run_bundled_script", fake_run_bundled_script)

    step = s1.Step1Prerequisites()
    result = step._run_mosquitto(ctx)

    assert result is not None
    assert result.status is StepStatus.FAILED


def test_mosquitto_forwards_non_interactive_flag(tmp_path, monkeypatch):
    ctx, _ = _make_ctx(tmp_path)
    ctx.non_interactive = True
    seen_args = {}

    def fake_run_bundled_script(*args, **kwargs):
        seen_args["args"] = kwargs.get("args")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(shellout, "run_bundled_script", fake_run_bundled_script)

    s1.Step1Prerequisites()._run_mosquitto(ctx)

    assert seen_args["args"] == ["--non-interactive"]


def test_mosquitto_omits_non_interactive_flag_when_interactive(tmp_path, monkeypatch):
    ctx, _ = _make_ctx(tmp_path)
    ctx.non_interactive = False
    seen_args = {}

    def fake_run_bundled_script(*args, **kwargs):
        seen_args["args"] = kwargs.get("args")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(shellout, "run_bundled_script", fake_run_bundled_script)

    s1.Step1Prerequisites()._run_mosquitto(ctx)

    assert seen_args["args"] == []


# ---------------------------------------------------------------------------
# Bundled asset presence
# ---------------------------------------------------------------------------


def test_mosquitto_script_and_conf_are_bundled():
    assert shellout.asset_path("scripts", "10_setup_mosquitto.sh").is_file()
    assert shellout.asset_path("mosquitto", "mv3dt.conf").is_file()
