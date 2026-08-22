"""Tests for `mv3dt_installer.shellout` (doc 00 §4.2, §8.2).

Run with:
    cd installer && python3 -m pytest tests/test_shellout.py -v
"""

from __future__ import annotations

import pathlib
import stat
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mv3dt_installer import logs, shellout  # noqa: E402


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
