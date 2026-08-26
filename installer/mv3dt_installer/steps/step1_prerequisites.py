"""Step 1 — Prerequisites (driver / CUDA / cuDNN / TensorRT / GStreamer).

Implements `installer/plan/STEP-1-PREREQUISITES.md` against the framework
contracts in `installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md` sections 7
(reboot), 8 (logging/reporting), 9 (privilege/USER-ACTION), and 12
(step-module interface). Installs and verifies every DeepStream 9.1 dGPU
prerequisite on a brand-new Ubuntu 24.04 workstation before the DeepStream
SDK itself is pulled (Step 2's job).

Two-launch structure (STEP-1 section 5)
----------------------------------------
The NVIDIA driver `.run` install requires a reboot mid-step, so `run()`
tracks its own internal stage via an idempotent probe -- `nvidia-smi`
reporting a driver version -- rather than writing `state.json` itself
(only the framework does that, doc 00 section 12.2):

* **Launch A** (driver not yet loaded): base deps, the DS 9.1 section 4.1
  apt prerequisites, the CUDA repo + toolkit, nouveau/distro-driver
  cleanup, the Secure Boot gate, stopping the desktop session, and running
  the driver `.run`. Ends in `USER_ACTION_REQUIRED` (reboot instructions)
  once the `.run` succeeds, or earlier for the nouveau/distro-driver
  cleanup, Secure Boot gate, staging gap, or a `gdm`/`lightdm` stop
  failure; `FAILED` only if the `.run` itself exits non-zero.
* **Launch B** (driver loaded): TensorRT + cuDNN via apt, a GStreamer pin
  confirmation, and the Mosquitto broker via the bundled
  `10_setup_mosquitto.sh` (section 3.2). Ends in `COMPLETE`, letting the
  framework call `verify()`.

Reboot handling deliberately does not use `ctx.reboot.request()` /
`StepStatus.REBOOT_REQUIRED` for either of Launch A's two reboot points
(the driver `.run` and the nouveau/distro-driver cleanup). The merged
`reboot.reconcile()` (`mv3dt_installer/reboot.py`) marks the *requesting*
step `COMPLETE` in `state.json` the instant it confirms a reboot happened,
and `app._dispatch()` skips a `COMPLETE` step without re-running its
lifecycle -- so a step with real work left to do post-reboot (this one:
TensorRT/cuDNN/mosquitto/verify) cannot safely ask the framework to
auto-complete it on reboot. Both reboot points instead return
`StepResult(status=USER_ACTION_REQUIRED, ...)` with the same reboot
instructions and `sudo reboot` action; `state.json` never records a
pending reboot for this step, and the step stays non-`COMPLETE` across the
reboot. On the next launch, `_driver_loaded(ctx)` -- the same idempotent
probe used to route `run()` to Launch A vs. B -- is what detects the
reboot actually happened and lets Launch A's own idempotent cleanup
probes (nouveau no longer loaded / no distro packages left to purge) fall
through to whatever comes next.

All subprocess work goes through `ctx.run_root` (never a bare
`subprocess.run`), which is the seam tests inject a fake `Context` through.
System paths this module writes directly (`/etc/profile.d/cuda.sh`,
`/etc/modprobe.d/blacklist-nouveau.conf`, `/etc/mosquitto/conf.d/mv3dt.conf`)
are named by module-level constants, following the same pattern
`systemd.py`'s `UNIT_DIR` uses, so tests can monkeypatch them to a `tmp_path`
without ever touching the real filesystem.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import sys
from typing import TYPE_CHECKING, Sequence

from mv3dt_installer import shellout
from mv3dt_installer.steps import StepResult, StepStatus, UserAction, register

if TYPE_CHECKING:  # pragma: no cover
    from mv3dt_installer.app import Context

__all__ = ["Step1Prerequisites"]

# ---------------------------------------------------------------------------
# STEP-1 section 2 -- the DS 9.1 dGPU prerequisite pins (EQUALITY)
# ---------------------------------------------------------------------------

DRIVER_VERSION = "595.58.03"
CUDA_VERSION = "13.2"
CUDNN_VERSION = "9.20.0.48"
TENSORRT_VERSION = "10.16.0.72-1+cuda13.2"
GSTREAMER_VERSION = "1.24.2"
CUDA_TOOLKIT_PACKAGE = "cuda-toolkit-13-2"

# STEP-1 section 2.1 -- every libnvinfer* package pinned to one version.
TENSORRT_PACKAGES: tuple[str, ...] = (
    "libnvinfer-dev",
    "libnvinfer-dispatch-dev",
    "libnvinfer-dispatch10",
    "libnvinfer-headers-dev",
    "libnvinfer-headers-plugin-dev",
    "libnvinfer-safe-headers-dev",
    "libnvinfer-lean-dev",
    "libnvinfer-lean10",
    "libnvinfer-plugin-dev",
    "libnvinfer-plugin10",
    "libnvinfer-vc-plugin-dev",
    "libnvinfer-vc-plugin10",
    "libnvinfer10",
    "libnvonnxparsers-dev",
    "libnvonnxparsers10",
    "tensorrt-dev",
    "libnvinfer-headers-python-plugin-dev",
    "libnvinfer-win-builder-resource10",
)

# cuDNN's package set is not enumerated by the DS 9.1 page the way
# TensorRT's is, so the family is still installed via a glob -- but (like
# TensorRT) the meta package itself is version-pinned in the apt invocation
# (`libcudnn9=9.20.0.48`, alongside the `libcudnn9*` glob for the rest of the
# family), so a version mismatch fails fast at apt-install time instead of
# only surfacing later at verify(). `libcudnn9` alone is used for
# before/after presence + verify().
CUDNN_APT_GLOB = "libcudnn9*"
CUDNN_QUERY_PACKAGE = "libcudnn9"

# STEP-1 section 4, caveats 1-2: kernel headers + minimal-24.04 tooling,
# installed before the DS 9.1 apt prerequisites and the CUDA repo.
BASE_KERNEL_PACKAGES: tuple[str, ...] = ("build-essential", "dkms")
BASE_TOOLING_PACKAGES: tuple[str, ...] = (
    "software-properties-common",
    "ca-certificates",
    "gnupg",
    "curl",
)

# STEP-1 section 3 -- the DS 9.1 section 4.1 apt prerequisite package list,
# installed in a single transaction. Includes both the DS 9.1-authoritative
# packages (section 3.1) and the four installer-subsystem additions
# (mosquitto, mosquitto-clients, arp-scan, ffmpeg, section 3.1's table).
APT_PREREQ_PACKAGES: tuple[str, ...] = (
    "libssl3",
    "libssl-dev",
    "libcurl4-openssl-dev",
    "libgles2-mesa-dev",
    "libgstreamer1.0-0",
    "gstreamer1.0-tools",
    "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad",
    "gstreamer1.0-plugins-ugly",
    "gstreamer1.0-libav",
    "libgstreamer-plugins-base1.0-dev",
    "libgstrtspserver-1.0-0",
    "libjansson4",
    "libyaml-cpp-dev",
    "libjsoncpp-dev",
    "protobuf-compiler",
    "libmosquitto1",
    "gcc",
    "make",
    "git",
    "python3",
    "mosquitto",
    "mosquitto-clients",
    "arp-scan",
    "ffmpeg",
)

# STEP-1 section 5.1 -- the driver .run is not bundled (too large); it must
# be staged by the operator at this path under the install dir.
DRIVER_RUN_FILENAME = f"NVIDIA-Linux-x86_64-{DRIVER_VERSION}.run"

# STEP-1 section 4, caveat 7 / section 6.2 -- CUDA on PATH for new shells.
CUDA_HOME = f"/usr/local/cuda-{CUDA_VERSION}"
CUDA_PROFILE_PATH = pathlib.Path("/etc/profile.d/cuda.sh")

# STEP-1 section 4, caveat 4 -- nouveau must be out of the way before the
# .run installer runs.
NOUVEAU_BLACKLIST_PATH = pathlib.Path("/etc/modprobe.d/blacklist-nouveau.conf")

# STEP-1 section 3.2 -- the Mosquitto broker drop-in this step's Python
# wraps the bundled script's install with change detection for.
MOSQUITTO_CONF_DIR = pathlib.Path("/etc/mosquitto/conf.d")
MOSQUITTO_CONF_NAME = "mv3dt.conf"

_NVCC_RELEASE_RE = re.compile(r"release\s+(\d+\.\d+)")
_GSTREAMER_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


# ---------------------------------------------------------------------------
# Small probes, all routed through ctx.run_root (the test seam)
# ---------------------------------------------------------------------------


def _dpkg_version(ctx: "Context", package: str) -> str | None:
    """`dpkg-query -W -f='${Version}' <package>`, or None if not installed."""
    result = ctx.run_root(
        "dpkg-query",
        "-W",
        "-f=${Version}",
        package,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def _cudnn_installed_version(ctx: "Context") -> str | None:
    return _dpkg_version(ctx, CUDNN_QUERY_PACKAGE)


def _driver_version(ctx: "Context") -> str:
    result = ctx.run_root(
        "nvidia-smi",
        "--query-gpu=driver_version",
        "--format=csv,noheader",
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _driver_loaded(ctx: "Context") -> bool:
    """The idempotent A/B stage probe (STEP-1 section 6.3 / 7.1): driver
    present and `nvidia-smi` loads."""
    return bool(_driver_version(ctx))


def _nvcc_release(ctx: "Context") -> str:
    result = ctx.run_root(
        "nvcc", "--version", capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return ""
    match = _NVCC_RELEASE_RE.search(result.stdout)
    return match.group(1) if match else ""


def _gstreamer_version(ctx: "Context") -> str:
    result = ctx.run_root(
        "gst-inspect-1.0", "--version", capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return ""
    match = _GSTREAMER_VERSION_RE.search(result.stdout)
    return match.group(1) if match else ""


def _os_version(ctx: "Context") -> str:
    result = ctx.run_root(
        "lsb_release", "-rs", capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _arch(ctx: "Context") -> str:
    result = ctx.run_root("uname", "-m", capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _kernel_release(ctx: "Context") -> str:
    result = ctx.run_root("uname", "-r", capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _gpu_present(ctx: "Context") -> bool:
    result = ctx.run_root(
        "bash", "-c", "lspci | grep -qi nvidia", check=False, capture_output=True, text=True
    )
    return result.returncode == 0


def _secure_boot_enabled(ctx: "Context") -> bool:
    """STEP-1 section 4, caveat 5. `mokutil` absent (or a non-UEFI system)
    is treated as "not enabled" -- there is nothing to gate on."""
    result = ctx.run_root(
        "mokutil", "--sb-state", capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return False
    return "enabled" in result.stdout.lower()


def _stop_display_manager(ctx: "Context") -> bool:
    """STEP-1 section 4, caveat 6: `service gdm stop` (fallback `lightdm`),
    then `pkill -9 Xorg`. Returns whether a display manager was actually
    stopped (or none was running to begin with)."""
    result = ctx.run_root(
        "service", "gdm", "stop", check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        result = ctx.run_root(
            "service", "lightdm", "stop", check=False, capture_output=True, text=True
        )
    ctx.run_root("pkill", "-9", "Xorg", check=False, capture_output=True, text=True)
    return result.returncode == 0


def _nouveau_loaded(ctx: "Context") -> bool:
    result = ctx.run_root(
        "bash",
        "-c",
        "lsmod | grep -q '^nouveau'",
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _driver_run_path(ctx: "Context") -> pathlib.Path:
    return pathlib.Path(ctx.install_dir) / "downloads" / "nvidia" / DRIVER_RUN_FILENAME


def _is_non_interactive(ctx: "Context") -> bool:
    """STEP-1 section 3.2 requires forwarding `--non-interactive` to the
    bundled mosquitto script under an unattended run, or the script's own
    `read` blocks forever on a TTY that will never answer it. `Context`
    (doc 00 section 12.3) carries no `non_interactive` field today, so this
    prefers one if a caller sets it (duck-typed, forward compatible) and
    otherwise falls back to checking `sys.argv` directly for the flag the
    operator actually passed to the installer. A tty-detection fallback
    (`not sys.stdin.isatty()`) was tried first but rejected: it silently
    drops `--non-interactive` forwarding whenever a tty happens to be
    attached to an otherwise-unattended run, risking the exact hang this
    forwarding exists to prevent. `sys.argv` has no such false negative.
    """
    value = getattr(ctx, "non_interactive", None)
    if value is not None:
        return bool(value)
    return "--non-interactive" in sys.argv


def _mosquitto_dst_path() -> pathlib.Path:
    return MOSQUITTO_CONF_DIR / MOSQUITTO_CONF_NAME


def _sha256_file(path: "pathlib.Path | str") -> str | None:
    try:
        return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _mosquitto_conf_matches_bundled(ctx: "Context") -> bool:
    dst_hash = _sha256_file(_mosquitto_dst_path())
    bundled_hash = _sha256_file(ctx.asset_path("mosquitto", "mv3dt.conf"))
    return dst_hash is not None and dst_hash == bundled_hash


# ---------------------------------------------------------------------------
# apt install + report_installed/report_already_installed wrapping
# ---------------------------------------------------------------------------


def _apt_install_reported(
    ctx: "Context",
    query_packages: Sequence[str],
    *,
    apt_args: Sequence[str] | None = None,
) -> None:
    """Install `query_packages` (or `apt_args`, if the apt invocation needs
    version-pinned `pkg=version` arguments) in one apt transaction, then
    report each with the doc 00 section 8.3 exact strings, based on a
    before/after `dpkg-query` presence probe (STEP-1 section 2.2 / 7.2)."""
    before = {pkg: _dpkg_version(ctx, pkg) for pkg in query_packages}
    argv = list(apt_args) if apt_args is not None else list(query_packages)
    ctx.run_root(
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        *argv,
        check=False,
        capture_output=True,
        text=True,
    )
    for pkg in query_packages:
        after = _dpkg_version(ctx, pkg) or "unknown"
        if before[pkg] is None:
            ctx.report_installed(pkg, after)
        else:
            ctx.report_already_installed(pkg, after)


def _install_cuda_toolkit(ctx: "Context") -> None:
    """STEP-1 section 5 step 4: CUDA repo + keyring, then
    `cuda-toolkit-13-2`. The reported version is the CUDA_VERSION pin
    (`13.2`), not the package's raw apt version suffix -- matching the
    exact example string in STEP-1 section 2.2
    ("installed cuda-toolkit-13-2 version 13.2")."""
    before = _dpkg_version(ctx, CUDA_TOOLKIT_PACKAGE)
    ctx.run_root(
        "bash",
        "-c",
        "wget -q -O /tmp/cuda-keyring.deb "
        "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb "
        "&& dpkg -i /tmp/cuda-keyring.deb",
        check=False,
        capture_output=True,
        text=True,
    )
    ctx.run_root("apt-get", "update", check=False, capture_output=True, text=True)
    ctx.run_root(
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        CUDA_TOOLKIT_PACKAGE,
        check=False,
        capture_output=True,
        text=True,
    )
    if before is None:
        ctx.report_installed(CUDA_TOOLKIT_PACKAGE, CUDA_VERSION)
    else:
        ctx.report_already_installed(CUDA_TOOLKIT_PACKAGE, CUDA_VERSION)


def _write_cuda_profile() -> None:
    """STEP-1 section 4, caveat 7."""
    CUDA_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CUDA_PROFILE_PATH.write_text(
        "# Managed by mv3dt-installer (installer/plan/STEP-1-PREREQUISITES.md section 4).\n"
        f'export PATH="{CUDA_HOME}/bin:$PATH"\n'
        f'export LD_LIBRARY_PATH="{CUDA_HOME}/lib64:$LD_LIBRARY_PATH"\n',
        encoding="utf-8",
    )


def _write_nouveau_blacklist() -> None:
    """STEP-1 section 4, caveat 4."""
    NOUVEAU_BLACKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOUVEAU_BLACKLIST_PATH.write_text(
        "# Managed by mv3dt-installer (installer/plan/STEP-1-PREREQUISITES.md section 4).\n"
        "blacklist nouveau\n"
        "options nouveau modeset=0\n",
        encoding="utf-8",
    )


def _purge_distro_nvidia_packages(ctx: "Context") -> bool:
    """STEP-1 section 4, caveat 3. Returns whether anything was purged."""
    result = ctx.run_root(
        "bash",
        "-c",
        "dpkg-query -W -f='${Package}\\n' 'nvidia-*' 'libnvidia-*' 2>/dev/null",
        capture_output=True,
        text=True,
        check=False,
    )
    packages = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not packages:
        return False
    ctx.run_root(
        "apt-get", "purge", "-y", *packages, check=False, capture_output=True, text=True
    )
    ctx.run_root(
        "apt-get", "autoremove", "-y", check=False, capture_output=True, text=True
    )
    return True


def _clean_nouveau_and_distro_driver(ctx: "Context") -> bool:
    """STEP-1 section 4, caveats 3-4 / section 5 step 5. Returns whether a
    reboot is now required (nouveau was loaded, or a distro package was
    purged)."""
    nouveau_loaded = _nouveau_loaded(ctx)
    _write_nouveau_blacklist()
    if nouveau_loaded:
        ctx.run_root(
            "update-initramfs", "-u", check=False, capture_output=True, text=True
        )

    purged = _purge_distro_nvidia_packages(ctx)
    return nouveau_loaded or purged


def _record_gpu_info(ctx: "Context") -> None:
    """STEP-1 section 8: informational, not a gate."""
    result = ctx.run_root(
        "nvidia-smi",
        "--query-gpu=name,compute_cap",
        "--format=csv,noheader",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        ctx.log.info(f"GPU: {result.stdout.strip()}")


def _verify_mosquitto_active(ctx: "Context") -> bool:
    """STEP-1 section 3.2 / section 7.3: broker active + drop-in matches the
    bundled asset. Uses `ctx.log`/`verify_pinned`-style wording rather than
    `verify_pinned` itself, since this is a pass/fail check, not a pinned
    version (section 3.2's explicit call-out)."""
    active = (
        ctx.run_root(
            "systemctl",
            "is-active",
            "--quiet",
            "mosquitto",
            check=False,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )
    conf_ok = _mosquitto_conf_matches_bundled(ctx)
    if active and conf_ok:
        ctx.log.info("Version OK: mosquitto broker == active, mv3dt.conf == bundled")
        return True
    ctx.log.info(
        "Version check failed: mosquitto broker — "
        f"expected active=True, conf_matches_bundled=True, "
        f"got active={active}, conf_matches_bundled={conf_ok}"
    )
    return False


# ---------------------------------------------------------------------------
# Reboot user-action blocks (STEP-1 section 6.2)
# ---------------------------------------------------------------------------


def _driver_reboot_actions() -> list[UserAction]:
    return [
        UserAction(
            text=(
                f"The NVIDIA driver {DRIVER_VERSION} kernel module is installed but not "
                "yet loaded. Reboot so nvidia.ko loads before TensorRT/cuDNN install."
            ),
        ),
        UserAction(
            text=(
                "CUDA 13.2 has been added to PATH/LD_LIBRARY_PATH for new login shells."
            ),
            path=str(CUDA_PROFILE_PATH),
        ),
        UserAction(command="sudo reboot", text="Reboot the workstation."),
    ]


def _cleanup_reboot_actions() -> list[UserAction]:
    return [
        UserAction(
            text=(
                "nouveau was unloaded and/or a conflicting distro NVIDIA package was "
                "purged. Reboot before the NVIDIA driver .run installer runs, since it "
                "cannot build against a live nouveau or a partially-removed driver."
            ),
        ),
        UserAction(command="sudo reboot", text="Reboot the workstation."),
    ]


# ---------------------------------------------------------------------------
# The Step
# ---------------------------------------------------------------------------


class Step1Prerequisites:
    """STEP-1-PREREQUISITES.md section 1: module identity."""

    id = "step1_prerequisites"
    title = "Prerequisites (driver / CUDA / cuDNN / TensorRT / GStreamer)"
    order = 1

    # -- preflight (section 7.1) -------------------------------------------

    def preflight(self, ctx: "Context") -> StepResult:
        os_version = _os_version(ctx)
        arch = _arch(ctx)
        if os_version != "24.04" or arch != "x86_64":
            return StepResult(
                status=StepStatus.FAILED,
                message=(
                    f"unsupported platform: Ubuntu {os_version or '?'} / {arch or '?'} "
                    "(Step 1 requires Ubuntu 24.04 / x86_64; see "
                    "laptop/docs/DEEPSTREAM-SETUP.md sections 2-3)"
                ),
            )
        if not _gpu_present(ctx):
            return StepResult(
                status=StepStatus.FAILED,
                message="no NVIDIA GPU detected via lspci",
            )
        return StepResult(status=StepStatus.COMPLETE)

    # -- run (section 7.2) ---------------------------------------------------

    def run(self, ctx: "Context") -> StepResult:
        if not _driver_loaded(ctx):
            return self._run_launch_a(ctx)
        return self._run_launch_b(ctx)

    def _run_launch_a(self, ctx: "Context") -> StepResult:
        kernel = _kernel_release(ctx)
        base_kernel_packages = [
            *BASE_KERNEL_PACKAGES,
            f"linux-headers-{kernel}" if kernel else "linux-headers-generic",
        ]
        _apt_install_reported(ctx, base_kernel_packages)
        _apt_install_reported(ctx, list(BASE_TOOLING_PACKAGES))

        _apt_install_reported(ctx, list(APT_PREREQ_PACKAGES))

        _install_cuda_toolkit(ctx)
        _write_cuda_profile()

        if _clean_nouveau_and_distro_driver(ctx):
            # USER_ACTION_REQUIRED, not ctx.reboot.request() -- see the
            # "Reboot handling" note in this class's module docstring. The
            # merged reboot.reconcile() marks the *requesting* step COMPLETE
            # the moment it confirms the reboot, and _dispatch() then skips
            # a COMPLETE step without re-running its lifecycle -- so a
            # REBOOT_REQUIRED here would let the framework auto-complete
            # Step 1 before the driver .run (let alone TensorRT/cuDNN) ever
            # runs. Rendering the same instructions as USER_ACTION_REQUIRED
            # instead leaves the step PENDING; on the next launch `run()`
            # re-enters _run_launch_a(), whose own idempotent probes (no
            # nouveau loaded, no distro packages left to purge) see the
            # cleanup already done and fall through to the Secure Boot
            # check / driver .run.
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message=(
                    "nouveau and/or a distro NVIDIA package were cleaned up; a reboot "
                    "is required before the driver .run installer can run"
                ),
                user_actions=_cleanup_reboot_actions(),
            )

        if _secure_boot_enabled(ctx):
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message=(
                    "Secure Boot is enabled; the unsigned NVIDIA driver module cannot "
                    "load"
                ),
                user_actions=[
                    UserAction(
                        text=(
                            "Disable Secure Boot in the BIOS, or complete MOK Manager "
                            "enrollment on the next boot."
                        ),
                    ),
                ],
            )

        run_path = _driver_run_path(ctx)
        if not run_path.is_file():
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message=f"NVIDIA driver installer not staged at {run_path}",
                user_actions=[
                    UserAction(
                        text=(
                            f"Download driver {DRIVER_VERSION} for Linux x86_64 from "
                            "NVIDIA's driver download page and place it at the exact "
                            "path shown below, then re-run the installer."
                        ),
                        command="https://www.nvidia.com/en-us/drivers/",
                        path=str(run_path),
                    ),
                ],
            )

        if not _stop_display_manager(ctx):
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message=(
                    "could not stop the desktop session (gdm/lightdm) to run the "
                    "driver installer"
                ),
                user_actions=[
                    UserAction(
                        text=(
                            "Switch to a TTY (Ctrl+Alt+F3), log in, and re-run the "
                            "installer from there."
                        ),
                    ),
                ],
            )

        run_path.chmod(0o755)
        result = ctx.run_root(
            str(run_path),
            "--no-cc-version-check",
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"NVIDIA driver installer exited {result.returncode}",
            )
        ctx.report_installed("nvidia-driver", DRIVER_VERSION)

        # USER_ACTION_REQUIRED, not ctx.reboot.request() -- see the reboot
        # cleanup branch above for the full "why". On the next launch,
        # `run()`'s `_driver_loaded(ctx)` probe (nvidia-smi reporting a
        # driver version) is what actually detects the reboot happened and
        # routes into `_run_launch_b`; the framework's boot-id auto-complete
        # is never asked to do that job.
        return StepResult(
            status=StepStatus.USER_ACTION_REQUIRED,
            message=(
                "the NVIDIA driver kernel module is installed but not yet loaded; a "
                "reboot is required before TensorRT/cuDNN install"
            ),
            user_actions=_driver_reboot_actions(),
        )

    def _run_launch_b(self, ctx: "Context") -> StepResult:
        apt_args = [f"{pkg}={TENSORRT_VERSION}" for pkg in TENSORRT_PACKAGES]
        _apt_install_reported(ctx, TENSORRT_PACKAGES, apt_args=apt_args)

        cudnn_before = _dpkg_version(ctx, CUDNN_QUERY_PACKAGE)
        ctx.run_root(
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            f"{CUDNN_QUERY_PACKAGE}={CUDNN_VERSION}",
            CUDNN_APT_GLOB,
            check=False,
            capture_output=True,
            text=True,
        )
        cudnn_after = _dpkg_version(ctx, CUDNN_QUERY_PACKAGE) or "unknown"
        if cudnn_before is None:
            ctx.report_installed(CUDNN_QUERY_PACKAGE, cudnn_after)
        else:
            ctx.report_already_installed(CUDNN_QUERY_PACKAGE, cudnn_after)

        # No separate GStreamer report here: `gstreamer1.0-tools` is already
        # reported (installed/already-installed) by Launch A's
        # `APT_PREREQ_PACKAGES` transaction; `verify()`'s `verify_pinned`
        # call is what confirms the `1.24.2` pin.

        mosquitto_failure = self._run_mosquitto(ctx)
        if mosquitto_failure is not None:
            return mosquitto_failure

        return StepResult(status=StepStatus.COMPLETE)

    def _run_mosquitto(self, ctx: "Context") -> StepResult | None:
        """STEP-1 section 3.2. Runs the bundled script and wraps it with the
        before/after diff-detection reporting the script itself does not
        do. Returns a `StepResult` only on failure; `None` means "continue".
        """
        before_version = _dpkg_version(ctx, "mosquitto")
        dst_path = _mosquitto_dst_path()
        before_hash = _sha256_file(dst_path)
        bundled_hash = _sha256_file(ctx.asset_path("mosquitto", "mv3dt.conf"))

        args = ["--non-interactive"] if _is_non_interactive(ctx) else []
        result = shellout.run_bundled_script(
            "scripts",
            "10_setup_mosquitto.sh",
            args=args,
            env={"MV3DT_INSTALLER_CONF": str(pathlib.Path(ctx.install_dir) / "installer.conf")},
            tree=(),
        )

        if result.returncode != 0:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"mosquitto setup script exited {result.returncode}",
            )

        after_version = _dpkg_version(ctx, "mosquitto") or "unknown"
        if before_version is None:
            ctx.report_installed("mosquitto", after_version)
        else:
            ctx.report_already_installed("mosquitto", after_version)

        after_hash = _sha256_file(dst_path)
        conf_label = (after_hash or "unknown")[:12]
        if before_hash != bundled_hash:
            ctx.report_installed("mv3dt.conf", conf_label)
        else:
            ctx.report_already_installed("mv3dt.conf", conf_label)

        return None

    # -- verify (section 7.3) -------------------------------------------------

    def verify(self, ctx: "Context") -> StepResult:
        checks = [
            ctx.verify_pinned("NVIDIA driver", _driver_version(ctx), DRIVER_VERSION),
            ctx.verify_pinned("CUDA (nvcc release)", _nvcc_release(ctx), CUDA_VERSION),
            ctx.verify_pinned(
                "cuDNN (libcudnn9)",
                _cudnn_installed_version(ctx) or "",
                CUDNN_VERSION,
            ),
            ctx.verify_pinned(
                "TensorRT (libnvinfer10)",
                _dpkg_version(ctx, "libnvinfer10") or "",
                TENSORRT_VERSION,
            ),
            ctx.verify_pinned("GStreamer", _gstreamer_version(ctx), GSTREAMER_VERSION),
            _verify_mosquitto_active(ctx),
        ]

        _record_gpu_info(ctx)

        if all(checks):
            return StepResult(status=StepStatus.COMPLETE)

        return StepResult(
            status=StepStatus.USER_ACTION_REQUIRED,
            message=(
                "one or more DeepStream 9.1 prerequisite pins did not verify; see the "
                "transcript above for which"
            ),
            user_actions=[
                UserAction(
                    text=(
                        "Resolve the version mismatch reported above, then re-run the "
                        "installer."
                    ),
                ),
            ],
        )

    # -- report (section 7.4) --------------------------------------------------

    def report(self, ctx: "Context") -> None:
        ctx.log.info(
            "Step 1 (Prerequisites) summary: "
            f"driver {DRIVER_VERSION}, CUDA {CUDA_VERSION}, cuDNN {CUDNN_VERSION}, "
            f"TensorRT {TENSORRT_VERSION}, GStreamer {GSTREAMER_VERSION}; "
            f"CUDA profile at {CUDA_PROFILE_PATH}; "
            f"nouveau blacklist at {NOUVEAU_BLACKLIST_PATH}; "
            f"mosquitto broker active with drop-in at {_mosquitto_dst_path()}."
        )


register(Step1Prerequisites())
