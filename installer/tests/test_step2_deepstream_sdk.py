"""Tests for mv3dt_installer.steps.step2_deepstream_sdk
(installer/plan/STEP-2-DEEPSTREAM-SDK.md).

Run from installer/: `python3 -m pytest tests/test_step2_deepstream_sdk.py -v`

No test spawns a real apt/dpkg/curl/docker/deepstream-app process or opens a
socket -- every `ctx.run_root`/`ctx.run_as_user` call is served by a
`ScriptedRunner` fake, and every fixed filesystem path the module reads
(`DS_SDK_DIR`, `DS_SDK_SYMLINK`, `PROFILE_D_PATH`) is monkeypatched at a
`tmp_path`, the same discipline `test_systemd.py` uses for `UNIT_DIR`.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mv3dt_installer import config as config_mod  # noqa: E402
from mv3dt_installer import logs, report  # noqa: E402
from mv3dt_installer.steps import StepStatus  # noqa: E402
from mv3dt_installer.steps import step2_deepstream_sdk as step2  # noqa: E402

PINS_OK = {
    "nvidia-smi": ("595.58.03\n", 0),
}


@pytest.fixture(autouse=True)
def _reset_transcript_state():
    logs._transcript_path = None
    yield
    logs._transcript_path = None


@pytest.fixture(autouse=True)
def _force_no_colour(monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)


@pytest.fixture(autouse=True)
def _sdk_paths(tmp_path, monkeypatch):
    """Point every fixed filesystem constant at a scratch tree so a bug in
    the code under test can never touch /opt/nvidia or /etc/profile.d."""
    sdk_dir = tmp_path / "opt" / "deepstream-9.1"
    symlink = tmp_path / "opt" / "deepstream"
    profile = tmp_path / "profile.d" / "deepstream.sh"
    monkeypatch.setattr(step2, "DS_SDK_DIR", sdk_dir)
    monkeypatch.setattr(step2, "DS_SDK_SYMLINK", symlink)
    monkeypatch.setattr(step2, "PROFILE_D_PATH", profile)
    return sdk_dir, symlink, profile


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class ScriptedRunner:
    """Stand-in for `ctx.run_root`/`ctx.run_as_user`. Records every call as
    the `args` tuple it was invoked with, and resolves a canned
    `CompletedProcess` by the first matching registered rule, else a
    configurable default."""

    def __init__(self, *, default_returncode: int = 1, default_stdout: str = "", default_stderr: str = ""):
        # Fails ("not found"/"not installed") by default: every detection
        # probe in this module (dpkg -s, docker image inspect, docker
        # info, ...) treats a nonzero exit as "absent", so an
        # unconfigured runner reads as "nothing is installed yet" rather
        # than accidentally satisfying an idempotent-reinstall branch a
        # test didn't ask for. Tests that need an action (curl, apt-get,
        # docker pull/login) to succeed register that explicitly.
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
        # Most-recently-added rule wins: lets a test start from a shared
        # "everything passes" runner (e.g. `_passing_pin_runner()`) and
        # override one probe's answer without needing to rebuild the whole
        # rule set.
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
    home = pathlib.Path("/home/op")
    uid = 1000
    gid = 1000


class FakeNgc:
    def __init__(self, key="a-fake-ngc-key"):
        self._key = key
        self.configure_calls = 0

    def load_key(self):
        return self._key

    def configure_ngc_cli(self):
        self.configure_calls += 1
        return pathlib.Path("/home/op/.ngc/config")


class FakeContext:
    def __init__(self, tmp_path, *, conf=None, ngc=None, runner_root=None, runner_user=None):
        self.install_dir = tmp_path / "mv3dt"
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self.conf = conf if conf is not None else {}
        self.user = FakeUser()
        self.log = logs.log
        self.report_installed = report.report_installed
        self.report_already_installed = report.report_already_installed
        self.verify_pinned = report.verify_pinned
        self.ngc = ngc if ngc is not None else FakeNgc()
        self.runner_root = runner_root if runner_root is not None else ScriptedRunner()
        self.runner_user = runner_user if runner_user is not None else ScriptedRunner()

    def run_root(self, *args, **kwargs):
        return self.runner_root(*args, **kwargs)

    def run_as_user(self, *args, **kwargs):
        return self.runner_user(*args, **kwargs)


def _passing_pin_runner() -> ScriptedRunner:
    """A `run_root` double that answers every doc section 2 prereq probe
    with the pinned value."""
    runner = ScriptedRunner()
    runner.when(lambda a: a[:1] == ("nvidia-smi",), stdout="595.58.03\n")
    runner.when(lambda a: a[:1] == ("nvcc",), stdout="Cuda compilation tools, release 13.2, V13.2.1\n")
    runner.when(
        lambda a: a[:2] == ("dpkg-query", "-W") and a[-1] == "libcudnn9*",
        stdout="9.20.0.48\n",
    )
    runner.when(
        lambda a: a[:2] == ("dpkg-query", "-W") and a[-1] == "libnvinfer10",
        stdout="10.16.0.72-1+cuda13.2\n",
    )
    runner.when(lambda a: a[:1] == ("gst-inspect-1.0",), stdout="GStreamer 1.24.2\n")
    return runner


# ---------------------------------------------------------------------------
# detect_method -- doc section 4.2 precedence
# ---------------------------------------------------------------------------


def test_detect_method_explicit_override_wins(tmp_path):
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "tar"})
    method, reason = step2.detect_method(ctx)
    assert method is step2.Method.TAR
    assert "override" in reason


def test_detect_method_unknown_override_is_ignored(tmp_path):
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "bogus"})
    method, _ = step2.detect_method(ctx)
    # Falls through to the product default (host pipeline intent).
    assert method is step2.Method.DEB


def test_detect_method_already_installed_deb_wins_over_tar_marker(tmp_path):
    runner = ScriptedRunner()
    runner.when(lambda a: a[:2] == ("dpkg", "-s"), stdout="Version: 9.1.0-1\n")
    ctx = FakeContext(tmp_path, runner_root=runner)
    method, reason = step2.detect_method(ctx)
    assert method is step2.Method.DEB
    assert "already installed" in reason


def test_detect_method_already_installed_tar(tmp_path, _sdk_paths):
    sdk_dir, _symlink, _profile = _sdk_paths
    sdk_dir.mkdir(parents=True)
    (sdk_dir / "version").write_text("DeepStream 9.1.0\n")

    runner = ScriptedRunner(default_returncode=1)  # dpkg -s: not installed
    ctx = FakeContext(tmp_path, runner_root=runner)
    method, reason = step2.detect_method(ctx)
    assert method is step2.Method.TAR
    assert "already installed" in reason


def test_detect_method_already_installed_docker(tmp_path):
    runner_root = ScriptedRunner(default_returncode=1)
    runner_user = ScriptedRunner()
    runner_user.when(lambda a: a[:3] == ("docker", "image", "inspect"), returncode=0)
    ctx = FakeContext(tmp_path, runner_root=runner_root, runner_user=runner_user)
    method, reason = step2.detect_method(ctx)
    assert method is step2.Method.DOCKER
    assert "already installed" in reason


def test_detect_method_relocatable_requests_tar(tmp_path):
    runner = ScriptedRunner(default_returncode=1)
    ctx = FakeContext(tmp_path, conf={"ds_relocatable": "true"}, runner_root=runner)
    method, reason = step2.detect_method(ctx)
    assert method is step2.Method.TAR
    assert "ds_relocatable=true" in reason


def test_detect_method_docker_when_toolkit_usable_and_host_not_required(tmp_path):
    runner_root = ScriptedRunner(default_returncode=1)
    runner_root.when(lambda a: a[:2] == ("dpkg", "-s") and a[-1] == "nvidia-container-toolkit", returncode=0)
    runner_user = ScriptedRunner(default_returncode=1)
    runner_user.when(lambda a: a[:2] == ("docker", "info"), returncode=0, stdout="Runtimes: nvidia runc\n")
    runner_user.when(lambda a: a == ("which", "nvidia-ctk"), returncode=0)

    ctx = FakeContext(
        tmp_path,
        conf={"ds_host_pipeline_required": "false"},
        runner_root=runner_root,
        runner_user=runner_user,
    )
    method, reason = step2.detect_method(ctx)
    assert method is step2.Method.DOCKER
    assert "Docker" in reason


def test_detect_method_defaults_to_deb_for_host_pipeline_intent(tmp_path):
    runner_root = ScriptedRunner(default_returncode=1)
    runner_user = ScriptedRunner(default_returncode=1)
    ctx = FakeContext(tmp_path, runner_root=runner_root, runner_user=runner_user)
    method, reason = step2.detect_method(ctx)
    assert method is step2.Method.DEB
    assert "host pipeline intent" in reason


def test_detect_method_ambiguous_when_host_not_required_and_docker_unusable(tmp_path):
    runner_root = ScriptedRunner(default_returncode=1)
    runner_user = ScriptedRunner(default_returncode=1)  # docker info fails
    ctx = FakeContext(
        tmp_path,
        conf={"ds_host_pipeline_required": "false"},
        runner_root=runner_root,
        runner_user=runner_user,
    )
    method, reason = step2.detect_method(ctx)
    assert method is None
    assert reason == "ambiguous"


# ---------------------------------------------------------------------------
# run() -- ambiguous-method prompt / non-interactive fallback (doc section 12)
# ---------------------------------------------------------------------------


def _ambiguous_ctx(tmp_path) -> FakeContext:
    runner_root = ScriptedRunner(default_returncode=1)
    runner_user = ScriptedRunner(default_returncode=1)
    return FakeContext(
        tmp_path,
        conf={"ds_host_pipeline_required": "false"},
        runner_root=runner_root,
        runner_user=runner_user,
    )


def test_resolve_method_non_interactive_ambiguous_defaults_to_deb(tmp_path, monkeypatch):
    monkeypatch.setattr(step2, "_is_interactive", lambda: False)
    ctx = _ambiguous_ctx(tmp_path)
    method, reason = step2._resolve_method(ctx)
    assert method is step2.Method.DEB
    assert "non-interactive default" in reason


def test_resolve_method_interactive_ambiguous_prompts_and_honors_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(step2, "_is_interactive", lambda: True)
    monkeypatch.setattr(step2, "_INPUT", lambda _prompt: "tar")
    ctx = _ambiguous_ctx(tmp_path)
    method, reason = step2._resolve_method(ctx)
    assert method is step2.Method.TAR
    assert "operator selection" in reason


def test_prompt_for_method_blank_answer_defaults_to_deb(monkeypatch):
    monkeypatch.setattr(step2, "_INPUT", lambda _prompt: "")
    assert step2._prompt_for_method() is step2.Method.DEB


def test_prompt_for_method_reprompts_on_garbage(monkeypatch):
    answers = iter(["nope", "docker"])
    monkeypatch.setattr(step2, "_INPUT", lambda _prompt: next(answers))
    assert step2._prompt_for_method() is step2.Method.DOCKER


# ---------------------------------------------------------------------------
# run() -- deb branch
# ---------------------------------------------------------------------------


def test_run_deb_already_installed_skips_download(tmp_path):
    runner_root = ScriptedRunner()
    runner_root.when(lambda a: a[:2] == ("dpkg", "-s"), stdout="Version: 9.1.0-1\n")
    runner_user = ScriptedRunner()
    ctx = FakeContext(tmp_path, runner_root=runner_root, runner_user=runner_user)

    step = step2.Step2DeepStreamSdk()
    result = step.run(ctx)

    assert result.status is StepStatus.COMPLETE
    assert not runner_user.calls  # curl never invoked
    assert ctx.conf["ds_install_method"] == "deb"


def test_run_deb_download_failure_is_user_action_required(tmp_path):
    runner_root = ScriptedRunner(default_returncode=1)  # dpkg -s: not installed
    runner_user = ScriptedRunner(default_returncode=1)  # curl fails
    ctx = FakeContext(tmp_path, runner_root=runner_root, runner_user=runner_user)

    step = step2.Step2DeepStreamSdk()
    result = step.run(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert result.user_actions
    action = result.user_actions[0]
    assert step2.DEB_ARTIFACT in action.text
    assert "curl" in (action.command or "")
    assert str(ctx.install_dir / "downloads" / "deepstream") == action.path


def test_run_deb_apt_install_failure_is_failed(tmp_path):
    runner_root = ScriptedRunner(default_returncode=1)
    runner_root.when(lambda a: a[:2] == ("apt-get", "install"), returncode=1, stderr="dependency problems")
    runner_user = ScriptedRunner()  # curl is never invoked: artifact is pre-placed below

    ctx = FakeContext(tmp_path, runner_root=runner_root, runner_user=runner_user)
    artifact_dir = ctx.install_dir / "downloads" / "deepstream"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / step2.DEB_ARTIFACT).write_bytes(b"stub")

    step = step2.Step2DeepStreamSdk()
    result = step.run(ctx)

    assert result.status is StepStatus.FAILED
    assert "apt-get install" in result.message
    assert "dependency problems" in result.message


def test_run_deb_fresh_install_reports_installed_and_completes(tmp_path):
    runner_root = ScriptedRunner()
    runner_root.when(lambda a: a[:2] == ("dpkg", "-s"), returncode=1)  # not installed yet
    runner_root.when(lambda a: a[:2] == ("apt-get", "install"), returncode=0)

    ctx = FakeContext(tmp_path, runner_root=runner_root)
    artifact_path = ctx.install_dir / "downloads" / "deepstream" / step2.DEB_ARTIFACT
    ctx.runner_user.when(
        lambda a: a[:1] == ("curl",),
        returncode=0,
        side_effect=lambda: artifact_path.write_bytes(b"stub"),
    )

    step = step2.Step2DeepStreamSdk()
    result = step.run(ctx)

    assert result.status is StepStatus.COMPLETE
    assert runner_root.called_with_prefix("apt-get", "install", "-y", f"./{step2.DEB_ARTIFACT}")
    assert ctx.conf["ds_install_method"] == "deb"
    # installer.conf was persisted so a resumed run reads back the choice.
    assert config_mod._read_conf(ctx.install_dir / config_mod.CONF_FILENAME)[
        step2.CONF_METHOD_KEY
    ] == "deb"
    # Post-install tail ran (update_rtpmanager.sh, ldconfig, profile.d).
    assert runner_root.called_with_prefix("ldconfig")


# ---------------------------------------------------------------------------
# run() -- docker branch (doc section 5.2 / 12: no fallback once key present)
# ---------------------------------------------------------------------------


def test_run_docker_already_local_skips_login_and_pull(tmp_path):
    runner_user = ScriptedRunner()
    runner_user.when(lambda a: a[:3] == ("docker", "image", "inspect"), returncode=0)
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "docker"}, runner_user=runner_user)

    step = step2.Step2DeepStreamSdk()
    result = step.run(ctx)

    assert result.status is StepStatus.COMPLETE
    assert not runner_user.called_with_prefix("docker", "login")
    assert not runner_user.called_with_prefix("docker", "pull")


def test_run_docker_login_failure_is_failed_not_user_action(tmp_path):
    runner_user = ScriptedRunner(default_returncode=1)  # image inspect: absent
    runner_user.when(lambda a: len(a) >= 3 and a[2] == "bash", returncode=1, stderr="unauthorized")
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "docker"}, runner_user=runner_user)

    step = step2.Step2DeepStreamSdk()
    result = step.run(ctx)

    assert result.status is StepStatus.FAILED
    assert "docker login" in result.message
    assert "unauthorized" in result.message
    assert result.user_actions == []


def test_run_docker_pull_failure_is_failed(tmp_path):
    runner_user = ScriptedRunner(default_returncode=0)  # image inspect + login + bash succeed
    # image inspect must report "not present" so the pull path is exercised.
    runner_user.when(lambda a: a[:3] == ("docker", "image", "inspect"), returncode=1)
    runner_user.when(lambda a: a[:2] == ("docker", "pull"), returncode=1, stderr="manifest unknown")
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "docker"}, runner_user=runner_user)

    step = step2.Step2DeepStreamSdk()
    result = step.run(ctx)

    assert result.status is StepStatus.FAILED
    assert "docker pull" in result.message


def test_run_docker_success_reports_installed(tmp_path):
    runner_user = ScriptedRunner(default_returncode=0)
    runner_user.when(lambda a: a[:3] == ("docker", "image", "inspect"), returncode=1)
    runner_user.when(lambda a: a[:2] == ("docker", "pull"), returncode=0)
    ngc = FakeNgc()
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "docker"}, runner_user=runner_user, ngc=ngc)

    step = step2.Step2DeepStreamSdk()
    result = step.run(ctx)

    assert result.status is StepStatus.COMPLETE
    assert ngc.configure_calls == 1
    assert runner_user.called_with_prefix("docker", "pull", step2.DOCKER_IMAGE)


# ---------------------------------------------------------------------------
# preflight() -- doc section 2 / 12
# ---------------------------------------------------------------------------


def test_preflight_fails_on_prereq_pin_mismatch(tmp_path):
    runner = _passing_pin_runner()
    runner.when(lambda a: a[:1] == ("nvidia-smi",), stdout="000.00.00\n")  # wrong driver
    ctx = FakeContext(tmp_path, runner_root=runner)

    step = step2.Step2DeepStreamSdk()
    result = step.preflight(ctx)

    assert result.status is StepStatus.FAILED
    assert "NVIDIA driver" in result.message
    assert "re-run Step 1" in result.message


def test_preflight_fails_when_ngc_key_missing(tmp_path):
    runner = _passing_pin_runner()
    ctx = FakeContext(tmp_path, runner_root=runner, ngc=FakeNgc(key=None))

    step = step2.Step2DeepStreamSdk()
    result = step.preflight(ctx)

    assert result.status is StepStatus.FAILED
    assert "NGC API key" in result.message


def test_preflight_fails_on_wrong_arch(tmp_path, monkeypatch):
    monkeypatch.setattr(step2.platform, "machine", lambda: "aarch64")
    ctx = FakeContext(tmp_path, runner_root=_passing_pin_runner())

    step = step2.Step2DeepStreamSdk()
    result = step.preflight(ctx)

    assert result.status is StepStatus.FAILED
    assert "x86_64" in result.message


def test_preflight_passes_when_everything_pinned(tmp_path):
    ctx = FakeContext(tmp_path, runner_root=_passing_pin_runner())

    step = step2.Step2DeepStreamSdk()
    result = step.preflight(ctx)

    assert result.status is StepStatus.COMPLETE


# ---------------------------------------------------------------------------
# verify() -- doc section 7 / 12
# ---------------------------------------------------------------------------


def _host_ready_runner(*, version_ok=True, app_version_ok=True) -> ScriptedRunner:
    runner = _passing_pin_runner()
    runner.when(
        lambda a: a[:2] == ("dpkg", "-s") and a[-1] == "deepstream-9.1",
        stdout=f"Version: {step2.DS_VERSION_DEB if version_ok else '9.0.0-1'}\n",
    )
    version_stdout = "deepstream-app version 9.1.0\nDeepStreamSDK 9.1.0\n" if app_version_ok else "unknown\n"
    runner.when(lambda a: a[:1] == ("timeout",), stdout="PERF: FPS 30.0 (30.0)\nPLAYING\n")
    runner.when(lambda a: a[:1] == ("deepstream-app",), stdout=version_stdout)
    return runner


def test_verify_fails_without_recorded_method(tmp_path, _sdk_paths):
    ctx = FakeContext(tmp_path)
    step = step2.Step2DeepStreamSdk()
    result = step.verify(ctx)
    assert result.status is StepStatus.FAILED
    assert "no DeepStream install method" in result.message


def test_verify_host_fails_when_sdk_dir_missing(tmp_path, _sdk_paths):
    runner = _host_ready_runner()
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "deb"}, runner_root=runner)

    step = step2.Step2DeepStreamSdk()
    result = step.verify(ctx)

    assert result.status is StepStatus.FAILED
    assert "not found after install" in result.message


def _make_sdk_tree(sdk_dir, symlink, profile, *, version_text="DeepStream 9.1.0\n"):
    sdk_dir.mkdir(parents=True)
    (sdk_dir / "version").write_text(version_text)
    symlink.symlink_to(sdk_dir)
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(step2._PROFILE_D_CONTENT)


def test_verify_host_fails_on_version_mismatch(tmp_path, _sdk_paths):
    sdk_dir, symlink, profile = _sdk_paths
    _make_sdk_tree(sdk_dir, symlink, profile)
    runner = _host_ready_runner(version_ok=False)
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "deb"}, runner_root=runner)

    step = step2.Step2DeepStreamSdk()
    result = step.verify(ctx)

    assert result.status is StepStatus.FAILED
    assert "version pin mismatch" in result.message


def test_verify_host_fails_on_smoke_test_error(tmp_path, _sdk_paths):
    sdk_dir, symlink, profile = _sdk_paths
    _make_sdk_tree(sdk_dir, symlink, profile)
    runner = _host_ready_runner()
    runner.when(lambda a: a[:1] == ("timeout",), returncode=1, stderr="ERROR: pipeline failed")
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "deb"}, runner_root=runner)

    step = step2.Step2DeepStreamSdk()
    result = step.verify(ctx)

    assert result.status is StepStatus.FAILED
    assert "smoke test failed" in result.message


def test_verify_host_passes_end_to_end(tmp_path, _sdk_paths):
    sdk_dir, symlink, profile = _sdk_paths
    _make_sdk_tree(sdk_dir, symlink, profile)
    runner = _host_ready_runner()
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "deb"}, runner_root=runner)

    step = step2.Step2DeepStreamSdk()
    result = step.verify(ctx)

    assert result.status is StepStatus.COMPLETE
    assert step._smoke_passed is True


def test_verify_tar_uses_short_version_pin(tmp_path, _sdk_paths):
    sdk_dir, symlink, profile = _sdk_paths
    _make_sdk_tree(sdk_dir, symlink, profile)
    runner = _host_ready_runner()
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "tar"}, runner_root=runner)

    step = step2.Step2DeepStreamSdk()
    result = step.verify(ctx)

    assert result.status is StepStatus.COMPLETE


def test_verify_docker_fails_when_image_absent(tmp_path):
    runner_user = ScriptedRunner(default_returncode=1)
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "docker"}, runner_user=runner_user)

    step = step2.Step2DeepStreamSdk()
    result = step.verify(ctx)

    assert result.status is StepStatus.FAILED
    assert "not present locally" in result.message


def test_verify_docker_passes_end_to_end(tmp_path):
    runner_user = ScriptedRunner()
    runner_user.when(lambda a: a[:3] == ("docker", "image", "inspect"), returncode=0)
    runner_user.when(
        lambda a: a[:2] == ("docker", "run") and "deepstream-app" in a,
        stdout="deepstream-app version 9.1.0\n",
    )
    runner_user.when(
        lambda a: a[:2] == ("docker", "run") and "timeout" in a,
        stdout="PERF: FPS 30.0\nPLAYING\n",
    )
    runner_user.when(lambda a: a[:2] == ("docker", "info"), stdout="Runtimes: nvidia runc\n")
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "docker"}, runner_user=runner_user)

    step = step2.Step2DeepStreamSdk()
    result = step.verify(ctx)

    assert result.status is StepStatus.COMPLETE


def test_verify_unrecognized_method_is_failed(tmp_path):
    ctx = FakeContext(tmp_path, conf={"ds_install_method": "bogus"})
    step = step2.Step2DeepStreamSdk()
    result = step.verify(ctx)
    assert result.status is StepStatus.FAILED
    assert "unrecognized" in result.message


# ---------------------------------------------------------------------------
# report() -- no side effects, human summary
# ---------------------------------------------------------------------------


def test_report_prints_summary_after_successful_run(tmp_path, capsys, _sdk_paths):
    sdk_dir, symlink, profile = _sdk_paths
    runner_root = ScriptedRunner()
    runner_root.when(lambda a: a[:2] == ("dpkg", "-s"), returncode=1)
    runner_root.when(lambda a: a[:2] == ("apt-get", "install"), returncode=0)

    ctx = FakeContext(tmp_path, runner_root=runner_root)
    artifact_path = ctx.install_dir / "downloads" / "deepstream" / step2.DEB_ARTIFACT
    ctx.runner_user.when(
        lambda a: a[:1] == ("curl",),
        returncode=0,
        side_effect=lambda: artifact_path.write_bytes(b"stub"),
    )

    step = step2.Step2DeepStreamSdk()
    step.run(ctx)
    step.report(ctx)

    err = capsys.readouterr().err
    assert "DeepStream 9.1 SDK install summary" in err
    assert "method: deb" in err
    assert "post-install actions:" in err


# ---------------------------------------------------------------------------
# Full StepResult matrix -- doc section 12
# ---------------------------------------------------------------------------


def test_full_lifecycle_all_pass_is_complete(tmp_path, _sdk_paths):
    """preflight -> run -> verify, all COMPLETE, for a DeepStream 9.1 deb
    install already present (idempotent re-run) -- doc section 12's
    "deb/tar/docker install + post-install + smoke all pass" row."""
    sdk_dir, symlink, profile = _sdk_paths
    sdk_dir.mkdir(parents=True)
    (sdk_dir / "version").write_text("DeepStream 9.1.0\n")
    symlink.symlink_to(sdk_dir)
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(step2._PROFILE_D_CONTENT)

    runner_root = _passing_pin_runner()
    runner_root.when(
        lambda a: a[:2] == ("dpkg", "-s") and a[-1] == "deepstream-9.1",
        stdout=f"Version: {step2.DS_VERSION_DEB}\n",
    )
    runner_root.when(lambda a: a[:1] == ("timeout",), stdout="PERF: FPS 30.0\nPLAYING\n")
    runner_root.when(lambda a: a[:1] == ("deepstream-app",), stdout="deepstream-app version 9.1.0\n")

    ngc = FakeNgc()
    ctx = FakeContext(tmp_path, runner_root=runner_root, ngc=ngc)

    step = step2.Step2DeepStreamSdk()

    pre = step.preflight(ctx)
    assert pre.status is StepStatus.COMPLETE

    run_result = step.run(ctx)
    assert run_result.status is StepStatus.COMPLETE

    verify_result = step.verify(ctx)
    assert verify_result.status is StepStatus.COMPLETE
