"""Tests for mv3dt_installer.steps.step5_per_project_exes
(STEP-5-PER-PROJECT-EXES.md).

Run from installer/: `python3 -m pytest tests/test_step5_per_project_exes.py -v`

No test here shells out for real, opens a browser, or touches
docker/git/systemctl -- every `ctx.run_root`/`ctx.run_as_user` call is
served by a `ScriptedRunner` fake (mirroring `test_step3_amc_launcher.py`'s
convention), `os.execv` is always injected, and the start-or-close /
reconciliation prompts are injected too.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mv3dt_installer import app  # noqa: E402
from mv3dt_installer import logs, report  # noqa: E402
from mv3dt_installer.steps import STEP_REGISTRY, StepStatus  # noqa: E402
from mv3dt_installer.steps import step3_amc_launcher as step3  # noqa: E402
from mv3dt_installer.steps import step5_per_project_exes as step5  # noqa: E402


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
    monkeypatch.setattr(step3, "repo_root", lambda: None)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class ScriptedRunner:
    """Stand-in for `ctx.run_root`/`ctx.run_as_user` (mirrors
    test_step3_amc_launcher.py's identical fake)."""

    def __init__(self, *, default_returncode: int = 0, default_stdout: str = "", default_stderr: str = ""):
        self.calls: list[tuple] = []
        self._rules: list[tuple] = []
        self.default_returncode = default_returncode
        self.default_stdout = default_stdout
        self.default_stderr = default_stderr

    def when(self, matcher, *, returncode=0, stdout="", stderr=""):
        self._rules.append((matcher, returncode, stdout, stderr))
        return self

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        for matcher, returncode, stdout, stderr in reversed(self._rules):
            if matcher(args):
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
        self.conf = conf if conf is not None else {}
        self.user = FakeUser(home=tmp_path / "home" / "op")
        self.user.home.mkdir(parents=True, exist_ok=True)
        self.log = logs.log
        self.report_installed = report.report_installed
        self.report_already_installed = report.report_already_installed
        self.verify_pinned = report.verify_pinned
        self.runner_root = runner_root if runner_root is not None else ScriptedRunner()
        self.runner_user = runner_user if runner_user is not None else ScriptedRunner()
        self.non_interactive = non_interactive

    def run_root(self, *args, **kwargs):
        return self.runner_root(*args, **kwargs)

    def run_as_user(self, *args, **kwargs):
        return self.runner_user(*args, **kwargs)


def _make_entry(install_dir, *, project_name="North Lobby #2", slug="north-lobby-2", location_id="loc-1"):
    exe = install_dir / "bin" / f"pipeline-{slug}"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(f'exec "x" pipeline --project "{project_name}" "$@"\n')
    exe.chmod(0o755)
    rendered = install_dir / "deepstream" / "deepstream_app_config.rendered.txt"
    rendered.parent.mkdir(parents=True, exist_ok=True)
    rendered.write_text("[application]\n")
    calib = install_dir / "deepstream" / "calibration" / location_id
    calib.mkdir(parents=True, exist_ok=True)
    return step5.upsert(
        install_dir,
        project_name=project_name,
        location_id=location_id,
        rendered_config=str(rendered),
        calibration_dir=str(calib),
        exe=str(exe),
        slug=slug,
    )


# ---------------------------------------------------------------------------
# Module identity + subcommand registration
# ---------------------------------------------------------------------------


def test_registers_itself_with_the_expected_identity():
    matches = [s for s in STEP_REGISTRY if s.id == "step5_per_project_exes"]
    assert len(matches) == 1
    step = matches[0]
    assert step.order == 5
    assert "Per-project executables" in step.title


def test_registers_the_pipeline_record_and_projects_subcommands():
    assert app.SUBCOMMAND_REGISTRY.get("pipeline") is step5.handle_pipeline_subcommand
    assert app.SUBCOMMAND_REGISTRY.get("record") is step5.handle_record_subcommand
    assert app.SUBCOMMAND_REGISTRY.get("projects") is step5.handle_projects_subcommand


# ---------------------------------------------------------------------------
# section 3.1 -- slug sanitization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("North Lobby #2", "north-lobby-2"),
        ("  Weird   Spacing  ", "weird-spacing"),
        ("ALLCAPS", "allcaps"),
        ("--leading-and-trailing--", "leading-and-trailing"),
        ("a" * 100, "a" * 64),
    ],
)
def test_slugify_sanitizes(name, expected):
    assert step5.slugify(name) == expected


def test_slugify_falls_back_to_location_id_when_name_is_all_punctuation():
    assert step5.slugify("####", fallback="loc-9") == "loc-9"


def test_slugify_falls_back_to_project_literal_when_everything_is_empty():
    assert step5.slugify("###", fallback="***") == "project"
    assert step5.slugify("", fallback="") == "project"


# ---------------------------------------------------------------------------
# section 3.1 -- collision policy (resolve_slug)
# ---------------------------------------------------------------------------


def test_resolve_slug_no_collision(tmp_path):
    slug, err = step5.resolve_slug(tmp_path, "North Lobby #2", "loc-1")
    assert slug == "north-lobby-2"
    assert err is None


def test_resolve_slug_rerun_of_same_project_keeps_its_existing_slug(tmp_path):
    step5.upsert(
        tmp_path,
        project_name="North Lobby #2",
        location_id="loc-1",
        rendered_config="r",
        calibration_dir="c",
        exe="e",
        slug="custom-slug",
    )
    slug, err = step5.resolve_slug(tmp_path, "North Lobby #2", "loc-1")
    assert slug == "custom-slug"
    assert err is None


def test_resolve_slug_collision_appends_location_id(tmp_path):
    step5.upsert(
        tmp_path,
        project_name="Other Project",
        location_id="other-loc",
        rendered_config="r",
        calibration_dir="c",
        exe="e",
        slug="north-lobby-2",
    )
    slug, err = step5.resolve_slug(tmp_path, "North Lobby #2", "loc-1")
    assert slug == "north-lobby-2-loc-1"
    assert err is None


def test_resolve_slug_unresolvable_collision_is_an_error(tmp_path):
    step5.upsert(
        tmp_path,
        project_name="Other Project",
        location_id="other-loc",
        rendered_config="r",
        calibration_dir="c",
        exe="e",
        slug="north-lobby-2",
    )
    step5.upsert(
        tmp_path,
        project_name="Yet Another",
        location_id="yet-loc",
        rendered_config="r",
        calibration_dir="c",
        exe="e",
        slug="north-lobby-2-loc-1",
    )
    slug, err = step5.resolve_slug(tmp_path, "North Lobby #2", "loc-1")
    assert err is not None
    assert "distinct" in err


# ---------------------------------------------------------------------------
# section 4 -- the project registry
# ---------------------------------------------------------------------------


def test_load_registry_missing_file_is_empty(tmp_path):
    registry = step5.load_registry(tmp_path)
    assert registry.projects == {}
    assert registry.schema_version == step5.REGISTRY_SCHEMA_VERSION


def test_load_registry_corrupt_file_is_forgiving(tmp_path):
    path = step5.registry_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json {{{")
    registry = step5.load_registry(tmp_path)
    assert registry.projects == {}


def test_load_registry_non_dict_json_is_forgiving(tmp_path):
    path = step5.registry_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]")
    registry = step5.load_registry(tmp_path)
    assert registry.projects == {}


def test_save_registry_writes_atomically_and_round_trips(tmp_path):
    registry = step5.Registry()
    entry = step5.ProjectEntry(
        project_name="P1",
        slug="p1",
        location_id="loc",
        exe="e",
        rendered_config="r",
        calibration_dir="c",
        created_utc="2026-01-01T00:00:00Z",
        updated_utc="2026-01-01T00:00:00Z",
    )
    registry.projects["P1"] = entry
    step5.save_registry(tmp_path, registry)

    path = step5.registry_path(tmp_path)
    assert path.is_file()
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["projects"]["P1"]["slug"] == "p1"

    reloaded = step5.load_registry(tmp_path)
    assert reloaded.projects["P1"].slug == "p1"


def test_upsert_creates_then_updates_bumping_calib_runs(tmp_path):
    first = step5.upsert(
        tmp_path,
        project_name="P1",
        location_id="loc",
        rendered_config="r1",
        calibration_dir="c1",
        exe="e1",
    )
    assert first.calib_runs == 1
    created = first.created_utc

    second = step5.upsert(
        tmp_path,
        project_name="P1",
        location_id="loc",
        rendered_config="r2",
        calibration_dir="c2",
        exe="e1",
    )
    assert second.calib_runs == 2
    assert second.created_utc == created
    assert second.rendered_config == "r2"
    assert second.slug == first.slug  # slug is stable across re-runs


def test_get_and_list_projects(tmp_path):
    assert step5.get(tmp_path, "nope") is None
    assert step5.list_projects(tmp_path) == []

    step5.upsert(
        tmp_path, project_name="P1", location_id="loc", rendered_config="r", calibration_dir="c", exe="e"
    )
    assert step5.get(tmp_path, "P1") is not None
    assert len(step5.list_projects(tmp_path)) == 1


def test_remove_registry_entry(tmp_path):
    step5.upsert(
        tmp_path, project_name="P1", location_id="loc", rendered_config="r", calibration_dir="c", exe="e"
    )
    assert step5.remove_registry_entry(tmp_path, "P1") is True
    assert step5.get(tmp_path, "P1") is None
    assert step5.remove_registry_entry(tmp_path, "P1") is False


# ---------------------------------------------------------------------------
# section 2 -- start-or-close prompt
# ---------------------------------------------------------------------------


def test_confirm_start_now_defaults_to_close_under_non_interactive(tmp_path, monkeypatch):
    ctx = FakeContext(tmp_path, non_interactive=True)

    def _boom(_prompt):  # pragma: no cover -- must never be called
        raise AssertionError("input() must not be called under non_interactive")

    monkeypatch.setattr(step5, "_INPUT", _boom)
    assert step5._confirm_start_now(ctx) is False


def test_confirm_start_now_interactive_start(tmp_path, monkeypatch):
    ctx = FakeContext(tmp_path, non_interactive=False)
    monkeypatch.setattr(step5, "_INPUT", lambda _prompt: "S")
    assert step5._confirm_start_now(ctx) is True


@pytest.mark.parametrize("answer", ["c", "C", "", "no", "n"])
def test_confirm_start_now_interactive_close(tmp_path, monkeypatch, answer):
    ctx = FakeContext(tmp_path, non_interactive=False)
    monkeypatch.setattr(step5, "_INPUT", lambda _prompt: answer)
    assert step5._confirm_start_now(ctx) is False


# ---------------------------------------------------------------------------
# section 3.3 -- pipeline subcommand flag handling
# ---------------------------------------------------------------------------


def test_pipeline_subcommand_unknown_project_fails(tmp_path):
    ctx = FakeContext(tmp_path)
    rc = step5.handle_pipeline_subcommand(["--project", "nope"], ctx)
    assert rc == 1


def test_pipeline_subcommand_dry_run_does_not_exec(tmp_path):
    ctx = FakeContext(tmp_path)
    entry = _make_entry(ctx.install_dir)

    calls = []
    monkeypatch_execv = lambda *a: calls.append(a)  # noqa: E731
    rc = step5._start_pipeline_foreground(ctx, entry, dry_run=True, execv=monkeypatch_execv)
    assert rc == 0
    assert calls == []


def test_pipeline_subcommand_dispatches_to_start_foreground(tmp_path, monkeypatch):
    ctx = FakeContext(tmp_path)
    _make_entry(ctx.install_dir)

    seen = {}

    def _fake_start(ctx_, entry_, **kwargs):
        seen["entry"] = entry_
        seen["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(step5, "_start_pipeline_foreground", _fake_start)
    rc = step5.handle_pipeline_subcommand(
        ["--project", "North Lobby #2", "--preview", "--skip-ping", "--dry-run", "--config", "/tmp/x.txt"],
        ctx,
    )
    assert rc == 0
    assert seen["kwargs"]["preview"] is True
    assert seen["kwargs"]["skip_ping"] is True
    assert seen["kwargs"]["dry_run"] is True
    assert seen["kwargs"]["config_override"] == "/tmp/x.txt"


def test_pipeline_subcommand_missing_config_fails(tmp_path):
    ctx = FakeContext(tmp_path)
    entry = _make_entry(ctx.install_dir)
    entry = step5.upsert(
        ctx.install_dir,
        project_name=entry.project_name,
        location_id=entry.location_id,
        rendered_config=str(ctx.install_dir / "does-not-exist.txt"),
        calibration_dir=entry.calibration_dir,
        exe=entry.exe,
        slug=entry.slug,
    )
    rc = step5._start_pipeline_foreground(ctx, entry, dry_run=True)
    assert rc == 1


# ---------------------------------------------------------------------------
# section 3.4 -- --stop / --stop-all
# ---------------------------------------------------------------------------


def test_stop_pipeline_default_only_stops_deepstream(tmp_path):
    runner = ScriptedRunner()
    runner.when(lambda a: a[:2] == ("pgrep", "-x"), returncode=0)
    ctx = FakeContext(tmp_path, runner_root=runner)
    entry = _make_entry(ctx.install_dir)

    rc = step5._stop_pipeline(ctx, entry)
    assert rc == 0
    assert runner.called_with_prefix("pkill", "-TERM", "-x", "deepstream-app")
    assert not runner.called_with_prefix("systemctl", "stop", "mosquitto")


def test_stop_pipeline_deepstream_not_running_does_not_kill(tmp_path):
    runner = ScriptedRunner(default_returncode=1)  # pgrep finds nothing
    ctx = FakeContext(tmp_path, runner_root=runner)
    entry = _make_entry(ctx.install_dir)

    step5._stop_pipeline(ctx, entry)
    assert not runner.called_with_prefix("pkill")


def test_stop_pipeline_escalates_to_sigkill_if_still_running(tmp_path):
    runner = ScriptedRunner()
    runner.when(lambda a: a[:2] == ("pgrep", "-x"), returncode=0)  # always still running
    ctx = FakeContext(tmp_path, runner_root=runner)
    entry = _make_entry(ctx.install_dir)

    step5._stop_pipeline(ctx, entry)
    assert runner.called_with_prefix("pkill", "-KILL", "-x", "deepstream-app")


def test_stop_all_also_stops_mosquitto_and_amc(tmp_path, monkeypatch):
    runner = ScriptedRunner()
    runner.when(lambda a: a[:2] == ("pgrep", "-x"), returncode=1)
    runner.when(lambda a: a[:3] == ("systemctl", "is-active", "--quiet"), returncode=0)
    ctx = FakeContext(tmp_path, runner_root=runner)
    entry = _make_entry(ctx.install_dir)

    teardown_calls = []
    monkeypatch.setattr(step3, "teardown_amc", lambda ctx_: teardown_calls.append(ctx_))

    rc = step5._stop_pipeline(ctx, entry, stop_all=True)
    assert rc == 0
    assert runner.called_with_prefix("systemctl", "stop", "mosquitto")
    assert len(teardown_calls) == 1


def test_stop_all_skip_flags_are_honored(tmp_path, monkeypatch):
    runner = ScriptedRunner()
    runner.when(lambda a: a[:2] == ("pgrep", "-x"), returncode=1)
    runner.when(lambda a: a[:3] == ("systemctl", "is-active", "--quiet"), returncode=0)
    ctx = FakeContext(tmp_path, runner_root=runner)
    entry = _make_entry(ctx.install_dir)

    teardown_calls = []
    monkeypatch.setattr(step3, "teardown_amc", lambda ctx_: teardown_calls.append(ctx_))

    step5._stop_pipeline(ctx, entry, stop_all=True, skip_mosquitto=True, skip_amc=True)
    assert not runner.called_with_prefix("systemctl", "stop", "mosquitto")
    assert teardown_calls == []


def test_pipeline_subcommand_stop_flag_dispatches_to_stop_pipeline(tmp_path, monkeypatch):
    ctx = FakeContext(tmp_path)
    _make_entry(ctx.install_dir)

    seen = {}

    def _fake_stop(ctx_, entry_, **kwargs):
        seen["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(step5, "_stop_pipeline", _fake_stop)
    rc = step5.handle_pipeline_subcommand(["--project", "North Lobby #2", "--stop-all", "--no-mosquitto"], ctx)
    assert rc == 0
    assert seen["kwargs"]["stop_all"] is True
    assert seen["kwargs"]["skip_mosquitto"] is True


# ---------------------------------------------------------------------------
# section 5.4 -- reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_registry_present_amc_dir_is_a_no_op(tmp_path, monkeypatch):
    ctx = FakeContext(tmp_path)
    entry = _make_entry(ctx.install_dir)

    amc_dir = tmp_path / "amc-projects" / entry.project_name
    amc_dir.mkdir(parents=True)
    monkeypatch.setattr(step5, "amc_project_dir", lambda ctx_, name: amc_dir)

    result = step5.reconcile_registry(ctx, apply=True, yes=True)
    assert result == []
    assert step5.get(ctx.install_dir, entry.project_name) is not None
    assert pathlib.Path(entry.exe).exists()


def test_reconcile_registry_absent_amc_dir_read_only_never_deletes(tmp_path, monkeypatch):
    ctx = FakeContext(tmp_path)
    entry = _make_entry(ctx.install_dir)

    missing_dir = tmp_path / "amc-projects" / "does-not-exist"
    monkeypatch.setattr(step5, "amc_project_dir", lambda ctx_, name: missing_dir)

    result = step5.reconcile_registry(ctx, apply=False)
    assert result == [entry.project_name]
    assert step5.get(ctx.install_dir, entry.project_name) is not None
    assert pathlib.Path(entry.exe).exists()


def test_reconcile_registry_applies_deletion_when_non_interactive(tmp_path, monkeypatch):
    ctx = FakeContext(tmp_path, non_interactive=True)
    entry = _make_entry(ctx.install_dir)

    missing_dir = tmp_path / "amc-projects" / "does-not-exist"
    monkeypatch.setattr(step5, "amc_project_dir", lambda ctx_, name: missing_dir)

    result = step5.reconcile_registry(ctx, apply=True)
    assert result == [entry.project_name]
    assert step5.get(ctx.install_dir, entry.project_name) is None
    assert not pathlib.Path(entry.exe).exists()


def test_reconcile_registry_interactive_requires_confirm(tmp_path, monkeypatch):
    ctx = FakeContext(tmp_path, non_interactive=False)
    entry = _make_entry(ctx.install_dir)

    missing_dir = tmp_path / "amc-projects" / "does-not-exist"
    monkeypatch.setattr(step5, "amc_project_dir", lambda ctx_, name: missing_dir)

    # Declined -> nothing removed.
    result = step5.reconcile_registry(ctx, apply=True, confirm=lambda _msg: False)
    assert result == []
    assert step5.get(ctx.install_dir, entry.project_name) is not None

    # Confirmed -> removed.
    result = step5.reconcile_registry(ctx, apply=True, confirm=lambda _msg: True)
    assert result == [entry.project_name]
    assert step5.get(ctx.install_dir, entry.project_name) is None


def test_reconcile_registry_yes_flag_skips_confirm(tmp_path, monkeypatch):
    ctx = FakeContext(tmp_path, non_interactive=False)
    entry = _make_entry(ctx.install_dir)

    missing_dir = tmp_path / "amc-projects" / "does-not-exist"
    monkeypatch.setattr(step5, "amc_project_dir", lambda ctx_, name: missing_dir)

    def _boom(_msg):  # pragma: no cover -- must never be called
        raise AssertionError("confirm() must not be called when yes=True")

    result = step5.reconcile_registry(ctx, apply=True, yes=True, confirm=_boom)
    assert result == [entry.project_name]


def test_projects_subcommand_list_reports_drift_without_deleting(tmp_path, monkeypatch, capsys):
    ctx = FakeContext(tmp_path)
    entry = _make_entry(ctx.install_dir)
    missing_dir = tmp_path / "amc-projects" / "does-not-exist"
    monkeypatch.setattr(step5, "amc_project_dir", lambda ctx_, name: missing_dir)

    rc = step5.handle_projects_subcommand([], ctx)
    assert rc == 0
    assert step5.get(ctx.install_dir, entry.project_name) is not None


def test_projects_subcommand_reconcile_applies_under_yes(tmp_path, monkeypatch):
    ctx = FakeContext(tmp_path)
    entry = _make_entry(ctx.install_dir)
    missing_dir = tmp_path / "amc-projects" / "does-not-exist"
    monkeypatch.setattr(step5, "amc_project_dir", lambda ctx_, name: missing_dir)

    rc = step5.handle_projects_subcommand(["--reconcile", "--yes"], ctx)
    assert rc == 0
    assert step5.get(ctx.install_dir, entry.project_name) is None


def test_projects_subcommand_remove_removes_named_project(tmp_path):
    ctx = FakeContext(tmp_path)
    entry = _make_entry(ctx.install_dir)

    rc = step5.handle_projects_subcommand(["--remove", entry.project_name], ctx)
    assert rc == 0
    assert step5.get(ctx.install_dir, entry.project_name) is None
    assert not pathlib.Path(entry.exe).exists()


def test_projects_subcommand_remove_unknown_project_fails(tmp_path):
    ctx = FakeContext(tmp_path)
    rc = step5.handle_projects_subcommand(["--remove", "nope"], ctx)
    assert rc == 1


# ---------------------------------------------------------------------------
# section 3.2 -- generated wrapper content + idempotency
# ---------------------------------------------------------------------------


def test_write_pipeline_wrapper_is_content_idempotent(tmp_path):
    ctx = FakeContext(tmp_path)
    installer_bin = ctx.install_dir / "bin" / "mv3dt-installer"

    path1, changed1 = step5.write_pipeline_wrapper(ctx, installer_bin, "P1", "loc", "p1")
    assert changed1 is True
    assert "--project \"P1\"" in path1.read_text(encoding="utf-8")

    path2, changed2 = step5.write_pipeline_wrapper(ctx, installer_bin, "P1", "loc", "p1")
    assert changed2 is False
    assert path1 == path2


def test_write_record_wrapper_uses_record_subcommand(tmp_path):
    ctx = FakeContext(tmp_path)
    installer_bin = ctx.install_dir / "bin" / "mv3dt-installer"
    path, _changed = step5.write_record_wrapper(ctx, installer_bin, "P1", "loc", "p1")
    content = path.read_text(encoding="utf-8")
    assert '" record --project "P1"' in content


# ---------------------------------------------------------------------------
# generate_preview_config -- basic structural port check
# ---------------------------------------------------------------------------


def test_generate_preview_config_enables_display_and_keeps_mqtt():
    source = "\n".join(
        [
            "[source0]",
            "enable=1",
            "[source1]",
            "enable=1",
            "[sink1]",
            "enable=1",
            "msg-conv-config=msgconv_config.txt",
        ]
    )
    rendered = step5.generate_preview_config(source)
    assert "[tiled-display]" in rendered
    assert "rows=2" in rendered or "rows=1" in rendered
    assert "[sink0]" in rendered
    assert "enable=1" in rendered
    # sink1 (MQTT) config line is preserved.
    assert "msg-conv-config=msgconv_config.txt" in rendered
