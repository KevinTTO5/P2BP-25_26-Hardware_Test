"""Tests for `mv3dt_installer.shellout` (doc 00 §4.2, §8.2; doc 08 §4.2).

Run with:
    cd installer && python3 -m pytest tests/test_shellout.py -v
"""

from __future__ import annotations

import io
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mv3dt_installer import logs, progress, shellout  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_transcript_state():
    """Every test starts with no transcript open, and leaves none dangling."""
    logs._transcript_path = None
    yield
    logs._transcript_path = None


@pytest.fixture(autouse=True)
def _force_no_colour(monkeypatch):
    """Non-tty stderr so plain, escape-free text is asserted on."""
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)


@pytest.fixture(autouse=True)
def _stage_under_tmp_path(tmp_path, monkeypatch):
    """Point `tempfile.mkdtemp` at `tmp_path` so every staging directory this
    module creates is inspectable and is cleaned up with the test."""
    staging_parent = tmp_path / "staging"
    staging_parent.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(staging_parent))
    return staging_parent


def _fixture_script(tmp_path, name, body):
    """Write a standalone fragment and point `asset_path` at it."""
    path = tmp_path / name
    path.write_text(body)
    path.chmod(0o644)  # deliberately not executable; shellout must fix this
    return path


@pytest.fixture
def assets_root(tmp_path, monkeypatch):
    """A fake bundled `assets/` tree, with `asset_path` rebound onto it.

    Layout mirrors what the real bundle will hold (U12 owns the content):

        assets/scripts/run.sh
        assets/scripts/lib/common.sh
        assets/scripts/data/note.txt
        assets/mosquitto/mv3dt.conf
    """
    root = tmp_path / "assets"
    scripts = root / "scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "data").mkdir(parents=True)
    (root / "mosquitto").mkdir(parents=True)

    (scripts / "run.sh").write_text(
        "#!/bin/sh\n"
        'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        '. "$SCRIPT_DIR/lib/common.sh"\n'
        "common_hello\n"
    )
    (scripts / "lib" / "common.sh").write_text(
        "#!/bin/sh\ncommon_hello() { echo hello-from-common; }\n"
    )
    (scripts / "data" / "note.txt").write_text("not a script\n")
    (root / "mosquitto" / "mv3dt.conf").write_text("listener 1883\n")

    # Everything arrives from git with ordinary non-executable modes; staging
    # is what has to make the fragments runnable.
    for entry in root.rglob("*"):
        if entry.is_file():
            entry.chmod(0o600)

    monkeypatch.setattr(shellout, "asset_path", lambda *parts: root.joinpath(*parts))
    return root


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------------------
# asset_path
# ---------------------------------------------------------------------------


def test_asset_path_dev_mode_resolves_relative_to_module_dir(monkeypatch):
    """No `sys._MEIPASS` set -> assets resolve under the module's own dir."""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    result = shellout.asset_path("scripts", "foo.sh")

    expected = (
        pathlib.Path(shellout.__file__).parent / "assets" / "scripts" / "foo.sh"
    )
    assert result == expected


def test_asset_path_frozen_mode_resolves_under_meipass(monkeypatch):
    """`sys._MEIPASS` set -> assets resolve under `<_MEIPASS>/assets/...`."""
    monkeypatch.setattr(sys, "_MEIPASS", "/fake/base", raising=False)

    result = shellout.asset_path("scripts", "foo.sh")

    assert result == pathlib.Path("/fake/base", "assets", "scripts", "foo.sh")


# ---------------------------------------------------------------------------
# stage_assets -- tree staging
# ---------------------------------------------------------------------------


def test_stage_assets_preserves_subdirectories(assets_root):
    """The whole directory lands, nested layout intact, at the same relative
    path it occupies inside the bundle."""
    stage_root = shellout.stage_assets("scripts")

    staged = stage_root / "scripts"
    assert staged.is_dir()
    assert (staged / "run.sh").is_file()
    assert (staged / "lib" / "common.sh").is_file()
    assert (staged / "data" / "note.txt").read_text() == "not a script\n"


def test_stage_assets_sets_executable_mode_on_shell_fragments(assets_root):
    """`*.sh` is staged 0755, everything else 0644, directories traversable."""
    staged = shellout.stage_assets("scripts") / "scripts"

    assert _mode(staged / "run.sh") == 0o755
    assert _mode(staged / "lib" / "common.sh") == 0o755
    assert _mode(staged / "data" / "note.txt") == 0o644
    assert _mode(staged / "lib") == 0o755


def test_stage_assets_with_no_parts_stages_whole_assets_tree(assets_root):
    """`stage_assets()` stages everything, so sibling subtrees stay reachable
    by the same relative paths they have inside the bundle."""
    stage_root = shellout.stage_assets()

    assert (stage_root / "scripts" / "run.sh").is_file()
    assert (stage_root / "mosquitto" / "mv3dt.conf").is_file()
    resolved = (stage_root / "scripts" / ".." / "mosquitto" / "mv3dt.conf").resolve()
    assert resolved.read_text() == "listener 1883\n"


def test_partial_stage_does_not_bring_sibling_subtrees(assets_root):
    """A partial stage copies only what was asked for, so a fragment reaching
    across to `../mosquitto/mv3dt.conf` gets ENOENT. This is the documented
    precondition behind the whole-tree case above, and the reason a fragment
    that needs a sibling subtree has to be run with `tree=()`."""
    stage_root = shellout.stage_assets("scripts")

    assert (stage_root / "scripts" / "run.sh").is_file()
    assert not (stage_root / "mosquitto").exists()
    assert sorted(p.name for p in stage_root.iterdir()) == ["scripts"]


def test_stage_assets_honours_prefix(assets_root):
    stage_root = shellout.stage_assets("scripts", prefix="custom-prefix-")

    assert stage_root.name.startswith("custom-prefix-")


def test_stage_assets_rejects_a_missing_tree(assets_root):
    with pytest.raises(NotADirectoryError):
        shellout.stage_assets("nope")


def test_stage_assets_rejects_a_file(assets_root):
    """A single file is `run_bundled_script`'s no-tree path, not a tree."""
    with pytest.raises(NotADirectoryError):
        shellout.stage_assets("scripts", "run.sh")


def test_staged_script_can_source_its_sibling_library(assets_root):
    """The whole point of tree staging: `source "$SCRIPT_DIR/lib/common.sh"`
    resolves, which a single-file copy could never do."""
    result = shellout.run_bundled_script(
        "scripts", "run.sh", tree=("scripts",), cleanup=False
    )

    assert result.returncode == 0, result.stderr
    assert "hello-from-common" in result.stdout


_WHERE_PROBE = '#!/bin/sh\necho "root=$MV3DT_ASSET_ROOT"\necho "cwd=$(pwd)"\n'


def test_partial_tree_run_exports_the_staged_assets_root(assets_root):
    """`MV3DT_ASSET_ROOT` is the staged stand-in for `assets/` itself, so a
    fragment addresses subtrees as `$MV3DT_ASSET_ROOT/<subtree>/...`. cwd is
    the staged copy of the tree that was asked for."""
    (assets_root / "scripts" / "where.sh").write_text(_WHERE_PROBE)

    result = shellout.run_bundled_script(
        "scripts", "where.sh", tree=("scripts",), cleanup=False
    )

    lines = dict(line.split("=", 1) for line in result.stdout.splitlines())
    asset_root = pathlib.Path(lines["root"])
    assert (asset_root / "scripts" / "lib" / "common.sh").is_file()
    assert pathlib.Path(lines["cwd"]).resolve() == (asset_root / "scripts").resolve()


def test_whole_tree_run_exports_the_same_asset_root_spelling(assets_root):
    """The variable means the same thing under `tree=()`; only what else sits
    beside `scripts/` differs. A bundled `common.sh` resolving a path against
    it therefore behaves identically in both modes."""
    (assets_root / "scripts" / "where.sh").write_text(_WHERE_PROBE)

    result = shellout.run_bundled_script(
        "scripts", "where.sh", tree=(), cleanup=False
    )

    lines = dict(line.split("=", 1) for line in result.stdout.splitlines())
    asset_root = pathlib.Path(lines["root"])
    assert (asset_root / "scripts" / "lib" / "common.sh").is_file()
    assert (asset_root / "mosquitto" / "mv3dt.conf").is_file()
    assert pathlib.Path(lines["cwd"]).resolve() == asset_root.resolve()


def test_caller_supplied_asset_root_never_wins(assets_root, capsys):
    """The staged path is a fact about this run, not a caller preference: a
    supplied value is ignored and the override is logged, since honouring it
    would point the fragment at a directory that does not exist."""
    (assets_root / "scripts" / "where.sh").write_text(_WHERE_PROBE)

    result = shellout.run_bundled_script(
        "scripts",
        "where.sh",
        tree=("scripts",),
        env={shellout.ASSET_ROOT_ENV: "/nowhere/stale"},
        cleanup=False,
    )

    lines = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert lines["root"] != "/nowhere/stale"
    assert (pathlib.Path(lines["root"]) / "scripts").is_dir()
    assert "ignoring supplied MV3DT_ASSET_ROOT" in capsys.readouterr().err


def test_tree_run_rejects_a_fragment_outside_the_tree(assets_root):
    with pytest.raises(ValueError):
        shellout.run_bundled_script("mosquitto", "mv3dt.conf", tree=("scripts",))


def test_tree_run_rejects_naming_the_tree_itself(assets_root):
    with pytest.raises(ValueError):
        shellout.run_bundled_script("scripts", tree=("scripts",))


# ---------------------------------------------------------------------------
# run_bundled_script -- staging, args, exit codes
# ---------------------------------------------------------------------------


def test_run_bundled_script_executes_copy_and_returns_output(tmp_path, monkeypatch):
    """The fragment is copied out (never run from _MEIPASS), chmod +x'd,
    executed, and its stdout/exit code come back on the CompletedProcess."""
    fixture = _fixture_script(
        tmp_path, "greet.sh", "#!/bin/sh\necho hello-from-fixture\nexit 0\n"
    )
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)

    result = shellout.run_bundled_script("scripts", "greet.sh")

    assert result.returncode == 0
    assert "hello-from-fixture" in result.stdout


def test_run_bundled_script_copies_out_before_executing(tmp_path, monkeypatch):
    """The script that actually runs is a copy in a fresh temp dir, not the
    original source path (guards the "never exec straight out of _MEIPASS"
    rule when the fragment would write next to itself)."""
    fixture = _fixture_script(tmp_path, "print_self.sh", '#!/bin/sh\necho "$0"\n')
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)

    result = shellout.run_bundled_script("scripts", "print_self.sh", cleanup=False)

    executed_path = result.stdout.strip()
    assert executed_path != str(fixture)
    assert pathlib.Path(executed_path).name == fixture.name


def test_run_bundled_script_forwards_args(tmp_path, monkeypatch):
    fixture = _fixture_script(tmp_path, "echo_args.sh", '#!/bin/sh\necho "$1" "$2"\n')
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)

    result = shellout.run_bundled_script("scripts", "echo_args.sh", args=["one", "two"])

    assert result.stdout.strip() == "one two"


@pytest.mark.parametrize("bad_exit", [1, 2, 42])
def test_run_bundled_script_returns_nonzero_exit_code(tmp_path, monkeypatch, bad_exit):
    fixture = _fixture_script(tmp_path, "fail.sh", f"#!/bin/sh\nexit {bad_exit}\n")
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)

    result = shellout.run_bundled_script("scripts", "fail.sh")

    assert result.returncode == bad_exit


# ---------------------------------------------------------------------------
# run_bundled_script -- environment merging
# ---------------------------------------------------------------------------


_ENV_PROBE = (
    "#!/bin/sh\n"
    'echo "MY_VAR=$MY_VAR"\n'
    'echo "MARKER=$SHELLOUT_PARENT_MARKER"\n'
    'echo "HAS_PATH=$([ -n "$PATH" ] && echo yes || echo no)"\n'
)


def test_run_bundled_script_merges_env_over_os_environ(tmp_path, monkeypatch):
    """Default `inherit_env=True`: the child sees the parent environment with
    `env` layered on top. Replacing it outright would strip `PATH` and break
    every `command -v` in the staged bash."""
    fixture = _fixture_script(tmp_path, "echo_env.sh", _ENV_PROBE)
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    monkeypatch.setenv("SHELLOUT_PARENT_MARKER", "from-parent")

    result = shellout.run_bundled_script(
        "scripts", "echo_env.sh", env={"MY_VAR": "shellout-env-value"}
    )

    assert "MY_VAR=shellout-env-value" in result.stdout
    assert "MARKER=from-parent" in result.stdout
    assert "HAS_PATH=yes" in result.stdout


def test_run_bundled_script_env_overrides_an_inherited_value(tmp_path, monkeypatch):
    fixture = _fixture_script(tmp_path, "echo_env.sh", _ENV_PROBE)
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    monkeypatch.setenv("SHELLOUT_PARENT_MARKER", "from-parent")

    result = shellout.run_bundled_script(
        "scripts", "echo_env.sh", env={"SHELLOUT_PARENT_MARKER": "from-caller"}
    )

    assert "MARKER=from-caller" in result.stdout


def test_run_bundled_script_inherit_env_false_replaces_the_environment(
    tmp_path, monkeypatch
):
    """`inherit_env=False` keeps the old replace semantics: the child sees the
    caller's `env` and nothing carried over from the parent."""
    fixture = _fixture_script(tmp_path, "echo_env.sh", _ENV_PROBE)
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    monkeypatch.setenv("SHELLOUT_PARENT_MARKER", "from-parent")

    result = shellout.run_bundled_script(
        "scripts",
        "echo_env.sh",
        env={"MY_VAR": "shellout-env-value"},
        inherit_env=False,
    )

    assert "MY_VAR=shellout-env-value" in result.stdout
    assert "MARKER=" in result.stdout
    assert "MARKER=from-parent" not in result.stdout


# ---------------------------------------------------------------------------
# run_bundled_script -- staging-directory cleanup
# ---------------------------------------------------------------------------


def test_run_bundled_script_removes_the_staging_dir_by_default(tmp_path, monkeypatch):
    fixture = _fixture_script(tmp_path, "print_self.sh", '#!/bin/sh\necho "$0"\n')
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)

    result = shellout.run_bundled_script("scripts", "print_self.sh")

    staged = pathlib.Path(result.stdout.strip())
    assert not staged.exists()
    assert not staged.parent.exists()


def test_run_bundled_script_keeps_the_staging_dir_when_cleanup_false(
    tmp_path, monkeypatch
):
    fixture = _fixture_script(tmp_path, "print_self.sh", '#!/bin/sh\necho "$0"\n')
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)

    result = shellout.run_bundled_script("scripts", "print_self.sh", cleanup=False)

    staged = pathlib.Path(result.stdout.strip())
    assert staged.is_file()


def test_tree_run_removes_the_whole_staged_tree_by_default(assets_root):
    (assets_root / "scripts" / "where.sh").write_text(
        '#!/bin/sh\necho "$MV3DT_ASSET_ROOT"\n'
    )

    result = shellout.run_bundled_script("scripts", "where.sh", tree=("scripts",))

    asset_root = pathlib.Path(result.stdout.strip())
    assert not (asset_root / "scripts").exists()
    assert not asset_root.exists()


def test_cleanup_still_happens_when_the_fragment_fails(tmp_path, monkeypatch):
    """The removal is in a `finally`, so a non-zero exit leaves nothing behind."""
    fixture = _fixture_script(tmp_path, "fail.sh", '#!/bin/sh\necho "$0"\nexit 3\n')
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)

    result = shellout.run_bundled_script("scripts", "fail.sh")

    assert result.returncode == 3
    assert not pathlib.Path(result.stdout.strip()).parent.exists()


# ---------------------------------------------------------------------------
# run_bundled_script -- transcript capture and redaction (§8.2)
# ---------------------------------------------------------------------------


def test_stdout_and_stderr_reach_the_transcript(tmp_path, monkeypatch):
    """§8.2 wants the fragment's own output in the transcript, not swallowed."""
    fixture = _fixture_script(
        tmp_path,
        "chatty.sh",
        "#!/bin/sh\necho out-line-one\necho err-line-one >&2\nexit 0\n",
    )
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    run_file = logs.open_transcript(tmp_path / "logs")

    shellout.run_bundled_script("scripts", "chatty.sh")

    transcript = run_file.read_text()
    assert "[info ] [chatty.sh] out-line-one" in transcript
    assert "[warn ] [chatty.sh] err-line-one" in transcript
    assert "chatty.sh exited 0" in transcript


def test_command_line_and_args_reach_the_transcript(tmp_path, monkeypatch):
    fixture = _fixture_script(tmp_path, "echo_args.sh", '#!/bin/sh\necho "$1"\n')
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    run_file = logs.open_transcript(tmp_path / "logs")

    shellout.run_bundled_script("scripts", "echo_args.sh", args=["--with-firewall"])

    transcript = run_file.read_text()
    assert "shellout: " in transcript
    assert "--with-firewall" in transcript


@pytest.mark.parametrize("secret_key", shellout._REDACT_KEYS)
def test_secret_env_values_never_reach_the_transcript(
    tmp_path, monkeypatch, secret_key
):
    """A secret handed to a fragment via `env` must appear in the transcript
    only as `<redacted>` -- in the env dump and in the fragment's own echo."""
    fixture = _fixture_script(
        tmp_path,
        "leak.sh",
        f'#!/bin/sh\necho "{secret_key}=${secret_key}"\necho "{secret_key}=${secret_key}" >&2\n',
    )
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    run_file = logs.open_transcript(tmp_path / "logs")

    result = shellout.run_bundled_script(
        "scripts", "leak.sh", env={secret_key: "super-secret-value"}
    )

    # The caller still gets the real output; only the transcript is scrubbed.
    assert "super-secret-value" in result.stdout

    transcript = run_file.read_text()
    assert f"{secret_key}={shellout._REDACTED}" in transcript
    assert "super-secret-value" not in transcript


def test_secret_in_an_argument_is_redacted_in_the_transcript(tmp_path, monkeypatch):
    fixture = _fixture_script(tmp_path, "noop.sh", "#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    run_file = logs.open_transcript(tmp_path / "logs")

    shellout.run_bundled_script(
        "scripts", "noop.sh", args=["--auth", "MQTT_PASSWORD=hunter2"]
    )

    transcript = run_file.read_text()
    assert f"MQTT_PASSWORD={shellout._REDACTED}" in transcript
    assert "hunter2" not in transcript


def test_non_secret_env_keys_are_logged_in_full(tmp_path, monkeypatch):
    """Redaction is targeted: ordinary configuration stays legible."""
    fixture = _fixture_script(tmp_path, "noop.sh", "#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    run_file = logs.open_transcript(tmp_path / "logs")

    shellout.run_bundled_script(
        "scripts", "noop.sh", env={"LOCATION_ID": "gainesville-01"}
    )

    assert "LOCATION_ID=gainesville-01" in run_file.read_text()


def test_secret_value_without_a_key_prefix_never_reaches_the_transcript(
    tmp_path, monkeypatch
):
    """Key-based redaction only fires on a literal `KEY=` occurrence, and real
    fragments leak secrets without one: `set -x` tracing, an Authorization
    header, a tool echoing an argument back in an error. The value itself is
    scrubbed, so none of those reach the transcript."""
    fixture = _fixture_script(
        tmp_path,
        "bearer.sh",
        "#!/bin/sh\n"
        'echo "curl -H \'Authorization: Bearer $NGC_API_KEY\' https://ngc"\n'
        'echo "+ login --token $NGC_API_KEY" >&2\n',
    )
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    run_file = logs.open_transcript(tmp_path / "logs")

    result = shellout.run_bundled_script(
        "scripts", "bearer.sh", env={"NGC_API_KEY": "nvapi-plaintext-key"}
    )

    assert "nvapi-plaintext-key" in result.stdout  # the caller still sees it
    transcript = run_file.read_text()
    assert "nvapi-plaintext-key" not in transcript
    assert transcript.count(shellout._REDACTED) >= 2
    assert "Authorization: Bearer" in transcript  # only the value is scrubbed


def test_secret_value_containing_whitespace_is_redacted_whole(tmp_path, monkeypatch):
    """A password may legitimately contain spaces. Matching the value up to the
    next space would have logged `MQTT_PASSWORD=hunter 2 three` as
    `MQTT_PASSWORD=<redacted> 2 three`, exposing the remainder."""
    secret = "hunter 2 three"
    fixture = _fixture_script(
        tmp_path,
        "spaces.sh",
        "#!/bin/sh\n"
        'echo "MQTT_PASSWORD=$MQTT_PASSWORD"\n'
        'echo "connecting as admin with $MQTT_PASSWORD"\n'
        'echo "MQTT_PASSWORD=$MQTT_PASSWORD" >&2\n',
    )
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    run_file = logs.open_transcript(tmp_path / "logs")

    shellout.run_bundled_script("scripts", "spaces.sh", env={"MQTT_PASSWORD": secret})

    transcript = run_file.read_text()
    assert secret not in transcript
    assert "2 three" not in transcript  # the tail must not survive either
    assert f"MQTT_PASSWORD={shellout._REDACTED}" in transcript


def test_secret_inherited_from_the_parent_environment_is_scrubbed(
    tmp_path, monkeypatch
):
    """Doc 00 §9.1 lets `NGC_API_KEY` arrive from the parent environment rather
    than an explicit `env`; it must be scrubbed on that path too."""
    fixture = _fixture_script(
        tmp_path, "inherited.sh", '#!/bin/sh\necho "token is $NGC_API_KEY"\n'
    )
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    monkeypatch.setenv("NGC_API_KEY", "inherited-plaintext-key")
    run_file = logs.open_transcript(tmp_path / "logs")

    shellout.run_bundled_script("scripts", "inherited.sh")

    transcript = run_file.read_text()
    assert "inherited-plaintext-key" not in transcript
    assert f"token is {shellout._REDACTED}" in transcript


def test_blank_secret_value_does_not_scrub_the_whole_transcript(tmp_path, monkeypatch):
    """An unset-but-present secret has an empty value; substituting that would
    match at every position and shred the transcript for no benefit."""
    fixture = _fixture_script(
        tmp_path, "quiet.sh", "#!/bin/sh\necho ordinary-output-line\n"
    )
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    run_file = logs.open_transcript(tmp_path / "logs")

    shellout.run_bundled_script("scripts", "quiet.sh", env={"CAM_PASSWORD": ""})

    assert "ordinary-output-line" in run_file.read_text()


def test_a_secret_argument_does_not_swallow_later_arguments(tmp_path, monkeypatch):
    """The command line is redacted per argument, so a `KEY=` argument blanks
    only itself and the rest of the invocation stays auditable."""
    fixture = _fixture_script(tmp_path, "noop.sh", "#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    run_file = logs.open_transcript(tmp_path / "logs")

    shellout.run_bundled_script(
        "scripts", "noop.sh", args=["MQTT_PASSWORD=hunter2", "--with-firewall"]
    )

    transcript = run_file.read_text()
    assert "hunter2" not in transcript
    assert "--with-firewall" in transcript


def test_redact_blanks_the_rest_of_the_line_after_a_secret_key(tmp_path):
    """The key-based rule covers a secret this process never held, so it cannot
    stop at the first space: everything after `KEY=` on that line goes."""
    assert (
        shellout._redact("MQTT_PASSWORD=hunter 2 three")
        == f"MQTT_PASSWORD={shellout._REDACTED}"
    )
    assert shellout._redact("first line\nCAM_PASSWORD=a b\nlast line") == (
        f"first line\nCAM_PASSWORD={shellout._REDACTED}\nlast line"
    )


def test_redact_leaves_a_longer_key_intact(tmp_path):
    """`API_KEY` is a suffix of `NGC_API_KEY`; the longer key must win so the
    log line still names which key was scrubbed."""
    assert (
        shellout._redact("NGC_API_KEY=abc123")
        == f"NGC_API_KEY={shellout._REDACTED}"
    )
    assert (
        shellout._redact("MY_OWN_API_KEY=abc123")
        == "MY_OWN_API_KEY=abc123"
    )


# ---------------------------------------------------------------------------
# run_streamed -- the tee runner (doc 08 §4.2)
# ---------------------------------------------------------------------------


class _RecordingSink:
    """Stands in for `progress.Progress`: the runner needs only `.line()`."""

    def __init__(self):
        self.lines = []

    def line(self, text):
        self.lines.append(text)


def _py(source):
    """A child program, run on this interpreter so the tests stay portable."""
    return [sys.executable, "-c", source]


def _sh(source):
    return ["/bin/sh", "-c", source]


def _transcript(tmp_path):
    return logs.open_transcript(tmp_path / "logs")


def test_run_streamed_capture_matches_subprocess_run(capsys):
    """The load-bearing promise: `.stdout`/`.stderr` are populated exactly as
    `subprocess.run(..., capture_output=True, text=True)` would populate them,
    trailing-newline handling included. 72 call sites depend on it."""
    program = _sh("echo one; echo two >&2; printf no-trailing-newline")

    streamed = shellout.run_streamed(program)
    reference = subprocess.run(program, capture_output=True, text=True)

    assert streamed.stdout == reference.stdout
    assert streamed.stderr == reference.stderr
    assert streamed.returncode == reference.returncode
    assert streamed.args == program
    capsys.readouterr()


def test_run_streamed_reaches_all_three_destinations(tmp_path, capsys):
    """One pass, three destinations: renderer, returned buffer, transcript."""
    run_file = _transcript(tmp_path)
    sink = _RecordingSink()

    result = shellout.run_streamed(
        _sh("echo visible-line; echo warned-line >&2"), renderer=sink
    )

    assert sorted(sink.lines) == ["visible-line", "warned-line"]
    assert result.stdout == "visible-line\n"
    assert result.stderr == "warned-line\n"
    transcript = run_file.read_text()
    assert "[info ] [sh] visible-line" in transcript
    assert "[warn ] [sh] warned-line" in transcript
    capsys.readouterr()


def test_run_streamed_does_not_deadlock_when_one_stream_is_quiet(capsys):
    """The two-pipe deadlock, reproduced deliberately: the child floods stderr
    well past a pipe buffer while stdout stays quiet until the very end. A
    reader that drained stdout first would block the child forever. The
    `timeout` turns a regression into a failed assertion rather than a hung
    test run."""
    result = shellout.run_streamed(
        _py(
            "import sys\n"
            "for i in range(4000):\n"
            "    sys.stderr.write('noise line %d of stderr padding\\n' % i)\n"
            "sys.stderr.flush()\n"
            "sys.stdout.write('quiet-stdout-line\\n')\n"
        ),
        timeout=60,
    )

    assert result.returncode == 0
    assert result.stdout == "quiet-stdout-line\n"
    assert len(result.stderr.splitlines()) == 4000
    assert len(result.stderr) > 64 * 1024  # past any plausible pipe buffer
    capsys.readouterr()


def test_run_streamed_keeps_both_streams_whole_and_in_order(capsys):
    """Both pipes are drained concurrently and both arrive complete, each in
    its own order. Interleaving *between* the two is deliberately not asserted:
    which reader is scheduled first is up to the OS, and a test that pinned it
    down would be testing the scheduler rather than the runner."""
    sink = _RecordingSink()

    result = shellout.run_streamed(
        _py(
            "import sys\n"
            "for i in range(20):\n"
            "    sys.stdout.write('out-%02d\\n' % i); sys.stdout.flush()\n"
            "    sys.stderr.write('err-%02d\\n' % i); sys.stderr.flush()\n"
        ),
        renderer=sink,
        timeout=60,
    )

    assert result.stdout.splitlines() == ["out-%02d" % i for i in range(20)]
    assert result.stderr.splitlines() == ["err-%02d" % i for i in range(20)]
    # Every line reached the renderer, and per-stream order survived the trip
    # through the reader threads.
    assert len(sink.lines) == 40
    assert [t for t in sink.lines if t.startswith("out-")] == result.stdout.splitlines()
    assert [t for t in sink.lines if t.startswith("err-")] == result.stderr.splitlines()
    capsys.readouterr()


def test_run_streamed_handles_a_large_volume_of_output(capsys):
    """An apt transaction is thousands of lines; nothing may be dropped or
    reordered on the way to either the renderer or the buffer."""
    sink = _RecordingSink()

    result = shellout.run_streamed(
        _py(
            "import sys\n"
            "for i in range(3000):\n"
            "    sys.stdout.write('Setting up package-%04d (1.2.3)\\n' % i)\n"
        ),
        renderer=sink,
        timeout=120,
    )

    lines = result.stdout.splitlines()
    assert len(lines) == 3000
    assert lines[0] == "Setting up package-0000 (1.2.3)"
    assert lines[-1] == "Setting up package-2999 (1.2.3)"
    assert sink.lines == lines
    capsys.readouterr()


@pytest.mark.parametrize("bad_exit", [1, 2, 42])
def test_run_streamed_returns_a_nonzero_exit_code_with_its_output(bad_exit, capsys):
    """A failing command still hands back everything it printed -- doc 08 §6.1
    needs the last lines of a failed command's output, and today they are
    discarded."""
    result = shellout.run_streamed(
        _sh(f"echo before-failing; echo why-it-failed >&2; exit {bad_exit}")
    )

    assert result.returncode == bad_exit
    assert result.stdout == "before-failing\n"
    assert result.stderr == "why-it-failed\n"
    capsys.readouterr()


def test_run_streamed_check_raises_like_subprocess_run(capsys):
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        shellout.run_streamed(_sh("echo partial-output; exit 7"), check=True)

    assert excinfo.value.returncode == 7
    assert excinfo.value.output == "partial-output\n"
    capsys.readouterr()


def test_run_streamed_check_passes_a_zero_exit_through(capsys):
    result = shellout.run_streamed(_sh("echo fine"), check=True)

    assert result.stdout == "fine\n"
    capsys.readouterr()


# ---------------------------------------------------------------------------
# run_streamed -- redaction across all three destinations (doc 08 §4.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret_key", shellout._REDACT_KEYS)
def test_streamed_secret_reaches_none_of_the_three_destinations(
    tmp_path, capsys, secret_key
):
    """The rule streaming must not break: a secret in a child's output must
    not appear on the terminal, in the transcript, or in the returned buffer.
    Output becoming visible live is not a reason for a secret to become
    visible with it."""
    secret = "super-secret-value"
    run_file = _transcript(tmp_path)
    sink = _RecordingSink()

    result = shellout.run_streamed(
        _sh(f'echo "{secret_key}=${secret_key}"; echo "bare ${secret_key}" >&2'),
        renderer=sink,
        env={secret_key: secret, "PATH": os.environ["PATH"]},
    )

    rendered = "\n".join(sink.lines)
    transcript = run_file.read_text()
    terminal = capsys.readouterr().err

    assert secret not in rendered
    assert secret not in transcript
    assert secret not in terminal
    assert secret not in result.stdout
    assert secret not in result.stderr
    # Scrubbed, not swallowed: the shape of the output survives.
    assert f"{secret_key}={shellout._REDACTED}" in result.stdout
    assert shellout._REDACTED in result.stderr


def test_streamed_secret_is_scrubbed_without_a_key_prefix(tmp_path, capsys):
    """Value scrubbing, live: `set -x` tracing and an echoed Authorization
    header carry the secret with no `KEY=` for the key rule to catch."""
    run_file = _transcript(tmp_path)
    sink = _RecordingSink()

    result = shellout.run_streamed(
        _sh('echo "curl -H \'Authorization: Bearer $NGC_API_KEY\'"'),
        renderer=sink,
        env={"NGC_API_KEY": "nvapi-plaintext-key", "PATH": os.environ["PATH"]},
    )

    assert "nvapi-plaintext-key" not in result.stdout
    assert "nvapi-plaintext-key" not in "\n".join(sink.lines)
    assert "nvapi-plaintext-key" not in run_file.read_text()
    assert "Authorization: Bearer" in result.stdout  # only the value goes
    capsys.readouterr()


def test_streamed_secret_inherited_from_the_parent_is_scrubbed(
    tmp_path, monkeypatch, capsys
):
    """`secrets=None` derives them from the child's environment, and a child
    with no explicit `env` inherits this process's -- which is how doc 00 §9.1
    lets `NGC_API_KEY` arrive. The caller does not have to remember."""
    monkeypatch.setenv("NGC_API_KEY", "inherited-plaintext-key")
    run_file = _transcript(tmp_path)
    sink = _RecordingSink()

    result = shellout.run_streamed(_sh('echo "token is $NGC_API_KEY"'), renderer=sink)

    assert "inherited-plaintext-key" not in result.stdout
    assert "inherited-plaintext-key" not in "\n".join(sink.lines)
    assert "inherited-plaintext-key" not in run_file.read_text()
    capsys.readouterr()


def test_streamed_redact_capture_false_keeps_the_buffer_verbatim(tmp_path, capsys):
    """`run_bundled_script`'s documented exception: its callers parse the
    fragment's real output, so only the buffer opts out. The terminal and the
    transcript stay scrubbed."""
    run_file = _transcript(tmp_path)
    sink = _RecordingSink()

    result = shellout.run_streamed(
        _sh('echo "MQTT_PASSWORD=$MQTT_PASSWORD"'),
        renderer=sink,
        redact_capture=False,
        env={"MQTT_PASSWORD": "hunter2", "PATH": os.environ["PATH"]},
    )

    assert "hunter2" in result.stdout
    assert "hunter2" not in "\n".join(sink.lines)
    assert "hunter2" not in run_file.read_text()
    capsys.readouterr()


def test_streamed_blank_secret_does_not_shred_the_output(tmp_path, capsys):
    """An unset-but-present secret has an empty value; substituting it would
    match at every position."""
    result = shellout.run_streamed(
        _sh("echo ordinary-output-line"),
        env={"CAM_PASSWORD": "", "PATH": os.environ["PATH"]},
    )

    assert result.stdout == "ordinary-output-line\n"
    capsys.readouterr()


# ---------------------------------------------------------------------------
# run_streamed -- off a tty, and control characters (doc 08 §7)
# ---------------------------------------------------------------------------


_ANSI_CHILD = _sh(
    'printf "\\033[32mgreen-line\\033[0m\\n"; printf "\\033]0;a title\\007plain\\n"'
)


def test_streamed_output_off_a_tty_is_plain_lines_with_no_escapes(tmp_path, capsys):
    """Section 7: off a tty there are no bars, no spinners and no escapes --
    just the lines. `progress.Progress` on a non-tty stream is exactly that
    path, so this exercises the real renderer rather than a double."""
    run_file = _transcript(tmp_path)
    out = io.StringIO()  # a StringIO reports isatty() False

    result = shellout.run_streamed(
        _ANSI_CHILD, renderer=progress.Progress(out=out, total_steps=7)
    )

    written = out.getvalue()
    assert "\x1b" not in written
    assert "green-line" in written
    assert "plain" in written
    assert "\x1b" not in run_file.read_text()
    # The buffer is what `subprocess.run` would have returned, escapes and all:
    # a parser is entitled to the child's own bytes.
    assert "\x1b[32m" in result.stdout
    capsys.readouterr()


def test_streamed_control_characters_never_reach_the_transcript(tmp_path, capsys):
    """Doc 00 §8.2's no-escape rule, which `logs.py` cannot enforce by itself
    because it never sees the raw line."""
    run_file = _transcript(tmp_path)

    shellout.run_streamed(_ANSI_CHILD)

    transcript = run_file.read_text()
    assert "\x1b" not in transcript
    assert "\x07" not in transcript
    assert "[info ] [sh] green-line" in transcript
    capsys.readouterr()


# ---------------------------------------------------------------------------
# run_streamed -- verbosity and suppression (doc 08 §4.3, §8)
# ---------------------------------------------------------------------------


def test_stream_false_suppresses_live_output_but_still_captures(tmp_path, capsys):
    """The escape hatch for a short probe whose output would be noise: nothing
    live, everything still captured and recorded."""
    run_file = _transcript(tmp_path)
    sink = _RecordingSink()

    result = shellout.run_streamed(
        _sh("echo probe-output"), renderer=sink, stream=False
    )

    assert sink.lines == []
    assert result.stdout == "probe-output\n"
    assert "probe-output" in run_file.read_text()
    capsys.readouterr()


def test_no_renderer_and_no_verbose_streams_nothing(capsys):
    """A caller holding no renderer has asked for nothing live; this is the
    behaviour every call site had before streaming existed."""
    out = io.StringIO()

    result = shellout.run_streamed(_sh("echo quiet-output"), out=out)

    assert out.getvalue() == ""
    assert result.stdout == "quiet-output\n"
    capsys.readouterr()


def test_verbose_with_no_renderer_streams_every_line_verbatim(capsys):
    """`--verbose` means everything, verbatim (doc 08 §8), even when the
    caller has no progress handle to hand over."""
    out = io.StringIO()

    shellout.run_streamed(
        _sh("echo first-line; echo second-line; echo third-line >&2"),
        out=out,
        verbose=True,
    )

    written = out.getvalue().splitlines()
    assert "first-line" in written
    assert "second-line" in written
    assert "third-line" in written
    capsys.readouterr()


def test_verbose_does_not_override_a_supplied_renderer(capsys):
    """The renderer owns the window and its own verbose mode; writing behind
    its back would corrupt the live region it is redrawing."""
    out = io.StringIO()
    sink = _RecordingSink()

    shellout.run_streamed(_sh("echo only-once"), renderer=sink, out=out, verbose=True)

    assert sink.lines == ["only-once"]
    assert out.getvalue() == ""
    capsys.readouterr()


# ---------------------------------------------------------------------------
# run_streamed -- subprocess.run compatibility surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("owned", ["stdout", "stderr"])
def test_run_streamed_rejects_caller_supplied_pipes(owned):
    """The runner owns both pipes; redirecting one would silently defeat the
    tee, so it is refused rather than half-honoured."""
    with pytest.raises(ValueError):
        shellout.run_streamed(
            _sh("true"), **{owned: subprocess.DEVNULL}
        )


def test_run_streamed_ignores_implied_capture_kwargs(capsys):
    """A call site already spelling `capture_output=True, text=True` needs no
    edit to adopt streaming -- that is what makes the §4.1 seam a one-method
    change."""
    result = shellout.run_streamed(
        _sh("echo implied"), capture_output=True, text=True, check=False
    )

    assert result.stdout == "implied\n"
    capsys.readouterr()


def test_run_streamed_forwards_cwd_and_env(tmp_path, capsys):
    (tmp_path / "marker.txt").write_text("here\n")

    result = shellout.run_streamed(
        _sh('ls marker.txt; echo "VAR=$MY_VAR"'),
        cwd=str(tmp_path),
        env={"MY_VAR": "forwarded", "PATH": os.environ["PATH"]},
    )

    assert "marker.txt" in result.stdout
    assert "VAR=forwarded" in result.stdout
    capsys.readouterr()


def test_run_streamed_delivers_input_to_the_child(capsys):
    """`input` is written from its own thread for the same reason both pipes
    are read from theirs: a child that talks before it listens would otherwise
    deadlock a parent that insisted on writing first."""
    result = shellout.run_streamed(_sh("cat"), input="fed-on-stdin\n")

    assert result.stdout == "fed-on-stdin\n"
    capsys.readouterr()


def test_run_streamed_times_out_and_keeps_what_it_saw(capsys):
    """`TimeoutExpired` carries the output collected so far, so a wedged
    command is diagnosable rather than merely reported as slow."""
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        shellout.run_streamed(
            _py(
                "import sys, time\n"
                "sys.stdout.write('started\\n'); sys.stdout.flush()\n"
                "time.sleep(30)\n"
            ),
            timeout=1.0,
        )

    assert "started" in (excinfo.value.output or "")
    capsys.readouterr()


def test_run_streamed_propagates_a_missing_executable(capsys):
    """`Context.run_root` turns `FileNotFoundError` into a 127 result itself;
    the runner must not swallow it and hide that decision."""
    with pytest.raises(FileNotFoundError):
        shellout.run_streamed(["/nonexistent/binary-that-is-not-there"])

    capsys.readouterr()


def test_run_streamed_labels_transcript_lines_with_the_command_name(tmp_path, capsys):
    run_file = _transcript(tmp_path)

    shellout.run_streamed(_sh("echo labelled"), label="custom-label")

    assert "[info ] [custom-label] labelled" in run_file.read_text()
    capsys.readouterr()


# ---------------------------------------------------------------------------
# run_bundled_script shares the same mechanism
# ---------------------------------------------------------------------------


def test_bundled_script_streams_through_the_tee_runner(tmp_path, monkeypatch, capsys):
    """One subprocess path, not two: a bundled fragment streams to a renderer
    with the same call the rest of the installer uses."""
    fixture = _fixture_script(
        tmp_path,
        "chatty.sh",
        "#!/bin/sh\necho staged-out\necho staged-err >&2\n",
    )
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    sink = _RecordingSink()

    result = shellout.run_bundled_script("scripts", "chatty.sh", renderer=sink)

    assert sorted(sink.lines) == ["staged-err", "staged-out"]
    assert result.stdout == "staged-out\n"
    assert result.stderr == "staged-err\n"
    capsys.readouterr()


def test_bundled_script_keeps_its_unredacted_buffer_while_streaming(
    tmp_path, monkeypatch, capsys
):
    """Its documented contract survives the move onto the shared runner: the
    caller parses the fragment's real output, the terminal and transcript do
    not see it."""
    fixture = _fixture_script(
        tmp_path, "leak.sh", '#!/bin/sh\necho "NGC_API_KEY=$NGC_API_KEY"\n'
    )
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    run_file = _transcript(tmp_path)
    sink = _RecordingSink()

    result = shellout.run_bundled_script(
        "scripts", "leak.sh", env={"NGC_API_KEY": "staged-secret"}, renderer=sink
    )

    assert "staged-secret" in result.stdout
    assert "staged-secret" not in "\n".join(sink.lines)
    assert "staged-secret" not in run_file.read_text()
    capsys.readouterr()


def test_bundled_script_streams_nothing_by_default(tmp_path, monkeypatch, capsys):
    """No `Context`, no progress handle: a fragment is quiet unless a caller
    asks for it, which keeps every existing call site behaving as it did."""
    fixture = _fixture_script(tmp_path, "quiet.sh", "#!/bin/sh\necho unseen\n")
    monkeypatch.setattr(shellout, "asset_path", lambda *parts: fixture)
    out = io.StringIO()

    result = shellout.run_bundled_script("scripts", "quiet.sh")

    assert out.getvalue() == ""
    assert result.stdout == "unseen\n"
    capsys.readouterr()
