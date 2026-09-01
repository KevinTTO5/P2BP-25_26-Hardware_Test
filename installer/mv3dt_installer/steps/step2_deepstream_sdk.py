"""Step 2 -- DeepStream 9.1 SDK install.

Implements `installer/plan/STEP-2-DEEPSTREAM-SDK.md` against the framework
contract in `installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md` (step-module
interface section 12.1, `StepResult`/`StepStatus` section 12.2, `Context`
section 12.3, logging/reporting section 8, privilege/USER-ACTION section 9,
NGC key handoff section 10). Those are framework modules already built and
merged on `main`; this module only consumes their public API -- it does not
reimplement any of it.

Scope: install the DeepStream 9.1 SDK on Ubuntu 24.04 / x86_64 by one of
three official methods -- deb (Method A, default), tar (Method B), or docker
(Method C) -- then run the host post-install tail and a smoke test. deb/tar
are plain anonymous GitHub Release downloads (no NGC key); only the docker
method is NGC-gated, using the key doc 00 section 10 guarantees is already
captured by the time any step runs.

`Context` (doc 00 section 12.3) is deliberately never imported at runtime
here -- only under `TYPE_CHECKING`, mirroring `steps/__init__.py`'s own
docstring rationale -- so this module has no import-time dependency on
`app.py`. Every subprocess call goes through `ctx.run_root(...)` /
`ctx.run_as_user(...)` (both plain `subprocess.run`-shaped wrappers a test
can substitute on a fake `Context`-like object), so no test here touches a
real apt/dpkg/curl/docker/deepstream-app invocation or the network.

Public API:
    Method              -- the three install methods (`deb`, `tar`, `docker`).
    detect_method(ctx) -> tuple[Method | None, str] -- doc section 4.2's
        auto-detection precedence table. Returns `(None, "ambiguous")` when
        no tier matches, so `run()` knows to prompt (or default under
        `--non-interactive`).
    Step2DeepStreamSdk -- the registered `Step` implementation
        (`id="step2_deepstream_sdk"`, `order=2`).

Doc section 4.2 point 6 requires Step 2 to skip prompting "under
`--non-interactive`". `Context` (doc section 12.3) now carries a real
`non_interactive` field (threaded through from `app.py`'s `--non-interactive`
flag via `build_context(..., non_interactive)`), so `_resolve_method(ctx)`
below reads `ctx.non_interactive` directly rather than inferring it from
`sys.stdin.isatty()`.
"""

from __future__ import annotations

import os
import pathlib
import platform
import re
import shutil
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional

from .. import config as config_mod
from .. import shellout
from ..logs import log
from . import StepResult, StepStatus, UserAction, register

if TYPE_CHECKING:  # pragma: no cover -- import-time only, never at runtime.
    from ..app import Context

__all__ = ["Method", "detect_method", "Step2DeepStreamSdk"]


# ---------------------------------------------------------------------------
# doc section 1 -- locked facts and pins
# ---------------------------------------------------------------------------

DRIVER_VERSION = "595.58.03"
CUDA_VERSION = "13.2"
CUDNN_VERSION = "9.20.0.48"
TENSORRT_VERSION = "10.16.0.72-1+cuda13.2"
GSTREAMER_VERSION = "1.24.2"

DS_VERSION_DEB = "9.1.0-1"  # dpkg Version field.
DS_VERSION_SHORT = "9.1.0"  # tar/docker normalized version.

DEB_ARTIFACT = "deepstream-9.1_9.1.0-1_amd64.deb"
TAR_ARTIFACT = "deepstream_sdk_v9.1.0_x86_64.tbz2"
DOCKER_IMAGE = "nvcr.io/nvidia/deepstream:9.1-triton-multiarch"

_GITHUB_RELEASE_TAG = "v9.1.0"
GITHUB_RELEASE_BASE = (
    f"https://github.com/NVIDIA/DeepStream/releases/download/{_GITHUB_RELEASE_TAG}"
)
GITHUB_RELEASE_TAG_URL = (
    f"https://github.com/NVIDIA/DeepStream/releases/tag/{_GITHUB_RELEASE_TAG}"
)

# Fixed NVIDIA-owned SDK path (doc section 6) -- NOT governed by the
# framework `install_dir` prompt. Module-level (not inlined) so tests can
# `monkeypatch` them at a tmp_path, the same discipline `systemd.py` uses
# for `UNIT_DIR`.
DS_SDK_DIR = pathlib.Path("/opt/nvidia/deepstream/deepstream-9.1")
DS_SDK_SYMLINK = pathlib.Path("/opt/nvidia/deepstream/deepstream")
PROFILE_D_PATH = pathlib.Path("/etc/profile.d/deepstream.sh")

_PROFILE_D_CONTENT = (
    "export DEEPSTREAM_DIR=/opt/nvidia/deepstream/deepstream-9.1\n"
    "export PATH=/opt/nvidia/deepstream/deepstream-9.1/bin:$PATH\n"
    "export LD_LIBRARY_PATH="
    "/opt/nvidia/deepstream/deepstream-9.1/lib:$LD_LIBRARY_PATH\n"
)

_SMOKE_TEST_TIMEOUT_S = 30
_SMOKE_CONFIG_ASSET = ("deepstream", "smoke_app_config.txt")

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


class Method(str, Enum):
    """The three official DS 9.1 x86_64 install methods (doc section 3)."""

    DEB = "deb"
    TAR = "tar"
    DOCKER = "docker"


_METHOD_DESCRIPTIONS: dict[Method, tuple[str, str]] = {
    Method.DEB: (
        "Method A -- GitHub Release Debian package (bare-metal host install)",
        "Install DeepStream directly onto this machine as a system package. "
        "Best for running the DeepStream pipeline on the host (no "
        "container). Pulls in its apt prerequisites automatically.",
    ),
    Method.TAR: (
        "Method B -- GitHub Release tar archive (relocatable / non-apt install)",
        "Extract the DeepStream SDK from a tarball and run its installer "
        "script. Use when you want a self-contained SDK tree not managed "
        "by apt, or need to sit alongside another install. Does not "
        "register with dpkg.",
    ),
    Method.DOCKER: (
        "Method C -- NGC Docker image (containerized runtime)",
        "Run DeepStream inside NVIDIA's official container. The host only "
        "needs the driver + Docker + NVIDIA Container Toolkit; DeepStream "
        "and its dependencies live in the image. Best when you prefer "
        "containerized/reproducible runtime or already run Docker.",
    ),
}

# installer.conf key mirroring the resolved method (doc section 4.2's
# "persisted by writing ds_install_method back through the framework
# config"). Lower-case, matching the doc's own spelling -- distinct from the
# framework's own upper-case MV3DT_* gate keys (config.py's GATE_KEYS),
# since this is a step-owned key, not a framework-owned gate.
CONF_METHOD_KEY = "ds_install_method"
CONF_RELOCATABLE_KEY = "ds_relocatable"
CONF_HOST_PIPELINE_KEY = "ds_host_pipeline_required"

# doc STEP-4 section 6.3's "Step 2 owns the PeopleNet model fetch" --
# originally Phase 10 of laptop/scripts/00_bootstrap.sh, never ported when
# Step 1/2 replaced that script (STEP-2-DEEPSTREAM-SDK.md section 11's
# "Open gap, not yet resolved" callout). Same default tag laptop.env.example
# pins (laptop/config/laptop.env.example's PEOPLENET_NGC_TAG), just an
# installer.conf key here rather than a laptop/-only env var.
CONF_PEOPLENET_TAG_KEY = "peoplenet_ngc_tag"
PEOPLENET_NGC_TAG_DEFAULT = "nvidia/tao/peoplenet:deployable_quantized_onnx_v2.6.3"

# Relative to `ctx.install_dir` -- where `config_infer_primary.txt`'s
# relative `onnx-file=models/peoplenet/...` / `labelfile-path=...` resolve
# once Step 5 execs `deepstream-app` with `cwd=<install_dir>/deepstream`
# (doc STEP-4 section 6.3/6.5's output tree).
PEOPLENET_RELATIVE_DIR = pathlib.Path("deepstream") / "models" / "peoplenet"
PEOPLENET_ONNX_NAME = "resnet34_peoplenet.onnx"
PEOPLENET_LABELS_NAME = "labels.txt"
# Fixed PeopleNet 3-class label set -- not downloaded, just written verbatim
# (laptop/scripts/00_bootstrap.sh Phase 10, lines ~1006-1011).
_PEOPLENET_LABELS_CONTENT = "person\nbag\nface\n"


# ---------------------------------------------------------------------------
# Interactivity
# ---------------------------------------------------------------------------

# Injectable so tests never touch the real stdin/tty or block on input().
_INPUT: Callable[[str], str] = input


# ---------------------------------------------------------------------------
# doc section 4 -- auto-detection vs prompt
# ---------------------------------------------------------------------------


def _run_root(ctx: "Context", *args: str, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return ctx.run_root(*args, **kwargs)


def _run_as_user(ctx: "Context", *args: str, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return ctx.run_as_user(*args, **kwargs)


def _dpkg_version(ctx: "Context", package: str = "deepstream-9.1") -> Optional[str]:
    """`dpkg -s <package>`'s `Version:` field, or `None` if not installed."""
    result = _run_root(ctx, "dpkg", "-s", package)
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _read_sdk_version_file() -> Optional[str]:
    """Raw contents of `<DS_SDK_DIR>/version`, or `None` if unreadable.

    Plain filesystem read (not shelled out) -- the installer process is
    already root by the time any step runs, so no privilege helper is
    needed to read a world-readable SDK file.
    """
    try:
        return (DS_SDK_DIR / "version").read_text(encoding="utf-8")
    except OSError:
        return None


def _normalize_version(raw: Optional[str]) -> Optional[str]:
    """Extract the first `X.Y.Z` substring, or `None`."""
    if not raw:
        return None
    match = _VERSION_RE.search(raw)
    return match.group(1) if match else None


def _docker_image_present(ctx: "Context") -> bool:
    result = _run_as_user(ctx, "docker", "image", "inspect", DOCKER_IMAGE)
    return result.returncode == 0


def _docker_and_toolkit_usable(ctx: "Context") -> bool:
    """doc section 4.1's "Docker present + usable" + "NVIDIA runtime
    present" rows, collapsed into one boolean the way section 4.2 point 4
    consumes them together."""
    docker_info = _run_as_user(ctx, "docker", "info")
    if docker_info.returncode != 0:
        return False
    toolkit = _run_root(ctx, "dpkg", "-s", "nvidia-container-toolkit")
    if toolkit.returncode != 0:
        return False
    ctk = _run_as_user(ctx, "which", "nvidia-ctk")
    if ctk.returncode != 0:
        return False
    return "nvidia" in (docker_info.stdout or "").lower()


def _detect_existing_install(ctx: "Context") -> Optional[Method]:
    """doc section 4.1's "Existing DS install" row. deb takes precedence
    over tar since a deb install also leaves a version file behind at the
    same fixed SDK path."""
    if _dpkg_version(ctx) == DS_VERSION_DEB:
        return Method.DEB
    if _normalize_version(_read_sdk_version_file()) == DS_VERSION_SHORT:
        return Method.TAR
    if _docker_image_present(ctx):
        return Method.DOCKER
    return None


def detect_method(ctx: "Context") -> tuple[Optional[Method], str]:
    """doc section 4.2's precedence table, first match wins.

    Returns `(method, reason)`, or `(None, "ambiguous")` when no tier
    matches -- the caller (`run()`) prompts or falls back to `deb`.
    """
    override = (ctx.conf.get(CONF_METHOD_KEY) or "").strip().lower()
    if override:
        try:
            return Method(override), f"explicit override ({CONF_METHOD_KEY}={override})"
        except ValueError:
            log.warn(
                f"{CONF_METHOD_KEY}={override!r} is not deb/tar/docker; "
                "ignoring the override and continuing auto-detection"
            )

    existing = _detect_existing_install(ctx)
    if existing is not None:
        return existing, f"already installed ({existing.value})"

    if (ctx.conf.get(CONF_RELOCATABLE_KEY) or "").strip().lower() == "true":
        return Method.TAR, f"{CONF_RELOCATABLE_KEY}=true"

    host_pipeline_required = (
        ctx.conf.get(CONF_HOST_PIPELINE_KEY) or "true"
    ).strip().lower() != "false"

    docker_ok = _docker_and_toolkit_usable(ctx)
    if docker_ok and not host_pipeline_required:
        return (
            Method.DOCKER,
            "Docker + NVIDIA Container Toolkit usable, host pipeline not required",
        )

    if host_pipeline_required:
        return Method.DEB, "host pipeline intent (product default)"

    return None, "ambiguous"


def _prompt_for_method() -> Method:
    log.info("Multiple DeepStream install methods are available:")
    for method in (Method.DEB, Method.TAR, Method.DOCKER):
        title, description = _METHOD_DESCRIPTIONS[method]
        log.info(f"  [{method.value}] {title}")
        log.info(f"      {description}")
    while True:
        answer = _INPUT(f"DeepStream install method [{Method.DEB.value}]: ").strip().lower()
        if not answer:
            return Method.DEB
        try:
            return Method(answer)
        except ValueError:
            log.warn(f"{answer!r} is not one of deb, tar, docker; please answer again")


def _resolve_method(ctx: "Context") -> tuple[Method, str]:
    method, reason = detect_method(ctx)
    if method is not None:
        return method, reason
    if not ctx.non_interactive:
        return _prompt_for_method(), "operator selection (ambiguous auto-detect)"
    return Method.DEB, "non-interactive default (ambiguous auto-detect)"


# ---------------------------------------------------------------------------
# doc section 2 -- prereq pins (shared by preflight() and verify())
# ---------------------------------------------------------------------------


def _probe_driver(ctx: "Context") -> Optional[str]:
    result = _run_root(
        ctx, "nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"
    )
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _probe_cuda(ctx: "Context") -> Optional[str]:
    result = _run_root(ctx, "nvcc", "--version")
    if result.returncode != 0:
        return None
    match = re.search(r"release (\d+\.\d+)", result.stdout or "")
    return match.group(1) if match else None


def _probe_cudnn(ctx: "Context") -> Optional[str]:
    # dpkg-query supports package-name globbing without a shell, matching
    # doc section 2 point 2's "dpkg -l | grep libcudnn9" intent.
    result = _run_root(ctx, "dpkg-query", "-W", "-f=${Version}", "libcudnn9*")
    if result.returncode != 0:
        return None
    first_line = (result.stdout or "").strip().splitlines()
    return first_line[0].strip() if first_line else None


def _probe_tensorrt(ctx: "Context") -> Optional[str]:
    result = _run_root(ctx, "dpkg-query", "-W", "-f=${Version}", "libnvinfer10")
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _probe_gstreamer(ctx: "Context") -> Optional[str]:
    result = _run_root(ctx, "gst-inspect-1.0", "--version")
    if result.returncode != 0:
        return None
    match = re.search(r"GStreamer\s+(\d+\.\d+\.\d+)", result.stdout or "")
    return match.group(1) if match else None


def _prereq_pins(ctx: "Context") -> list[tuple[str, Optional[str], str]]:
    return [
        ("NVIDIA driver", _probe_driver(ctx), DRIVER_VERSION),
        ("CUDA", _probe_cuda(ctx), CUDA_VERSION),
        ("cuDNN", _probe_cudnn(ctx), CUDNN_VERSION),
        ("TensorRT", _probe_tensorrt(ctx), TENSORRT_VERSION),
        ("GStreamer", _probe_gstreamer(ctx), GSTREAMER_VERSION),
    ]


def _check_prereq_pins(ctx: "Context") -> Optional[str]:
    """Returns `None` when every pin matches, else a remediation message."""
    for label, actual, expected in _prereq_pins(ctx):
        if actual is None or not ctx.verify_pinned(label, actual, expected):
            return (
                f"{label} prerequisite pin missing or mismatched "
                f"(expected {expected}, got {actual or 'not found'}); "
                "re-run Step 1 (--reset-step 1)."
            )
    return None


# ---------------------------------------------------------------------------
# doc section 5 -- acquisition
# ---------------------------------------------------------------------------


def _ensure_artifact(
    ctx: "Context", artifact_dir: pathlib.Path, artifact_name: str, url: str
) -> tuple[bool, str, Optional[StepResult]]:
    """Ensure `<artifact_dir>/<artifact_name>` exists, downloading it as the
    invoking user if not already placed there.

    Returns `(ok, source, early_result)`. `source` is `"pre-placed"` when
    the artifact was already on disk (a prior manual placement or a
    previous successful download), `"downloaded"` when this call fetched
    it. When `ok` is `False`, `early_result` is the
    `USER_ACTION_REQUIRED` `StepResult` the caller should return as-is.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(artifact_dir, ctx.user.uid, ctx.user.gid)
    except OSError:
        pass  # Best-effort; a permission mismatch here isn't fatal.

    artifact_path = artifact_dir / artifact_name
    if artifact_path.is_file() and artifact_path.stat().st_size > 0:
        log.info(f"DeepStream artifact already present: {artifact_path}")
        return True, "pre-placed", None

    result = _run_as_user(
        ctx, "curl", "-fsSL", "-o", artifact_name, url, cwd=str(artifact_dir)
    )
    if result.returncode == 0 and artifact_path.is_file():
        log.info(f"Downloaded {artifact_name} to {artifact_dir}")
        return True, "downloaded", None

    action = UserAction(
        text=(
            f"Download {artifact_name} from the NVIDIA/DeepStream GitHub "
            "Release on a machine with internet access, then place it in "
            f"{artifact_dir}"
        ),
        command=f"curl -fsSL -o {artifact_name} {url}",
        path=str(artifact_dir),
    )
    early_result = StepResult(
        status=StepStatus.USER_ACTION_REQUIRED,
        message=(
            f"could not download {artifact_name} "
            f"({GITHUB_RELEASE_TAG_URL}) -- no internet or GitHub unreachable"
        ),
        user_actions=[action],
    )
    return False, "download-failed", early_result


def _docker_login(ctx: "Context"):
    """`docker login nvcr.io` using the onboarding-stored NGC key.

    Mirrors `ngc.configure_ngc_cli`'s own pattern (doc section 5.2 / doc 00
    section 10.2's "Design note"): the key is never interpolated into
    Python source or passed as a CLI argument. It is sourced from the
    canonical `<install_dir>/secrets/ngc.env` file (doc 00 section 10.1)
    inside the child shell via `NGC_ENV_FILE`, so it never appears in argv
    or in this module's source.
    """
    secrets_path = ctx.install_dir / "secrets" / "ngc.env"
    script = (
        'set -a; . "$NGC_ENV_FILE"; set +a; '
        "echo \"$NGC_API_KEY\" | docker login nvcr.io "
        "-u '$oauthtoken' --password-stdin"
    )
    return _run_as_user(
        ctx, "env", f"NGC_ENV_FILE={secrets_path}", "bash", "-lc", script
    )


# ---------------------------------------------------------------------------
# doc STEP-4 section 6.3 -- PeopleNet model acquisition
#
# Ported from laptop/scripts/00_bootstrap.sh Phase 10 (lines ~942-1014):
# `ngc registry model download-version <tag> --dest <tmp>`, copy the one
# versioned subdirectory NGC creates into place, write the fixed 3-class
# labels.txt. Runs as the invoking user (doc 00 section 9.2 -- `ngc`, like
# `docker` and the AMC clone, must never run unwrapped as root), same as
# `ngc.configure_ngc_cli()`.
# ---------------------------------------------------------------------------


def _peoplenet_dir(ctx: "Context") -> pathlib.Path:
    return ctx.install_dir / PEOPLENET_RELATIVE_DIR


def _peoplenet_tag(ctx: "Context") -> str:
    return (ctx.conf.get(CONF_PEOPLENET_TAG_KEY) or "").strip() or PEOPLENET_NGC_TAG_DEFAULT


def _peoplenet_model_present(ctx: "Context") -> bool:
    onnx_path = _peoplenet_dir(ctx) / PEOPLENET_ONNX_NAME
    return onnx_path.is_file() and onnx_path.stat().st_size > 0


def _write_peoplenet_labels(ctx: "Context") -> None:
    labels_path = _peoplenet_dir(ctx) / PEOPLENET_LABELS_NAME
    if labels_path.is_file():
        return
    labels_path.write_text(_PEOPLENET_LABELS_CONTENT, encoding="utf-8")
    try:
        os.chown(labels_path, ctx.user.uid, ctx.user.gid)
    except OSError:
        pass  # best-effort, e.g. under a non-root test process


def _ngc_cli_missing_action(ctx: "Context") -> UserAction:
    """Mirrors 00_bootstrap.sh Phase 5's manual-install banner -- this
    installer does not attempt to fetch/install the NGC CLI itself, the
    same way it never auto-installs Docker or the NVIDIA driver."""
    return UserAction(
        text=(
            "Install the NGC CLI as your regular user, run 'ngc config "
            "set' (API key, ascii, your NGC org -- see "
            "https://ngc.nvidia.com/setup), then re-run this step."
        ),
        command=(
            "cd ~ && mkdir -p ngc-cli && cd ngc-cli && "
            "curl -LO https://ngc.nvidia.com/downloads/ngccli_linux.zip && "
            "unzip -o ngccli_linux.zip && chmod u+x ngc-cli/ngc && "
            'echo \'export PATH="$HOME/ngc-cli/ngc-cli:$PATH"\' >> ~/.bashrc'
        ),
        path=str(ctx.user.home / "ngc-cli"),
    )


def _download_peoplenet(
    ctx: "Context", target_dir: pathlib.Path, tag: str
) -> Optional[tuple[str, StepStatus]]:
    """Runs `ngc registry model download-version` into a throwaway tmp dir
    (chowned to the invoking user so `ngc`, run as that user, can write
    into it) and copies every file under the one versioned subdirectory NGC
    creates into `target_dir`. Returns `(error_message, status)` on failure
    -- `USER_ACTION_REQUIRED` for a command failure the operator can act on
    (bad tag, auth), `FAILED` for an unexpected NGC output shape -- or
    `None` on success."""
    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="mv3dt-peoplenet-"))
    try:
        os.chown(tmp_dir, ctx.user.uid, ctx.user.gid)
    except OSError:
        pass

    try:
        result = _run_as_user(
            ctx, "ngc", "registry", "model", "download-version", tag, "--dest", str(tmp_dir)
        )
        if result.returncode != 0:
            return (
                f"ngc registry model download-version {tag} failed "
                f"(exit {result.returncode}): {(result.stderr or '').strip()[-2000:]}",
                StepStatus.USER_ACTION_REQUIRED,
            )

        version_dirs = [p for p in tmp_dir.iterdir() if p.is_dir()]
        if not version_dirs:
            return (
                f"ngc registry model download-version {tag} produced no "
                f"versioned subdirectory under its --dest",
                StepStatus.FAILED,
            )

        target_dir.mkdir(parents=True, exist_ok=True)
        for version_dir in version_dirs:
            for entry in version_dir.rglob("*"):
                if not entry.is_file():
                    continue
                dest = target_dir / entry.relative_to(version_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, dest)

        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _ensure_peoplenet_model(ctx: "Context") -> Optional[StepResult]:
    """Ensure the PeopleNet ONNX + labels.txt exist under
    `_peoplenet_dir(ctx)`. Returns `None` on success (already present, or
    freshly downloaded), else the `StepResult` `run()` should return as-is.
    """
    target_dir = _peoplenet_dir(ctx)

    if _peoplenet_model_present(ctx):
        log.info(f"PeopleNet model already present: {target_dir / PEOPLENET_ONNX_NAME}")
        _write_peoplenet_labels(ctx)
        return None

    which_ngc = _run_as_user(ctx, "which", "ngc")
    if which_ngc.returncode != 0:
        return StepResult(
            status=StepStatus.USER_ACTION_REQUIRED,
            message="NGC CLI ('ngc') not found; cannot download the PeopleNet model",
            user_actions=[_ngc_cli_missing_action(ctx)],
        )

    ctx.ngc.configure_ngc_cli()

    tag = _peoplenet_tag(ctx)
    failure = _download_peoplenet(ctx, target_dir, tag)
    if failure is not None:
        message, status = failure
        user_actions = (
            [
                UserAction(
                    text=(
                        f"Check {CONF_PEOPLENET_TAG_KEY} and that 'ngc config "
                        "set' has run for this user, then re-run this step."
                    ),
                    command=f"ngc registry model download-version {tag} --dest <dir>",
                    path=str(target_dir),
                )
            ]
            if status is StepStatus.USER_ACTION_REQUIRED
            else []
        )
        return StepResult(status=status, message=message, user_actions=user_actions)

    if not _peoplenet_model_present(ctx):
        return StepResult(
            status=StepStatus.FAILED,
            message=(
                f"ngc download completed but {PEOPLENET_ONNX_NAME} still "
                f"not found under {target_dir}"
            ),
        )

    try:
        os.chown(target_dir, ctx.user.uid, ctx.user.gid)
        for entry in target_dir.rglob("*"):
            os.chown(entry, ctx.user.uid, ctx.user.gid)
    except OSError:
        pass  # best-effort, e.g. under a non-root test process

    _write_peoplenet_labels(ctx)
    ctx.report_installed("peoplenet-model", tag)
    log.info(f"PeopleNet model downloaded to {target_dir}")
    return None


# ---------------------------------------------------------------------------
# doc section 9 -- post-install tail (host installs only)
# ---------------------------------------------------------------------------


# Marker recording whether `update_rtpmanager.sh` ran and its outcome (doc
# section 7.1 item 4 / section 7.4: "update_rtpmanager.sh was executed
# (record a marker/log line)"). An in-memory attribute on the `Step`
# instance does not survive a re-run/re-verify in a fresh process, so this
# is a small durable file under the framework `install_dir` -- the one
# location this step already writes non-fixed-path artifacts under (doc
# section 6's `<install_dir>/downloads/deepstream`).
_RTPMANAGER_MARKER_RELATIVE = pathlib.Path("deepstream") / "step2_rtpmanager_marker.txt"


def _rtpmanager_marker_path(ctx: "Context") -> pathlib.Path:
    return ctx.install_dir / _RTPMANAGER_MARKER_RELATIVE


def _write_rtpmanager_marker(
    ctx: "Context", status: str, returncode: Optional[int], detail: str = ""
) -> None:
    path = _rtpmanager_marker_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"status={status}", f"returncode={returncode if returncode is not None else ''}"]
    if detail:
        # Single line: the marker is a plain KEY=VALUE file, not a log --
        # collapse any embedded newlines so it stays two-to-three lines.
        lines.append(f"detail={' '.join(detail.split())}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_rtpmanager_marker(ctx: "Context") -> Optional[dict[str, str]]:
    """`None` when the marker file doesn't exist -- i.e. `run()`'s
    post-install tail never wrote one -- else its parsed KEY=VALUE
    contents (possibly `{}` for an empty/unparseable file, which is still
    "the marker is present")."""
    try:
        text = _rtpmanager_marker_path(ctx).read_text(encoding="utf-8")
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _run_update_rtpmanager(ctx: "Context") -> tuple[str, Optional[int], str]:
    """Run `update_rtpmanager.sh` and classify the outcome as `"ok"`,
    `"failed"` (nonzero exit -- doc section 9 point 1: logged as a
    warning, not fatal), or `"missing"` (the script itself could not be
    launched, e.g. absent from a broken/partial SDK tree). Returns
    `(status, returncode, detail)`; `detail` is a captured stderr tail
    (or the exception text for `"missing"`), empty on success.
    """
    script = str(DS_SDK_SYMLINK / "update_rtpmanager.sh")
    try:
        result = _run_root(ctx, script)
    except OSError as exc:
        log.warn(f"update_rtpmanager.sh not found or not executable at {script}: {exc}")
        return "missing", None, str(exc)

    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()
        log.warn(f"update_rtpmanager.sh exited {result.returncode}: {stderr_tail}")
        return "failed", result.returncode, stderr_tail

    log.info("update_rtpmanager.sh completed")
    return "ok", result.returncode, ""


def _post_install(ctx: "Context") -> list[str]:
    actions: list[str] = []

    status, returncode, detail = _run_update_rtpmanager(ctx)
    _write_rtpmanager_marker(ctx, status, returncode, detail)
    actions.append("update_rtpmanager.sh")

    _run_root(ctx, "ldconfig")
    actions.append("ldconfig")

    _write_profile_d()
    actions.append(f"wrote {PROFILE_D_PATH}")

    return actions


def _describe_rtpmanager_marker(marker: Optional[dict[str, str]]) -> str:
    """Human summary of the rtpmanager marker for `report()` (doc section
    9: a non-zero exit is "surfaced in report()")."""
    if marker is None:
        return "not run (no marker found)"
    status = marker.get("status", "unknown")
    returncode = marker.get("returncode", "")
    if status == "ok":
        return f"succeeded (exit {returncode or 0})"
    if status == "failed":
        return f"FAILED (exit {returncode}) -- treated as a warning, re-check manually"
    if status == "missing":
        return "script not found"
    return status


def _write_profile_d() -> None:
    """Idempotent write of `/etc/profile.d/deepstream.sh` (doc section 9
    point 3). The process is already root by the time any step runs, so
    this writes directly rather than through a privilege helper."""
    if PROFILE_D_PATH.is_file():
        try:
            existing = PROFILE_D_PATH.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing == _PROFILE_D_CONTENT:
            log.info(f"{PROFILE_D_PATH} already up to date")
            return
    PROFILE_D_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_D_PATH.write_text(_PROFILE_D_CONTENT, encoding="utf-8")
    PROFILE_D_PATH.chmod(0o644)
    log.info(f"wrote {PROFILE_D_PATH}")


# ---------------------------------------------------------------------------
# doc section 7.3 -- smoke test
# ---------------------------------------------------------------------------


def _deepstream_app_version(ctx: "Context", *, docker: bool) -> Optional[str]:
    if docker:
        result = _run_as_user(
            ctx, "docker", "run", "--rm", "--gpus", "all", DOCKER_IMAGE,
            "deepstream-app", "--version-all",
        )
    else:
        result = _run_root(ctx, "deepstream-app", "--version-all")
        if result.returncode != 0:
            result = _run_root(ctx, "deepstream-app", "--version")
    if result.returncode != 0:
        return None
    return result.stdout or ""


def _run_smoke_test(ctx: "Context", *, docker: bool) -> tuple[bool, str]:
    """Run the bundled fakesink smoke config for a bounded number of
    seconds, and assert the app reached PLAYING with no error (doc section
    7.3). Returns `(passed, message)`; `message` is a captured stderr/
    stdout tail on failure, empty on success.
    """
    stage_root = shellout.stage_assets("deepstream")
    try:
        config_path = stage_root.joinpath(*_SMOKE_CONFIG_ASSET)
        if not config_path.is_file():
            return False, f"smoke config asset missing: {config_path}"

        if docker:
            container_config = "/tmp/mv3dt-smoke/smoke_app_config.txt"
            result = _run_as_user(
                ctx,
                "docker", "run", "--rm", "--gpus", "all",
                "-v", f"{config_path.parent}:/tmp/mv3dt-smoke:ro",
                DOCKER_IMAGE,
                "timeout", str(_SMOKE_TEST_TIMEOUT_S),
                "deepstream-app", "-c", container_config,
            )
        else:
            result = _run_root(
                ctx,
                "timeout", str(_SMOKE_TEST_TIMEOUT_S),
                "deepstream-app", "-c", str(config_path),
                cwd=str(config_path.parent),
            )

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        combined = f"{stdout}\n{stderr}".upper()
        tail = (stderr.strip() or stdout.strip())[-2000:]

        # exit 124 is `timeout`'s own "still running when the clock ran
        # out" code -- expected for a pipeline we deliberately bound by
        # wall-clock rather than frame count, and not itself a failure.
        if result.returncode not in (0, 124):
            return False, tail
        if "ERROR" in combined:
            return False, tail
        if "PLAYING" not in combined and "PERF" not in combined:
            return False, "no PLAYING/perf output observed within the bounded run"

        return True, ""
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# The Step
# ---------------------------------------------------------------------------


@dataclass
class _RunOutcome:
    """Bookkeeping `run()` hands to `report()` (doc section 10). Not part
    of the `Step` protocol -- purely an implementation detail of this
    class."""

    method: Method
    reason: str
    artifact_source: str = "n/a"


class Step2DeepStreamSdk:
    id = "step2_deepstream_sdk"
    title = "DeepStream 9.1 SDK"
    order = 2

    def __init__(self) -> None:
        self._outcome: Optional[_RunOutcome] = None
        self._post_install_actions: list[str] = []
        self._smoke_passed: Optional[bool] = None

    # -- preflight -----------------------------------------------------

    def preflight(self, ctx: "Context") -> StepResult:
        arch = platform.machine()
        if arch != "x86_64":
            return StepResult(
                status=StepStatus.FAILED,
                message=f"x86_64 required (found {arch or 'unknown'}).",
            )

        pin_failure = _check_prereq_pins(ctx)
        if pin_failure is not None:
            return StepResult(status=StepStatus.FAILED, message=pin_failure)

        if ctx.ngc.load_key() is None:
            return StepResult(
                status=StepStatus.FAILED,
                message=(
                    "NGC API key not found; onboarding did not run before "
                    "Step 2 dispatch (internal ordering bug, not operator-"
                    "recoverable)."
                ),
            )

        return StepResult(status=StepStatus.COMPLETE)

    # -- run -------------------------------------------------------------

    def run(self, ctx: "Context") -> StepResult:
        method, reason = _resolve_method(ctx)
        log.info(f"DS install method: {method.value} ({reason})")

        ctx.conf[CONF_METHOD_KEY] = method.value
        config_mod.persist_value(ctx.install_dir, CONF_METHOD_KEY, method.value)

        self._outcome = _RunOutcome(method=method, reason=reason)
        self._post_install_actions = []
        self._smoke_passed = None

        if method is Method.DEB:
            result = self._run_deb(ctx)
        elif method is Method.TAR:
            result = self._run_tar(ctx)
        else:
            result = self._run_docker(ctx)

        if result.status is not StepStatus.COMPLETE:
            return result

        # doc STEP-4 section 6.3: Step 2 owns the PeopleNet model fetch,
        # independent of which DS SDK install method was chosen -- Step 5
        # execs deepstream-app against `<install_dir>/deepstream` regardless.
        peoplenet_result = _ensure_peoplenet_model(ctx)
        if peoplenet_result is not None:
            return peoplenet_result
        return result

    def _run_deb(self, ctx: "Context") -> StepResult:
        existing = _dpkg_version(ctx)
        if existing == DS_VERSION_DEB:
            ctx.report_already_installed("deepstream-9.1", DS_VERSION_DEB)
            return StepResult(status=StepStatus.COMPLETE)

        artifact_dir = ctx.install_dir / "downloads" / "deepstream"
        ok, source, early = _ensure_artifact(
            ctx, artifact_dir, DEB_ARTIFACT, f"{GITHUB_RELEASE_BASE}/{DEB_ARTIFACT}"
        )
        if self._outcome is not None:
            self._outcome.artifact_source = source
        if not ok:
            assert early is not None
            return early

        result = _run_root(
            ctx, "apt-get", "install", "-y", f"./{DEB_ARTIFACT}", cwd=str(artifact_dir)
        )
        if result.returncode != 0:
            return StepResult(
                status=StepStatus.FAILED,
                message=(
                    f"apt-get install ./{DEB_ARTIFACT} failed "
                    f"(exit {result.returncode}): "
                    f"{(result.stderr or '').strip()[-2000:]}"
                ),
            )

        installed_version = _dpkg_version(ctx) or DS_VERSION_DEB
        ctx.report_installed("deepstream-9.1", installed_version)

        self._post_install_actions = _post_install(ctx)
        return StepResult(status=StepStatus.COMPLETE)

    def _run_tar(self, ctx: "Context") -> StepResult:
        existing = _normalize_version(_read_sdk_version_file())
        if existing == DS_VERSION_SHORT:
            ctx.report_already_installed("deepstream-sdk", DS_VERSION_SHORT)
            return StepResult(status=StepStatus.COMPLETE)

        artifact_dir = ctx.install_dir / "downloads" / "deepstream"
        ok, source, early = _ensure_artifact(
            ctx, artifact_dir, TAR_ARTIFACT, f"{GITHUB_RELEASE_BASE}/{TAR_ARTIFACT}"
        )
        if self._outcome is not None:
            self._outcome.artifact_source = source
        if not ok:
            assert early is not None
            return early

        extract = _run_root(
            ctx, "tar", "-xvf", str(artifact_dir / TAR_ARTIFACT), "-C", "/"
        )
        if extract.returncode != 0:
            return StepResult(
                status=StepStatus.FAILED,
                message=(
                    f"tar extract of {TAR_ARTIFACT} failed "
                    f"(exit {extract.returncode}): "
                    f"{(extract.stderr or '').strip()[-2000:]}"
                ),
            )

        install = _run_root(ctx, "./install.sh", cwd=str(DS_SDK_DIR))
        if install.returncode != 0:
            return StepResult(
                status=StepStatus.FAILED,
                message=(
                    f"{DS_SDK_DIR}/install.sh failed (exit {install.returncode}): "
                    f"{(install.stderr or '').strip()[-2000:]}"
                ),
            )

        installed_version = _normalize_version(_read_sdk_version_file()) or DS_VERSION_SHORT
        ctx.report_installed("deepstream-sdk", installed_version)

        self._post_install_actions = _post_install(ctx)
        return StepResult(status=StepStatus.COMPLETE)

    def _run_docker(self, ctx: "Context") -> StepResult:
        if _docker_image_present(ctx):
            ctx.report_already_installed("deepstream-image", "9.1-triton-multiarch")
            if self._outcome is not None:
                self._outcome.artifact_source = "n/a (image already local)"
            return StepResult(status=StepStatus.COMPLETE)

        ctx.ngc.configure_ngc_cli()

        login = _docker_login(ctx)
        if login.returncode != 0:
            return StepResult(
                status=StepStatus.FAILED,
                message=(
                    f"docker login nvcr.io failed (exit {login.returncode}): "
                    f"{(login.stderr or '').strip()[-2000:]}"
                ),
            )

        pull = _run_as_user(ctx, "docker", "pull", DOCKER_IMAGE)
        if pull.returncode != 0:
            return StepResult(
                status=StepStatus.FAILED,
                message=(
                    f"docker pull {DOCKER_IMAGE} failed (exit {pull.returncode}): "
                    f"{(pull.stderr or '').strip()[-2000:]}"
                ),
            )

        ctx.report_installed("deepstream-image", "9.1-triton-multiarch")
        if self._outcome is not None:
            self._outcome.artifact_source = "NGC (docker pull)"
        return StepResult(status=StepStatus.COMPLETE)

    # -- verify ------------------------------------------------------------

    def verify(self, ctx: "Context") -> StepResult:
        raw_method = ctx.conf.get(CONF_METHOD_KEY)
        if not raw_method:
            return StepResult(
                status=StepStatus.FAILED,
                message="no DeepStream install method recorded; run() did not complete",
            )
        try:
            method = Method(raw_method)
        except ValueError:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"unrecognized {CONF_METHOD_KEY}={raw_method!r}",
            )

        if method is Method.DOCKER:
            result = self._verify_docker(ctx)
        else:
            result = self._verify_host(ctx, method)

        if result.status is not StepStatus.COMPLETE:
            return result

        if not _peoplenet_model_present(ctx):
            return StepResult(
                status=StepStatus.FAILED,
                message=(
                    f"PeopleNet model not found under {_peoplenet_dir(ctx)}; "
                    "re-run Step 2 (--reset-step 2)"
                ),
            )

        return result

    def _verify_host(self, ctx: "Context", method: Method) -> StepResult:
        pin_failure = _check_prereq_pins(ctx)
        if pin_failure is not None:
            return StepResult(status=StepStatus.FAILED, message=pin_failure)

        if not DS_SDK_DIR.is_dir():
            return StepResult(
                status=StepStatus.FAILED,
                message=f"{DS_SDK_DIR} not found after install",
            )

        try:
            symlink_ok = DS_SDK_SYMLINK.resolve() == DS_SDK_DIR.resolve()
        except OSError:
            symlink_ok = False
        if not symlink_ok:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"{DS_SDK_SYMLINK} does not resolve to {DS_SDK_DIR}",
            )

        if method is Method.DEB:
            actual_version = _dpkg_version(ctx)
            expected_version = DS_VERSION_DEB
        else:
            actual_version = _normalize_version(_read_sdk_version_file())
            expected_version = DS_VERSION_SHORT

        if not actual_version or not ctx.verify_pinned(
            "DeepStream", actual_version, expected_version
        ):
            return StepResult(
                status=StepStatus.FAILED, message="DeepStream version pin mismatch"
            )

        version_output = _deepstream_app_version(ctx, docker=False)
        if version_output is None or "9.1" not in version_output:
            return StepResult(
                status=StepStatus.FAILED,
                message="deepstream-app --version-all did not report DeepStream 9.1",
            )

        if not PROFILE_D_PATH.is_file():
            return StepResult(
                status=StepStatus.FAILED,
                message=f"{PROFILE_D_PATH} missing",
            )
        try:
            profile_text = PROFILE_D_PATH.read_text(encoding="utf-8")
        except OSError:
            profile_text = ""
        if "DEEPSTREAM_DIR" not in profile_text:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"{PROFILE_D_PATH} does not export DEEPSTREAM_DIR",
            )

        # doc section 7.1 item 4 / 7.4: "update_rtpmanager.sh was executed
        # (record a marker/log line)". A missing marker means the
        # post-install tail never ran; the marker's *status* (ok/failed/
        # missing) is not itself a verify failure -- a nonzero exit is a
        # warning surfaced in report() (doc section 9 point 1), not a
        # blocker here.
        if _read_rtpmanager_marker(ctx) is None:
            return StepResult(
                status=StepStatus.FAILED,
                message=(
                    "update_rtpmanager.sh marker not found at "
                    f"{_rtpmanager_marker_path(ctx)}; post-install tail did "
                    "not run"
                ),
            )

        passed, tail = _run_smoke_test(ctx, docker=False)
        self._smoke_passed = passed
        if not passed:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"DeepStream smoke test failed: {tail}",
            )

        return StepResult(status=StepStatus.COMPLETE)

    def _verify_docker(self, ctx: "Context") -> StepResult:
        # doc section 7.4: "Prereq pins still match" applies to both verify
        # paths -- the docker runtime still depends on the host driver.
        pin_failure = _check_prereq_pins(ctx)
        if pin_failure is not None:
            return StepResult(status=StepStatus.FAILED, message=pin_failure)

        inspect = _run_as_user(ctx, "docker", "image", "inspect", DOCKER_IMAGE)
        if inspect.returncode != 0:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"docker image {DOCKER_IMAGE} not present locally",
            )

        version_output = _deepstream_app_version(ctx, docker=True)
        actual_version = None
        if version_output:
            match = _VERSION_RE.search(version_output)
            actual_version = match.group(1) if match else None
        if (
            version_output is None
            or "9.1" not in version_output
            or not actual_version
            or not ctx.verify_pinned("DeepStream", actual_version, DS_VERSION_SHORT)
        ):
            return StepResult(
                status=StepStatus.FAILED,
                message=(
                    "deepstream-app --version-all (in container) did not "
                    "report DeepStream 9.1"
                ),
            )

        info = _run_as_user(ctx, "docker", "info")
        if info.returncode != 0 or "nvidia" not in (info.stdout or "").lower():
            return StepResult(
                status=StepStatus.FAILED,
                message="docker info does not show the nvidia runtime",
            )

        passed, tail = _run_smoke_test(ctx, docker=True)
        self._smoke_passed = passed
        if not passed:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"DeepStream smoke test failed (docker): {tail}",
            )

        return StepResult(status=StepStatus.COMPLETE)

    # -- report --------------------------------------------------------

    def report(self, ctx: "Context") -> None:
        method_value = ctx.conf.get(CONF_METHOD_KEY, "unknown")
        outcome = self._outcome
        reason = outcome.reason if outcome is not None else "n/a"
        artifact_source = outcome.artifact_source if outcome is not None else "n/a"
        sdk_path = (
            "in-container (no host path)"
            if method_value == Method.DOCKER.value
            else str(DS_SDK_DIR)
        )
        actions = self._post_install_actions
        smoke = (
            "passed"
            if self._smoke_passed
            else ("failed" if self._smoke_passed is False else "not run")
        )

        log.info("DeepStream 9.1 SDK install summary:")
        log.info(f"  method: {method_value} ({reason})")
        log.info(f"  artifact source: {artifact_source}")
        log.info(f"  SDK path: {sdk_path}")
        log.info(
            "  post-install actions: "
            + (", ".join(actions) if actions else "none (already installed)")
        )
        peoplenet_status = (
            f"present at {_peoplenet_dir(ctx) / PEOPLENET_ONNX_NAME}"
            if _peoplenet_model_present(ctx)
            else "MISSING"
        )
        log.info(f"  PeopleNet model: {peoplenet_status}")
        # doc section 9 point 1: a non-zero update_rtpmanager.sh exit is
        # only a warning, but it must be surfaced here. Host installs only
        # -- docker skips the host post-install tail entirely (doc
        # section 9's own header), so the marker is never written for it.
        if method_value != Method.DOCKER.value:
            marker = _read_rtpmanager_marker(ctx)
            log.info(f"  update_rtpmanager.sh: {_describe_rtpmanager_marker(marker)}")
        log.info(f"  smoke test: {smoke}")


register(Step2DeepStreamSdk())
