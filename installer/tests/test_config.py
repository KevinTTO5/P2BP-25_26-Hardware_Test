"""Tests for `mv3dt_installer.config` (doc 00 §11, §3.4).

Run from installer/: `python3 -m pytest tests/test_config.py -v`

Never touches the real `/opt/mv3dt` install dir or the real
`/var/lib/mv3dt-installer/state.json` path -- every test constructs a
`StateMachine` against a `tmp_path`-derived file, and passes a
`tmp_path`-derived `default_install_dir` so even the "installer.conf
default" precedence tier (§11.2) never consults the real `/opt/mv3dt`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mv3dt_installer import config  # noqa: E402
from mv3dt_installer.state import StateMachine  # noqa: E402


def _state(tmp_path: Path) -> StateMachine:
    """A StateMachine pointed at an isolated state.json under tmp_path."""
    return StateMachine(path=tmp_path / "var" / "state.json")


def _refuse_prompt(_msg: str) -> str:
    """Stand-in for input() that fails the test if load() ever prompts."""
    raise AssertionError("load() prompted interactively when it should not have")


# ---------------------------------------------------------------------------
# Precedence order
# ---------------------------------------------------------------------------


def test_install_dir_override_wins_over_state_and_default(tmp_path):
    sm = _state(tmp_path)
    # Pre-seed state.json with a different install_dir than the override.
    state_dir = tmp_path / "from-state"
    config.load(str(state_dir), sm, non_interactive=True)

    override_dir = tmp_path / "from-override"
    cfg = config.load(str(override_dir), sm, non_interactive=True)

    assert cfg.install_dir == override_dir
    assert override_dir.is_dir()


def test_state_json_wins_over_installer_conf_default_tier(tmp_path):
    # Seed a "previous install" installer.conf at the fallback default
    # location recording a *different* dir than what state.json will hold.
    fallback_default = tmp_path / "fallback-default"
    fallback_default.mkdir()
    (fallback_default / config.CONF_FILENAME).write_text(
        "INSTALL_DIR=" + str(tmp_path / "from-conf-fallback") + "\n",
        encoding="utf-8",
    )

    sm = _state(tmp_path)
    state_dir = tmp_path / "from-state"
    # Establish state.json's install_dir = state_dir via an explicit
    # override (a separate install dir from fallback_default).
    config.load(str(state_dir), sm, non_interactive=True)

    # A subsequent no-override load must read state.json (now populated)
    # rather than falling through to the installer.conf default tier.
    cfg = config.load(
        None, sm, non_interactive=True, default_install_dir=fallback_default
    )

    assert cfg.install_dir == state_dir


def test_installer_conf_default_tier_used_when_state_missing(tmp_path):
    """When state.json has never been written, a previously-chosen dir
    recorded in installer.conf at the default location wins over the
    hardcoded default (doc 00 §11.2 edge case)."""
    fallback_default = tmp_path / "fallback-default"
    fallback_default.mkdir()
    recorded_dir = tmp_path / "previously-chosen"
    (fallback_default / config.CONF_FILENAME).write_text(
        f"INSTALL_DIR={recorded_dir}\n", encoding="utf-8"
    )

    sm = _state(tmp_path)  # state.json does not exist yet
    cfg = config.load(
        None, sm, non_interactive=True, default_install_dir=fallback_default
    )

    assert cfg.install_dir == recorded_dir


def test_default_used_when_nothing_recorded_anywhere(tmp_path):
    sm = _state(tmp_path)
    default_dir = tmp_path / "opt-mv3dt-stand-in"

    cfg = config.load(
        None, sm, non_interactive=True, default_install_dir=default_dir
    )

    assert cfg.install_dir == default_dir


# ---------------------------------------------------------------------------
# First run / re-run behaviour
# ---------------------------------------------------------------------------


def test_first_run_creates_dir_writes_conf_and_updates_state(tmp_path):
    sm = _state(tmp_path)
    install_dir = tmp_path / "install"

    cfg = config.load(str(install_dir), sm, non_interactive=True)

    assert install_dir.is_dir()
    conf_path = install_dir / config.CONF_FILENAME
    assert conf_path.is_file()
    assert cfg.conf_path == conf_path

    loaded_state = sm.load()
    assert loaded_state.install_dir == str(install_dir)


def test_rerun_reads_back_existing_conf_without_prompting(tmp_path):
    sm = _state(tmp_path)
    install_dir = tmp_path / "install"

    first = config.load(str(install_dir), sm, non_interactive=True)
    # Simulate an operator hand-editing installer.conf with a custom var
    # that config.py does not own -- it must survive the re-run untouched.
    conf_text = (install_dir / config.CONF_FILENAME).read_text(encoding="utf-8")
    conf_text += "SOME_OTHER_MODULE_VAR=custom-value\n"
    (install_dir / config.CONF_FILENAME).write_text(conf_text, encoding="utf-8")

    # Re-run with no override: must resolve via state.json (already holds
    # install_dir from the first run) and must NOT prompt.
    second = config.load(None, sm, non_interactive=False, prompt=_refuse_prompt)

    assert second.install_dir == first.install_dir == install_dir
    assert second.values["SOME_OTHER_MODULE_VAR"] == "custom-value"


def test_non_interactive_uses_default_silently(tmp_path):
    sm = _state(tmp_path)
    default_dir = tmp_path / "silent-default"

    cfg = config.load(
        None,
        sm,
        non_interactive=True,
        default_install_dir=default_dir,
        prompt=_refuse_prompt,
    )

    assert cfg.install_dir == default_dir


def test_interactive_prompts_when_nothing_else_resolved(tmp_path):
    sm = _state(tmp_path)
    default_dir = tmp_path / "prefilled-default"
    chosen_dir = tmp_path / "operator-chosen"
    prompts_seen = []

    def _fake_prompt(msg: str) -> str:
        prompts_seen.append(msg)
        assert str(default_dir) in msg  # prefilled with the default
        return str(chosen_dir)

    cfg = config.load(
        None,
        sm,
        non_interactive=False,
        default_install_dir=default_dir,
        prompt=_fake_prompt,
    )

    assert prompts_seen  # prompt was actually invoked
    assert cfg.install_dir == chosen_dir
    assert not default_dir.exists()  # never created; the chosen dir was used


def test_interactive_empty_answer_accepts_prefilled_default(tmp_path):
    sm = _state(tmp_path)
    default_dir = tmp_path / "prefilled-default"

    cfg = config.load(
        None,
        sm,
        non_interactive=False,
        default_install_dir=default_dir,
        prompt=lambda _msg: "",
    )

    assert cfg.install_dir == default_dir


# ---------------------------------------------------------------------------
# §3.4 gate keys
# ---------------------------------------------------------------------------


def test_gate_keys_default_to_off_when_absent(tmp_path):
    sm = _state(tmp_path)
    install_dir = tmp_path / "install"

    cfg = config.load(str(install_dir), sm, non_interactive=True)

    assert cfg.remote_supervision == "off"
    assert cfg.webapp_integration == "off"
    assert cfg.values[config.GATE_REMOTE_SUPERVISION] == "off"
    assert cfg.values[config.GATE_WEBAPP_INTEGRATION] == "off"

    conf_text = (install_dir / config.CONF_FILENAME).read_text(encoding="utf-8")
    assert "MV3DT_REMOTE_SUPERVISION=off" in conf_text
    assert "MV3DT_WEBAPP_INTEGRATION=off" in conf_text


def test_gate_keys_round_trip_when_present(tmp_path):
    sm = _state(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / config.CONF_FILENAME).write_text(
        "MV3DT_REMOTE_SUPERVISION=local\n"
        "MV3DT_WEBAPP_INTEGRATION=on\n",
        encoding="utf-8",
    )

    cfg = config.load(str(install_dir), sm, non_interactive=True)

    assert cfg.remote_supervision == "local"
    assert cfg.webapp_integration == "on"

    # And it round-trips again on a second load.
    cfg2 = config.load(str(install_dir), sm, non_interactive=True)
    assert cfg2.remote_supervision == "local"
    assert cfg2.webapp_integration == "on"


def test_gate_keys_seeded_from_env_var_on_first_write(tmp_path, monkeypatch):
    """Regression test (code review finding): a gate's environment variable
    must reach the persisted `installer.conf` on first write.

    `bootstrap.sh` §5.1 step 5 decides whether to capture the web-app
    credential based on `MV3DT_WEBAPP_INTEGRATION` in its own environment,
    then `exec sudo -E`'s into the built binary -- so that env var *is*
    present in the launched process's environment. Before this fix,
    `config.load()` never consulted it: `installer.conf` was always created
    with both gates hardcoded to "off", so Step 7 would stay auto-skipped
    by the dispatch loop regardless of what the operator did at bootstrap
    time, even after successfully capturing and storing credentials.
    """
    monkeypatch.setenv("MV3DT_WEBAPP_INTEGRATION", "on")
    monkeypatch.setenv("MV3DT_REMOTE_SUPERVISION", "local")

    sm = _state(tmp_path)
    install_dir = tmp_path / "install"

    cfg = config.load(str(install_dir), sm, non_interactive=True)

    assert cfg.webapp_integration == "on"
    assert cfg.remote_supervision == "local"

    conf_text = (install_dir / config.CONF_FILENAME).read_text(encoding="utf-8")
    assert "MV3DT_WEBAPP_INTEGRATION=on" in conf_text
    assert "MV3DT_REMOTE_SUPERVISION=local" in conf_text


def test_gate_keys_env_var_does_not_override_already_persisted_value(
    tmp_path, monkeypatch
):
    """A gate's env var only seeds the *first* write, mirroring the
    "capture once, then just read back" discipline already used for the
    NGC/webapp credentials (doc 00 §10, §14) -- once persisted, a later
    env var must not silently flip an already-chosen gate value out from
    under the operator."""
    sm = _state(tmp_path)
    install_dir = tmp_path / "install"

    cfg1 = config.load(str(install_dir), sm, non_interactive=True)
    assert cfg1.webapp_integration == "off"

    monkeypatch.setenv("MV3DT_WEBAPP_INTEGRATION", "on")
    cfg2 = config.load(str(install_dir), sm, non_interactive=True)
    assert cfg2.webapp_integration == "off"


# ---------------------------------------------------------------------------
# installer.conf shape
# ---------------------------------------------------------------------------


def test_conf_file_is_plain_key_value_no_quoting(tmp_path):
    sm = _state(tmp_path)
    install_dir = tmp_path / "install"

    config.load(str(install_dir), sm, non_interactive=True)

    conf_text = (install_dir / config.CONF_FILENAME).read_text(encoding="utf-8")
    for line in conf_text.splitlines():
        if not line or line.startswith("#"):
            continue
        assert '"' not in line
        assert "'" not in line
        assert "=" in line
