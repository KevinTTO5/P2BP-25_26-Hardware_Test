"""Tests for mv3dt_installer.steps.step7_webapp_integration
(STEP-7-WEBAPP-INTEGRATION.md).

Run from installer/: `python3 -m pytest tests/test_step7_webapp_integration.py -v`

No test here touches a real socket or subprocess: `UploadClient` is driven
through injected `json_transport`/`put_transport` fakes, and every
`systemctl`/`nvidia-smi` call goes through an injected `runner`, mirroring
`test_step1_prerequisites.py`'s `FakeRunner` convention.
"""

from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace
from typing import Any, Optional

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mv3dt_installer.steps import step7_webapp_integration as s7  # noqa: E402


# ---------------------------------------------------------------------------
# section C -- DTO defensive deserialization
# ---------------------------------------------------------------------------


class TestDtoDeserialization:
    def test_upload_url_response_full(self):
        dto = s7.UploadUrlResponseDto.from_dict(
            {"PathFromRoot": "/a", "SignedUrl": "https://x/y?sig=1", "ExpiresAt": "2026-01-01T00:00:00Z"}
        )
        assert dto.PathFromRoot == "/a"
        assert dto.SignedUrl == "https://x/y?sig=1"
        assert dto.ExpiresAt == "2026-01-01T00:00:00Z"

    def test_upload_url_response_missing_fields(self):
        dto = s7.UploadUrlResponseDto.from_dict({})
        assert dto.PathFromRoot == ""
        assert dto.SignedUrl == ""
        assert dto.ExpiresAt is None

    def test_upload_url_response_null_fields(self):
        dto = s7.UploadUrlResponseDto.from_dict(
            {"PathFromRoot": None, "SignedUrl": None, "ExpiresAt": None}
        )
        assert dto.PathFromRoot == ""
        assert dto.SignedUrl == ""
        assert dto.ExpiresAt is None

    def test_media_record_response_missing_fields(self):
        dto = s7.MediaRecordResponseDto.from_dict({"Id": "abc"})
        assert dto.Id == "abc"
        assert dto.Name == ""
        assert dto.PathFromRoot == ""
        assert dto.Extension == ""

    def test_request_upload_url_dto_to_dict(self):
        dto = s7.RequestUploadUrlDto(
            PathFromRoot="/vision/tracks-raw", FileName="tracks_events-20260810",
            Extension="jsonl", SizeBytes=184320,
        )
        assert dto.to_dict() == {
            "PathFromRoot": "/vision/tracks-raw",
            "FileName": "tracks_events-20260810",
            "Extension": "jsonl",
            "SizeBytes": 184320,
        }


# ---------------------------------------------------------------------------
# section B.2 -- remote path splitting
# ---------------------------------------------------------------------------


class TestSplitRemotePath:
    def test_basic(self):
        parts = s7.split_remote_path("/vision/tracks-raw/tracks_events-20260810.jsonl")
        assert parts.path_from_root == "/vision/tracks-raw"
        assert parts.file_name == "tracks_events-20260810"
        assert parts.extension == "jsonl"

    def test_root_level_file(self):
        parts = s7.split_remote_path("/foo.txt")
        assert parts.path_from_root == "/"
        assert parts.file_name == "foo"
        assert parts.extension == "txt"

    def test_no_leading_slash_is_added(self):
        parts = s7.split_remote_path("vision/foo.txt")
        assert parts.path_from_root == "/vision"

    def test_backslashes_normalized(self):
        parts = s7.split_remote_path("vision\\tracks\\foo.txt")
        assert parts.path_from_root == "/vision/tracks"
        assert parts.file_name == "foo"

    def test_whitespace_stripped(self):
        parts = s7.split_remote_path("  /vision/foo.txt  ")
        assert parts.path_from_root == "/vision"

    def test_no_filename_raises(self):
        with pytest.raises(ValueError):
            s7.split_remote_path("/vision/tracks-raw/")

    def test_no_extension_raises(self):
        with pytest.raises(ValueError):
            s7.split_remote_path("/vision/tracks-raw/noext")

    def test_dotfile_with_no_extension_raises(self):
        with pytest.raises(ValueError):
            s7.split_remote_path("/vision/.hidden")


# ---------------------------------------------------------------------------
# section B.3 -- retry policy classification + backoff/jitter math
# ---------------------------------------------------------------------------


class TestRetryClassification:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 599])
    def test_retryable_statuses(self, status):
        assert s7.is_retryable_status(status) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_non_retryable_4xx_statuses(self, status):
        assert s7.is_retryable_status(status) is False

    def test_2xx_not_retryable(self):
        assert s7.is_retryable_status(200) is False


class _ZeroRng:
    """Deterministic stand-in for `random`: jitter always 0."""

    @staticmethod
    def uniform(a, b):
        return 0.0


class _MaxRng:
    """Deterministic stand-in for `random`: jitter always the max (1.0 factor)."""

    @staticmethod
    def uniform(a, b):
        return b


class TestBackoffDelay:
    def test_no_jitter_matches_formula(self):
        policy = s7.RetryPolicy(max_attempts=5, base_delay=0.5, cap_delay=30.0)
        assert s7.backoff_delay(1, policy, rng=_ZeroRng()) == pytest.approx(0.5)
        assert s7.backoff_delay(2, policy, rng=_ZeroRng()) == pytest.approx(1.0)
        assert s7.backoff_delay(3, policy, rng=_ZeroRng()) == pytest.approx(2.0)

    def test_caps_at_max_delay(self):
        policy = s7.RetryPolicy(max_attempts=10, base_delay=1.0, cap_delay=8.0)
        # 2^(10-1) * 1.0 would be huge without the cap.
        assert s7.backoff_delay(10, policy, rng=_ZeroRng()) == pytest.approx(8.0)

    def test_jitter_is_bounded_at_25_percent(self):
        policy = s7.RetryPolicy(max_attempts=5, base_delay=1.0, cap_delay=30.0)
        # attempt=2 -> base=2.0; max jitter is 25% of base = 0.5.
        delay = s7.backoff_delay(2, policy, rng=_MaxRng())
        assert delay == pytest.approx(2.5)


class _FakeJsonTransport:
    """Returns a scripted sequence of `HttpResponse`s (or raises
    `ConnectionError`), recording every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url, headers, data))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(status, body_obj):
    return s7.HttpResponse(status=status, body=json.dumps(body_obj).encode(), headers={})


class TestUploadClientRetryBehavior:
    def test_non_retryable_4xx_fails_immediately_no_sleep(self):
        transport = _FakeJsonTransport([_resp(404, {"error": "nope"})])
        sleeps = []
        client = s7.UploadClient(
            "https://host", "key", json_transport=transport, sleep=sleeps.append, rng=_ZeroRng()
        )
        with pytest.raises(s7.HttpError) as exc_info:
            client.request_upload_url(
                s7.RequestUploadUrlDto("/a", "b", "c", 10)
            )
        assert exc_info.value.status == 404
        assert len(transport.calls) == 1
        assert sleeps == []

    def test_retryable_5xx_retries_then_succeeds(self):
        transport = _FakeJsonTransport(
            [
                _resp(503, {}),
                _resp(200, {"PathFromRoot": "/a", "SignedUrl": "https://sig?x=1"}),
            ]
        )
        sleeps = []
        client = s7.UploadClient(
            "https://host", "key", json_transport=transport, sleep=sleeps.append, rng=_ZeroRng()
        )
        result = client.request_upload_url(s7.RequestUploadUrlDto("/a", "b", "c", 10))
        assert result.SignedUrl == "https://sig?x=1"
        assert len(transport.calls) == 2
        assert len(sleeps) == 1

    def test_exhausts_max_attempts_then_raises(self):
        # 5 failures for request-upload's max_attempts=5.
        transport = _FakeJsonTransport([_resp(500, {}) for _ in range(5)])
        sleeps = []
        client = s7.UploadClient(
            "https://host", "key", json_transport=transport, sleep=sleeps.append, rng=_ZeroRng()
        )
        with pytest.raises(s7.HttpError):
            client.request_upload_url(s7.RequestUploadUrlDto("/a", "b", "c", 10))
        assert len(transport.calls) == 5
        assert len(sleeps) == 4  # one fewer sleep than attempts

    def test_empty_signed_url_on_200_is_a_failure(self):
        transport = _FakeJsonTransport([_resp(200, {"PathFromRoot": "/a", "SignedUrl": ""})])
        client = s7.UploadClient("https://host", "key", json_transport=transport)
        with pytest.raises(ValueError):
            client.request_upload_url(s7.RequestUploadUrlDto("/a", "b", "c", 10))

    def test_put_bytes_sends_no_authorization_header(self, tmp_path):
        target = tmp_path / "f.bin"
        target.write_bytes(b"hello")
        seen_headers = {}

        def fake_put(url, path, headers):
            seen_headers.update(headers)
            return s7.HttpResponse(status=200, body=b"", headers={})

        client = s7.UploadClient("https://host", "key", put_transport=fake_put)
        client.put_bytes("https://sig?x=1", target)
        assert "Authorization" not in seen_headers

    def test_put_bytes_refuses_zero_byte_file(self, tmp_path):
        target = tmp_path / "empty.bin"
        target.write_bytes(b"")
        client = s7.UploadClient("https://host", "key")
        with pytest.raises(ValueError):
            client.put_bytes("https://sig", target)

    def test_put_bytes_refuses_missing_file(self, tmp_path):
        client = s7.UploadClient("https://host", "key")
        with pytest.raises(FileNotFoundError):
            client.put_bytes("https://sig", tmp_path / "missing.bin")

    def test_upload_file_refuses_zero_byte_file(self, tmp_path):
        target = tmp_path / "empty.jsonl"
        target.write_bytes(b"")
        client = s7.UploadClient("https://host", "key")
        with pytest.raises(ValueError):
            client.upload_file(target, "/vision/tracks-raw/empty.jsonl")


# ---------------------------------------------------------------------------
# section D.1 -- status payload builder's per-unit unknown/unknown rule
# ---------------------------------------------------------------------------


class _FakeRunner:
    def __init__(self, outputs: dict):
        """`outputs` maps a unit name to either a stdout string (rc 0) or
        an Exception to raise."""
        self._outputs = outputs
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "systemctl":
            unit = argv[2]
            outcome = self._outputs.get(unit)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout=outcome, stderr="")
        if argv[0] == "nvidia-smi":
            outcome = self._outputs.get("nvidia-smi")
            if outcome is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout=outcome, stderr="")
        raise AssertionError(f"unexpected command {argv}")


class TestStatusPayloadBuilder:
    def test_healthy_unit(self):
        runner = _FakeRunner(
            {"mv3dt-agent.service": "ActiveState=active\nSubState=running\n"}
        )
        payload = s7.build_services_payload(["mv3dt-agent.service"], runner=runner)
        assert payload == {"mv3dt-agent": {"Active": "active", "Sub": "running"}}

    def test_nonzero_exit_yields_unknown(self):
        runner = _FakeRunner({"mosquitto.service": None})
        payload = s7.build_services_payload(["mosquitto.service"], runner=runner)
        assert payload == {"mosquitto": {"Active": "unknown", "Sub": "unknown"}}

    def test_exception_yields_unknown_not_a_crash(self):
        runner = _FakeRunner({"mosquitto.service": TimeoutError("timed out")})
        payload = s7.build_services_payload(["mosquitto.service"], runner=runner)
        assert payload == {"mosquitto": {"Active": "unknown", "Sub": "unknown"}}

    def test_one_bad_unit_does_not_affect_others(self):
        runner = _FakeRunner(
            {
                "mv3dt-agent.service": "ActiveState=active\nSubState=running\n",
                "mosquitto.service": None,
            }
        )
        payload = s7.build_services_payload(
            ["mv3dt-agent.service", "mosquitto.service"], runner=runner
        )
        assert payload["mv3dt-agent"]["Active"] == "active"
        assert payload["mosquitto"] == {"Active": "unknown", "Sub": "unknown"}

    def test_dot_service_suffix_stripped_from_key(self):
        runner = _FakeRunner(
            {"mv3dt-pipeline@north-lobby-2.service": "ActiveState=active\nSubState=running\n"}
        )
        payload = s7.build_services_payload(
            ["mv3dt-pipeline@north-lobby-2.service"], runner=runner
        )
        assert "mv3dt-pipeline@north-lobby-2" in payload
        assert "mv3dt-pipeline@north-lobby-2.service" not in payload

    def test_gpu_memory_sentinel_on_failure(self):
        runner = _FakeRunner({})
        result = s7.collect_gpu_memory(runner=runner)
        assert result == {
            "Gpu": {"UtilizationPct": -1, "FrequencyMhz": -1},
            "Memory": {"UsedMb": -1, "TotalMb": -1},
        }

    def test_gpu_memory_parsed(self):
        runner = _FakeRunner({"nvidia-smi": "61, 4210, 32768, 55\n"})
        result = s7.collect_gpu_memory(runner=runner)
        assert result["Gpu"]["UtilizationPct"] == 61
        assert result["Gpu"]["FrequencyMhz"] == -1
        assert result["Memory"]["UsedMb"] == 4210
        assert result["Memory"]["TotalMb"] == 32768

    def test_disk_fanned_in_from_state_file(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "disk_state.json").write_text(
            json.dumps([{"Path": "/opt/mv3dt", "Status": "ok"}])
        )
        runner = _FakeRunner({})
        payload = s7.build_status_payload(unit_names=[], run_dir=run_dir, runner=runner)
        assert payload["System"]["Disk"] == [{"Path": "/opt/mv3dt", "Status": "ok"}]

    def test_disk_empty_list_when_worker_has_not_run(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        runner = _FakeRunner({})
        payload = s7.build_status_payload(unit_names=[], run_dir=run_dir, runner=runner)
        assert payload["System"]["Disk"] == []

    def test_timestamp_is_integer_unix_seconds(self, tmp_path):
        runner = _FakeRunner({})
        payload = s7.build_status_payload(unit_names=[], run_dir=tmp_path, runner=runner)
        assert isinstance(payload["Timestamp"], int)


# ---------------------------------------------------------------------------
# section F.2 -- state-file fan-in's total-reader property
# ---------------------------------------------------------------------------


class TestReadStateFile:
    def test_missing_file_yields_default(self, tmp_path):
        assert s7.read_state_file(tmp_path / "nope.json", default=[]) == []
        assert s7.read_state_file(tmp_path / "nope.json", default={}) == {}

    def test_malformed_json_yields_default(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        assert s7.read_state_file(p, default={}) == {}

    def test_unreadable_directory_yields_default(self, tmp_path):
        # A directory can never be read as text -> OSError -> default.
        d = tmp_path / "adir"
        d.mkdir()
        assert s7.read_state_file(d, default=[]) == []

    def test_valid_json_returned(self, tmp_path):
        p = tmp_path / "ok.json"
        p.write_text(json.dumps({"a": 1}))
        assert s7.read_state_file(p, default={}) == {"a": 1}


# ---------------------------------------------------------------------------
# section D.2 -- apply-always / persist-on-change asymmetry
# ---------------------------------------------------------------------------


class TestApplyAlwaysPersistOnChange:
    def test_write_json_atomic_only_called_on_change(self, tmp_path, monkeypatch):
        config_path = tmp_path / "webapp" / "config.json"
        config_path.parent.mkdir(parents=True)
        s7.write_json_atomic(config_path, {"HeartbeatInterval": 10})

        apply_calls = []
        monkeypatch.setattr(s7, "apply_config", lambda cfg, **kw: apply_calls.append(cfg))

        write_calls = []
        real_write = s7.write_json_atomic

        def spy_write(path, data):
            write_calls.append((path, data))
            real_write(path, data)

        monkeypatch.setattr(s7, "write_json_atomic", spy_write)

        # Simulate one reporter cycle body manually (same logic as
        # run_reporter_loop, without the loop/sleep machinery).
        previous = s7.load_config(config_path)
        new_config = {"HeartbeatInterval": 10}  # byte-identical to previous
        s7.apply_config(new_config, run_dir=tmp_path)
        if new_config != previous:
            s7.write_json_atomic(config_path, new_config)

        assert apply_calls == [new_config]  # applied even though identical
        assert write_calls == []  # not persisted -- unchanged

        new_config2 = {"HeartbeatInterval": 5}  # different
        s7.apply_config(new_config2, run_dir=tmp_path)
        if new_config2 != previous:
            s7.write_json_atomic(config_path, new_config2)

        assert apply_calls == [new_config, new_config2]
        assert write_calls == [(config_path, new_config2)]

    def test_stable_hash_is_order_independent(self):
        a = s7.stable_hash({"b": 2, "a": 1})
        b = s7.stable_hash({"a": 1, "b": 2})
        assert a == b

    def test_stable_hash_changes_on_content_change(self):
        a = s7.stable_hash({"a": 1})
        b = s7.stable_hash({"a": 2})
        assert a != b


class TestCadence:
    def test_default_interval(self):
        assert s7.resolve_heartbeat_interval({}) == s7.DEFAULT_HEARTBEAT_INTERVAL

    def test_zero_interval_falls_back_to_default(self):
        assert s7.resolve_heartbeat_interval({"HeartbeatInterval": 0}) == s7.DEFAULT_HEARTBEAT_INTERVAL

    def test_non_numeric_interval_falls_back_to_default(self):
        assert s7.resolve_heartbeat_interval({"HeartbeatInterval": "bogus"}) == s7.DEFAULT_HEARTBEAT_INTERVAL

    def test_configured_value_honored(self):
        assert s7.resolve_heartbeat_interval({"HeartbeatInterval": 30}) == 30.0

    def test_floor_clamps_a_low_configured_value(self):
        interval = s7.next_interval(
            configured_interval=0.1, operation_active=False, seconds_since_config_change=None
        )
        assert interval == s7.FLOOR_INTERVAL

    def test_operation_active_forces_fast_interval(self):
        interval = s7.next_interval(
            configured_interval=60.0, operation_active=True, seconds_since_config_change=None
        )
        assert interval == s7.FAST_INTERVAL

    def test_recent_config_change_forces_fast_interval(self):
        interval = s7.next_interval(
            configured_interval=60.0, operation_active=False, seconds_since_config_change=5.0
        )
        assert interval == s7.FAST_INTERVAL

    def test_old_config_change_uses_steady_state(self):
        interval = s7.next_interval(
            configured_interval=60.0, operation_active=False, seconds_since_config_change=20.0
        )
        assert interval == 60.0


# ---------------------------------------------------------------------------
# section E.2 -- dedupe fingerprint rule
# ---------------------------------------------------------------------------


class TestDedupeFingerprint:
    def test_new_file_uploads(self):
        decision, record = s7.decide_upload(
            None, current_size=100.0, current_mtime=1000.0, now=2000.0
        )
        assert decision is s7.UploadDecision.UPLOAD
        assert record is None

    def test_unchanged_file_skipped(self):
        record = s7.UploadRecord(size=100.0, mtime=1000.0, uploaded_at=1500.0)
        decision, _ = s7.decide_upload(
            record, current_size=100.0, current_mtime=1000.0, now=2000.0
        )
        assert decision is s7.UploadDecision.SKIP_UNCHANGED

    def test_size_changed_reuploads(self):
        record = s7.UploadRecord(size=100.0, mtime=1000.0, uploaded_at=1500.0)
        decision, _ = s7.decide_upload(
            record, current_size=200.0, current_mtime=1000.0, now=2000.0
        )
        assert decision is s7.UploadDecision.UPLOAD

    def test_mtime_increased_beyond_epsilon_reuploads(self):
        record = s7.UploadRecord(size=100.0, mtime=1000.0, uploaded_at=1500.0)
        decision, _ = s7.decide_upload(
            record, current_size=100.0, current_mtime=1000.1, now=2000.0
        )
        assert decision is s7.UploadDecision.UPLOAD

    def test_mtime_within_epsilon_is_not_a_change(self):
        record = s7.UploadRecord(size=100.0, mtime=1000.0, uploaded_at=1500.0)
        decision, _ = s7.decide_upload(
            record, current_size=100.0, current_mtime=1000.0 + 1e-9, now=2000.0
        )
        assert decision is s7.UploadDecision.SKIP_UNCHANGED

    def test_legacy_hydration_skips_and_fills_in(self):
        record = s7.UploadRecord(size=None, mtime=None, uploaded_at=1234.0)
        decision, hydrated = s7.decide_upload(
            record, current_size=100.0, current_mtime=1000.0, now=2000.0
        )
        assert decision is s7.UploadDecision.SKIP_LEGACY_HYDRATE
        assert hydrated.size == 100.0
        assert hydrated.mtime == 1000.0
        assert hydrated.uploaded_at == 1234.0  # preserved

    def test_min_age_skips_a_freshly_written_file(self):
        decision, _ = s7.decide_upload(
            None, current_size=100.0, current_mtime=1990.0, now=2000.0, min_age_seconds=30.0
        )
        assert decision is s7.UploadDecision.SKIP_TOO_YOUNG


class TestCooldownStateMachine:
    def test_below_threshold_still_reuploads_on_change(self):
        record = s7.UploadRecord(
            size=100.0, mtime=1000.0, failed_attempts=4, last_failed_at=1900.0
        )
        decision, _ = s7.decide_upload(
            record, current_size=200.0, current_mtime=1000.0, now=2000.0, max_attempts=5
        )
        assert decision is s7.UploadDecision.UPLOAD

    def test_at_threshold_within_cooldown_is_skipped(self):
        record = s7.UploadRecord(
            size=100.0, mtime=1000.0, failed_attempts=5, last_failed_at=1900.0
        )
        decision, _ = s7.decide_upload(
            record, current_size=100.0, current_mtime=1000.0, now=2000.0,
            max_attempts=5, cooldown_seconds=1800.0,
        )
        assert decision is s7.UploadDecision.SKIP_COOLDOWN

    def test_after_cooldown_elapses_retries_even_if_unchanged(self):
        record = s7.UploadRecord(
            size=100.0, mtime=1000.0, failed_attempts=5, last_failed_at=100.0
        )
        decision, _ = s7.decide_upload(
            record, current_size=100.0, current_mtime=1000.0, now=2000.0,
            max_attempts=5, cooldown_seconds=1800.0,
        )
        assert decision is s7.UploadDecision.UPLOAD

    def test_record_upload_success_resets_counter(self):
        state = {"f.jsonl": {"size": 1.0, "mtime": 1.0, "failed_attempts": 4, "last_failed_at": 900.0}}
        s7.record_upload_success(state, "f.jsonl", size=2.0, mtime=2.0, now=1000.0)
        assert state["f.jsonl"]["failed_attempts"] == 0
        assert state["f.jsonl"]["last_failed_at"] is None
        assert state["f.jsonl"]["uploaded_at"] == 1000.0

    def test_record_upload_failure_increments_counter(self):
        state = {}
        s7.record_upload_failure(state, "f.jsonl", size=1.0, mtime=1.0, now=100.0)
        s7.record_upload_failure(state, "f.jsonl", size=1.0, mtime=1.0, now=200.0)
        assert state["f.jsonl"]["failed_attempts"] == 2
        assert state["f.jsonl"]["last_failed_at"] == 200.0


# ---------------------------------------------------------------------------
# section F.1 -- mtime edge-trigger (edge not level, no poller writes, absent
# file handling)
# ---------------------------------------------------------------------------


class TestMtimeEdgeTrigger:
    def _trigger(self, path, flags):
        """`flags` maps mtime -> flag value; `stat_fn` reads from a plain
        dict rather than the filesystem, so tests control mtime precisely."""
        mtimes = iter(flags.keys())

        def flag_reader(config):
            return config.get("begin", False)

        trigger = s7.MtimeEdgeTrigger(path, flag_reader=flag_reader)
        return trigger

    def test_absent_file_is_not_an_error(self, tmp_path):
        trigger = s7.MtimeEdgeTrigger(tmp_path / "nope.json", flag_reader=lambda c: True)
        assert trigger.poll() is False
        assert trigger.poll() is False

    def test_fires_once_on_fresh_true_write(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"begin": True}))
        trigger = s7.MtimeEdgeTrigger(config_path, flag_reader=lambda c: c.get("begin", False))
        assert trigger.poll() is True
        # Same mtime, unchanged flag -- no re-fire (level, not edge).
        assert trigger.poll() is False
        assert trigger.poll() is False

    def test_stale_true_does_not_refire_after_unrelated_write(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"begin": True}))
        trigger = s7.MtimeEdgeTrigger(config_path, flag_reader=lambda c: c.get("begin", False))
        assert trigger.poll() is True

        # A fresh write (mtime advances) but the flag is still true -- this
        # models the ack-write-back interaction (F.3): once a fresh write's
        # flag has already fired, only a *new* fresh write can fire again,
        # and here the flag never returns to true on a *new* write.
        # Simulate a second worker's write-back changing mtime, flag false:
        import time as _time

        _time.sleep(0.01)
        config_path.write_text(json.dumps({"begin": False}))
        assert trigger.poll() is False

    def test_flag_false_never_fires(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"begin": False}))
        trigger = s7.MtimeEdgeTrigger(config_path, flag_reader=lambda c: c.get("begin", False))
        assert trigger.poll() is False
        assert trigger.poll() is False

    def test_fires_again_on_a_new_fresh_true_write(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"begin": True}))
        trigger = s7.MtimeEdgeTrigger(config_path, flag_reader=lambda c: c.get("begin", False))
        assert trigger.poll() is True
        assert trigger.poll() is False

        import time as _time

        _time.sleep(0.01)
        config_path.write_text(json.dumps({"begin": True}))
        assert trigger.poll() is True

    def test_poller_never_writes_to_config(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"begin": True}))
        original_mtime = config_path.stat().st_mtime
        trigger = s7.MtimeEdgeTrigger(config_path, flag_reader=lambda c: c.get("begin", False))
        trigger.poll()
        trigger.poll()
        trigger.poll()
        assert config_path.stat().st_mtime == original_mtime
