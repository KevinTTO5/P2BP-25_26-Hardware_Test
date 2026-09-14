"""Integration tests for the adapter-to-command wiring seam."""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

from mv3dt_installer import app, progress_exec
from mv3dt_installer.steps import step2_deepstream_sdk as step2


class RecordingProgress:
    live = False

    def __init__(self):
        self.tasks = []
        self.percentages = []
        self.byte_counts = []
        self.lines = []
        self.ticks = 0

    def task(self, name):
        self.tasks.append(name)

    def percent(self, value, note=None):
        self.percentages.append((value, note))

    def bytes(self, done, total):
        self.byte_counts.append((done, total))

    def line(self, text):
        self.lines.append(text)

    def tick(self):
        self.ticks += 1


class ObservedContext:
    def __init__(self):
        self.progress = RecordingProgress()

    def run_observed(self, run, observe):
        return app.Context.run_observed(self, run, observe)


def test_apt_adds_status_fd_and_follows_the_same_transaction():
    ctx = ObservedContext()
    call = {}

    def runner(*args, **kwargs):
        call["args"] = args
        call["kwargs"] = kwargs
        status_fd = kwargs["pass_fds"][0]
        os.write(status_fd, b"pmstatus:curl:64:Setting up curl\n")
        return subprocess.CompletedProcess(args, 0, "apt stdout", "")

    result = progress_exec.apt(
        ctx,
        "install",
        "-y",
        "curl",
        runner=runner,
        capture_output=True,
        text=True,
    )

    status_fd = call["kwargs"]["pass_fds"][0]
    assert call["args"][:4] == ("apt-get", "install", "-y", "curl")
    assert call["args"][-2:] == ("-o", f"APT::Status-Fd={status_fd}")
    assert ctx.progress.percentages[-1] == (64, "curl")
    assert "Setting up curl" in ctx.progress.lines
    assert result.returncode == 0
    assert result.stdout == "apt stdout"


def test_apt_keeps_legacy_fake_context_call_shape():
    calls = []
    ctx = SimpleNamespace(
        run_root=lambda *args, **kwargs: calls.append((args, kwargs)) or "result",
        progress=RecordingProgress(),
    )

    result = progress_exec.apt(ctx, "update", capture_output=True, text=True)

    assert result == "result"
    assert calls == [
        (("apt-get", "update"), {"capture_output": True, "text": True})
    ]


def test_download_uses_content_length_and_observes_destination(
    tmp_path, monkeypatch
):
    ctx = ObservedContext()
    destination = tmp_path / "artifact.part"
    probes = []
    monkeypatch.setattr(
        progress_exec.progress,
        "content_length",
        lambda url: probes.append(url) or 100,
    )

    def run():
        destination.write_bytes(b"x" * 40)
        return subprocess.CompletedProcess(["curl"], 0, "", "")

    result = progress_exec.download(
        ctx,
        destination,
        "https://example.invalid/artifact",
        run,
        task="SDK artifact",
    )

    assert probes == ["https://example.invalid/artifact"]
    assert ctx.progress.tasks == ["SDK artifact"]
    assert ctx.progress.byte_counts[-1] == (40, 100)
    assert result.returncode == 0


def test_download_without_length_uses_spinner_not_bar(tmp_path, monkeypatch):
    ctx = ObservedContext()
    destination = tmp_path / "artifact.part"
    monkeypatch.setattr(progress_exec.progress, "content_length", lambda url: None)

    def run():
        destination.write_bytes(b"payload")
        return subprocess.CompletedProcess(["curl"], 0, "", "")

    progress_exec.download(
        ctx, destination, "https://example.invalid/artifact", run
    )

    assert ctx.progress.byte_counts == [(0, None)]
    assert ctx.progress.ticks >= 1


def test_download_keeps_legacy_fake_context_off_the_network(tmp_path, monkeypatch):
    ctx = SimpleNamespace(progress=RecordingProgress())
    monkeypatch.setattr(
        progress_exec.progress,
        "content_length",
        lambda url: (_ for _ in ()).throw(AssertionError("unexpected probe")),
    )

    result = progress_exec.download(
        ctx,
        tmp_path / "artifact",
        "https://example.invalid/artifact",
        lambda: "result",
    )

    assert result == "result"


def test_step2_artifact_download_keeps_the_invoking_user_seam(
    tmp_path, monkeypatch
):
    artifact_dir = tmp_path / "downloads"
    calls = []
    observed = []

    def run_as_user(*args, **kwargs):
        calls.append((args, kwargs))
        (artifact_dir / args[args.index("-o") + 1]).write_bytes(b"artifact")
        return subprocess.CompletedProcess(args, 0, "", "")

    ctx = SimpleNamespace(
        user=SimpleNamespace(uid=os.getuid(), gid=os.getgid()),
        run_as_user=run_as_user,
    )

    def fake_download(ctx_arg, path, url, run, *, task=None):
        observed.append((ctx_arg, path, url, task))
        return run()

    monkeypatch.setattr(progress_exec, "download", fake_download)

    ok, source, early = step2._ensure_artifact(
        ctx,
        artifact_dir,
        step2.DEB_ARTIFACT,
        f"{step2.GITHUB_RELEASE_BASE}/{step2.DEB_ARTIFACT}",
    )

    assert (ok, source, early) == (True, "downloaded", None)
    assert calls[0][0][0] == "curl"
    assert calls[0][1]["cwd"] == str(artifact_dir)
    assert calls[0][1]["stream"] is True
    assert observed[0][1] == artifact_dir / step2.DEB_ARTIFACT
    assert observed[0][3] == step2.DEB_ARTIFACT
