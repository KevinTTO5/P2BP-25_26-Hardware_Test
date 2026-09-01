"""Step 7 -- Web-app integration over HTTP.

Implements `installer/plan/STEP-7-WEBAPP-INTEGRATION.md` against the
framework contracts in `installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md`
(step-module interface section 12, install-location config section 11,
reporting/verify strings section 8.3-8.4, USER-ACTION display section 9.3,
and above all the web-app credential contract section 14, which this step
consumes via `ctx.webapp` rather than reimplementing).

Scope (STEP-7 section 1):

1. Resolve/validate the web-app credential (section A) -- delegated to
   `ctx.webapp`.
2. Install and enable the artifact upload daemon (section E) and the status
   reporter (section D) as supervised systemd units.
3. Provide the shared signed-URL upload client (section B) and DTO layer
   (section C).
4. Provide the web-app-initiated-operation primitives (section F): the
   mtime edge-trigger and the state-file fan-in reader. No concrete
   operation worker exists yet -- STEP-7's own scope note says so -- so this
   module only builds the mechanism.

Two long-running loops are registered as top-level subcommands, mirroring
Step 3's `amc` extension point (`app.SUBCOMMAND_REGISTRY` /
`app.register_subcommand`, STEP-3 section 6.2, built by the `feat
/installer-step3` unit this module depends on):

    mv3dt-installer reporter   -- the mv3dt-reporter.service ExecStart
    mv3dt-installer uploader   -- the mv3dt-uploader.service ExecStart

Judgment calls this module makes, documented here since the spec leaves
them open:

- **"Step 5 complete" without a state-machine handle on `Context`.** Same
  situation `step3_amc_launcher.py` already resolved for "Step 2 complete":
  doc 00 section 12.3's `Context` carries no accessor for another step's
  `state.json` status. This module follows the same pattern and treats the
  existence of `<install_dir>/projects/registry.json` as the durable proxy
  for "Step 5 ran", per STEP-7 section H.1's own preflight bullet ("Confirm
  Step 5 is COMPLETE and registry.json exists" -- the file's existence is
  the only signal available here).
- **No `DEBUG` log level exists.** `logs.py` (doc 00 section 8.1) exposes
  only `info`/`warn`/`error`. STEP-7 section D.4 requires the full status
  payload to log "at DEBUG only, never INFO" -- with no DEBUG level
  available, the payload is simply never logged in full, which satisfies
  the requirement's intent (never spam INFO) without inventing a level the
  framework doesn't have.
- **`redact_url`/endpoint `join` are reused from `webapp.py`, not
  reimplemented.** Doc 00 section 14.4 already defines both in
  `mv3dt_installer/webapp.py` (`redact_url` verbatim per the doc's own code
  block; `join` per section 14.2's strict route-joining rule) -- this
  module imports them rather than duplicating the logic STEP-7's task
  briefing flagged as "implement here if not already present".
- **Status-POST retries.** STEP-7 section B.3's retry table is scoped to
  the three signed-URL upload phases. The status/heartbeat POST (section D)
  is not one of them -- section D.2 says a bad response is simply "logged
  and retried on the next cycle" -- so `UploadClient.post_status` makes a
  single attempt per call and lets the reporter loop's own cadence (section
  D.3) be the retry mechanism, rather than layering the upload retry
  policy onto a call it was never specified for.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import pathlib
import random
import re
import shutil
import stat as stat_mod
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from mv3dt_installer import app as app_mod
from mv3dt_installer import systemd
from mv3dt_installer.logs import log
from mv3dt_installer.state import write_json_atomic
from mv3dt_installer.steps import StepResult, StepStatus, UserAction, register
from mv3dt_installer.webapp import join as endpoint_join
from mv3dt_installer.webapp import redact_url

__all__ = [
    "STEP_VERSION",
    "RequestUploadUrlDto",
    "UploadUrlResponseDto",
    "ConfirmUploadedMediaDto",
    "MediaRecordResponseDto",
    "RemotePathParts",
    "split_remote_path",
    "RetryPolicy",
    "REQUEST_UPLOAD_RETRY",
    "CONFIRM_UPLOAD_RETRY",
    "is_retryable_status",
    "backoff_delay",
    "HttpError",
    "HttpResponse",
    "UploadClient",
    "WEBAPP_HEARTBEAT_ROUTE",
    "build_services_payload",
    "collect_gpu_memory",
    "build_status_payload",
    "read_state_file",
    "load_config",
    "apply_config",
    "stable_hash",
    "resolve_heartbeat_interval",
    "next_interval",
    "UploadRecord",
    "UploadDecision",
    "decide_upload",
    "record_upload_success",
    "record_upload_failure",
    "DiskThresholds",
    "disk_status",
    "WatchSpec",
    "default_watch_specs",
    "MtimeEdgeTrigger",
    "Step7WebappIntegration",
]

STEP_VERSION = "7.0.0"
STEP_ID = "step7_webapp_integration"

_ONE_MB = 1024 * 1024


# ---------------------------------------------------------------------------
# STEP-7 section C -- the DTO contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestUploadUrlDto:
    PathFromRoot: str
    FileName: str
    Extension: str
    SizeBytes: int

    def to_dict(self) -> dict:
        return {
            "PathFromRoot": self.PathFromRoot,
            "FileName": self.FileName,
            "Extension": self.Extension,
            "SizeBytes": self.SizeBytes,
        }


@dataclass(frozen=True)
class UploadUrlResponseDto:
    PathFromRoot: str
    SignedUrl: str
    ExpiresAt: Optional[str] = None

    @staticmethod
    def from_dict(d: dict) -> "UploadUrlResponseDto":
        """Defensive deserialization (STEP-7 section C, REQUIRED): a missing
        or `null` field yields an empty/usable value, never a KeyError."""
        return UploadUrlResponseDto(
            PathFromRoot=str(d.get("PathFromRoot") or ""),
            SignedUrl=str(d.get("SignedUrl") or ""),
            ExpiresAt=d.get("ExpiresAt") or None,
        )


@dataclass(frozen=True)
class ConfirmUploadedMediaDto:
    PathFromRoot: str
    FileName: str
    Extension: str

    def to_dict(self) -> dict:
        return {
            "PathFromRoot": self.PathFromRoot,
            "FileName": self.FileName,
            "Extension": self.Extension,
        }


@dataclass(frozen=True)
class MediaRecordResponseDto:
    Id: str
    Name: str
    PathFromRoot: str
    Extension: str

    @staticmethod
    def from_dict(d: dict) -> "MediaRecordResponseDto":
        return MediaRecordResponseDto(
            Id=str(d.get("Id") or ""),
            Name=str(d.get("Name") or ""),
            PathFromRoot=str(d.get("PathFromRoot") or ""),
            Extension=str(d.get("Extension") or ""),
        )


# ---------------------------------------------------------------------------
# STEP-7 section B.2 -- remote path shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemotePathParts:
    path_from_root: str
    file_name: str
    extension: str


def split_remote_path(remote_path: str) -> RemotePathParts:
    """Split a single POSIX-style `remote_path` into the three DTO fields
    (STEP-7 section B.2).

    Normalization before splitting: backslashes become forward slashes,
    surrounding whitespace is stripped, and a leading `/` is added if
    absent. A path with no filename or no extension is a `ValueError` -- a
    caller bug, not a transient failure, so it must never enter a retry
    path.
    """
    normalized = remote_path.replace("\\", "/").strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized

    dir_part, _, base = normalized.rpartition("/")
    path_from_root = dir_part or "/"

    if not base or "." not in base:
        raise ValueError(
            f"remote_path {remote_path!r} has no filename/extension "
            "(basename must be 'name.ext')"
        )
    file_name, _, extension = base.rpartition(".")
    if not file_name or not extension:
        raise ValueError(
            f"remote_path {remote_path!r} has no filename/extension "
            "(basename must be 'name.ext')"
        )
    return RemotePathParts(
        path_from_root=path_from_root, file_name=file_name, extension=extension
    )


# ---------------------------------------------------------------------------
# STEP-7 section B.3 -- retry policy (REQUIRED)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay: float
    cap_delay: float


#: request-upload: 5 attempts, 0.5s base / 30s cap.
REQUEST_UPLOAD_RETRY = RetryPolicy(max_attempts=5, base_delay=0.5, cap_delay=30.0)
#: confirm-upload: 8 attempts, 1.0s base / 60s cap -- the longest budget,
#: because a confirmed-but-unrecorded object is the worst outcome.
CONFIRM_UPLOAD_RETRY = RetryPolicy(max_attempts=8, base_delay=1.0, cap_delay=60.0)


def is_retryable_status(status: int) -> bool:
    """Retry only 429 and 5xx (STEP-7 section B.3, REQUIRED). Every other
    4xx means the request itself is wrong and must fail immediately."""
    return status == 429 or status >= 500


def backoff_delay(
    attempt: int, policy: RetryPolicy, *, rng: "random.Random | Any" = random
) -> float:
    """Exponential backoff with jitter (STEP-7 section B.3):
    `delay = min(cap, base * 2^(attempt-1))`, plus a random `0..25%` of that
    value. `attempt` is the 1-based attempt number that just failed.
    """
    base = min(policy.cap_delay, policy.base_delay * (2 ** (attempt - 1)))
    jitter = rng.uniform(0, 0.25 * base) if base > 0 else 0.0
    return base + jitter


# ---------------------------------------------------------------------------
# HTTP transport (stdlib only -- no `requests` dependency)
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: dict


class HttpError(Exception):
    """A non-2xx HTTP response. Message is pre-redacted (section 14.4) and
    pre-truncated (~2000 chars, section B.4) so it is always safe to log."""

    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(
            f"HTTP {status} for {redact_url(url)}: {_truncate_body(body)}"
        )


def _truncate_body(body: str, limit: int = 2000) -> str:
    if len(body) <= limit:
        return body
    return body[:limit] + f"… <truncated {len(body) - limit} chars>"


def default_json_transport(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    data: Optional[bytes] = None,
    timeout: float = 30.0,
) -> HttpResponse:
    """Default transport for the JSON-bodied phases (request-upload,
    confirm-upload, status POST). Small bodies -- read fully is fine."""
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return HttpResponse(
                status=resp.getcode(), body=body, headers=dict(resp.headers)
            )
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return HttpResponse(
            status=exc.code, body=body, headers=dict(exc.headers or {})
        )
    except urllib.error.URLError as exc:
        raise ConnectionError(f"{redact_url(url)}: {exc.reason}") from exc
    except OSError as exc:
        raise ConnectionError(f"{redact_url(url)}: {exc}") from exc


def default_put_transport(
    url: str,
    path: pathlib.Path,
    headers: dict,
    *,
    connect_timeout: float = 10.0,
    read_timeout: float = 300.0,
) -> HttpResponse:
    """Default transport for the signed-URL PUT (section B.1): streams the
    file body rather than reading it into memory, and applies the
    asymmetric timeouts the doc calls for (~10s connect, ~300s read --
    large clips legitimately take minutes).
    """
    parsed = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parsed.hostname, parsed.port, timeout=connect_timeout)
    target = parsed.path + (("?" + parsed.query) if parsed.query else "")
    try:
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(read_timeout)
        with open(path, "rb") as f:
            conn.putrequest("PUT", target, skip_accept_encoding=True)
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.endheaders()
            while True:
                chunk = f.read(_ONE_MB)
                if not chunk:
                    break
                conn.send(chunk)
        resp = conn.getresponse()
        body = resp.read()
        return HttpResponse(status=resp.status, body=body, headers=dict(resp.getheaders()))
    except OSError as exc:
        raise ConnectionError(f"{redact_url(url)}: {exc}") from exc
    finally:
        conn.close()


JsonTransport = Callable[..., HttpResponse]
PutTransport = Callable[..., HttpResponse]

#: STEP-7 section G: proposed, unconfirmed registration route. `preflight`/
#: `verify` treat a 404 here as USER_ACTION_REQUIRED, not a hard failure.
WEBAPP_HEARTBEAT_ROUTE = "/api/Workstation/heartbeat"
_REQUEST_UPLOAD_ROUTE = "/api/files/request-upload"
_CONFIRM_UPLOAD_ROUTE = "/api/files/confirm-upload"


class UploadClient:
    """The shared signed-URL upload client (STEP-7 section B) plus the
    status POST (section D). Both transports are injectable so tests never
    touch real sockets."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        json_transport: JsonTransport = default_json_transport,
        put_transport: PutTransport = default_put_transport,
        sleep: Callable[[float], None] = time.sleep,
        rng: "random.Random | Any" = random,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self._json_transport = json_transport
        self._put_transport = put_transport
        self._sleep = sleep
        self._rng = rng

    def _headers(self) -> dict:
        # STEP-7 section A: every request except the signed-URL PUT carries
        # exactly these two headers.
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _post_json_with_retry(self, route: str, payload: dict, policy: RetryPolicy) -> dict:
        url = endpoint_join(self.endpoint, route)
        data = json.dumps(payload).encode("utf-8")
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._json_transport(
                    "POST", url, headers=self._headers(), data=data
                )
            except ConnectionError:
                if attempt >= policy.max_attempts:
                    raise
                log.warn(
                    f"step7: {route} connection error "
                    f"(attempt {attempt}/{policy.max_attempts}), retrying"
                )
                self._sleep(backoff_delay(attempt, policy, rng=self._rng))
                continue

            if resp.status < 300:
                text = resp.body.decode("utf-8", "replace") if resp.body else "{}"
                try:
                    return json.loads(text or "{}")
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"non-JSON response from {redact_url(url)}"
                    ) from exc

            if not is_retryable_status(resp.status) or attempt >= policy.max_attempts:
                raise HttpError(resp.status, resp.body.decode("utf-8", "replace"), url)

            log.warn(
                f"step7: {route} HTTP {resp.status} "
                f"(attempt {attempt}/{policy.max_attempts}), retrying"
            )
            self._sleep(backoff_delay(attempt, policy, rng=self._rng))

    def request_upload_url(self, dto: RequestUploadUrlDto) -> UploadUrlResponseDto:
        body = self._post_json_with_retry(
            _REQUEST_UPLOAD_ROUTE, dto.to_dict(), REQUEST_UPLOAD_RETRY
        )
        result = UploadUrlResponseDto.from_dict(body)
        if not result.SignedUrl:
            # section B.1: a 200 with an empty SignedUrl is a failure, not a
            # success -- observed under load.
            raise ValueError(
                "request-upload returned HTTP 2xx with an empty SignedUrl"
            )
        return result

    def put_bytes(self, signed_url: str, path: pathlib.Path) -> None:
        """Phase 2 (section B.1): no Authorization header, no retry here --
        the outer daemon retries the whole file on failure (section B.3)."""
        if not path.exists():
            raise FileNotFoundError(f"cannot upload missing file: {path}")
        size = path.stat().st_size
        if size == 0:
            raise ValueError(f"refusing to upload zero-byte file: {path}")
        resp = self._put_transport(
            signed_url,
            path,
            {
                "Content-Length": str(size),
                "Content-Type": "application/octet-stream",
            },
        )
        if resp.status >= 300:
            raise HttpError(
                resp.status, resp.body.decode("utf-8", "replace"), signed_url
            )

    def confirm_upload(self, dto: ConfirmUploadedMediaDto) -> MediaRecordResponseDto:
        body = self._post_json_with_retry(
            _CONFIRM_UPLOAD_ROUTE, dto.to_dict(), CONFIRM_UPLOAD_RETRY
        )
        return MediaRecordResponseDto.from_dict(body)

    def upload_file(
        self, local_path: pathlib.Path, remote_path: str
    ) -> MediaRecordResponseDto:
        """The full three-phase flow (section B.1) for one local file."""
        if not local_path.exists():
            raise FileNotFoundError(f"cannot upload missing file: {local_path}")
        size = local_path.stat().st_size
        if size == 0:
            raise ValueError(f"refusing to upload zero-byte file: {local_path}")

        parts = split_remote_path(remote_path)
        upload_url = self.request_upload_url(
            RequestUploadUrlDto(
                PathFromRoot=parts.path_from_root,
                FileName=parts.file_name,
                Extension=parts.extension,
                SizeBytes=size,
            )
        )
        self.put_bytes(upload_url.SignedUrl, local_path)
        return self.confirm_upload(
            ConfirmUploadedMediaDto(
                PathFromRoot=parts.path_from_root,
                FileName=parts.file_name,
                Extension=parts.extension,
            )
        )

    def post_status(self, payload: dict) -> dict:
        """The registration/status POST (section D.1). Single attempt --
        see this module's docstring for why no retry policy applies here."""
        url = endpoint_join(self.endpoint, WEBAPP_HEARTBEAT_ROUTE)
        data = json.dumps(payload).encode("utf-8")
        resp = self._json_transport("POST", url, headers=self._headers(), data=data)
        if resp.status >= 300:
            raise HttpError(resp.status, resp.body.decode("utf-8", "replace"), url)
        text = resp.body.decode("utf-8", "replace") if resp.body else "{}"
        try:
            return json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"non-JSON status response from {redact_url(url)}"
            ) from exc


# ---------------------------------------------------------------------------
# STEP-7 section F.2 -- state-file fan-in (total reader)
# ---------------------------------------------------------------------------


def read_state_file(path: pathlib.Path, *, default: Any) -> Any:
    """The REQUIRED total reader (section F.2): missing file, unreadable
    file, malformed JSON -- every case yields `default`, never an
    exception."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


# ---------------------------------------------------------------------------
# STEP-7 section D.1 -- the status payload
# ---------------------------------------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


def _systemctl_show(unit: str, runner: Runner, *, timeout: float = 5.0) -> dict:
    """`systemctl show <unit> -p ActiveState -p SubState`, parsed line-wise.

    REQUIRED (section D.1): any failure -- timeout, missing unit, non-zero
    exit, an exception from the runner itself -- yields
    `{"Active": "unknown", "Sub": "unknown"}`, never propagates.
    """
    try:
        result = runner(
            ["systemctl", "show", unit, "-p", "ActiveState", "-p", "SubState"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return {"Active": "unknown", "Sub": "unknown"}

    if getattr(result, "returncode", 1) != 0:
        return {"Active": "unknown", "Sub": "unknown"}

    stdout = getattr(result, "stdout", "") or ""
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()

    active = values.get("ActiveState")
    sub = values.get("SubState")
    if not active or not sub:
        return {"Active": "unknown", "Sub": "unknown"}
    return {"Active": active, "Sub": sub}


def build_services_payload(unit_names: list, *, runner: Runner) -> dict:
    """One entry per supervised unit, keyed with the `.service` suffix
    stripped (section D.1)."""
    payload: dict[str, dict] = {}
    for name in unit_names:
        key = name[: -len(".service")] if name.endswith(".service") else name
        payload[key] = _systemctl_show(name, runner)
    return payload


def collect_gpu_memory(*, runner: Runner) -> dict:
    """`nvidia-smi` GPU/memory collection with `-1` sentinels for
    unavailable fields (section D.1). `FrequencyMhz` has no equivalent in
    this query and always stays `-1`."""
    util = mem_used = mem_total = -1
    try:
        result = runner(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except Exception:
        result = None

    if result is not None and getattr(result, "returncode", 1) == 0:
        stdout = (getattr(result, "stdout", "") or "").strip()
        first_line = stdout.splitlines()[0] if stdout else ""
        fields = [f.strip() for f in first_line.split(",")]
        if len(fields) >= 3:
            try:
                util = int(float(fields[0]))
                mem_used = int(float(fields[1]))
                mem_total = int(float(fields[2]))
            except ValueError:
                util = mem_used = mem_total = -1

    return {
        "Gpu": {"UtilizationPct": util, "FrequencyMhz": -1},
        "Memory": {"UsedMb": mem_used, "TotalMb": mem_total},
    }


def build_status_payload(
    *, unit_names: list, run_dir: pathlib.Path, runner: Runner
) -> dict:
    """The full status payload (section D.1)."""
    gpu_mem = collect_gpu_memory(runner=runner)
    disk = read_state_file(run_dir / "disk_state.json", default=[])
    return {
        "Timestamp": int(time.time()),
        "Services": build_services_payload(unit_names, runner=runner),
        "System": {
            "Gpu": gpu_mem["Gpu"],
            "Memory": gpu_mem["Memory"],
            "Disk": disk,
        },
    }


# ---------------------------------------------------------------------------
# STEP-7 section D.2 -- the response is the config (apply always, persist
# on change)
# ---------------------------------------------------------------------------


def load_config(path: pathlib.Path) -> dict:
    return read_state_file(path, default={})


def apply_config(config: dict, *, run_dir: pathlib.Path) -> None:
    """Applies the received config document (section F). No concrete
    one-shot operation worker exists yet -- STEP-7's own scope note ("no
    concrete 'operation worker' exists yet since that's future work") --
    so this is the extension point a later unit plugs a worker into. It is
    still called on *every* successful response (section D.2 step 4), which
    is the property that must hold regardless of whether a worker exists.
    """
    del config, run_dir  # placeholder -- see docstring


def stable_hash(doc: Any) -> str:
    """First 12 hex chars of a SHA-256 over `doc`'s stable JSON encoding
    (section D.4): `sort_keys=True, separators=(",", ":")`, so an unchanged
    document hashes identically across runs."""
    encoded = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# STEP-7 section D.3 -- cadence
# ---------------------------------------------------------------------------

DEFAULT_HEARTBEAT_INTERVAL = 10.0
FLOOR_INTERVAL = 2.0
FAST_INTERVAL = 2.0
CONFIG_CHANGE_FAST_WINDOW = 15.0


def resolve_heartbeat_interval(config: dict) -> float:
    """`HeartbeatInterval` from the config, falling back to the default on
    `0` or a non-numeric value rather than raising (section D.3)."""
    raw = config.get("HeartbeatInterval", DEFAULT_HEARTBEAT_INTERVAL)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_HEARTBEAT_INTERVAL
    if value <= 0:
        return DEFAULT_HEARTBEAT_INTERVAL
    return value


def next_interval(
    *,
    configured_interval: float,
    operation_active: bool,
    seconds_since_config_change: Optional[float],
) -> float:
    """The section D.3 cadence table. The floor always clamps a bad
    configured value out of a busy loop; the two fast-mode triggers pin the
    interval at exactly 2s."""
    if operation_active:
        return FAST_INTERVAL
    if (
        seconds_since_config_change is not None
        and seconds_since_config_change < CONFIG_CHANGE_FAST_WINDOW
    ):
        return FAST_INTERVAL
    return max(FLOOR_INTERVAL, configured_interval)


# ---------------------------------------------------------------------------
# STEP-7 section D.4 -- log discipline
# ---------------------------------------------------------------------------


class RateLimiter:
    """`allow(key)` returns True at most once per `interval_seconds` for a
    given key (section D.4's "status OK" line and per-error rate limit)."""

    def __init__(
        self, *, interval_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._interval = interval_seconds
        self._clock = clock
        self._last_emit: dict[Any, float] = {}

    def allow(self, key: Any = None) -> bool:
        now = self._clock()
        last = self._last_emit.get(key)
        if last is None or now - last >= self._interval:
            self._last_emit[key] = now
            return True
        return False


# ---------------------------------------------------------------------------
# STEP-7 section E.2 -- dedupe and upload state (the fingerprint rule)
# ---------------------------------------------------------------------------

_MTIME_EPSILON = 1e-6


@dataclass
class UploadRecord:
    size: Optional[float]
    mtime: Optional[float]
    uploaded_at: Optional[float] = None
    failed_attempts: int = 0
    last_failed_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "size": self.size,
            "mtime": self.mtime,
            "uploaded_at": self.uploaded_at,
            "failed_attempts": self.failed_attempts,
            "last_failed_at": self.last_failed_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "UploadRecord":
        size = d.get("size")
        mtime = d.get("mtime")
        return UploadRecord(
            size=float(size) if size is not None else None,
            mtime=float(mtime) if mtime is not None else None,
            uploaded_at=d.get("uploaded_at"),
            failed_attempts=int(d.get("failed_attempts") or 0),
            last_failed_at=d.get("last_failed_at"),
        )


class UploadDecision(str, Enum):
    UPLOAD = "upload"
    SKIP_UNCHANGED = "skip_unchanged"
    SKIP_COOLDOWN = "skip_cooldown"
    SKIP_LEGACY_HYDRATE = "skip_legacy_hydrate"
    SKIP_TOO_YOUNG = "skip_too_young"


DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_COOLDOWN_SECONDS = 1800.0
DEFAULT_MIN_AGE_SECONDS = 0.0
DEFAULT_SCAN_INTERVAL_SECONDS = 240.0


def decide_upload(
    record: Optional[UploadRecord],
    *,
    current_size: float,
    current_mtime: float,
    now: float,
    min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
) -> tuple:
    """The REQUIRED fingerprint rule (section E.2): a file is (re)uploaded
    when new to the state, or when `size != previous size` **or**
    `mtime > previous mtime + 1e-6`. Two further guards precede it: minimum
    age (skip a possibly-still-writing file) and legacy hydration (an entry
    lacking size/mtime is filled in from the current file and skipped this
    cycle, never re-uploaded).

    **Failure/cooldown state is checked before the fingerprint comparison,
    not after it (section E.3).** `record_upload_failure` stamps the
    file's current size/mtime into the record at the moment of failure, so
    if the fingerprint check ran first, an unchanged file would compare
    equal to its own just-recorded failure fingerprint and read as
    `SKIP_UNCHANGED` forever -- `failed_attempts` would never advance past
    1 and the cooldown branch below would never be reached. The fingerprint
    check exists to decide "does this look like a different file than the
    one we last *successfully* processed"; a failed attempt must not be
    able to satisfy that question in the negative. So: while
    `failed_attempts` is between 1 and `max_attempts - 1`, the file is
    retried unconditionally on every scan (this is what makes "attempted 5
    times" a real cadence rather than "attempted once"). Once
    `failed_attempts >= max_attempts` the file enters cooldown until
    `cooldown_seconds` since `last_failed_at` elapse, at which point it is
    also retried unconditionally; a success anywhere in this path resets
    the counter to zero (`record_upload_success`), which is what lets a
    since-fixed file's fingerprint comparisons resume mattering again.

    Returns `(UploadDecision, UploadRecord | None)` -- the second element is
    a hydrated record for `SKIP_LEGACY_HYDRATE`, else the record unchanged
    (or None for a brand new file).
    """
    age = now - current_mtime
    if age < min_age_seconds:
        return UploadDecision.SKIP_TOO_YOUNG, record

    if record is None:
        return UploadDecision.UPLOAD, None

    if record.size is None or record.mtime is None:
        hydrated = UploadRecord(
            size=current_size,
            mtime=current_mtime,
            uploaded_at=record.uploaded_at,
            failed_attempts=record.failed_attempts,
            last_failed_at=record.last_failed_at,
        )
        return UploadDecision.SKIP_LEGACY_HYDRATE, hydrated

    if record.failed_attempts > 0:
        if record.failed_attempts >= max_attempts:
            last_failed = record.last_failed_at or 0.0
            if now - last_failed < cooldown_seconds:
                return UploadDecision.SKIP_COOLDOWN, record
            return UploadDecision.UPLOAD, record  # cooldown elapsed -> retry unconditionally
        return UploadDecision.UPLOAD, record  # still under max_attempts -> keep retrying

    changed = current_size != record.size or current_mtime > record.mtime + _MTIME_EPSILON
    if changed:
        return UploadDecision.UPLOAD, record
    return UploadDecision.SKIP_UNCHANGED, record


def record_upload_success(
    state: dict, key: str, *, size: float, mtime: float, now: float
) -> None:
    """On success the failure counter resets to zero (section E.3)."""
    state[key] = UploadRecord(
        size=size, mtime=mtime, uploaded_at=now, failed_attempts=0, last_failed_at=None
    ).to_dict()


def record_upload_failure(
    state: dict, key: str, *, size: float, mtime: float, now: float
) -> None:
    """Each failure increments `failed_attempts` and stamps
    `last_failed_at` (section E.3)."""
    existing = state.get(key) or {}
    failed_attempts = int(existing.get("failed_attempts") or 0) + 1
    state[key] = UploadRecord(
        size=size,
        mtime=mtime,
        uploaded_at=existing.get("uploaded_at"),
        failed_attempts=failed_attempts,
        last_failed_at=now,
    ).to_dict()


# ---------------------------------------------------------------------------
# STEP-7 section E.4 -- disk-pressure remediation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiskThresholds:
    critical_mb: float = 512.0
    warning_mb: float = 2048.0


def disk_status(free_mb: float, *, critical_mb: float, warning_mb: float) -> str:
    if free_mb < critical_mb:
        return "critical"
    if free_mb < warning_mb:
        return "warning"
    return "ok"


def _free_space_mb(path: pathlib.Path) -> float:
    return shutil.disk_usage(path).free / _ONE_MB


def _delete_oldest_uploaded(
    adir: pathlib.Path,
    upload_state: dict,
    *,
    now: float,
    min_age_seconds: float,
    target_free_mb: float,
) -> int:
    """Delete oldest already-uploaded artifacts until free space recovers
    (section E.4). REQUIRED: never an artifact whose upload was never
    confirmed, never one younger than the minimum age."""
    candidates = []
    for name, raw in upload_state.items():
        uploaded_at = raw.get("uploaded_at") if isinstance(raw, dict) else None
        if uploaded_at is None:
            continue
        mtime = raw.get("mtime")
        if mtime is None or now - mtime < min_age_seconds:
            continue
        candidate = adir / name
        if candidate.exists():
            candidates.append((mtime, candidate))
    candidates.sort(key=lambda pair: pair[0])

    deleted = 0
    for _, candidate in candidates:
        try:
            if _free_space_mb(adir) >= target_free_mb:
                break
        except OSError:
            break
        try:
            candidate.unlink()
            deleted += 1
        except OSError:
            continue
    return deleted


def remediate_disk_pressure(
    *,
    artifact_dirs: list,
    upload_state: dict,
    now: float,
    min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS,
    thresholds: DiskThresholds = DiskThresholds(),
    var_log_dir: pathlib.Path = pathlib.Path("/var/log"),
    root_dir: pathlib.Path = pathlib.Path("/"),
    runner: Runner = subprocess.run,
) -> list:
    """One disk-pressure remediation pass (section E.4). Returns the
    `System.Disk` list the reporter folds into the status payload."""
    results: list[dict] = []
    for adir in artifact_dirs:
        try:
            free_mb = _free_space_mb(adir)
        except OSError:
            continue
        status = disk_status(free_mb, critical_mb=thresholds.critical_mb, warning_mb=thresholds.warning_mb)
        deleted = 0
        if status == "critical":
            deleted = _delete_oldest_uploaded(
                adir,
                upload_state,
                now=now,
                min_age_seconds=min_age_seconds,
                target_free_mb=thresholds.critical_mb,
            )
            free_mb = _free_space_mb(adir)
            status = disk_status(free_mb, critical_mb=thresholds.critical_mb, warning_mb=thresholds.warning_mb)
        total_mb = shutil.disk_usage(adir).total / _ONE_MB
        used_mb = total_mb - free_mb
        results.append(
            {
                "Path": str(adir),
                "TotalMb": round(total_mb),
                "UsedMb": round(used_mb),
                "FreeMb": round(free_mb),
                "UsePct": round(used_mb / total_mb * 100) if total_mb else 0,
                "Status": status,
                "DeletedFiles": deleted,
            }
        )

    try:
        log_free_mb = _free_space_mb(var_log_dir)
    except OSError:
        log_free_mb = None
    if log_free_mb is not None and log_free_mb < thresholds.critical_mb:
        runner(
            ["journalctl", f"--vacuum-size={int(thresholds.critical_mb)}M"],
            check=False,
        )

    try:
        root_free_mb = _free_space_mb(root_dir)
        root_status = disk_status(
            root_free_mb, critical_mb=thresholds.critical_mb, warning_mb=thresholds.warning_mb
        )
        if root_status != "ok":
            log.warn(
                f"step7 uploader: {root_dir} free space {root_free_mb:.0f}MB is "
                f"{root_status} (report only -- never auto-delete on the root fs)"
            )
    except OSError:
        pass

    return results


# ---------------------------------------------------------------------------
# STEP-7 section E.1 -- what gets uploaded
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchSpec:
    local_dir: pathlib.Path
    remote_prefix: str
    min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS


def default_watch_specs(ctx: Any) -> list:
    """Watch directories and remote prefixes are configuration, not
    constants (section E.1) -- read from `installer.conf`, with the
    doc's suggested defaults as a fallback."""
    tracking_dir = ctx.conf.get("TRACKING_EXPORTS_DIR") or str(
        pathlib.Path(ctx.install_dir) / "tracking_exports"
    )
    tracking_prefix = ctx.conf.get("TRACKING_REMOTE_PREFIX") or "/vision/tracks-raw"
    specs = [WatchSpec(local_dir=pathlib.Path(tracking_dir), remote_prefix=tracking_prefix)]

    calib_dir = ctx.conf.get("CALIBRATION_EXPORT_DIR")
    if calib_dir:
        location_id = ctx.conf.get("LOCATION_ID", "")
        prefix = ctx.conf.get("CALIBRATION_REMOTE_PREFIX") or f"/vision/calibration/{location_id}"
        # MP4/clip-style artifacts should not be uploaded half-written;
        # a still-recording calibration export gets a non-zero minimum age.
        min_age = float(ctx.conf.get("CALIBRATION_MIN_AGE_SECONDS", 30))
        specs.append(
            WatchSpec(local_dir=pathlib.Path(calib_dir), remote_prefix=prefix, min_age_seconds=min_age)
        )
    return specs


# ---------------------------------------------------------------------------
# STEP-7 section F.1 -- command-by-config with an mtime edge-trigger
# ---------------------------------------------------------------------------


class MtimeEdgeTrigger:
    """The REQUIRED non-obvious primitive (section F.1): fires exactly once
    per fresh config write whose flag is true. Never writes to the config
    file itself (that stays the reporter's job); a missing file is not an
    error, just "nothing to do yet".
    """

    def __init__(
        self,
        config_path: pathlib.Path,
        *,
        flag_reader: Callable[[dict], bool],
        stat_fn: Callable[[pathlib.Path], float] = lambda p: p.stat().st_mtime,
    ) -> None:
        self._config_path = config_path
        self._flag_reader = flag_reader
        self._stat_fn = stat_fn
        self._last_seen_mtime: Optional[float] = None
        self._last_fired_mtime: Optional[float] = None

    def poll(self) -> bool:
        """One poll cycle. Returns True exactly on the cycle the trigger
        fires (edge, not level)."""
        try:
            current = self._stat_fn(self._config_path)
        except OSError:
            return False

        if current == self._last_seen_mtime:
            return False
        self._last_seen_mtime = current

        config = read_state_file(self._config_path, default={})
        begin = self._flag_reader(config)
        if begin and current != self._last_fired_mtime:
            self._last_fired_mtime = current
            return True
        return False


# ---------------------------------------------------------------------------
# systemd unit install (STEP-7 section H.1, mirrors STEP-6 section A.4)
# ---------------------------------------------------------------------------

_PERCENT_RE = re.compile(r"%(?!%)")


def _escape_specifiers(value: str) -> str:
    """Double a literal `%` for a systemd unit value (mirrors
    `systemd._escape_specifiers`, duplicated locally since that helper is
    module-private)."""
    return value.replace("%", "%%")


def _reporter_unit_names(ctx: Any) -> list:
    """The unit set for `Services` (section D.1): the fixed `mv3dt-agent`
    and `mosquitto` units, plus one `mv3dt-pipeline@<slug>` per Step 5
    registry entry -- enumerated fresh every cycle so an added/removed
    project is reflected without a restart. Step 5 is out of scope for this
    unit; `registry.json`'s exact shape is read defensively so a missing or
    differently-shaped registry never breaks a status cycle.
    """
    names = ["mv3dt-agent.service", "mosquitto.service"]
    registry = read_state_file(
        pathlib.Path(ctx.install_dir) / "projects" / "registry.json", default={}
    )
    projects = registry.get("projects") if isinstance(registry, dict) else None
    if isinstance(projects, list):
        for proj in projects:
            slug = proj.get("slug") if isinstance(proj, dict) else None
            if slug:
                names.append(f"mv3dt-pipeline@{slug}.service")
    return names


def _runner_from_ctx(ctx: Any) -> Runner:
    return lambda argv, **kwargs: ctx.run_root(*argv, **kwargs)


def _install_units(ctx: Any) -> tuple:
    """Renders + installs both units, mirroring STEP-6 section A.4's flow:
    write, `daemon-reload`, `enable --now`. Returns
    `(reporter_changed, uploader_changed)`."""
    installer_bin = pathlib.Path(ctx.install_dir) / "bin" / "mv3dt-installer"
    substitutions = {
        "INSTALL_DIR": _escape_specifiers(str(ctx.install_dir)),
        "INSTALLER_BIN": str(installer_bin),
        "USER": ctx.user.name,
    }

    reporter_content = systemd.render_unit(
        (systemd.TEMPLATE_DIR, "mv3dt-reporter.service.in"), substitutions
    )
    uploader_content = systemd.render_unit(
        (systemd.TEMPLATE_DIR, "mv3dt-uploader.service.in"), substitutions
    )

    runner = _runner_from_ctx(ctx)
    reporter_changed = systemd.install_unit(
        "mv3dt-reporter.service", reporter_content, runner=runner
    )
    uploader_changed = systemd.install_unit(
        "mv3dt-uploader.service", uploader_content, runner=runner
    )

    systemd.daemon_reload(runner=runner)
    systemd.enable_now("mv3dt-reporter.service", runner=runner)
    systemd.enable_now("mv3dt-uploader.service", runner=runner)

    return reporter_changed, uploader_changed


def _host_only(endpoint: Optional[str]) -> str:
    if not endpoint:
        return "(not configured)"
    parsed = urllib.parse.urlsplit(endpoint)
    return parsed.netloc or endpoint


def _gate_on(ctx: Any) -> bool:
    return ctx.webapp.gate_value == "on"


# ---------------------------------------------------------------------------
# The Step protocol implementation (STEP-7 section H.1)
# ---------------------------------------------------------------------------


class Step7WebappIntegration:
    id = STEP_ID
    title = "Web-app integration"
    order = 7

    def preflight(self, ctx: Any) -> StepResult:
        if not _gate_on(ctx):
            return StepResult(status=StepStatus.COMPLETE)

        creds = ctx.webapp.load_credentials()
        if creds is None or not creds.api_key or not creds.endpoint:
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message="web-app credentials are not configured",
                user_actions=[
                    UserAction(
                        text="Set API_KEY and ENDPOINT in secrets/webapp.env",
                        path=str(pathlib.Path(ctx.install_dir) / "secrets" / "webapp.env"),
                    )
                ],
            )

        registry_path = pathlib.Path(ctx.install_dir) / "projects" / "registry.json"
        if not registry_path.exists():
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message="Step 5's project registry is missing",
                user_actions=[
                    UserAction(
                        text="Complete Step 5 (per-project executables) before "
                        "enabling web-app integration"
                    )
                ],
            )

        client = UploadClient(creds.endpoint, creds.api_key)
        try:
            client.post_status({})
        except ConnectionError as exc:
            return StepResult(
                status=StepStatus.FAILED,
                message=f"cannot reach web-app endpoint {redact_url(creds.endpoint)}: {exc}",
            )
        except HttpError as exc:
            if exc.status in (401, 403):
                return StepResult(
                    status=StepStatus.USER_ACTION_REQUIRED,
                    message="the web-app API key was rejected",
                    user_actions=[UserAction(text="Verify API_KEY in secrets/webapp.env")],
                )
            if exc.status == 404:
                return StepResult(
                    status=StepStatus.USER_ACTION_REQUIRED,
                    message=(
                        "the registration route was not found -- the "
                        "/api/Workstation/* route names are a proposal, not "
                        "yet confirmed (STEP-7 section G)"
                    ),
                    user_actions=[
                        UserAction(text="Confirm the registration route with the backend owner")
                    ],
                )
            # Any other status still proves the endpoint is reachable and
            # the credential got as far as the server -- only reachability
            # is being proven here.
        except ValueError:
            pass  # non-JSON body still proves reachability

        return StepResult(status=StepStatus.COMPLETE)

    def run(self, ctx: Any) -> StepResult:
        if not _gate_on(ctx):
            return StepResult(status=StepStatus.COMPLETE)

        webapp_dir = pathlib.Path(ctx.install_dir) / "webapp"
        run_dir = pathlib.Path(ctx.install_dir) / "run"
        for directory in (webapp_dir, run_dir):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chown(directory, ctx.user.uid, ctx.user.gid)
            except OSError:
                pass  # best-effort, e.g. under a non-root test process

        reporter_changed, uploader_changed = _install_units(ctx)

        if reporter_changed:
            ctx.report_installed("mv3dt-reporter.service", STEP_VERSION)
        else:
            ctx.report_already_installed("mv3dt-reporter.service", STEP_VERSION)
        if uploader_changed:
            ctx.report_installed("mv3dt-uploader.service", STEP_VERSION)
        else:
            ctx.report_already_installed("mv3dt-uploader.service", STEP_VERSION)

        return StepResult(status=StepStatus.COMPLETE)

    def verify(self, ctx: Any) -> StepResult:
        if not _gate_on(ctx):
            return StepResult(status=StepStatus.COMPLETE)

        runner = _runner_from_ctx(ctx)
        for unit in ("mv3dt-reporter.service", "mv3dt-uploader.service"):
            if not (systemd.is_enabled(unit, runner=runner) and systemd.is_active(unit, runner=runner)):
                return StepResult(
                    status=StepStatus.USER_ACTION_REQUIRED,
                    message=f"{unit} is not enabled and active",
                    user_actions=[
                        UserAction(
                            text=f"Check the unit's status",
                            command=f"systemctl status {unit}",
                        )
                    ],
                )

        creds = ctx.webapp.load_credentials()
        if creds is None or not creds.api_key or not creds.endpoint:
            return StepResult(status=StepStatus.USER_ACTION_REQUIRED, message="web-app credentials missing")

        secrets_path = pathlib.Path(ctx.install_dir) / "secrets" / "webapp.env"
        try:
            mode = stat_mod.S_IMODE(secrets_path.stat().st_mode)
        except OSError:
            mode = None
        if mode != 0o600:
            return StepResult(
                status=StepStatus.USER_ACTION_REQUIRED,
                message=f"{secrets_path} must be chmod 600 (found {oct(mode) if mode is not None else 'missing'})",
            )

        webapp_dir = pathlib.Path(ctx.install_dir) / "webapp"
        run_dir = pathlib.Path(ctx.install_dir) / "run"
        config_path = webapp_dir / "config.json"

        client = UploadClient(creds.endpoint, creds.api_key)
        try:
            payload = build_status_payload(
                unit_names=_reporter_unit_names(ctx), run_dir=run_dir, runner=runner
            )
            new_config = client.post_status(payload)
        except (ConnectionError, HttpError, ValueError) as exc:
            if ctx.non_interactive:
                log.warn(
                    f"step7 verify: no reachable backend under --non-interactive "
                    f"({exc}); degrading to units-and-credentials-only checks"
                )
                return StepResult(status=StepStatus.COMPLETE)
            return StepResult(status=StepStatus.FAILED, message=f"status round-trip failed: {exc}")

        write_json_atomic(config_path, new_config)
        if not config_path.exists():
            return StepResult(status=StepStatus.FAILED, message="config.json was not written after round trip")

        for spec in default_watch_specs(ctx):
            if not spec.local_dir.is_dir():
                continue
            candidates = [p for p in sorted(spec.local_dir.iterdir()) if p.is_file()]
            if not candidates:
                continue
            sample = candidates[0]
            remote_path = spec.remote_prefix.rstrip("/") + "/" + sample.name
            parts = split_remote_path(remote_path)
            log.info(
                f"step7 verify: dry-run upload target "
                f"PathFromRoot={parts.path_from_root} FileName={parts.file_name} "
                f"Extension={parts.extension} (no bytes transferred)"
            )
            break

        return StepResult(status=StepStatus.COMPLETE)

    def report(self, ctx: Any) -> None:
        if not _gate_on(ctx):
            log.info("step7_webapp_integration: gate is off; no units installed")
            return

        creds = ctx.webapp.load_credentials()
        endpoint_host = _host_only(creds.endpoint if creds else None)
        log.info(f"web-app integration: endpoint={endpoint_host}")

        runner = _runner_from_ctx(ctx)
        for unit in ("mv3dt-reporter.service", "mv3dt-uploader.service"):
            active = "active" if systemd.is_active(unit, runner=runner) else "inactive"
            enabled = "enabled" if systemd.is_enabled(unit, runner=runner) else "disabled"
            log.info(f"  {unit}: {enabled}/{active}")

        for spec in default_watch_specs(ctx):
            log.info(f"  watch {spec.local_dir} -> {spec.remote_prefix}")

        log.info(
            f"  routes: {WEBAPP_HEARTBEAT_ROUTE} (proposed, unconfirmed), "
            f"{_REQUEST_UPLOAD_ROUTE}, {_CONFIRM_UPLOAD_ROUTE}"
        )
        log.info("  logs: journalctl -u mv3dt-reporter  |  journalctl -u mv3dt-uploader")


register(Step7WebappIntegration())


# ---------------------------------------------------------------------------
# `mv3dt-installer reporter` -- the mv3dt-reporter.service ExecStart
# ---------------------------------------------------------------------------


def _idle_forever(sleep: Callable[[float], None] = time.sleep) -> int:
    while True:
        sleep(60)


def run_reporter_loop(
    ctx: Any,
    *,
    once: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """The long-running reporter (section D). `once=True` runs a single
    cycle and returns -- used by tests and by `--once` for smoke checks."""
    creds = ctx.webapp.load_credentials()
    if creds is None or not creds.api_key or not creds.endpoint:
        log.error("step7 reporter: no web-app credentials configured; idling")
        return 1 if once else _idle_forever(sleep)

    client = UploadClient(creds.endpoint, creds.api_key)
    run_dir = pathlib.Path(ctx.install_dir) / "run"
    config_path = pathlib.Path(ctx.install_dir) / "webapp" / "config.json"
    runner = _runner_from_ctx(ctx)

    status_limiter = RateLimiter(interval_seconds=300, clock=clock)
    error_limiter = RateLimiter(interval_seconds=60, clock=clock)
    last_config_change: Optional[float] = None
    last_config_hash: Optional[str] = None
    new_config: dict = load_config(config_path)

    while True:
        cycle_start = clock()
        try:
            previous_config = load_config(config_path)
            unit_names = _reporter_unit_names(ctx)
            payload = build_status_payload(unit_names=unit_names, run_dir=run_dir, runner=runner)
            new_config = client.post_status(payload)
            apply_config(new_config, run_dir=run_dir)

            if new_config != previous_config:
                write_json_atomic(config_path, new_config)
                last_config_change = clock()

            new_hash = stable_hash(new_config)
            if new_hash != last_config_hash:
                log.info(f"step7 reporter: received config {new_hash}")
                last_config_hash = new_hash

            if status_limiter.allow():
                mem = payload["System"]["Memory"]
                pipelines = sum(
                    1 for k in payload["Services"] if k.startswith("mv3dt-pipeline")
                )
                log.info(
                    f"status OK (services={len(payload['Services'])} "
                    f"pipelines={pipelines} "
                    f"payload={len(json.dumps(payload))}B "
                    f"mem={mem['UsedMb']}/{mem['TotalMb']}MB)"
                )
        except Exception as exc:  # noqa: BLE001 -- this loop must never die
            key = (type(exc).__name__, str(exc)[:200])
            if error_limiter.allow(key):
                log.warn(f"step7 reporter: cycle failed: {exc}")
            new_config = load_config(config_path)

        if once:
            return 0

        interval = next_interval(
            configured_interval=resolve_heartbeat_interval(new_config),
            operation_active=False,  # no §F operation worker exists yet
            seconds_since_config_change=(
                clock() - last_config_change if last_config_change is not None else None
            ),
        )
        elapsed = clock() - cycle_start
        sleep(max(0.0, interval - elapsed))


def _handle_reporter(argv: list, ctx: Any) -> int:
    parser = argparse.ArgumentParser(prog="mv3dt-installer reporter", add_help=True)
    parser.add_argument(
        "--once", action="store_true", help="run a single status cycle and exit"
    )
    args = parser.parse_args(argv)
    return run_reporter_loop(ctx, once=args.once)


app_mod.register_subcommand("reporter", _handle_reporter)


# ---------------------------------------------------------------------------
# `mv3dt-installer uploader` -- the mv3dt-uploader.service ExecStart
# ---------------------------------------------------------------------------


def _scan_and_upload(client: UploadClient, spec: WatchSpec, state: dict, *, now: float) -> bool:
    """One scan pass over `spec.local_dir` (section E). Returns True if
    `state` changed and should be persisted."""
    changed = False
    if not spec.local_dir.is_dir():
        return changed

    for path in sorted(spec.local_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            file_stat = path.stat()
        except OSError:
            continue

        key = path.name
        raw = state.get(key)
        record = UploadRecord.from_dict(raw) if raw is not None else None
        decision, hydrated = decide_upload(
            record,
            current_size=file_stat.st_size,
            current_mtime=file_stat.st_mtime,
            now=now,
            min_age_seconds=spec.min_age_seconds,
        )

        if decision is UploadDecision.SKIP_LEGACY_HYDRATE and hydrated is not None:
            state[key] = hydrated.to_dict()
            changed = True
            continue
        if decision is not UploadDecision.UPLOAD:
            continue

        remote_path = spec.remote_prefix.rstrip("/") + "/" + path.name
        try:
            client.upload_file(path, remote_path)
        except Exception as exc:  # noqa: BLE001 -- one bad file must not stop the scan
            record_upload_failure(
                state, key, size=file_stat.st_size, mtime=file_stat.st_mtime, now=now
            )
            log.warn(f"step7 uploader: failed to upload {path}: {exc}")
        else:
            record_upload_success(
                state, key, size=file_stat.st_size, mtime=file_stat.st_mtime, now=now
            )
            log.info(f"step7 uploader: uploaded {path} -> {remote_path}")
        changed = True

    return changed


def run_uploader_loop(
    ctx: Any,
    *,
    once: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    scan_interval: float = DEFAULT_SCAN_INTERVAL_SECONDS,
) -> int:
    """The long-running uploader (section E). `once=True` runs a single
    scan-and-remediate pass and returns."""
    creds = ctx.webapp.load_credentials()
    if creds is None or not creds.api_key or not creds.endpoint:
        log.error("step7 uploader: no web-app credentials configured; idling")
        return 1 if once else _idle_forever(sleep)

    client = UploadClient(creds.endpoint, creds.api_key)
    run_dir = pathlib.Path(ctx.install_dir) / "run"
    state_path = pathlib.Path(ctx.install_dir) / "webapp" / "uploaded_state.json"
    thresholds = DiskThresholds()
    runner = _runner_from_ctx(ctx)

    while True:
        now = time.time()
        specs = default_watch_specs(ctx)
        state = read_state_file(state_path, default={})

        for spec in specs:
            if _scan_and_upload(client, spec, state, now=now):
                write_json_atomic(state_path, state)

        disk_result = remediate_disk_pressure(
            artifact_dirs=[spec.local_dir for spec in specs],
            upload_state=state,
            now=now,
            thresholds=thresholds,
            runner=runner,
        )
        write_json_atomic(run_dir / "disk_state.json", disk_result)

        if once:
            return 0
        sleep(scan_interval)


def _handle_uploader(argv: list, ctx: Any) -> int:
    parser = argparse.ArgumentParser(prog="mv3dt-installer uploader", add_help=True)
    parser.add_argument(
        "--once", action="store_true", help="run a single scan-and-upload pass and exit"
    )
    args = parser.parse_args(argv)
    return run_uploader_loop(ctx, once=args.once)


app_mod.register_subcommand("uploader", _handle_uploader)
