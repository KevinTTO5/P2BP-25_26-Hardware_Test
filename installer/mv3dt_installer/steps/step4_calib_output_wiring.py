"""Step 4 -- Calibration output placement + DeepStream config wiring.

Implements `installer/plan/STEP-4-CALIB-OUTPUT-WIRING.md` against the
framework contract in `installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md`
(step-module interface section 12, install-location config section 11,
USER-ACTION display section 9, logging/reporting section 8, camera
discovery section 15) and against the `ingest` subcommand extension point
STEP-3 section 6.2 flags and `app.py` builds
(`SUBCOMMAND_REGISTRY`/`register_subcommand`).

Once a human finishes AutoMagicCalib's Results/Export step (STEP-3 brings up
the browser UI; the operator drives the 6-step workflow through Execute and
Results/Export), this module:

1. **Ingests** the export into a calibration directory -- exporter-first,
   raw-copy fallback (section 4.2, a direct port of
   `laptop/scripts/40_export_watcher.sh`'s `ingest_exports()`).
2. **Prompts** (on success) for where that directory should live, with the
   framework's "default prefilled" path-prompt convention (section 5, the
   same convention `config._prompt_for_install_dir` establishes for
   `--install-dir`).
3. **Wires** the chosen location into `<install_dir>/deepstream/`: patches
   `config_tracker_NvMOT.yml`'s `calibrationDirectory`/`nodeID`, renders
   `deepstream_app_config.rendered.txt` from the committed template, and
   copies the two reference-only siblings verbatim (section 6).
4. **Installs** the `mv3dt-ingest-<slug>.path`/`.service` pair after the
   first successful ingest, so every later recalibration is picked up with
   no installer run at all (section 4.5).

Order decision (section 4.1's "DevD picks one; document the chosen order")
----------------------------------------------------------------------------
This module **prompts before ingesting**, not after: `_resolve_calibration_dir`
resolves the final destination (persisted `CALIBRATION_DIR` if one already
exists, else the section-5 prompt against the section-4.1 default) *before*
`ingest_export()` ever runs, and the ingest writes directly into that final
directory. This is the "equivalently, prompt first" branch section 4.1
explicitly allows, and it avoids ever having to move a freshly-ingested
export tree into an operator-chosen alternate location -- there is exactly
one destination directory per run, chosen once, and idempotent re-runs
(section 4.3) resolve the very same directory from `CALIBRATION_DIR` without
re-prompting.

"Step 3 complete" without a state-machine handle on `Context`
----------------------------------------------------------------------------
Like `step3_amc_launcher.py` does for "Step 2 complete" (see that module's
own docstring), `Context` (doc 00 section 12.3) carries no accessor for
another step's recorded `state.json` status. This module follows the same
precedent: Step 3's durable, always-present deliverable after a successful
run is the `<install_dir>/bin/amc` wrapper it writes
(`step3_amc_launcher.AMC_WRAPPER_NAME`), so `preflight()` checks that file's
presence/executability rather than inventing a new `Context` capability.

Every subprocess call goes through `ctx.run_root`/`ctx.run_as_user` (doc 00
section 9.2: the AMC exporter runs under the invoking user's home, so it
goes through `run_as_user`; `systemctl` calls run as root, already the
process's privilege level by the time any step runs), so no test here
shells out to a real python3/systemctl.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import re
import shutil
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

import yaml

from mv3dt_installer import app as app_mod
from mv3dt_installer import cameras as cameras_mod
from mv3dt_installer import config as config_mod
from mv3dt_installer import systemd
from mv3dt_installer import waitui
from mv3dt_installer.steps import StepResult, StepStatus, UserAction, register
from mv3dt_installer.steps import step3_amc_launcher as step3_mod

if TYPE_CHECKING:  # pragma: no cover -- import-time only, never at runtime.
    from mv3dt_installer.app import Context

__all__ = [
    "ProjectInputs",
    "resolve_project_inputs",
    "default_calibration_dir",
    "IngestOutcome",
    "ingest_export",
    "render_tracker_yaml",
    "render_app_config",
    "INCOMPLETE_CALIBRATION_MESSAGE",
    "handle_ingest_subcommand",
    "Step4CalibOutputWiring",
]

# ---------------------------------------------------------------------------
# section 7 -- installer.conf keys this step reads/writes
# ---------------------------------------------------------------------------

CONF_LOCATION_ID_KEY = "LOCATION_ID"
CONF_PROJECT_NAME_KEY = "PROJECT_NAME"
CONF_AMC_ROOT_KEY = "AMC_ROOT"
CONF_CAM_USER_KEY = "CAM_USER"
CONF_CAM_PASSWORD_KEY = "CAM_PASSWORD"
CONF_AMC_EXPORT_WAIT_S_KEY = "AMC_EXPORT_WAIT_S"
CONF_CALIBRATION_DIR_KEY = "CALIBRATION_DIR"

DEFAULT_EXPORT_WAIT_S = 3600.0

# section 6 -- bundled template names, resolved via ctx.asset_path("deepstream", ...)
APP_CONFIG_TEMPLATE_NAME = "deepstream_app_config.txt"
TRACKER_YAML_TEMPLATE_NAME = "config_tracker_NvMOT.yml"
INFER_PRIMARY_TEMPLATE_NAME = "config_infer_primary.txt"
MSGCONV_TEMPLATE_NAME = "msgconv_config.txt"

# section 6.2 -- rendered output name.
RENDERED_APP_CONFIG_NAME = "deepstream_app_config.rendered.txt"


# ---------------------------------------------------------------------------
# section 7 -- project input resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectInputs:
    """Resolved section-7 sourcing: `LOCATION_ID`, `PROJECT_NAME`,
    `AMC_ROOT`, `CAM_USER`, `CAM_PASSWORD`, `AMC_EXPORT_WAIT_S`."""

    location_id: str
    project_name: str
    amc_root: pathlib.Path
    cam_user: str
    cam_password: str
    export_wait_s: float


_REQUIRED_CONF_KEYS: tuple[str, ...] = (
    CONF_LOCATION_ID_KEY,
    CONF_CAM_USER_KEY,
    CONF_CAM_PASSWORD_KEY,
)


def _to_float(value: Optional[str], default: float) -> float:
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def resolve_project_inputs(
    ctx: "Context",
) -> tuple[Optional[ProjectInputs], Optional[str]]:
    """Resolve section 3 step 2's required inputs from `ctx.conf`.

    Returns `(inputs, None)` on success, or `(None, missing_key)` naming the
    first required key that was missing/empty -- mirroring the `:
    "${VAR:?}"` guards at the top of `40_export_watcher.sh`.
    """
    conf = ctx.conf
    for key in _REQUIRED_CONF_KEYS:
        if not conf.get(key):
            return None, key

    location_id = conf[CONF_LOCATION_ID_KEY]
    project_name = conf.get(CONF_PROJECT_NAME_KEY) or location_id
    amc_root = pathlib.Path(
        conf.get(CONF_AMC_ROOT_KEY)
        or str(pathlib.Path(ctx.user.home) / "auto-magic-calib")
    ).expanduser()

    inputs = ProjectInputs(
        location_id=location_id,
        project_name=project_name,
        amc_root=amc_root,
        cam_user=conf[CONF_CAM_USER_KEY],
        cam_password=conf[CONF_CAM_PASSWORD_KEY],
        export_wait_s=_to_float(
            conf.get(CONF_AMC_EXPORT_WAIT_S_KEY), DEFAULT_EXPORT_WAIT_S
        ),
    )
    return inputs, None


def _export_dir(inputs: ProjectInputs) -> pathlib.Path:
    return inputs.amc_root / "projects" / inputs.project_name / "exports"


def default_calibration_dir(ctx: "Context", location_id: str) -> pathlib.Path:
    """section 4.1 default: `<install_dir>/deepstream/calibration/<LOCATION_ID>/`."""
    return ctx.install_dir / "deepstream" / "calibration" / location_id


def _missing_conf_result(ctx: "Context", key: str) -> StepResult:
    return StepResult(
        status=StepStatus.USER_ACTION_REQUIRED,
        message=f"{key} is not set",
        user_actions=[
            UserAction(
                text=f"Set {key} in installer.conf.",
                path=str(ctx.install_dir / config_mod.CONF_FILENAME),
            )
        ],
    )


def _step3_complete(ctx: "Context") -> bool:
    """Durable Step-3 signal -- see this module's docstring."""
    wrapper = ctx.install_dir / "bin" / step3_mod.AMC_WRAPPER_NAME
    return wrapper.is_file() and os.access(wrapper, os.X_OK)


# ---------------------------------------------------------------------------
# section 4.4 -- the export-wait hint list
# ---------------------------------------------------------------------------


def _export_wait_hints(ctx: "Context") -> list[UserAction]:
    ui_port = ctx.conf.get(step3_mod.CONF_UI_PORT_KEY) or step3_mod.DEFAULT_UI_PORT
    return [
        UserAction(
            text=(
                f"Open the AMC UI (Step 3 left it running at "
                f"http://localhost:{ui_port})."
            )
        ),
        UserAction(
            text=(
                "Complete the 6-step workflow through Execute and Results / "
                "Export; on Results choose MV3DT ZIP AMC (or MV3DT ZIP VGGT)."
            )
        ),
        UserAction(
            text="Confirm files appear under $AMC_ROOT/projects/$PROJECT_NAME/exports/."
        ),
    ]


# ---------------------------------------------------------------------------
# section 4.1/4.5 -- calibration directory resolution + the path prompt
# ---------------------------------------------------------------------------

#: Injectable stand-in for `input()`, mirroring `step3_amc_launcher._INPUT`.
_PROMPT: Callable[[str], str] = input


def _prompt_for_calibration_dir(
    default: pathlib.Path, prompt: Callable[[str], str]
) -> pathlib.Path:
    """section 5's prompt: framework path-prompt convention (default
    prefilled, empty answer accepts it) -- the same shape
    `config._prompt_for_install_dir` uses for `--install-dir`."""
    answer = prompt(
        "Choose where the calibration output for this project should live "
        f"[{default}]: "
    ).strip()
    return pathlib.Path(answer).expanduser() if answer else default


def _resolve_calibration_dir(
    ctx: "Context", inputs: ProjectInputs, *, allow_prompt: bool = True
) -> pathlib.Path:
    """Resolve the ingest destination (module docstring's "prompt first"
    order): the already-persisted `CALIBRATION_DIR` if one exists, else the
    section-5 prompt against the section-4.1 default (skipped when
    `allow_prompt` is False -- the `ingest` subcommand's contract, section
    4.5: "no in-session wait", the same rule `--non-interactive` follows for
    a normal run)."""
    persisted = ctx.conf.get(CONF_CALIBRATION_DIR_KEY)
    if persisted:
        return pathlib.Path(persisted)

    default_dest = default_calibration_dir(ctx, inputs.location_id)
    if allow_prompt and not ctx.non_interactive:
        return _prompt_for_calibration_dir(default_dest, _PROMPT)
    return default_dest


def _calibration_directory_value(
    ctx: "Context", location_id: str, calibration_dir: pathlib.Path
) -> str:
    """section 6.1: keep the default location's relative form
    (`calibration/<LOCATION_ID>`, working-dir-relative to `deepstream/`),
    write the absolute path for any alternate."""
    if calibration_dir == default_calibration_dir(ctx, location_id):
        return f"calibration/{location_id}"
    return str(calibration_dir)


def _ensure_dir(ctx: "Context", path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(path, ctx.user.uid, ctx.user.gid)
    except OSError:
        pass  # best-effort, e.g. under a non-root test process


def _chown_tree(ctx: "Context", root: pathlib.Path) -> None:
    """doc 00 section 9.2: files created for the operator MUST be chowned
    to the invoking user/group. Best-effort per entry -- a single
    unreadable/racy entry must not abort an otherwise-successful ingest."""
    try:
        os.chown(root, ctx.user.uid, ctx.user.gid)
    except OSError:
        pass
    for entry in root.rglob("*"):
        try:
            os.chown(entry, ctx.user.uid, ctx.user.gid)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# section 4.2 -- exporter-first, copy-fallback ingest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestOutcome:
    used_exporter: bool
    ingested: bool
    stamp: str


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _unpack_zip_exports(dest: pathlib.Path) -> None:
    """section 4.2 step 2: a "MV3DT ZIP AMC/VGGT" export lands in `dest` as
    a flat copy of `EXPORT_DIR`'s contents, including the zip itself --
    unpack it in place so the tracker sees a flat `camInfo`-style directory."""
    for zip_path in sorted(dest.glob("*.zip")):
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)


def ingest_export(
    ctx: "Context",
    *,
    amc_root: pathlib.Path,
    project_name: str,
    dest: pathlib.Path,
) -> IngestOutcome:
    """Port of `40_export_watcher.sh`'s `ingest_exports()` (section 4.2).

    Tries the upstream exporter first (`run_as_user`, doc 00 section 9.2 --
    it runs under the invoking user's `$AMC_ROOT`); on a missing or failing
    exporter, falls back to a raw recursive copy of `EXPORT_DIR`, then
    unpacks any zip export found in `dest`. Returns `ingested=False` (no
    breadcrumb written) when neither path produced anything -- the caller
    maps that to `USER_ACTION_REQUIRED` (section 4.2 step 3).
    """
    export_dir = amc_root / "projects" / project_name / "exports"
    exporter = amc_root / "scripts" / "export_mv3dt.py"
    stamp = _utc_stamp()

    used_exporter = False
    if exporter.is_file():
        ctx.log.info(f"running upstream exporter: {exporter}")
        result = ctx.run_as_user(
            "python3",
            "scripts/export_mv3dt.py",
            "--project",
            project_name,
            "--output",
            str(dest),
            cwd=str(amc_root),
            capture_output=True,
            text=True,
        )
        if getattr(result, "returncode", 1) == 0:
            used_exporter = True
        else:
            ctx.log.warn(
                f"upstream exporter failed (exit {getattr(result, 'returncode', '?')}); "
                f"falling back to raw copy from {export_dir}"
            )
    else:
        ctx.log.info(
            "no upstream exporter script found (upstream layout may have changed)"
        )

    if not used_exporter:
        if not export_dir.is_dir() or not any(export_dir.iterdir()):
            return IngestOutcome(used_exporter=False, ingested=False, stamp=stamp)
        shutil.copytree(export_dir, dest, dirs_exist_ok=True)
        _unpack_zip_exports(dest)

    with open(dest / ".ingest.log", "a", encoding="utf-8") as f:
        f.write(f"{stamp}  ingested from {export_dir}\n")

    return IngestOutcome(used_exporter=used_exporter, ingested=True, stamp=stamp)


# ---------------------------------------------------------------------------
# section 8.1 -- "poor calibration (bad RMSE)" incomplete-export detection
# ---------------------------------------------------------------------------

#: Exact wording from section 8.1's "Poor calibration (bad RMSE)" failure
#: surface -- used verbatim as both the `StepResult.message` and the single
#: `UserAction` text.
INCOMPLETE_CALIBRATION_MESSAGE = (
    "Re-run AMC Execute and re-export — the calibration looks incomplete / "
    "RMSE was rejected on the Results screen."
)

#: Breadcrumb file this module itself writes (section 4.2 step 4) -- never a
#: per-camera calibration file, so it must not count toward completeness.
_IGNORED_CALIBRATION_FILENAMES = {".ingest.log"}


def _calibration_file_count(dest: pathlib.Path) -> int:
    """Count of per-camera calibration files ingested into `dest` (section
    8.1's "e.g. fewer camInfo files than enabled cameras" check): every
    regular file except the `.ingest.log` breadcrumb and a `.zip` archive
    (already unpacked by `_unpack_zip_exports`, so it is packaging, not a
    calibration file itself) -- counted recursively since section 2 treats
    the export as "an opaque directory of calibration files" with no fixed
    layout guaranteed."""
    if not dest.is_dir():
        return 0
    count = 0
    for entry in dest.rglob("*"):
        if not entry.is_file():
            continue
        if entry.name in _IGNORED_CALIBRATION_FILENAMES or entry.suffix == ".zip":
            continue
        count += 1
    return count


def _enabled_camera_count(ctx: "Context") -> Optional[int]:
    """Number of `enabled: true` entries in the `CAMERAS_FILE` inventory, or
    `None` when it is unset/unreadable. `preflight()` already guarantees
    `CAMERAS_FILE` is set and parses before `run()`/`verify()` ever reach
    this point, but this stays defensive (returns "unknown", never raises)
    rather than assume that invariant always holds."""
    cameras_path = pathlib.Path(ctx.conf.get(config_mod.CAMERAS_FILE_KEY, ""))
    if not cameras_path.is_file():
        return None
    try:
        cams = cameras_mod.parse_inventory(cameras_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return sum(1 for cam in cams if cam.enabled)


def _incomplete_calibration_result() -> StepResult:
    return StepResult(
        status=StepStatus.USER_ACTION_REQUIRED,
        message=INCOMPLETE_CALIBRATION_MESSAGE,
        user_actions=[UserAction(text=INCOMPLETE_CALIBRATION_MESSAGE)],
    )


# ---------------------------------------------------------------------------
# section 6.1 -- config_tracker_NvMOT.yml patching
# ---------------------------------------------------------------------------

# Line-based substitution, not a YAML parse/dump round-trip: the committed
# template's `%YAML:1.0` first line is OpenCV's FileStorage marker, not
# valid YAML (`yaml.safe_load` rejects it -- see `_parse_tracker_yaml`,
# which strips it for read-back verification only). Regex substitution also
# preserves every comment and the field ordering untouched, matching
# `40_export_watcher.sh`'s own text-substitution discipline for the app
# config (section 6.2).
_NODE_ID_RE = re.compile(r"(?m)^(\s*nodeID:\s*).*$")
_CALIBRATION_DIR_RE = re.compile(r"(?m)^(\s*calibrationDirectory:\s*).*$")


def render_tracker_yaml(
    template_text: str, *, location_id: str, calibration_directory: str
) -> str:
    """Patch `SV3DT.calibrationDirectory` and `MV3DT.nodeID` (section 6.1).
    Every other field -- `projectionType`, the MQTT broker fields, all the
    tuning blocks -- is left byte-for-byte as the template wrote it."""
    text = _NODE_ID_RE.sub(lambda m: m.group(1) + location_id, template_text, count=1)
    text = _CALIBRATION_DIR_RE.sub(
        lambda m: m.group(1) + calibration_directory, text, count=1
    )
    return text


def _parse_tracker_yaml(text: str) -> Optional[dict]:
    """Read-back parse for `verify()`: strip the OpenCV `%YAML:...` marker
    line (if present) before handing the rest to `yaml.safe_load`."""
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("%YAML"):
        text = "\n".join(lines[1:])
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# section 6.2 -- deepstream_app_config.rendered.txt rendering
# ---------------------------------------------------------------------------

_SOURCE_BLOCK_RE = re.compile(r"\[source(\d+)\][^\[]*", re.DOTALL)
_URI_LINE_RE = re.compile(r"(?m)^uri=.*$")


def _rtsp_uri(cam, *, user: str, password: str) -> str:
    return f"rtsp://{user}:{password}@{cam.ip}:554{cam.rtsp_path}"


def render_app_config(
    template_text: str,
    *,
    cam_user: str,
    cam_password: str,
    location_id: str,
    cameras: list,
    template_path: str,
    calibration_dir: "pathlib.Path | str",
) -> str:
    """Render `deepstream_app_config.txt` -> `.rendered.txt` (section 6.2),
    porting `40_export_watcher.sh`'s `render_pipeline()`/`rewrite_source_uris()`.

    `cameras` is the **enabled** subset of the parsed `CAMERAS_FILE`
    inventory, in file order -- `[sourceN]`'s `uri=` is rewritten from
    `cameras[N]` when present; a `[sourceN]` block with no matching entry
    (more source blocks than enabled cameras) is left untouched.
    """
    text = template_text
    text = text.replace("${CAM_USER}", cam_user)
    text = text.replace("${CAM_PASSWORD}", cam_password)
    text = text.replace("${LOCATION_ID}", location_id)

    def _replace_block(match: "re.Match[str]") -> str:
        idx = int(match.group(1))
        block = match.group(0)
        if idx >= len(cameras):
            return block
        uri = _rtsp_uri(cameras[idx], user=cam_user, password=cam_password)
        return _URI_LINE_RE.sub(f"uri={uri}", block, count=1)

    text = _SOURCE_BLOCK_RE.sub(_replace_block, text)

    header = (
        "# Rendered by mv3dt-installer step4_calib_output_wiring\n"
        f"# Source template: {template_path}\n"
        f"# Calibration dir: {calibration_dir}\n"
        f"# LOCATION_ID    : {location_id}\n"
        "# Edit the committed template, not this file; it is regenerated.\n"
        "\n"
    )
    return header + text


def _source_block(text: str, idx: int) -> str:
    match = re.search(rf"\[source{idx}\][^\[]*", text, re.DOTALL)
    return match.group(0) if match else ""


# ---------------------------------------------------------------------------
# Writing rendered/copied artifacts (report + chown, content-idempotent)
# ---------------------------------------------------------------------------


def _write_text_report(ctx: "Context", path: pathlib.Path, text: str, *, label: str) -> bool:
    changed = True
    if path.exists():
        try:
            changed = path.read_text(encoding="utf-8") != text
        except OSError:
            changed = True
    path.write_text(text, encoding="utf-8")
    try:
        os.chown(path, ctx.user.uid, ctx.user.gid)
    except OSError:
        pass  # best-effort, e.g. under a non-root test process
    if changed:
        ctx.report_installed(label, str(path))
    else:
        ctx.report_already_installed(label, str(path))
    return changed


def _render_configs(
    ctx: "Context", *, inputs: ProjectInputs, calibration_dir: pathlib.Path
) -> None:
    deepstream_dir = ctx.install_dir / "deepstream"
    _ensure_dir(ctx, deepstream_dir)

    tracker_template_path = ctx.asset_path("deepstream", TRACKER_YAML_TEMPLATE_NAME)
    tracker_template = tracker_template_path.read_text(encoding="utf-8")
    calibration_value = _calibration_directory_value(
        ctx, inputs.location_id, calibration_dir
    )
    tracker_text = render_tracker_yaml(
        tracker_template,
        location_id=inputs.location_id,
        calibration_directory=calibration_value,
    )
    _write_text_report(
        ctx,
        deepstream_dir / TRACKER_YAML_TEMPLATE_NAME,
        tracker_text,
        label=TRACKER_YAML_TEMPLATE_NAME,
    )

    cameras_path = pathlib.Path(ctx.conf.get(config_mod.CAMERAS_FILE_KEY, ""))
    cameras = []
    if cameras_path.is_file():
        cameras = [
            cam
            for cam in cameras_mod.parse_inventory(
                cameras_path.read_text(encoding="utf-8")
            )
            if cam.enabled
        ]

    app_template_path = ctx.asset_path("deepstream", APP_CONFIG_TEMPLATE_NAME)
    app_template = app_template_path.read_text(encoding="utf-8")
    app_text = render_app_config(
        app_template,
        cam_user=inputs.cam_user,
        cam_password=inputs.cam_password,
        location_id=inputs.location_id,
        cameras=cameras,
        template_path=str(app_template_path),
        calibration_dir=calibration_dir,
    )
    _write_text_report(
        ctx,
        deepstream_dir / RENDERED_APP_CONFIG_NAME,
        app_text,
        label=RENDERED_APP_CONFIG_NAME,
    )

    for sibling in (INFER_PRIMARY_TEMPLATE_NAME, MSGCONV_TEMPLATE_NAME):
        src = ctx.asset_path("deepstream", sibling)
        if not src.is_file():
            ctx.log.warn(f"missing bundled template {src}; not copying {sibling}")
            continue
        _write_text_report(
            ctx,
            deepstream_dir / sibling,
            src.read_text(encoding="utf-8"),
            label=sibling,
        )


# ---------------------------------------------------------------------------
# section 4.5 -- re-ingest systemd path/service unit pair
# ---------------------------------------------------------------------------

_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_LEN = 64


def _slugify(name: str, *, location_id: str) -> str:
    """STEP-5 section 3.1's sanitization rule, ported here because the
    section-4.5 systemd unit names need it before Step 5 exists."""
    slug = _SLUG_INVALID_RE.sub("-", name.strip().lower()).strip("-")
    if not slug:
        slug = _SLUG_INVALID_RE.sub("-", location_id.strip().lower()).strip("-")
    if not slug:
        slug = "project"
    return slug[:_SLUG_MAX_LEN].strip("-") or "project"


def _systemd_runner(ctx: "Context"):
    return lambda argv, **kwargs: ctx.run_root(*argv, **kwargs)


def _install_reingest_units(
    ctx: "Context", *, inputs: ProjectInputs, export_dir: pathlib.Path
) -> None:
    slug = _slugify(inputs.project_name, location_id=inputs.location_id)
    installer_bin = ctx.install_dir / "bin" / step3_mod.INSTALLER_BIN_NAME
    runner = _systemd_runner(ctx)

    units = systemd.render_ingest_units(
        project=inputs.project_name,
        slug=slug,
        export_dir=export_dir,
        install_dir=ctx.install_dir,
        installer_bin=installer_bin,
    )

    any_changed = False
    for name, content in units.items():
        changed = systemd.install_unit(
            name, content, unit_dir=systemd.UNIT_DIR, runner=runner
        )
        any_changed = any_changed or changed
        if changed:
            ctx.report_installed(name, "systemd unit")
        else:
            ctx.report_already_installed(name, "systemd unit")

    if any_changed:
        systemd.daemon_reload(runner=runner)

    # section 4.5's carve-out: only the .path unit is ever enabled;
    # systemd.enable_now() itself refuses (and logs) when ExecStart's
    # binary is absent -- see this module's verify()/report() for the
    # matching downgrade-to-warning surface.
    systemd.enable_now(f"mv3dt-ingest-{slug}.path", runner=runner)


# ---------------------------------------------------------------------------
# The Step
# ---------------------------------------------------------------------------


class Step4CalibOutputWiring:
    """STEP-4-CALIB-OUTPUT-WIRING.md section 1: module identity."""

    id = "step4_calib_output_wiring"
    title = "Calibration output wiring"
    order = 4

    # -- preflight (section 3) -----------------------------------------------

    def preflight(self, ctx: "Context") -> StepResult:
        if not _step3_complete(ctx):
            return StepResult(
                status=StepStatus.FAILED,
                message="AutoMagicCalib launcher (Step 3) is not complete; run Step 3 first",
            )

        inputs, missing = resolve_project_inputs(ctx)
        if inputs is None:
            return _missing_conf_result(ctx, missing)  # type: ignore[arg-type]

        if not inputs.amc_root.is_dir():
            return StepResult(
                status=StepStatus.FAILED,
                message=f"AMC not present at {inputs.amc_root}; run Step 3 first",
            )

        export_dir = _export_dir(inputs)
        outcome = waitui.wait_until(
            waitui.dir_has_files(export_dir),
            description=f"Waiting for the AMC export for {inputs.project_name}",
            hint_actions=_export_wait_hints(ctx),
            timeout_s=inputs.export_wait_s,
            non_interactive=ctx.non_interactive,
        )
        if outcome is not waitui.WaitOutcome.SATISFIED:
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message="AMC export is not ready yet",
                user_actions=_export_wait_hints(ctx),
            )

        for name in (APP_CONFIG_TEMPLATE_NAME, TRACKER_YAML_TEMPLATE_NAME):
            template_path = ctx.asset_path("deepstream", name)
            if not template_path.is_file():
                return StepResult(
                    status=StepStatus.FAILED,
                    message=f"missing bundled template: {template_path}",
                )

        cameras_file = ctx.conf.get(config_mod.CAMERAS_FILE_KEY)
        if not cameras_file:
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message="CAMERAS_FILE is not set",
                user_actions=[
                    UserAction(
                        text="Run the camera discovery scan.",
                        command="mv3dt-installer --scan-cameras",
                    )
                ],
            )
        cameras_path = pathlib.Path(cameras_file)
        if not cameras_path.is_file():
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message=f"CAMERAS_FILE ({cameras_path}) does not exist",
                user_actions=[
                    UserAction(
                        text="Run the camera discovery scan.",
                        command="mv3dt-installer --scan-cameras",
                    )
                ],
            )
        try:
            cameras_mod.parse_inventory(cameras_path.read_text(encoding="utf-8"))
        except OSError as exc:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"could not read {cameras_path}: {exc}",
            )

        return StepResult(status=StepStatus.COMPLETE)

    # -- run (section 4/5/6) --------------------------------------------------

    def run(self, ctx: "Context") -> StepResult:
        inputs, missing = resolve_project_inputs(ctx)
        if inputs is None:
            return _missing_conf_result(ctx, missing)  # type: ignore[arg-type]

        dest = _resolve_calibration_dir(ctx, inputs, allow_prompt=True)
        was_populated = dest.is_dir() and any(dest.iterdir())
        _ensure_dir(ctx, dest)

        outcome = ingest_export(
            ctx, amc_root=inputs.amc_root, project_name=inputs.project_name, dest=dest
        )
        if not outcome.ingested:
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message="AMC export was empty at ingest time",
                user_actions=_export_wait_hints(ctx),
            )
        _chown_tree(ctx, dest)

        label = f"{inputs.project_name}@{outcome.stamp}"
        if was_populated:
            ctx.report_already_installed("calibration-export", label)
        else:
            ctx.report_installed("calibration-export", label)

        enabled_count = _enabled_camera_count(ctx)
        file_count = _calibration_file_count(dest)
        if enabled_count is not None and file_count < enabled_count:
            ctx.log.warn(
                f"calibration export looks incomplete: {file_count} file(s) "
                f"in {dest} for {enabled_count} enabled camera(s)"
            )
            return _incomplete_calibration_result()

        # `first_success` gates (re-)installing the re-ingest units on
        # `CALIBRATION_DIR` not yet being persisted -- i.e. this is the very
        # first ingest that got this far. Known limitation: if the units
        # land installed-but-not-enabled here because
        # `<install_dir>/bin/mv3dt-installer` (STEP-3 section 6.1) has not
        # been dropped yet, a later re-run never retries `enable_now` --
        # `CALIBRATION_DIR` is persisted by then, so `first_success` is
        # False on every subsequent run. `verify()`'s downgrade-to-warning
        # (section 8 check 5) keeps that from failing loudly, but the path
        # unit can end up permanently un-enabled. Accepted as-is because
        # Step 3 always drops the binary before Step 4 runs in the normal
        # dispatch order (doc 00 section 12.4's step ordering); revisit with
        # an idempotent "always call enable_now, it already no-ops when the
        # unit is missing/absent-binary" tweak if that ordering assumption
        # ever stops holding.
        first_success = not bool(ctx.conf.get(CONF_CALIBRATION_DIR_KEY))
        config_mod.persist_value(ctx.install_dir, CONF_CALIBRATION_DIR_KEY, str(dest))
        ctx.conf[CONF_CALIBRATION_DIR_KEY] = str(dest)

        _render_configs(ctx, inputs=inputs, calibration_dir=dest)

        if first_success:
            _install_reingest_units(ctx, inputs=inputs, export_dir=_export_dir(inputs))

        return StepResult(status=StepStatus.COMPLETE)

    # -- verify (section 8) ---------------------------------------------------

    def verify(self, ctx: "Context") -> StepResult:
        inputs, missing = resolve_project_inputs(ctx)
        if inputs is None:
            return StepResult(status=StepStatus.FAILED, message=f"{missing} is not set")

        calibration_dir = pathlib.Path(
            ctx.conf.get(CONF_CALIBRATION_DIR_KEY)
            or default_calibration_dir(ctx, inputs.location_id)
        )
        if not calibration_dir.is_dir() or not any(calibration_dir.iterdir()):
            return StepResult(
                status=StepStatus.FAILED,
                message=f"calibration dir {calibration_dir} is empty",
            )

        enabled_count = _enabled_camera_count(ctx)
        file_count = _calibration_file_count(calibration_dir)
        if enabled_count is not None and file_count < enabled_count:
            return _incomplete_calibration_result()

        deepstream_dir = ctx.install_dir / "deepstream"
        tracker_path = deepstream_dir / TRACKER_YAML_TEMPLATE_NAME
        if not tracker_path.is_file():
            return StepResult(status=StepStatus.FAILED, message=f"missing {tracker_path}")

        tracker_data = _parse_tracker_yaml(tracker_path.read_text(encoding="utf-8"))
        if tracker_data is None:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"could not parse {tracker_path} as YAML",
            )

        sv3dt = tracker_data.get("SV3DT") or {}
        mv3dt = tracker_data.get("MV3DT") or {}

        expected_cal_value = _calibration_directory_value(
            ctx, inputs.location_id, calibration_dir
        )
        if str(sv3dt.get("calibrationDirectory", "")) != expected_cal_value:
            return StepResult(
                status=StepStatus.FAILED,
                message="SV3DT.calibrationDirectory does not match the chosen calibration dir",
            )
        if str(mv3dt.get("nodeID", "")) != inputs.location_id:
            return StepResult(
                status=StepStatus.FAILED,
                message="MV3DT.nodeID does not match LOCATION_ID",
            )

        if not ctx.verify_pinned(
            "SV3DT.projectionType", str(sv3dt.get("projectionType", "")), "homography"
        ):
            return StepResult(
                status=StepStatus.FAILED, message="SV3DT.projectionType changed unexpectedly"
            )
        if not ctx.verify_pinned(
            "MV3DT.mqttBrokerIP", str(mv3dt.get("mqttBrokerIP", "")), "127.0.0.1"
        ):
            return StepResult(
                status=StepStatus.FAILED, message="MV3DT.mqttBrokerIP changed unexpectedly"
            )
        if not ctx.verify_pinned(
            "MV3DT.mqttBrokerPort", str(mv3dt.get("mqttBrokerPort", "")), "1883"
        ):
            return StepResult(
                status=StepStatus.FAILED,
                message="MV3DT.mqttBrokerPort changed unexpectedly",
            )

        rendered_path = deepstream_dir / RENDERED_APP_CONFIG_NAME
        if not rendered_path.is_file():
            return StepResult(status=StepStatus.FAILED, message=f"missing {rendered_path}")
        rendered_text = rendered_path.read_text(encoding="utf-8")
        if "${" in rendered_text:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"{rendered_path} still has unrendered placeholders",
            )
        if "ll-config-file=config_tracker_NvMOT.yml" not in rendered_text:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"{rendered_path} does not reference the tracker config",
            )

        cameras_path = pathlib.Path(ctx.conf.get(config_mod.CAMERAS_FILE_KEY, ""))
        enabled_cameras = []
        if cameras_path.is_file():
            enabled_cameras = [
                cam
                for cam in cameras_mod.parse_inventory(
                    cameras_path.read_text(encoding="utf-8")
                )
                if cam.enabled
            ]
        for idx in range(len(enabled_cameras)):
            if "uri=rtsp://" not in _source_block(rendered_text, idx):
                return StepResult(
                    status=StepStatus.FAILED,
                    message=f"[source{idx}] has no rtsp:// uri in {rendered_path}",
                )

        for sibling in (INFER_PRIMARY_TEMPLATE_NAME, MSGCONV_TEMPLATE_NAME):
            if not (deepstream_dir / sibling).is_file():
                return StepResult(
                    status=StepStatus.FAILED,
                    message=f"missing {deepstream_dir / sibling}",
                )

        slug = _slugify(inputs.project_name, location_id=inputs.location_id)
        path_unit = f"mv3dt-ingest-{slug}.path"
        service_unit = f"mv3dt-ingest-{slug}.service"
        if not (systemd.UNIT_DIR / path_unit).is_file() or not (
            systemd.UNIT_DIR / service_unit
        ).is_file():
            return StepResult(
                status=StepStatus.FAILED, message="re-ingest systemd units are missing"
            )

        installer_bin = ctx.install_dir / "bin" / step3_mod.INSTALLER_BIN_NAME
        runner = _systemd_runner(ctx)
        if not installer_bin.exists():
            ctx.log.warn(
                f"{installer_bin} not installed yet; {path_unit} is installed but "
                "not enabled (this is expected until Step 3's binary drop lands)"
            )
        else:
            if not systemd.is_enabled(path_unit, runner=runner):
                return StepResult(status=StepStatus.FAILED, message=f"{path_unit} is not enabled")
            if systemd.is_enabled(service_unit, runner=runner):
                return StepResult(
                    status=StepStatus.FAILED, message=f"{service_unit} must not be enabled"
                )

        return StepResult(status=StepStatus.COMPLETE)

    # -- report (section 8) ----------------------------------------------------

    def report(self, ctx: "Context") -> None:
        inputs, missing = resolve_project_inputs(ctx)
        if inputs is None:
            ctx.log.info(f"step4: {missing} not set; nothing to report")
            return

        calibration_dir = pathlib.Path(
            ctx.conf.get(CONF_CALIBRATION_DIR_KEY)
            or default_calibration_dir(ctx, inputs.location_id)
        )
        file_count = 0
        if calibration_dir.is_dir():
            file_count = sum(1 for p in calibration_dir.rglob("*") if p.is_file())

        deepstream_dir = ctx.install_dir / "deepstream"
        ctx.log.info(
            "Calibration output wired.\n"
            f"  Calibration dir:   {calibration_dir}  ({file_count} file(s))\n"
            f"  Tracker config:    {deepstream_dir / TRACKER_YAML_TEMPLATE_NAME}\n"
            f"  Rendered pipeline: {deepstream_dir / RENDERED_APP_CONFIG_NAME}\n"
            f"  LOCATION_ID:       {inputs.location_id}\n"
            "\n"
            "Next: Step 5 builds the project-named DeepStream launcher that "
            "runs this rendered config."
        )


register(Step4CalibOutputWiring())


# ---------------------------------------------------------------------------
# section 4.5 -- the `ingest` subcommand (STEP-3 section 6.2's extension)
# ---------------------------------------------------------------------------


def _build_ingest_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(prog="mv3dt-installer ingest", add_help=True)
    parser.add_argument("--project", default=None)
    # doc 00 section 3.3-shaped framework flags this subcommand's argv may
    # carry (the systemd ExecStart= line, app._bootstrap_subcommand_context's
    # own peek parser) -- accepted here too so the parser does not reject
    # them, matching step3's `amc` subcommand parser convention.
    parser.add_argument("--install-dir", default=None)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--log-dir", default=None)
    return parser


def handle_ingest_subcommand(argv: list, ctx: "Context") -> int:
    """`mv3dt-installer ingest --project <PROJECT_NAME> ...` (section 4.5).

    The `mv3dt-ingest-<slug>.service` unit's `ExecStart`, triggered by its
    paired `.path` unit whenever the AMC export directory changes. Performs
    the same one-shot ingest+render pass `run()` does, with no in-session
    wait -- an unattended systemd trigger must never block on a human.
    """
    args = _build_ingest_arg_parser().parse_args(argv)

    inputs, missing = resolve_project_inputs(ctx)
    if inputs is None:
        ctx.log.error(f"ingest: {missing} is not set in installer.conf")
        return 1

    if args.project and args.project != inputs.project_name:
        ctx.log.warn(
            f"ingest: --project {args.project!r} does not match the configured "
            f"PROJECT_NAME {inputs.project_name!r}; proceeding with the "
            "configured project"
        )

    dest = _resolve_calibration_dir(ctx, inputs, allow_prompt=False)
    was_populated = dest.is_dir() and any(dest.iterdir())
    _ensure_dir(ctx, dest)

    outcome = ingest_export(
        ctx, amc_root=inputs.amc_root, project_name=inputs.project_name, dest=dest
    )
    if not outcome.ingested:
        ctx.log.warn(f"ingest: nothing to ingest yet at {_export_dir(inputs)}")
        return 0
    _chown_tree(ctx, dest)

    label = f"{inputs.project_name}@{outcome.stamp}"
    if was_populated:
        ctx.report_already_installed("calibration-export", label)
    else:
        ctx.report_installed("calibration-export", label)

    # section 8.1's "poor calibration (bad RMSE)" guard applies here too --
    # arguably *more* so than in `run()`. The dispatch loop only ever calls
    # `run()` once (a `COMPLETE` step is skipped on every later launch), so
    # this subcommand -- triggered by the mv3dt-ingest-<slug>.path unit on
    # every LATER recalibration -- is the only enforcement point that
    # actually executes during steady-state operation. There is no
    # `StepResult`/USER_ACTION_REQUIRED channel here (this does not go
    # through the Step lifecycle, and nothing is watching stdin for a
    # systemd-triggered process), so this logs an error and exits non-zero
    # instead -- visible in `journalctl -u mv3dt-ingest-<slug>.service` --
    # and, critically, returns before persisting CALIBRATION_DIR or
    # re-rendering the configs, so an incomplete recalibration never
    # silently overwrites a working setup.
    enabled_count = _enabled_camera_count(ctx)
    file_count = _calibration_file_count(dest)
    if enabled_count is not None and file_count < enabled_count:
        ctx.log.error(
            f"ingest: {INCOMPLETE_CALIBRATION_MESSAGE} "
            f"({file_count} file(s) in {dest} for {enabled_count} enabled "
            "camera(s); not wiring up this recalibration)"
        )
        return 1

    config_mod.persist_value(ctx.install_dir, CONF_CALIBRATION_DIR_KEY, str(dest))
    ctx.conf[CONF_CALIBRATION_DIR_KEY] = str(dest)
    _render_configs(ctx, inputs=inputs, calibration_dir=dest)
    return 0


app_mod.register_subcommand("ingest", handle_ingest_subcommand)
