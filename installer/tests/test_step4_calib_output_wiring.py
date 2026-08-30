"""Tests for mv3dt_installer.steps.step4_calib_output_wiring
(STEP-4-CALIB-OUTPUT-WIRING.md).

Run from installer/: `python3 -m pytest tests/test_step4_calib_output_wiring.py -v`

No test here shells out for real or touches a real systemd/filesystem
outside `tmp_path` -- every `ctx.run_root`/`ctx.run_as_user` call is served
by a `ScriptedRunner` fake (mirroring `test_step3_amc_launcher.py`'s
convention), `systemd.UNIT_DIR` is monkeypatched to a scratch dir (mirroring
`test_systemd.py`'s convention), and `ctx.asset_path("deepstream", ...)` is
served by fixture copies of the real templates under
`laptop/deepstream/` -- copied into a tmp dir at test time rather than
duplicated into this file, so template edits never go stale here (see
`_deepstream_templates` below; the production `assets/deepstream/` tree does
not yet carry the real templates -- see STEP-4's own note on template asset
resolution).
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import zipfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mv3dt_installer import config as config_mod  # noqa: E402
from mv3dt_installer import logs, report, systemd, waitui  # noqa: E402
from mv3dt_installer.steps import STEP_REGISTRY, StepStatus  # noqa: E402
from mv3dt_installer.steps import step3_amc_launcher as step3  # noqa: E402
from mv3dt_installer.steps import step4_calib_output_wiring as step4  # noqa: E402
from mv3dt_installer import app  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REAL_TEMPLATES_DIR = REPO_ROOT / "laptop" / "deepstream"


@pytest.fixture(autouse=True)
def _reset_transcript_state():
    logs._transcript_path = None
    yield
    logs._transcript_path = None


@pytest.fixture(autouse=True)
def _force_no_colour(monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)


@pytest.fixture(autouse=True)
def _no_real_unit_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(systemd, "UNIT_DIR", tmp_path / "unsafe-default-units")


@pytest.fixture
def deepstream_templates_dir(tmp_path) -> pathlib.Path:
    dest = tmp_path / "assets" / "deepstream"
    dest.mkdir(parents=True, exist_ok=True)
    for name in (
        step4.APP_CONFIG_TEMPLATE_NAME,
        step4.TRACKER_YAML_TEMPLATE_NAME,
        step4.INFER_PRIMARY_TEMPLATE_NAME,
        step4.MSGCONV_TEMPLATE_NAME,
    ):
        shutil.copy2(REAL_TEMPLATES_DIR / name, dest / name)
    return dest


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class ScriptedRunner:
    """Stand-in for `ctx.run_root`/`ctx.run_as_user`. See
    test_step3_amc_launcher.py's identical fake for the rationale."""

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
        self.calls.append((args, kwargs))
        for matcher, returncode, stdout, stderr, side_effect in reversed(self._rules):
            if matcher(args):
                if side_effect is not None:
                    side_effect()
                return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)
        return subprocess.CompletedProcess(
            list(args), self.default_returncode, self.default_stdout, self.default_stderr
        )


class FakeUser:
    def __init__(self, home: pathlib.Path):
        self.name = "op"
        self.uid = 1000
        self.gid = 1000
        self.home = home


class FakeContext:
    def __init__(
        self,
        tmp_path,
        *,
        conf=None,
        asset_dir: pathlib.Path | None = None,
        non_interactive=True,
        runner_root=None,
        runner_user=None,
    ):
        self.install_dir = tmp_path / "mv3dt"
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self.conf = conf if conf is not None else {}
        self.user = FakeUser(home=tmp_path / "home" / "op")
        self.user.home.mkdir(parents=True, exist_ok=True)
        self.log = logs.log
        self.report_installed = report.report_installed
        self.report_already_installed = report.report_already_installed
        self.verify_pinned = report.verify_pinned
        self.non_interactive = non_interactive
        self._asset_dir = asset_dir
        self.runner_root = runner_root if runner_root is not None else ScriptedRunner()
        self.runner_user = runner_user if runner_user is not None else ScriptedRunner()

    def asset_path(self, *parts):
        if self._asset_dir is not None and parts and parts[0] == "deepstream":
            return self._asset_dir.joinpath(*parts[1:])
        return pathlib.Path("/nonexistent").joinpath(*parts)

    def run_root(self, *args, **kwargs):
        return self.runner_root(*args, **kwargs)

    def run_as_user(self, *args, **kwargs):
        return self.runner_user(*args, **kwargs)


def _default_conf(tmp_path, **overrides) -> dict:
    conf = {
        step4.CONF_LOCATION_ID_KEY: "test-lab-01",
        step4.CONF_CAM_USER_KEY: "admin",
        step4.CONF_CAM_PASSWORD_KEY: "hunter2",
    }
    conf.update(overrides)
    return conf


def _write_cameras_yml(path: pathlib.Path, *, enabled_ips: list[str], disabled_ips: list[str] | None = None) -> None:
    disabled_ips = disabled_ips or []
    lines = ["# fleet header", "", "cameras:"]
    idx = 1
    for ip in enabled_ips:
        lines += [
            f"  - id: cam{idx}",
            f"    ip: {ip}",
            '    position: "north"',
            "    rtsp_path: /Streaming/Channels/101",
            "    enabled: true",
        ]
        idx += 1
    for ip in disabled_ips:
        lines += [
            f"  - id: cam{idx}",
            f"    ip: {ip}",
            '    position: "south"',
            "    rtsp_path: /Streaming/Channels/101",
            "    enabled: false",
        ]
        idx += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stub_amc_root(
    tmp_path,
    *,
    project_name="test-lab-01",
    # Two files by default -- matches `_cameras_file_set`'s default of two
    # enabled cameras, so a `run()`/`verify()` test that doesn't care about
    # the section 8.1 completeness check isn't tripped by it incidentally.
    export_files=("camInfo-01.yml", "camInfo-02.yml"),
) -> pathlib.Path:
    amc_root = tmp_path / "auto-magic-calib"
    export_dir = amc_root / "projects" / project_name / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    for name in export_files:
        (export_dir / name).write_text("dummy calibration data\n", encoding="utf-8")
    return amc_root


def _amc_wrapper_present(ctx) -> None:
    wrapper = ctx.install_dir / "bin" / step3.AMC_WRAPPER_NAME
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)


def _cameras_file_set(ctx, tmp_path, *, enabled_ips=("10.0.0.1", "10.0.0.2")) -> None:
    cameras_path = tmp_path / "cameras.yml"
    _write_cameras_yml(cameras_path, enabled_ips=list(enabled_ips))
    ctx.conf[config_mod.CAMERAS_FILE_KEY] = str(cameras_path)


# ---------------------------------------------------------------------------
# Module identity / subcommand registration
# ---------------------------------------------------------------------------


def test_registers_itself_with_the_expected_identity():
    matches = [s for s in STEP_REGISTRY if s.id == "step4_calib_output_wiring"]
    assert len(matches) == 1
    step = matches[0]
    assert step.title
    assert step.order == 4


def test_registers_the_ingest_subcommand():
    assert app.SUBCOMMAND_REGISTRY.get("ingest") is step4.handle_ingest_subcommand


# ---------------------------------------------------------------------------
# resolve_project_inputs
# ---------------------------------------------------------------------------


def test_resolve_project_inputs_reports_first_missing_key(tmp_path):
    ctx = FakeContext(tmp_path, conf={})
    inputs, missing = step4.resolve_project_inputs(ctx)
    assert inputs is None
    assert missing == step4.CONF_LOCATION_ID_KEY


def test_resolve_project_inputs_defaults_project_name_to_location_id(tmp_path):
    ctx = FakeContext(tmp_path, conf=_default_conf(tmp_path))
    inputs, missing = step4.resolve_project_inputs(ctx)
    assert missing is None
    assert inputs.project_name == "test-lab-01"
    assert inputs.amc_root == ctx.user.home / "auto-magic-calib"
    assert inputs.export_wait_s == step4.DEFAULT_EXPORT_WAIT_S


def test_resolve_project_inputs_honours_explicit_overrides(tmp_path):
    conf = _default_conf(
        tmp_path,
        PROJECT_NAME="North Lobby",
        AMC_ROOT=str(tmp_path / "custom-amc"),
        AMC_EXPORT_WAIT_S="120",
    )
    ctx = FakeContext(tmp_path, conf=conf)
    inputs, missing = step4.resolve_project_inputs(ctx)
    assert missing is None
    assert inputs.project_name == "North Lobby"
    assert inputs.amc_root == tmp_path / "custom-amc"
    assert inputs.export_wait_s == 120.0


# ---------------------------------------------------------------------------
# _slugify (STEP-5 section 3.1, ported)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,location_id,expected",
    [
        ("North Lobby #2", "loc", "north-lobby-2"),
        ("  Weird---Name  ", "loc", "weird-name"),
        ("###", "test-lab-01", "test-lab-01"),
        ("", "", "project"),
        ("a" * 100, "loc", ("a" * 64)),
    ],
)
def test_slugify(name, location_id, expected):
    assert step4._slugify(name, location_id=location_id) == expected


# ---------------------------------------------------------------------------
# ingest_export -- exporter-first / copy-fallback
# ---------------------------------------------------------------------------


def test_ingest_export_uses_exporter_when_present_and_succeeds(tmp_path):
    amc_root = _stub_amc_root(tmp_path)
    (amc_root / "scripts").mkdir(parents=True, exist_ok=True)
    (amc_root / "scripts" / "export_mv3dt.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    dest = tmp_path / "calibration" / "test-lab-01"
    dest.mkdir(parents=True)

    runner = ScriptedRunner()
    ctx = FakeContext(tmp_path, conf=_default_conf(tmp_path), runner_user=runner)

    outcome = step4.ingest_export(ctx, amc_root=amc_root, project_name="test-lab-01", dest=dest)

    assert outcome.used_exporter is True
    assert outcome.ingested is True
    assert (dest / ".ingest.log").exists()
    assert len(runner.calls) == 1
    args, kwargs = runner.calls[0]
    assert args[:2] == ("python3", "scripts/export_mv3dt.py")
    assert "--project" in args and "test-lab-01" in args
    assert kwargs["cwd"] == str(amc_root)


def test_ingest_export_falls_back_to_copy_when_exporter_missing(tmp_path):
    amc_root = _stub_amc_root(tmp_path, export_files=("camInfo-01.yml", "camInfo-02.yml"))
    dest = tmp_path / "calibration" / "test-lab-01"
    dest.mkdir(parents=True)
    ctx = FakeContext(tmp_path, conf=_default_conf(tmp_path))

    outcome = step4.ingest_export(ctx, amc_root=amc_root, project_name="test-lab-01", dest=dest)

    assert outcome.used_exporter is False
    assert outcome.ingested is True
    assert (dest / "camInfo-01.yml").exists()
    assert (dest / "camInfo-02.yml").exists()
    assert (dest / ".ingest.log").exists()


def test_ingest_export_falls_back_to_copy_when_exporter_fails(tmp_path):
    amc_root = _stub_amc_root(tmp_path)
    (amc_root / "scripts").mkdir(parents=True, exist_ok=True)
    (amc_root / "scripts" / "export_mv3dt.py").write_text("x", encoding="utf-8")
    dest = tmp_path / "calibration" / "test-lab-01"
    dest.mkdir(parents=True)

    runner = ScriptedRunner(default_returncode=1)
    ctx = FakeContext(tmp_path, conf=_default_conf(tmp_path), runner_user=runner)

    outcome = step4.ingest_export(ctx, amc_root=amc_root, project_name="test-lab-01", dest=dest)

    assert outcome.used_exporter is False
    assert outcome.ingested is True
    assert (dest / "camInfo-01.yml").exists()


def test_ingest_export_unpacks_zip_from_copy_fallback(tmp_path):
    amc_root = tmp_path / "auto-magic-calib"
    export_dir = amc_root / "projects" / "test-lab-01" / "exports"
    export_dir.mkdir(parents=True)
    zip_path = export_dir / "mv3dt_export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("camInfo-01.yml", "data")
    dest = tmp_path / "calibration" / "test-lab-01"
    dest.mkdir(parents=True)
    ctx = FakeContext(tmp_path, conf=_default_conf(tmp_path))

    outcome = step4.ingest_export(ctx, amc_root=amc_root, project_name="test-lab-01", dest=dest)

    assert outcome.ingested is True
    assert (dest / "camInfo-01.yml").is_file()
    assert (dest / "mv3dt_export.zip").exists()


def test_ingest_export_returns_not_ingested_when_export_dir_empty(tmp_path):
    amc_root = tmp_path / "auto-magic-calib"
    (amc_root / "projects" / "test-lab-01" / "exports").mkdir(parents=True)
    dest = tmp_path / "calibration" / "test-lab-01"
    dest.mkdir(parents=True)
    ctx = FakeContext(tmp_path, conf=_default_conf(tmp_path))

    outcome = step4.ingest_export(ctx, amc_root=amc_root, project_name="test-lab-01", dest=dest)

    assert outcome.ingested is False
    assert not (dest / ".ingest.log").exists()


def test_ingest_export_is_idempotent_on_repeat_pass(tmp_path):
    amc_root = _stub_amc_root(tmp_path)
    dest = tmp_path / "calibration" / "test-lab-01"
    dest.mkdir(parents=True)
    ctx = FakeContext(tmp_path, conf=_default_conf(tmp_path))

    first = step4.ingest_export(ctx, amc_root=amc_root, project_name="test-lab-01", dest=dest)
    second = step4.ingest_export(ctx, amc_root=amc_root, project_name="test-lab-01", dest=dest)

    assert first.ingested and second.ingested
    assert (dest / "camInfo-01.yml").is_file()
    # Two breadcrumb lines, one per pass -- re-running never destroys state.
    assert len((dest / ".ingest.log").read_text().splitlines()) == 2


# ---------------------------------------------------------------------------
# render_tracker_yaml
# ---------------------------------------------------------------------------


def test_render_tracker_yaml_patches_calibration_dir_and_node_id(deepstream_templates_dir):
    template = (deepstream_templates_dir / step4.TRACKER_YAML_TEMPLATE_NAME).read_text(encoding="utf-8")

    rendered = step4.render_tracker_yaml(
        template, location_id="site-9", calibration_directory="calibration/site-9"
    )

    assert "calibrationDirectory: calibration/site-9" in rendered
    assert "nodeID: site-9" in rendered
    # untouched fields survive verbatim
    assert "projectionType: homography" in rendered
    assert "mqttBrokerIP: 127.0.0.1" in rendered
    assert "mqttBrokerPort: 1883" in rendered


def test_render_tracker_yaml_writes_absolute_path_for_alternate_dir(deepstream_templates_dir):
    template = (deepstream_templates_dir / step4.TRACKER_YAML_TEMPLATE_NAME).read_text(encoding="utf-8")
    rendered = step4.render_tracker_yaml(
        template, location_id="site-9", calibration_directory="/srv/calib/site-9"
    )
    assert "calibrationDirectory: /srv/calib/site-9" in rendered


def test_parse_tracker_yaml_strips_the_opencv_marker_line(deepstream_templates_dir):
    template = (deepstream_templates_dir / step4.TRACKER_YAML_TEMPLATE_NAME).read_text(encoding="utf-8")
    rendered = step4.render_tracker_yaml(
        template, location_id="site-9", calibration_directory="calibration/site-9"
    )
    data = step4._parse_tracker_yaml(rendered)
    assert data is not None
    assert data["SV3DT"]["calibrationDirectory"] == "calibration/site-9"
    assert data["MV3DT"]["nodeID"] == "site-9"
    assert data["SV3DT"]["projectionType"] == "homography"


# ---------------------------------------------------------------------------
# render_app_config
# ---------------------------------------------------------------------------


class _Cam:
    def __init__(self, ip, rtsp_path="/Streaming/Channels/101"):
        self.ip = ip
        self.rtsp_path = rtsp_path


def test_render_app_config_substitutes_creds_and_location(deepstream_templates_dir):
    template = (deepstream_templates_dir / step4.APP_CONFIG_TEMPLATE_NAME).read_text(encoding="utf-8")
    rendered = step4.render_app_config(
        template,
        cam_user="admin",
        cam_password="s3cret",
        location_id="site-9",
        cameras=[],
        template_path="dummy",
        calibration_dir="calibration/site-9",
    )
    assert "${CAM_USER}" not in rendered
    assert "${CAM_PASSWORD}" not in rendered
    assert "${LOCATION_ID}" not in rendered
    assert "topic=mv3dt/site-9/sv3d" in rendered


def test_render_app_config_rewrites_source_uris_preserving_order(deepstream_templates_dir):
    template = (deepstream_templates_dir / step4.APP_CONFIG_TEMPLATE_NAME).read_text(encoding="utf-8")
    cameras = [_Cam("10.0.0.1"), _Cam("10.0.0.2"), _Cam("10.0.0.3")]
    rendered = step4.render_app_config(
        template,
        cam_user="admin",
        cam_password="s3cret",
        location_id="site-9",
        cameras=cameras,
        template_path="dummy",
        calibration_dir="calibration/site-9",
    )
    block0 = step4._source_block(rendered, 0)
    block1 = step4._source_block(rendered, 1)
    block2 = step4._source_block(rendered, 2)
    assert "uri=rtsp://admin:s3cret@10.0.0.1:554/Streaming/Channels/101" in block0
    assert "uri=rtsp://admin:s3cret@10.0.0.2:554/Streaming/Channels/101" in block1
    assert "uri=rtsp://admin:s3cret@10.0.0.3:554/Streaming/Channels/101" in block2
    # source blocks beyond the enabled-camera list are left untouched
    block7 = step4._source_block(rendered, 7)
    assert "uri=rtsp://${CAM_USER}" not in block7  # already substituted globally
    assert "169.254" in block7  # original placeholder IP survives (no camera[7])


def test_render_app_config_leaves_referenced_siblings_alone(deepstream_templates_dir):
    template = (deepstream_templates_dir / step4.APP_CONFIG_TEMPLATE_NAME).read_text(encoding="utf-8")
    rendered = step4.render_app_config(
        template,
        cam_user="admin",
        cam_password="s3cret",
        location_id="site-9",
        cameras=[],
        template_path="dummy",
        calibration_dir="calibration/site-9",
    )
    assert "ll-config-file=config_tracker_NvMOT.yml" in rendered
    assert "config-file=config_infer_primary.txt" in rendered
    assert "msg-conv-config=msgconv_config.txt" in rendered
    assert "batch-size=8" in rendered


# ---------------------------------------------------------------------------
# preflight -- wait-outcome mapping (section 4.4's table)
# ---------------------------------------------------------------------------


def _ready_ctx(tmp_path, deepstream_templates_dir, *, non_interactive=False) -> FakeContext:
    conf = _default_conf(tmp_path)
    ctx = FakeContext(
        tmp_path, conf=conf, asset_dir=deepstream_templates_dir, non_interactive=non_interactive
    )
    _amc_wrapper_present(ctx)
    _cameras_file_set(ctx, tmp_path)
    return ctx


def test_preflight_fails_when_step3_not_complete(tmp_path, deepstream_templates_dir):
    ctx = _ready_ctx(tmp_path, deepstream_templates_dir)
    (ctx.install_dir / "bin" / step3.AMC_WRAPPER_NAME).unlink()
    result = step4.Step4CalibOutputWiring().preflight(ctx)
    assert result.status is StepStatus.FAILED


def test_preflight_user_action_required_when_conf_key_missing(tmp_path, deepstream_templates_dir):
    ctx = _ready_ctx(tmp_path, deepstream_templates_dir)
    del ctx.conf[step4.CONF_CAM_USER_KEY]
    result = step4.Step4CalibOutputWiring().preflight(ctx)
    assert result.status is StepStatus.USER_ACTION_REQUIRED


def test_preflight_fails_when_amc_root_missing(tmp_path, deepstream_templates_dir):
    ctx = _ready_ctx(tmp_path, deepstream_templates_dir)
    # AMC_ROOT defaults to $HOME/auto-magic-calib, which does not exist.
    result = step4.Step4CalibOutputWiring().preflight(ctx)
    assert result.status is StepStatus.FAILED
    assert "AMC not present" in result.message


def test_preflight_satisfied_when_export_already_present(tmp_path, deepstream_templates_dir, monkeypatch):
    ctx = _ready_ctx(tmp_path, deepstream_templates_dir)
    amc_root = _stub_amc_root(tmp_path)
    ctx.conf[step4.CONF_AMC_ROOT_KEY] = str(amc_root)

    result = step4.Step4CalibOutputWiring().preflight(ctx)
    assert result.status is StepStatus.COMPLETE


def test_preflight_timeout_maps_to_user_action_required(tmp_path, deepstream_templates_dir, monkeypatch):
    ctx = _ready_ctx(tmp_path, deepstream_templates_dir, non_interactive=False)
    amc_root = tmp_path / "auto-magic-calib"
    (amc_root / "projects" / "test-lab-01" / "exports").mkdir(parents=True)
    ctx.conf[step4.CONF_AMC_ROOT_KEY] = str(amc_root)

    monkeypatch.setattr(waitui, "wait_until", lambda *a, **k: waitui.WaitOutcome.TIMEOUT)

    result = step4.Step4CalibOutputWiring().preflight(ctx)
    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert result.user_actions


def test_preflight_cancelled_maps_to_user_action_required(tmp_path, deepstream_templates_dir, monkeypatch):
    ctx = _ready_ctx(tmp_path, deepstream_templates_dir, non_interactive=False)
    amc_root = tmp_path / "auto-magic-calib"
    (amc_root / "projects" / "test-lab-01" / "exports").mkdir(parents=True)
    ctx.conf[step4.CONF_AMC_ROOT_KEY] = str(amc_root)

    monkeypatch.setattr(waitui, "wait_until", lambda *a, **k: waitui.WaitOutcome.CANCELLED)

    result = step4.Step4CalibOutputWiring().preflight(ctx)
    assert result.status is StepStatus.USER_ACTION_REQUIRED


def test_preflight_skipped_maps_to_user_action_required(tmp_path, deepstream_templates_dir, monkeypatch):
    ctx = _ready_ctx(tmp_path, deepstream_templates_dir, non_interactive=True)
    amc_root = tmp_path / "auto-magic-calib"
    (amc_root / "projects" / "test-lab-01" / "exports").mkdir(parents=True)
    ctx.conf[step4.CONF_AMC_ROOT_KEY] = str(amc_root)

    monkeypatch.setattr(waitui, "wait_until", lambda *a, **k: waitui.WaitOutcome.SKIPPED)

    result = step4.Step4CalibOutputWiring().preflight(ctx)
    assert result.status is StepStatus.USER_ACTION_REQUIRED


def test_preflight_user_action_required_when_cameras_file_unset(tmp_path, deepstream_templates_dir):
    ctx = _ready_ctx(tmp_path, deepstream_templates_dir)
    amc_root = _stub_amc_root(tmp_path)
    ctx.conf[step4.CONF_AMC_ROOT_KEY] = str(amc_root)
    del ctx.conf[config_mod.CAMERAS_FILE_KEY]

    result = step4.Step4CalibOutputWiring().preflight(ctx)
    assert result.status is StepStatus.USER_ACTION_REQUIRED


def test_preflight_fails_when_template_missing(tmp_path, deepstream_templates_dir):
    ctx = _ready_ctx(tmp_path, deepstream_templates_dir)
    amc_root = _stub_amc_root(tmp_path)
    ctx.conf[step4.CONF_AMC_ROOT_KEY] = str(amc_root)
    (deepstream_templates_dir / step4.TRACKER_YAML_TEMPLATE_NAME).unlink()

    result = step4.Step4CalibOutputWiring().preflight(ctx)
    assert result.status is StepStatus.FAILED


# ---------------------------------------------------------------------------
# run() -- end-to-end ingest + render, idempotency, re-ingest units
# ---------------------------------------------------------------------------


def _rigged_root_runner() -> ScriptedRunner:
    """A root runner that answers `systemctl is-enabled` truthfully for the
    `.path`/`.service` unit split (section 4.5's carve-out): only a `.path`
    unit is ever `enable`d, so only a `.path` `is-enabled` query should read
    back as enabled. Every other systemctl call (`enable`, `daemon-reload`)
    ignores its returncode, so the default of 0 there is inert."""
    runner = ScriptedRunner(default_returncode=0)
    runner.when(
        lambda a: a[:3] == ("systemctl", "is-enabled", "--quiet") and a[3].endswith(".path"),
        returncode=0,
    )
    runner.when(
        lambda a: a[:3] == ("systemctl", "is-enabled", "--quiet") and a[3].endswith(".service"),
        returncode=1,
    )
    return runner


def _provisioned_ctx(tmp_path, deepstream_templates_dir, **conf_overrides) -> tuple[FakeContext, pathlib.Path]:
    amc_root = _stub_amc_root(tmp_path)
    conf = _default_conf(tmp_path, AMC_ROOT=str(amc_root))
    conf.update(conf_overrides)
    ctx = FakeContext(
        tmp_path,
        conf=conf,
        asset_dir=deepstream_templates_dir,
        non_interactive=True,
        runner_root=_rigged_root_runner(),
    )
    _amc_wrapper_present(ctx)
    _cameras_file_set(ctx, tmp_path)
    return ctx, amc_root


def test_run_ingests_renders_and_completes(tmp_path, deepstream_templates_dir):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)

    result = step4.Step4CalibOutputWiring().run(ctx)

    assert result.status is StepStatus.COMPLETE
    dest = step4.default_calibration_dir(ctx, "test-lab-01")
    assert (dest / "camInfo-01.yml").is_file()
    assert ctx.conf[step4.CONF_CALIBRATION_DIR_KEY] == str(dest)

    deepstream_dir = ctx.install_dir / "deepstream"
    tracker_text = (deepstream_dir / step4.TRACKER_YAML_TEMPLATE_NAME).read_text(encoding="utf-8")
    assert "calibrationDirectory: calibration/test-lab-01" in tracker_text
    rendered_text = (deepstream_dir / step4.RENDERED_APP_CONFIG_NAME).read_text(encoding="utf-8")
    assert "${" not in rendered_text
    assert (deepstream_dir / step4.INFER_PRIMARY_TEMPLATE_NAME).is_file()
    assert (deepstream_dir / step4.MSGCONV_TEMPLATE_NAME).is_file()


def test_run_defaults_to_non_interactive_default_dir_without_prompting(tmp_path, deepstream_templates_dir, monkeypatch):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)
    ctx.non_interactive = True

    def _boom(*a, **k):
        raise AssertionError("prompt must not be called under --non-interactive")

    monkeypatch.setattr(step4, "_PROMPT", _boom)

    result = step4.Step4CalibOutputWiring().run(ctx)
    assert result.status is StepStatus.COMPLETE


def test_run_prompts_for_alternate_location_when_interactive(tmp_path, deepstream_templates_dir, monkeypatch):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)
    ctx.non_interactive = False
    alternate = tmp_path / "alt-calib"

    monkeypatch.setattr(step4, "_PROMPT", lambda _msg: str(alternate))

    result = step4.Step4CalibOutputWiring().run(ctx)
    assert result.status is StepStatus.COMPLETE
    assert ctx.conf[step4.CONF_CALIBRATION_DIR_KEY] == str(alternate)
    assert (alternate / "camInfo-01.yml").is_file()

    tracker_text = (ctx.install_dir / "deepstream" / step4.TRACKER_YAML_TEMPLATE_NAME).read_text(encoding="utf-8")
    assert f"calibrationDirectory: {alternate}" in tracker_text


def test_run_returns_user_action_required_when_export_empty(tmp_path, deepstream_templates_dir):
    amc_root = tmp_path / "auto-magic-calib"
    (amc_root / "projects" / "test-lab-01" / "exports").mkdir(parents=True)
    conf = _default_conf(tmp_path, AMC_ROOT=str(amc_root))
    ctx = FakeContext(tmp_path, conf=conf, asset_dir=deepstream_templates_dir, non_interactive=True)
    _amc_wrapper_present(ctx)
    _cameras_file_set(ctx, tmp_path)

    result = step4.Step4CalibOutputWiring().run(ctx)
    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert result.user_actions


# ---------------------------------------------------------------------------
# section 8.1 -- "poor calibration (bad RMSE)" incomplete-export detection
# ---------------------------------------------------------------------------


def test_calibration_file_count_ignores_ingest_log_and_zip(tmp_path):
    dest = tmp_path / "calib"
    dest.mkdir()
    (dest / "camInfo-01.yml").write_text("x", encoding="utf-8")
    (dest / "camInfo-02.yml").write_text("x", encoding="utf-8")
    (dest / ".ingest.log").write_text("stamp\n", encoding="utf-8")
    (dest / "mv3dt_export.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    assert step4._calibration_file_count(dest) == 2


def test_calibration_file_count_zero_for_missing_dir(tmp_path):
    assert step4._calibration_file_count(tmp_path / "nope") == 0


def test_enabled_camera_count_reads_only_enabled_entries(tmp_path):
    cameras_path = tmp_path / "cameras.yml"
    _write_cameras_yml(cameras_path, enabled_ips=["10.0.0.1", "10.0.0.2"], disabled_ips=["10.0.0.9"])
    ctx = FakeContext(tmp_path, conf={config_mod.CAMERAS_FILE_KEY: str(cameras_path)})
    assert step4._enabled_camera_count(ctx) == 2


def test_enabled_camera_count_none_when_cameras_file_unset(tmp_path):
    ctx = FakeContext(tmp_path, conf={})
    assert step4._enabled_camera_count(ctx) is None


def test_run_surfaces_user_action_required_when_export_looks_incomplete(tmp_path, deepstream_templates_dir):
    # Two enabled cameras (via `_cameras_file_set`'s default), but only one
    # camInfo file lands -- section 8.1's "fewer camInfo files than enabled
    # cameras" case.
    amc_root = _stub_amc_root(tmp_path, export_files=("camInfo-01.yml",))
    conf = _default_conf(tmp_path, AMC_ROOT=str(amc_root))
    ctx = FakeContext(tmp_path, conf=conf, asset_dir=deepstream_templates_dir, non_interactive=True)
    _amc_wrapper_present(ctx)
    _cameras_file_set(ctx, tmp_path, enabled_ips=("10.0.0.1", "10.0.0.2"))

    result = step4.Step4CalibOutputWiring().run(ctx)

    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert result.message == step4.INCOMPLETE_CALIBRATION_MESSAGE
    assert result.user_actions
    assert result.user_actions[0].text == step4.INCOMPLETE_CALIBRATION_MESSAGE
    # An incomplete export must not be treated as a wired-up success: no
    # CALIBRATION_DIR persisted, no rendered config written.
    assert step4.CONF_CALIBRATION_DIR_KEY not in ctx.conf
    assert not (ctx.install_dir / "deepstream" / step4.RENDERED_APP_CONFIG_NAME).exists()


def test_run_completes_when_camInfo_count_matches_enabled_cameras(tmp_path, deepstream_templates_dir):
    amc_root = _stub_amc_root(tmp_path, export_files=("camInfo-01.yml", "camInfo-02.yml"))
    conf = _default_conf(tmp_path, AMC_ROOT=str(amc_root))
    ctx = FakeContext(tmp_path, conf=conf, asset_dir=deepstream_templates_dir, non_interactive=True)
    _amc_wrapper_present(ctx)
    _cameras_file_set(ctx, tmp_path, enabled_ips=("10.0.0.1", "10.0.0.2"))

    result = step4.Step4CalibOutputWiring().run(ctx)
    assert result.status is StepStatus.COMPLETE


def test_verify_surfaces_user_action_required_when_calibration_looks_incomplete(tmp_path, deepstream_templates_dir):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)
    installer_bin = ctx.install_dir / "bin" / step3.INSTALLER_BIN_NAME
    installer_bin.parent.mkdir(parents=True, exist_ok=True)
    installer_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    step = step4.Step4CalibOutputWiring()
    assert step.run(ctx).status is StepStatus.COMPLETE

    dest = step4.default_calibration_dir(ctx, "test-lab-01")
    # Simulate a later, incomplete recalibration overwriting the directory
    # (e.g. via the `ingest` subcommand) -- verify() must catch it too, not
    # just run().
    for entry in dest.iterdir():
        if entry.is_file() and entry.name != ".ingest.log":
            entry.unlink()

    result = step.verify(ctx)
    assert result.status is StepStatus.USER_ACTION_REQUIRED
    assert result.message == step4.INCOMPLETE_CALIBRATION_MESSAGE


def test_run_is_idempotent_on_second_pass(tmp_path, deepstream_templates_dir):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)

    first = step4.Step4CalibOutputWiring().run(ctx)
    second = step4.Step4CalibOutputWiring().run(ctx)

    assert first.status is StepStatus.COMPLETE
    assert second.status is StepStatus.COMPLETE
    dest = step4.default_calibration_dir(ctx, "test-lab-01")
    assert (dest / "camInfo-01.yml").is_file()
    # Same destination both times -- no re-prompt, no move.
    assert ctx.conf[step4.CONF_CALIBRATION_DIR_KEY] == str(dest)


def test_run_installs_reingest_units_only_after_first_success(tmp_path, deepstream_templates_dir):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)
    installer_bin = ctx.install_dir / "bin" / step3.INSTALLER_BIN_NAME
    installer_bin.parent.mkdir(parents=True, exist_ok=True)
    installer_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    step4.Step4CalibOutputWiring().run(ctx)
    slug = step4._slugify("test-lab-01", location_id="test-lab-01")
    path_unit = systemd.UNIT_DIR / f"mv3dt-ingest-{slug}.path"
    service_unit = systemd.UNIT_DIR / f"mv3dt-ingest-{slug}.service"
    assert path_unit.is_file()
    assert service_unit.is_file()
    enable_calls_after_first = [c for c in ctx.runner_root.calls if c[0][:2] == ("systemctl", "enable")]
    assert len(enable_calls_after_first) == 1

    # Second run: same project, already COMPLETE in real dispatch (this test
    # calls run() directly) -- units must not be reinstalled/re-enabled.
    step4.Step4CalibOutputWiring().run(ctx)
    enable_calls_after_second = [c for c in ctx.runner_root.calls if c[0][:2] == ("systemctl", "enable")]
    assert len(enable_calls_after_second) == 1


def test_run_only_enables_the_path_unit_never_the_service(tmp_path, deepstream_templates_dir):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)
    installer_bin = ctx.install_dir / "bin" / step3.INSTALLER_BIN_NAME
    installer_bin.parent.mkdir(parents=True, exist_ok=True)
    installer_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    step4.Step4CalibOutputWiring().run(ctx)

    enabled_names = [c[0][3] for c in ctx.runner_root.calls if c[0][:2] == ("systemctl", "enable")]
    assert len(enabled_names) == 1
    assert enabled_names[0].endswith(".path")


def test_run_downgrades_to_no_enable_when_installer_binary_absent(tmp_path, deepstream_templates_dir):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)
    # No <install_dir>/bin/mv3dt-installer written.

    step4.Step4CalibOutputWiring().run(ctx)

    slug = step4._slugify("test-lab-01", location_id="test-lab-01")
    path_unit = systemd.UNIT_DIR / f"mv3dt-ingest-{slug}.path"
    assert path_unit.is_file()  # still installed
    enable_calls = [c for c in ctx.runner_root.calls if c[0][:2] == ("systemctl", "enable")]
    assert enable_calls == []  # but never enabled


# ---------------------------------------------------------------------------
# verify() -- ExecStart-binary-absent downgrade to warning, not FAILED
# ---------------------------------------------------------------------------


def test_verify_complete_after_a_full_run_with_binary_present(tmp_path, deepstream_templates_dir):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)
    installer_bin = ctx.install_dir / "bin" / step3.INSTALLER_BIN_NAME
    installer_bin.parent.mkdir(parents=True, exist_ok=True)
    installer_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    step = step4.Step4CalibOutputWiring()
    run_result = step.run(ctx)
    assert run_result.status is StepStatus.COMPLETE

    verify_result = step.verify(ctx)
    assert verify_result.status is StepStatus.COMPLETE


def test_verify_warns_but_does_not_fail_when_installer_binary_absent(tmp_path, deepstream_templates_dir):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)
    # No installer binary dropped -- run() installs the units but never
    # enables them (see test above).

    step = step4.Step4CalibOutputWiring()
    assert step.run(ctx).status is StepStatus.COMPLETE

    verify_result = step.verify(ctx)
    assert verify_result.status is StepStatus.COMPLETE


def test_verify_fails_when_calibration_dir_missing(tmp_path, deepstream_templates_dir):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)
    result = step4.Step4CalibOutputWiring().verify(ctx)
    assert result.status is StepStatus.FAILED


def test_verify_fails_when_tracker_yaml_pinned_field_mutated(tmp_path, deepstream_templates_dir):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)
    installer_bin = ctx.install_dir / "bin" / step3.INSTALLER_BIN_NAME
    installer_bin.parent.mkdir(parents=True, exist_ok=True)
    installer_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    step = step4.Step4CalibOutputWiring()
    step.run(ctx)

    tracker_path = ctx.install_dir / "deepstream" / step4.TRACKER_YAML_TEMPLATE_NAME
    text = tracker_path.read_text(encoding="utf-8")
    text = text.replace("projectionType: homography", "projectionType: cameraModel")
    tracker_path.write_text(text, encoding="utf-8")

    result = step.verify(ctx)
    assert result.status is StepStatus.FAILED


# ---------------------------------------------------------------------------
# ingest subcommand handler
# ---------------------------------------------------------------------------


def test_handle_ingest_subcommand_runs_the_one_shot_pass_without_waiting(tmp_path, deepstream_templates_dir):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)

    exit_code = step4.handle_ingest_subcommand(["--project", "test-lab-01"], ctx)

    assert exit_code == 0
    dest = step4.default_calibration_dir(ctx, "test-lab-01")
    assert (dest / "camInfo-01.yml").is_file()
    assert ctx.conf[step4.CONF_CALIBRATION_DIR_KEY] == str(dest)


def test_handle_ingest_subcommand_never_prompts_even_when_interactive(tmp_path, deepstream_templates_dir, monkeypatch):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)
    ctx.non_interactive = False  # would normally trigger the section-5 prompt

    def _boom(_msg):
        raise AssertionError("ingest subcommand must never prompt")

    monkeypatch.setattr(step4, "_PROMPT", _boom)

    exit_code = step4.handle_ingest_subcommand(["--project", "test-lab-01"], ctx)
    assert exit_code == 0


def test_handle_ingest_subcommand_uses_persisted_calibration_dir(tmp_path, deepstream_templates_dir):
    ctx, amc_root = _provisioned_ctx(tmp_path, deepstream_templates_dir)
    persisted = tmp_path / "already-chosen"
    persisted.mkdir(parents=True)
    ctx.conf[step4.CONF_CALIBRATION_DIR_KEY] = str(persisted)

    exit_code = step4.handle_ingest_subcommand(["--project", "test-lab-01"], ctx)

    assert exit_code == 0
    assert (persisted / "camInfo-01.yml").is_file()


def test_handle_ingest_subcommand_missing_conf_returns_error(tmp_path, deepstream_templates_dir):
    ctx = FakeContext(tmp_path, conf={}, asset_dir=deepstream_templates_dir)
    exit_code = step4.handle_ingest_subcommand(["--project", "x"], ctx)
    assert exit_code == 1


def test_handle_ingest_subcommand_returns_zero_when_export_still_empty(tmp_path, deepstream_templates_dir):
    amc_root = tmp_path / "auto-magic-calib"
    (amc_root / "projects" / "test-lab-01" / "exports").mkdir(parents=True)
    conf = _default_conf(tmp_path, AMC_ROOT=str(amc_root))
    ctx = FakeContext(tmp_path, conf=conf, asset_dir=deepstream_templates_dir)

    exit_code = step4.handle_ingest_subcommand(["--project", "test-lab-01"], ctx)
    assert exit_code == 0
    dest = step4.default_calibration_dir(ctx, "test-lab-01")
    assert not (dest / ".ingest.log").exists()
