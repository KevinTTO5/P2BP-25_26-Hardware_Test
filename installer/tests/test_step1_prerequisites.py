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
def _console_session(monkeypatch):
    """Every test runs as if launched from a virtual console -- the safe
    case for the caveat-6a guard. Tests for the guard itself override this.
    pytest's own stdin is not a tty, so without this the guard would trip
    in every Launch A test."""
    monkeypatch.setattr(s1, "_controlling_tty", lambda: "/dev/tty3")


@pytest.fixture(autouse=True)
def _safe_system_paths(monkeypatch, tmp_path):
    """Belt and braces: point every real system path this module writes at a
    scratch dir so a bug under test can never reach the real filesystem."""
    monkeypatch.setattr(s1, "CUDA_PROFILE_PATH", tmp_path / "etc-profile.d" / "cuda.sh")
    monkeypatch.setattr(
        s1,
        "NOUVEAU_BLACKLIST_PATH",
        tmp_path / "etc-modprobe.d" / "blacklist-nouveau.conf",
    )
    monkeypatch.setattr(s1, "MOSQUITTO_CONF_DIR", tmp_path / "etc-mosquitto" / "conf.d")
    handoff_root = tmp_path / "var-lib" / "driver-handoff"
    monkeypatch.setattr(s1, "DRIVER_HANDOFF_ROOT", handoff_root)
    monkeypatch.setattr(
        s1, "DRIVER_HANDOFF_WORKER_PATH", handoff_root / "install-driver.sh"
    )
    monkeypatch.setattr(
        s1, "DRIVER_HANDOFF_RUNFILE_PATH", handoff_root / s1.DRIVER_RUN_FILENAME
    )
    monkeypatch.setattr(s1, "DRIVER_HANDOFF_STATUS_PATH", handoff_root / "status")
    monkeypatch.setattr(
        s1, "DRIVER_HANDOFF_LOG_PATH", handoff_root / "driver-install.log"
    )
    monkeypatch.setattr(s1, "DRIVER_HANDOFF_UNIT_DIR", tmp_path / "systemd")
    boot_id_path = tmp_path / "boot-id"
    boot_id_path.write_text("boot-a\n")
    monkeypatch.setattr(s1, "DRIVER_HANDOFF_BOOT_ID_PATH", boot_id_path)


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
        gdm_starts: bool = True,
        nouveau_loaded: bool = False,
        distro_nvidia_packages: tuple = (),
        driver_run_returncode: int = 0,
        driver_download_ok: bool = True,
        driver_download_version: str = s1.DRIVER_VERSION,
        dpkg_versions: Optional[dict] = None,
        driver_version: str = "",
        nvcc_release: str = "",
        gstreamer_version: str = "",
        mosquitto_active: bool = True,
        display_manager_active: bool = True,
        handoff_start_ok: bool = True,
        cudnn_install_result: Optional[str] = s1.CUDNN_APT_VERSION,
        apt_fail_on: Optional[str] = None,
        keyring_install_ok: bool = True,
        cuda_install_provides_nvcc: bool = True,
        kernel_release: str = "6.8.0-generic",
    ) -> None:
        self.calls: list[tuple] = []
        self.os_version = os_version
        self.arch = arch
        self.gpu_present = gpu_present
        self.secure_boot_enabled = secure_boot_enabled
        self.gdm_stops = gdm_stops
        self.gdm_starts = gdm_starts
        self.nouveau_loaded = nouveau_loaded
        self.distro_nvidia_packages = list(distro_nvidia_packages)
        self.driver_run_returncode = driver_run_returncode
        self.driver_download_ok = driver_download_ok
        self.driver_download_version = driver_download_version
        self.dpkg_versions: dict[str, str] = dict(dpkg_versions or {})
        self.driver_version = driver_version
        self.nvcc_release = nvcc_release
        self.gstreamer_version = gstreamer_version
        self.mosquitto_active = mosquitto_active
        self.display_manager_active = display_manager_active
        self.handoff_start_ok = handoff_start_ok
        self.cudnn_install_result = cudnn_install_result
        self.apt_fail_on = apt_fail_on
        self.keyring_install_ok = keyring_install_ok
        self.cuda_install_provides_nvcc = cuda_install_provides_nvcc
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
                # Real `dpkg-query -W -f='${Status}|${Package}'` output. An
                # entry may be a bare name (installed) or an explicit
                # (name, status) pair.
                lines = []
                for entry in self.distro_nvidia_packages:
                    if isinstance(entry, tuple):
                        package, status = entry
                    else:
                        package, status = entry, "install ok installed"
                    lines.append(f"{status}|{package}")
                return _ok(args, "\n".join(lines))
            if "cuda-keyring.deb" in script:
                return _rc(args, 0 if self.keyring_install_ok else 1)
            return _ok(args)
        if cmd == "mokutil":
            state = "enabled" if self.secure_boot_enabled else "disabled"
            return _ok(args, f"SecureBoot {state}")
        if cmd == "service":
            return _rc(args, 0 if self.gdm_stops else 1)
        if cmd == "systemctl" and args[1] == "stop":
            return _rc(args, 0 if self.gdm_stops else 1)
        if cmd == "systemctl" and args[1] == "start" and "--no-block" in args:
            return _rc(args, 0 if self.handoff_start_ok else 1)
        if cmd == "systemctl" and args[1] == "start":
            return _rc(args, 0 if self.gdm_starts else 1)
        if cmd == "pkill":
            return _rc(args, 0)
        if cmd == "dpkg-query":
            pkg = args[3]
            version = self.dpkg_versions.get(pkg)
            return _ok(args, version) if version else _rc(args, 1)
        if cmd == "apt-get":
            if self.apt_fail_on and any(self.apt_fail_on in tok for tok in args):
                return _rc(args, 100)
            if args[1] == "install":
                for tok in args[4:]:
                    pkg = tok.split("=")[0]
                    if pkg in s1.CUDNN_PACKAGES and self.cudnn_install_result:
                        version = self.cudnn_install_result
                    else:
                        version = (
                            tok.split("=")[1]
                            if "=" in tok
                            else self.dpkg_versions.get(pkg, "1.0")
                        )
                    if (
                        pkg == s1.CUDA_TOOLKIT_PACKAGE
                        and self.cuda_install_provides_nvcc
                    ):
                        self.nvcc_release = s1.CUDA_VERSION
                    self.dpkg_versions[pkg] = version
            return _ok(args)
        if cmd == "update-initramfs":
            return _ok(args)
        if cmd == "curl":
            # Mirror real curl: write the body to the `-o` target on success,
            # leave nothing behind on failure.
            dest = pathlib.Path(args[args.index("-o") + 1])
            if not self.driver_download_ok:
                return _rc(args, 8)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(_runfile_bytes(self.driver_download_version))
            return _ok(args)
        if cmd == "nvidia-smi":
            if any("driver_version" in a for a in args):
                return (
                    _ok(args, self.driver_version)
                    if self.driver_version
                    else _rc(args, 1)
                )
            return _ok(args, "RTX PRO 4500 Blackwell, 8.9")
        if cmd == s1.CUDA_NVCC_PATH:
            if not self.nvcc_release:
                return _rc(args, 1)
            return _ok(
                args,
                f"Cuda compilation tools, release {self.nvcc_release}, V{self.nvcc_release}.100",
            )
        if cmd == "gst-inspect-1.0":
            if not self.gstreamer_version:
                return _rc(args, 1)
            return _ok(args, f"gst-inspect-1.0 version {self.gstreamer_version}")
        if cmd == "systemctl":
            unit = args[-1]
            if unit in s1.DISPLAY_MANAGER_UNITS:
                return _rc(args, 0 if self.display_manager_active else 1)
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
        # Doc 08 section 3.2: the dispatch loop announces every step it
        # enters through `ctx.progress`, and one test below feeds this
        # context to the real `app._dispatch()`. Nothing here asserts on
        # rendering, so the handle only has to exist and swallow the calls.
        self.progress = SimpleNamespace(
            begin_step=lambda step, index: None,
            end_step=lambda: None,
            phase=lambda number: None,
            task=lambda name: None,
            bytes=lambda done, total: None,
            line=lambda text: None,
        )

    def report_installed(self, dependency: str, version: str) -> None:
        self.installed.append((dependency, version))

    def report_already_installed(self, dependency: str, version: str) -> None:
        self.already_installed.append((dependency, version))

    def verify_pinned(self, label: str, actual: str, expected: str) -> bool:
        return actual == expected

    def run_root(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return self.runner(*args, **kwargs)

    def run_as_user(
        self, *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess:  # pragma: no cover
        return self.runner(*args, **kwargs)


def _make_ctx(
    tmp_path: pathlib.Path, **runner_kwargs: Any
) -> tuple[FakeContext, FakeRunner]:
    runner = FakeRunner(**runner_kwargs)
    ctx = FakeContext(install_dir=tmp_path / "opt" / "mv3dt", runner=runner)
    ctx.install_dir.mkdir(parents=True, exist_ok=True)
    return ctx, runner


def _runfile_bytes(version: str = s1.DRIVER_VERSION) -> bytes:
    """A stand-in for the self-extracting runfile header `_verify_driver_run`
    reads -- same shape as the real one, which stamps the build into a
    comment line near the top of the archive."""
    return (
        b"#!/bin/sh\n"
        b"#  NVIDIA Accelerated Graphics Driver for Linux-x86_64 "
        + version.encode()
        + b"\nskip=1234\n"
    )


def _stage_driver_run(
    ctx: FakeContext, version: str = s1.DRIVER_VERSION
) -> pathlib.Path:
    path = s1._driver_run_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_runfile_bytes(version))
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


def test_launch_a_returns_user_action_required_early_when_nouveau_cleanup_needed(
    tmp_path,
):
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


def test_launch_a_returns_user_action_required_early_when_distro_driver_purged(
    tmp_path,
):
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
    ctx, runner = _make_ctx(tmp_path, driver_run_returncode=1)
    _stage_driver_run(ctx)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.FAILED
    assert "desktop restore succeeded" in result.message
    assert any(c[:2] == ("systemctl", "start") for c in runner.calls)


def test_sync_runfile_failure_reports_when_desktop_restore_fails(tmp_path):
    ctx, _ = _make_ctx(tmp_path, driver_run_returncode=1, gdm_starts=False)
    _stage_driver_run(ctx)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.FAILED
    assert "desktop restore failed" in result.message
    assert result.user_actions[0].command == "sudo systemctl start gdm3"


# ---------------------------------------------------------------------------
# Regression: a confirmed reboot must not let the *real* merged
# reboot.reconcile()/app._dispatch() skip Launch B (review PR #44 finding 1).
# ---------------------------------------------------------------------------


def test_confirmed_reboot_still_lets_launch_b_run_on_next_dispatch(
    tmp_path, monkeypatch
):
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


def test_driver_download_url_is_derived_from_the_pinned_version():
    """A pin bump must not leave the URL pointing at the old build."""
    assert s1.DRIVER_VERSION in s1.DRIVER_DOWNLOAD_URL
    assert s1.DRIVER_DOWNLOAD_URL.endswith(s1.DRIVER_RUN_FILENAME)
    assert s1.DRIVER_DOWNLOAD_URL.startswith("https://")


def test_launch_a_downloads_the_driver_when_not_staged(tmp_path):
    ctx, runner = _make_ctx(tmp_path)
    # deliberately do not stage the .run file

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    curl_calls = [c for c in runner.calls if c[0] == "curl"]
    assert len(curl_calls) == 1
    assert s1.DRIVER_DOWNLOAD_URL in curl_calls[0]
    # Landed at the canonical path, with no .part left behind.
    run_path = s1._driver_run_path(ctx)
    assert run_path.is_file()
    assert not run_path.with_suffix(run_path.suffix + ".part").exists()
    # And the step proceeded all the way to the reboot gate.
    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "reboot" in result.message.lower()
    assert (s1.DRIVER_RUN_FILENAME, s1.DRIVER_VERSION) in ctx.installed


def test_launch_a_does_not_redownload_an_already_staged_driver(tmp_path):
    ctx, runner = _make_ctx(tmp_path)
    _stage_driver_run(ctx)

    s1.Step1Prerequisites()._run_launch_a(ctx)

    assert [c for c in runner.calls if c[0] == "curl"] == []


def test_launch_a_user_action_when_the_download_fails(tmp_path):
    ctx, _ = _make_ctx(tmp_path, driver_download_ok=False)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "could not obtain" in result.message
    assert len(result.user_actions) == 1
    action = result.user_actions[0]
    assert s1.DRIVER_RUN_FILENAME in action.path
    assert "nvidia.com" in (action.command or "")
    # A failed fetch leaves nothing a later launch could mistake for good.
    run_path = s1._driver_run_path(ctx)
    assert not run_path.exists()
    assert not run_path.with_suffix(run_path.suffix + ".part").exists()


def test_launch_a_rejects_a_download_of_the_wrong_driver_version(tmp_path):
    """A mirror redirect or withdrawn build must never reach the .run
    invocation -- DS 9.1 pins driver equality (STEP-1 section 2)."""
    ctx, runner = _make_ctx(tmp_path, driver_download_version="580.10.01")

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "580.10.01" in result.message
    assert s1.DRIVER_VERSION in result.message
    assert not s1._driver_run_path(ctx).exists()
    assert not any(str(c[0]).endswith(".run") for c in runner.calls)


def test_launch_a_replaces_a_staged_driver_of_the_wrong_version(tmp_path):
    ctx, runner = _make_ctx(tmp_path)
    _stage_driver_run(ctx, version="580.10.01")

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    # Rejected the stale file, fetched the pinned one, and carried on.
    assert len([c for c in runner.calls if c[0] == "curl"]) == 1
    assert (
        s1._driver_run_embedded_version(s1._driver_run_path(ctx)) == s1.DRIVER_VERSION
    )
    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "reboot" in result.message.lower()


def test_verify_driver_run_rejects_a_truncated_or_html_body(tmp_path):
    path = tmp_path / "bad.run"
    path.write_bytes(b"<html><head><title>404 Not Found</title></head></html>")

    assert "not a recognisable NVIDIA runfile" in (s1._verify_driver_run(path) or "")


def test_verify_driver_run_enforces_the_sha256_pin_when_set(tmp_path, monkeypatch):
    path = tmp_path / "good.run"
    path.write_bytes(_runfile_bytes())

    assert s1._verify_driver_run(path) is None  # unpinned: version check only

    monkeypatch.setattr(s1, "DRIVER_RUN_SHA256", "0" * 64)
    assert "SHA256 mismatch" in (s1._verify_driver_run(path) or "")

    monkeypatch.setattr(s1, "DRIVER_RUN_SHA256", s1._sha256_file(path))
    assert s1._verify_driver_run(path) is None


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


def test_apt_install_reported_reports_already_installed_when_version_unchanged(
    tmp_path,
):
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


def test_apt_install_reported_reports_upgrade_as_installed(tmp_path):
    package = s1.TENSORRT_PACKAGES[0]
    ctx, _ = _make_ctx(tmp_path, dpkg_versions={package: "10.0.0"})

    result = s1._apt_install_reported(
        ctx,
        [package],
        apt_args=[f"{package}={s1.TENSORRT_VERSION}"],
    )

    assert result is None
    assert (package, s1.TENSORRT_VERSION) in ctx.installed
    assert ctx.already_installed == []


def test_launch_a_apt_failure_stops_before_cuda_and_does_not_report(tmp_path):
    ctx, runner = _make_ctx(tmp_path, apt_fail_on="build-essential")

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.FAILED
    assert not any("cuda-keyring.deb" in " ".join(call) for call in runner.calls)
    assert ctx.installed == []
    assert ctx.already_installed == []


def test_launch_a_cuda_keyring_failure_stops_before_cleanup(tmp_path):
    ctx, runner = _make_ctx(tmp_path, keyring_install_ok=False)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.FAILED
    assert "keyring" in result.message
    assert not any(call[0] == "update-initramfs" for call in runner.calls)
    assert not any(str(call[0]).endswith(".run") for call in runner.calls)


def test_launch_a_nvidia_purge_failure_is_failed(tmp_path):
    ctx, _ = _make_ctx(
        tmp_path,
        distro_nvidia_packages=("nvidia-driver-550",),
        apt_fail_on="nvidia-driver-550",
    )

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.FAILED
    assert "apt purge failed" in result.message


def test_launch_b_pins_cudnn_version_at_apt_install(tmp_path):
    """CUDA 13 cuDNN uses concrete packages at the exact apt version."""
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
    cudnn_call = next(
        call for call in install_calls if s1.CUDNN_QUERY_PACKAGE in " ".join(call)
    )
    assert all(
        f"{pkg}={s1.CUDNN_APT_VERSION}" in cudnn_call for pkg in s1.CUDNN_PACKAGES
    )
    assert not any("*" in arg for arg in cudnn_call)
    assert all((pkg, s1.CUDNN_VERSION) in ctx.installed for pkg in s1.CUDNN_PACKAGES)


def test_launch_b_recovers_missing_cuda_and_writes_profile(tmp_path):
    ctx, runner = _make_ctx(
        tmp_path,
        driver_version=s1.DRIVER_VERSION,
        nvcc_release="",
        gstreamer_version=s1.GSTREAMER_VERSION,
        mosquitto_active=True,
    )
    restore = _patch_mosquitto_success(ctx)
    try:
        result = s1.Step1Prerequisites()._run_launch_b(ctx)
    finally:
        restore()

    assert result.status is StepStatus.COMPLETE
    assert any(
        call[:2] == ("apt-get", "install") and s1.CUDA_TOOLKIT_PACKAGE in call
        for call in runner.calls
    )
    assert s1.CUDA_HOME in s1.CUDA_PROFILE_PATH.read_text()


def test_launch_b_cuda_install_failure_stops_before_tensorrt(tmp_path):
    ctx, runner = _make_ctx(
        tmp_path,
        driver_version=s1.DRIVER_VERSION,
        nvcc_release="",
        apt_fail_on=s1.CUDA_TOOLKIT_PACKAGE,
    )

    result = s1.Step1Prerequisites()._run_launch_b(ctx)

    assert result.status is StepStatus.FAILED
    assert not any(s1.TENSORRT_PACKAGES[0] in call for call in runner.calls)
    assert ctx.installed == []


def test_launch_b_cudnn_failure_stops_without_reporting_or_mosquitto(
    tmp_path, monkeypatch
):
    ctx, _ = _make_ctx(
        tmp_path,
        driver_version=s1.DRIVER_VERSION,
        nvcc_release=s1.CUDA_VERSION,
        apt_fail_on=s1.CUDNN_QUERY_PACKAGE,
    )
    mosquitto_called = False

    def fake_run_bundled_script(*args, **kwargs):
        nonlocal mosquitto_called
        mosquitto_called = True
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(shellout, "run_bundled_script", fake_run_bundled_script)

    result = s1.Step1Prerequisites()._run_launch_b(ctx)

    assert result.status is StepStatus.FAILED
    assert not mosquitto_called
    assert not any(pkg in s1.CUDNN_PACKAGES for pkg, _ in ctx.installed)
    assert all(version != "unknown" for _, version in ctx.installed)


def test_launch_b_rejects_wrong_cudnn_revision_after_apt(tmp_path):
    ctx, _ = _make_ctx(
        tmp_path,
        driver_version=s1.DRIVER_VERSION,
        nvcc_release=s1.CUDA_VERSION,
        cudnn_install_result=f"{s1.CUDNN_VERSION}-2",
    )

    result = s1.Step1Prerequisites()._run_launch_b(ctx)

    assert result.status is StepStatus.FAILED
    assert s1.CUDNN_APT_VERSION in result.message
    assert not any(pkg in s1.CUDNN_PACKAGES for pkg, _ in ctx.installed)


def test_verify_probes_nvcc_at_pinned_absolute_path(tmp_path, monkeypatch):
    ctx, runner = _make_ctx(
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
    assert any(call[0] == s1.CUDA_NVCC_PATH for call in runner.calls)


# ---------------------------------------------------------------------------
# verify() -- pinned checklist (STEP-1 section 7.3)
# ---------------------------------------------------------------------------


def _fully_pinned_versions() -> dict:
    return {
        "libnvinfer10": s1.TENSORRT_VERSION,
        s1.CUDNN_QUERY_PACKAGE: s1.CUDNN_APT_VERSION,
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


def test_cudnn_probe_normalizes_only_the_exact_apt_revision(tmp_path):
    ctx, runner = _make_ctx(
        tmp_path,
        dpkg_versions={s1.CUDNN_QUERY_PACKAGE: s1.CUDNN_APT_VERSION},
    )

    assert s1._cudnn_installed_version(ctx) == s1.CUDNN_VERSION

    runner.dpkg_versions[s1.CUDNN_QUERY_PACKAGE] = f"{s1.CUDNN_VERSION}-2"
    assert s1._cudnn_installed_version(ctx) == f"{s1.CUDNN_VERSION}-2"


def test_verify_rejects_wrong_cudnn_debian_revision(tmp_path, monkeypatch):
    versions = _fully_pinned_versions()
    versions[s1.CUDNN_QUERY_PACKAGE] = f"{s1.CUDNN_VERSION}-2"
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

    assert result.status is StepStatus.USER_ACTION_REQUIRED


def test_verify_user_action_required_when_driver_version_mismatches(
    tmp_path, monkeypatch
):
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


def test_mosquitto_reports_installed_when_broker_absent_and_conf_missing(
    tmp_path, monkeypatch
):
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


def test_mosquitto_reports_already_installed_when_nothing_changed(
    tmp_path, monkeypatch
):
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


def test_mosquitto_reports_installed_conf_when_drop_in_differs_from_bundled(
    tmp_path, monkeypatch
):
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


def test_mosquitto_uses_non_interactive_child_when_parent_is_interactive(
    tmp_path, monkeypatch
):
    ctx, _ = _make_ctx(tmp_path)
    ctx.non_interactive = False
    seen_args = {}

    def fake_run_bundled_script(*args, **kwargs):
        seen_args["args"] = kwargs.get("args")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(shellout, "run_bundled_script", fake_run_bundled_script)

    s1.Step1Prerequisites()._run_mosquitto(ctx)

    assert seen_args["args"] == ["--non-interactive"]


# ---------------------------------------------------------------------------
# Bundled asset presence
# ---------------------------------------------------------------------------


def test_mosquitto_script_and_conf_are_bundled():
    assert shellout.asset_path("scripts", "10_setup_mosquitto.sh").is_file()
    assert shellout.asset_path("mosquitto", "mv3dt.conf").is_file()


# ---------------------------------------------------------------------------
# Distro NVIDIA purge probe (STEP-1 section 4, caveat 3)
# ---------------------------------------------------------------------------


# Exactly what a stock Ubuntu 24.04 desktop reports: names dpkg knows about
# as dependency references, none of them actually installed. Treating these
# as a purge made Step 1 demand a reboot on every launch, forever.
_NOT_INSTALLED_ON_STOCK_UBUNTU = (
    ("libnvidia-encode1", "unknown ok not-installed"),
    ("nvidia-common", "unknown ok not-installed"),
    ("nvidia-libopencl1-dev", "unknown ok not-installed"),
    ("nvidia-prime", "unknown ok not-installed"),
)


def test_purge_ignores_packages_dpkg_only_knows_the_name_of(tmp_path):
    ctx, runner = _make_ctx(
        tmp_path, distro_nvidia_packages=_NOT_INSTALLED_ON_STOCK_UBUNTU
    )

    assert s1._purge_distro_nvidia_packages(ctx) == (False, None)
    assert [c for c in runner.calls if c[0] == "apt-get" and c[1] == "purge"] == []


def test_purge_removes_installed_and_config_files_packages(tmp_path):
    ctx, runner = _make_ctx(
        tmp_path,
        distro_nvidia_packages=(
            ("nvidia-driver-550", "install ok installed"),
            ("nvidia-prime", "unknown ok not-installed"),
            ("libnvidia-gl-550", "deinstall ok config-files"),
        ),
    )

    assert s1._purge_distro_nvidia_packages(ctx) == (True, None)
    purge = [c for c in runner.calls if c[0] == "apt-get" and c[1] == "purge"][0]
    assert "nvidia-driver-550" in purge
    assert "libnvidia-gl-550" in purge  # a real leftover purging does clear
    assert "nvidia-prime" not in purge  # never installed


def test_launch_a_does_not_loop_on_a_clean_machine(tmp_path):
    """The regression: nouveau gone, blacklist already written, only
    not-installed names in dpkg. Step 1 must fall through the cleanup gate
    to the driver rather than asking for a reboot again."""
    ctx, _ = _make_ctx(
        tmp_path,
        nouveau_loaded=False,
        distro_nvidia_packages=_NOT_INSTALLED_ON_STOCK_UBUNTU,
    )

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    # Not the cleanup gate's message: it reached the driver .run and the
    # post-install reboot instead.
    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "nouveau" not in result.message.lower()
    assert "kernel module is installed but not yet loaded" in result.message


# ---------------------------------------------------------------------------
# Fetch tooling (STEP-1 section 4, caveat 2)
# ---------------------------------------------------------------------------


def test_every_fetch_uses_a_tool_step_1_actually_installs(tmp_path):
    """Step 1 downloads before it can assume anything beyond a base Ubuntu
    image, so it may only shell out to fetch tools it installs itself.
    `curl` is in BASE_TOOLING_PACKAGES; `wget` is not, and a minimal or
    server image need not ship it -- a wget call there fails as exit 127,
    which reads like a network fault rather than a missing binary.
    """
    ctx, runner = _make_ctx(tmp_path)

    s1.Step1Prerequisites()._run_launch_a(ctx)

    fetchers = {c[0] for c in runner.calls if c[0] in ("curl", "wget")}
    # Anything embedded in a `bash -c` fragment counts too.
    for call in runner.calls:
        if call[0] == "bash":
            script = call[2]
            for tool in ("curl", "wget"):
                if f"{tool} " in script:
                    fetchers.add(tool)

    assert "curl" in fetchers, "expected Step 1 to fetch with curl"
    assert "wget" not in fetchers, (
        "Step 1 shelled out to wget, which it never installs; use curl "
        "(BASE_TOOLING_PACKAGES) or add wget to that list"
    )


def test_display_manager_stop_warns_before_the_screen_goes_black(tmp_path, monkeypatch):
    """The black screen must never be the first the operator hears of it."""
    seen: list[str] = []
    monkeypatch.setattr(
        s1.waitui,
        "countdown",
        lambda *a, **kw: seen.append(kw.get("description", "")),
    )
    ctx, _ = _make_ctx(tmp_path)

    s1._stop_display_manager(ctx)

    assert len(seen) == 1
    assert "screen will go black" in seen[0]
    assert "do NOT power off" in seen[0]


# ---------------------------------------------------------------------------
# Caveat 6a -- refusing to kill the session the installer runs in
# ---------------------------------------------------------------------------


def test_driver_run_is_invoked_silently(tmp_path):
    """The runfile is interactive by default and run_root captures its
    output, so a question would block forever, invisibly."""
    ctx, runner = _make_ctx(tmp_path)
    _stage_driver_run(ctx)

    s1.Step1Prerequisites()._run_launch_a(ctx)

    run_calls = [c for c in runner.calls if str(c[0]).endswith(".run")]
    assert len(run_calls) == 1
    assert "--silent" in run_calls[0]
    assert "--no-cc-version-check" in run_calls[0]


def test_guard_allows_a_virtual_console(tmp_path, monkeypatch):
    monkeypatch.setattr(s1, "_controlling_tty", lambda: "/dev/tty3")
    ctx, _ = _make_ctx(tmp_path, display_manager_active=True)

    assert s1._session_hazard(ctx) is None


def test_guard_allows_an_ssh_session(tmp_path, monkeypatch):
    """Stopping gdm cannot disturb an SSH session, so a pts under sshd is
    safe. sudo strips SSH_CONNECTION, hence the /proc ancestry walk."""
    monkeypatch.setattr(s1, "_controlling_tty", lambda: "/dev/pts/1")
    monkeypatch.setattr(s1, "_has_sshd_ancestor", lambda: True)
    ctx, _ = _make_ctx(tmp_path, display_manager_active=True)

    assert s1._session_hazard(ctx) is None


def test_guard_allows_a_desktop_terminal_when_no_display_manager_runs(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(s1, "_controlling_tty", lambda: "/dev/pts/1")
    monkeypatch.setattr(s1, "_has_sshd_ancestor", lambda: False)
    ctx, _ = _make_ctx(tmp_path, display_manager_active=False)

    assert s1._session_hazard(ctx) is None


def test_guard_blocks_a_desktop_terminal_emulator(tmp_path, monkeypatch):
    monkeypatch.setattr(s1, "_controlling_tty", lambda: "/dev/pts/1")
    monkeypatch.setattr(s1, "_has_sshd_ancestor", lambda: False)
    ctx, _ = _make_ctx(tmp_path, display_manager_active=True)

    assert s1._session_hazard(ctx) is not None


def test_launch_a_hands_desktop_install_to_systemd(tmp_path, monkeypatch):
    """A desktop terminal exits before systemd owns the disruptive work."""
    monkeypatch.setattr(s1, "_controlling_tty", lambda: "/dev/pts/1")
    monkeypatch.setattr(s1, "_has_sshd_ancestor", lambda: False)
    ctx, runner = _make_ctx(tmp_path, display_manager_active=True)
    _stage_driver_run(ctx)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "handed to systemd" in result.message
    assert "automatically" in result.message
    assert s1.DRIVER_HANDOFF_STATUS_PATH.read_text().strip() == "scheduled"
    assert s1.DRIVER_HANDOFF_WORKER_PATH.is_file()
    assert s1._verify_driver_run(s1.DRIVER_HANDOFF_RUNFILE_PATH) is None
    assert (s1.DRIVER_HANDOFF_UNIT_DIR / s1.DRIVER_HANDOFF_UNIT_NAME).is_file()
    assert any(c[:3] == ("systemctl", "start", "--no-block") for c in runner.calls)
    # The foreground process never tears down the desktop or runs the file.
    assert not [c for c in runner.calls if c[0] == "pkill"]
    assert not [c for c in runner.calls if str(c[0]).endswith(".run")]


def test_launch_a_reports_a_systemd_schedule_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(s1, "_controlling_tty", lambda: "/dev/pts/1")
    monkeypatch.setattr(s1, "_has_sshd_ancestor", lambda: False)
    ctx, _ = _make_ctx(tmp_path, display_manager_active=True, handoff_start_ok=False)
    _stage_driver_run(ctx)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.FAILED
    assert "systemd could not start" in result.message
    assert s1.DRIVER_HANDOFF_STATUS_PATH.read_text().startswith("failed:systemd-start:")


@pytest.mark.parametrize("state", ["scheduled", "running"])
def test_launch_a_does_not_duplicate_an_active_handoff(tmp_path, state):
    ctx, runner = _make_ctx(tmp_path)
    s1.DRIVER_HANDOFF_STATUS_PATH.parent.mkdir(parents=True)
    s1.DRIVER_HANDOFF_STATUS_PATH.write_text(f"{state}\n")

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "still running" in result.message
    assert not runner.calls


def test_launch_a_surfaces_a_failed_handoff_without_retrying(tmp_path):
    ctx, runner = _make_ctx(tmp_path)
    s1.DRIVER_HANDOFF_STATUS_PATH.parent.mkdir(parents=True)
    s1.DRIVER_HANDOFF_STATUS_PATH.write_text("failed:runfile:1\n")

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "failed:runfile:1" in result.message
    assert result.user_actions[0].path == str(s1.DRIVER_HANDOFF_LOG_PATH)
    assert not runner.calls


def test_launch_a_keeps_a_successful_handoff_at_the_reboot_gate(tmp_path):
    ctx, runner = _make_ctx(tmp_path)
    s1.DRIVER_HANDOFF_STATUS_PATH.parent.mkdir(parents=True)
    s1.DRIVER_HANDOFF_STATUS_PATH.write_text("succeeded:boot-a\n")

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "waiting for reboot" in result.message
    assert result.user_actions[-1].command == "sudo reboot"
    assert not runner.calls


def test_launch_a_reports_driver_not_loaded_after_handoff_reboot(tmp_path):
    ctx, runner = _make_ctx(tmp_path)
    s1.DRIVER_HANDOFF_STATUS_PATH.parent.mkdir(parents=True)
    s1.DRIVER_HANDOFF_STATUS_PATH.write_text("succeeded:boot-before\n")

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert "did not load after reboot" in result.message
    assert "Secure Boot" in result.user_actions[0].text
    assert not runner.calls


def test_sshd_ancestor_walk_handles_a_comm_containing_parens(monkeypatch, tmp_path):
    """/proc/<pid>/stat's comm field is parenthesised and may itself contain
    spaces and parens, so the parser must not split on them."""
    proc = tmp_path / "proc"
    (proc / "10").mkdir(parents=True)
    (proc / "10" / "stat").write_text("10 (weird (name) here) S 1 10 10 0 -1 0")

    monkeypatch.setattr(
        s1.pathlib, "Path", lambda p: proc / str(p).replace("/proc/", "")
    )
    assert s1._has_sshd_ancestor(10) is False


def test_controlling_tty_falls_back_past_a_redirected_stdin(monkeypatch):
    """A closed or redirected stdin must not look like "no console": the
    guard would then refuse an operator sitting on a real tty."""

    def fake_ttyname(fd):
        if fd == 0:
            raise OSError("not a tty")
        return "/dev/tty3"

    monkeypatch.setattr(s1.os, "ttyname", fake_ttyname)
    assert s1._controlling_tty() == "/dev/tty3"


def test_guard_can_be_overridden_by_the_environment(tmp_path, monkeypatch):
    """The guard infers the session; a wrong inference must never be what
    makes the install impossible."""
    monkeypatch.setattr(s1, "_controlling_tty", lambda: "/dev/pts/1")
    monkeypatch.setattr(s1, "_has_sshd_ancestor", lambda: False)
    ctx, _ = _make_ctx(tmp_path, display_manager_active=True)

    assert s1._session_hazard(ctx) is not None

    monkeypatch.setenv(s1.ALLOW_DISPLAY_STOP_ENV, "1")
    assert s1._session_hazard(ctx) is None


def test_ubuntu_2404_display_manager_unit_is_tried():
    """Ubuntu 24.04 ships gdm3. Looking only for `gdm`/`lightdm` found no
    display manager on any real workstation."""
    assert "gdm3" in s1.DISPLAY_MANAGER_UNITS
    assert s1.DISPLAY_MANAGER_UNITS[0] == "gdm3"


def test_stopping_succeeds_when_no_display_manager_is_running(tmp_path):
    """The regression: on a console with no DM active, Step 1 reported
    "could not stop the desktop session" and told the operator to switch to
    a console they were already sitting on."""
    ctx, runner = _make_ctx(tmp_path, display_manager_active=False)

    assert s1._stop_display_manager(ctx) is True
    assert not [c for c in runner.calls if c[0] == "systemctl" and c[1] == "stop"]


def test_stopping_stops_the_active_display_manager(tmp_path):
    ctx, runner = _make_ctx(tmp_path, display_manager_active=True)

    assert s1._stop_display_manager(ctx) is True
    stops = [c for c in runner.calls if c[0] == "systemctl" and c[1] == "stop"]
    assert stops and stops[0][2] in s1.DISPLAY_MANAGER_UNITS


def test_stopping_fails_only_when_an_active_manager_will_not_stop(tmp_path):
    ctx, _ = _make_ctx(tmp_path, display_manager_active=True, gdm_stops=False)

    assert s1._stop_display_manager(ctx) is False


def test_handoff_message_names_what_it_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(s1, "_controlling_tty", lambda: "/dev/pts/1")
    monkeypatch.setattr(s1, "_has_sshd_ancestor", lambda: False)
    ctx, _ = _make_ctx(tmp_path, display_manager_active=True)
    _stage_driver_run(ctx)

    result = s1.Step1Prerequisites()._run_launch_a(ctx)

    assert "/dev/pts/1" in result.message
    assert "gdm3" in result.message
    assert result.user_actions[0].path == str(s1.DRIVER_HANDOFF_LOG_PATH)


def test_refusal_says_so_when_no_terminal_was_detected(tmp_path, monkeypatch):
    """The case actually hit on the workstation: the guard could not read a
    controlling terminal at all, and said nothing about it."""
    monkeypatch.setattr(s1, "_controlling_tty", lambda: None)
    monkeypatch.setattr(s1, "_has_sshd_ancestor", lambda: False)
    ctx, _ = _make_ctx(tmp_path, display_manager_active=True)

    assert "none detected" in (s1._session_hazard(ctx) or "")
