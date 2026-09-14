"""Tests for `mv3dt_installer.steps` (doc 00 §12.1-12.2).

Covers: `StepStatus`'s five named values, `StepResult` defaults, a minimal
`UserAction`, and that `STEP_REGISTRY` starts empty and `register()` orders
by `.order`.

Also covers the optional `phases` declaration and its accessors (doc
`08-PROGRESS-AND-OBSERVABILITY.md` §3.1 and §9): a step that declares
phases, a step that omits them, and every way a declaration can be
malformed.

`Context` (doc 00 §12.3) does not exist yet — it is built in a later,
separate integration PR. The dummy `Step` implementation below uses
`typing.Any` in place of `Context` for its method signatures; this is
test-only and is not part of the `mv3dt_installer.steps` module itself.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mv3dt_installer.steps import (  # noqa: E402
    STEP_REGISTRY,
    StepResult,
    StepStatus,
    UserAction,
    phase_count,
    phase_label,
    register,
    step_phases,
    validate_phases,
)


@pytest.fixture(autouse=True)
def _empty_registry():
    """Snapshot/restore STEP_REGISTRY so registry tests are order-independent
    and don't leak state to other tests in this file or a wider run.
    """

    saved = list(STEP_REGISTRY)
    STEP_REGISTRY.clear()
    yield
    STEP_REGISTRY.clear()
    STEP_REGISTRY.extend(saved)


def test_step_status_has_exactly_five_named_values() -> None:
    names = {member.name for member in StepStatus}
    assert names == {
        "PENDING",
        "COMPLETE",
        "REBOOT_REQUIRED",
        "USER_ACTION_REQUIRED",
        "FAILED",
    }
    assert len(StepStatus) == 5
    # Values match names exactly (§12.2 quotes them as identical strings).
    assert StepStatus.PENDING.value == "PENDING"
    assert StepStatus.COMPLETE.value == "COMPLETE"
    assert StepStatus.REBOOT_REQUIRED.value == "REBOOT_REQUIRED"
    assert StepStatus.USER_ACTION_REQUIRED.value == "USER_ACTION_REQUIRED"
    assert StepStatus.FAILED.value == "FAILED"


def test_step_result_defaults() -> None:
    result = StepResult(status=StepStatus.COMPLETE)
    assert result.status is StepStatus.COMPLETE
    assert result.message == ""
    assert result.user_actions == []


def test_step_result_defaults_are_independent_instances() -> None:
    # field(default_factory=list) must not share a mutable default across
    # instances.
    a = StepResult(status=StepStatus.COMPLETE)
    b = StepResult(status=StepStatus.COMPLETE)
    a.user_actions.append(UserAction(text="only on a"))
    assert a.user_actions != b.user_actions
    assert b.user_actions == []


def test_user_action_with_only_text_set() -> None:
    action = UserAction(text="Add DeepStream to your PATH")
    assert action.text == "Add DeepStream to your PATH"
    assert action.command is None
    assert action.path is None


def test_user_action_with_command_and_path() -> None:
    action = UserAction(
        text="Append the CUDA exports to your profile",
        command="echo 'export PATH=$PATH:/usr/local/cuda/bin' >> ~/.profile",
        path="~/.profile",
    )
    assert action.command is not None
    assert action.path is not None


_UNSET = object()


class _DummyStep:
    """Minimal conforming `Step` implementation, for registry tests only.

    `Any` stands in for `Context`, which does not exist yet (see module
    docstring).
    """

    def __init__(
        self, id_: str, title: str, order: int, phases: Any = _UNSET
    ) -> None:
        self.id = id_
        self.title = title
        self.order = order
        # A sentinel rather than a `None` default, so tests can distinguish
        # "declared nothing" from "declared something malformed".
        if phases is not _UNSET:
            self.phases = phases

    def preflight(self, ctx: Any) -> StepResult:
        return StepResult(status=StepStatus.COMPLETE)

    def run(self, ctx: Any) -> StepResult:
        return StepResult(status=StepStatus.COMPLETE)

    def verify(self, ctx: Any) -> StepResult:
        return StepResult(status=StepStatus.COMPLETE)

    def report(self, ctx: Any) -> None:
        return None


def test_step_registry_starts_empty() -> None:
    # `_empty_registry` clears STEP_REGISTRY before each test runs; a fresh
    # install (no stepN_*.py modules imported yet) starts with none
    # registered either way.
    assert STEP_REGISTRY == []


def test_register_orders_by_order_field() -> None:
    step3 = _DummyStep("step3_amc_launcher", "AMC launcher", 3)
    step1 = _DummyStep("step1_prerequisites", "Prerequisites", 1)
    step2 = _DummyStep("step2_deepstream_sdk", "DeepStream SDK", 2)

    # Register out of order; STEP_REGISTRY must end up ordered by `.order`.
    register(step3)
    register(step1)
    register(step2)

    assert [s.order for s in STEP_REGISTRY] == [1, 2, 3]
    assert [s.id for s in STEP_REGISTRY] == [
        "step1_prerequisites",
        "step2_deepstream_sdk",
        "step3_amc_launcher",
    ]


# --- phases declaration (doc 08 §3.1, §9) -------------------------------

_PHASES = (
    "base packages",
    "CUDA repo and toolkit",
    "NVIDIA driver runfile",
)


def _phased_step() -> _DummyStep:
    return _DummyStep("step1_prerequisites", "Prerequisites", 1, phases=_PHASES)


def test_step_declaring_phases_registers_and_exposes_them() -> None:
    step = _phased_step()
    register(step)

    assert STEP_REGISTRY == [step]
    assert step_phases(step) == _PHASES
    assert phase_count(step) == 3
    assert phase_label(step, 1) == "base packages"
    assert phase_label(step, 3) == "NVIDIA driver runfile"


def test_phases_declared_as_a_list_is_accepted_and_normalized() -> None:
    step = _DummyStep("step4_calib", "Calib wiring", 4, phases=list(_PHASES))
    register(step)

    assert step_phases(step) == _PHASES


def test_step_without_phases_registers_as_one_unnamed_phase() -> None:
    # The backward-compatibility case that lets the seven steps adopt
    # phases one at a time.
    step = _DummyStep("step5_per_project_exes", "Per-project exes", 5)
    register(step)

    assert STEP_REGISTRY == [step]
    assert not hasattr(step, "phases")
    assert step_phases(step) == ()
    assert phase_count(step) == 1
    assert phase_label(step, 1) is None


def test_phase_index_out_of_range_raises() -> None:
    # Doc 08 §9: raise rather than render a wrong denominator.
    step = _phased_step()
    unphased = _DummyStep("step5_per_project_exes", "Per-project exes", 5)

    for index in (0, -1, 4):
        with pytest.raises(IndexError):
            phase_label(step, index)
    with pytest.raises(IndexError):
        phase_label(unphased, 2)


def test_phase_index_must_be_an_int() -> None:
    step = _phased_step()
    for index in ("1", 1.0, True, None):
        with pytest.raises(TypeError):
            phase_label(step, index)


@pytest.mark.parametrize(
    "phases",
    [
        "base packages",  # a str is itself a sequence of strings
        b"base packages",
        {"base packages"},  # unordered, so meaningless as phase 1..N
        {"1": "base packages"},
        7,
        object(),
    ],
)
def test_register_rejects_phases_that_are_not_a_string_sequence(phases: Any) -> None:
    step = _DummyStep("step1_prerequisites", "Prerequisites", 1, phases=phases)
    with pytest.raises(TypeError):
        register(step)
    assert STEP_REGISTRY == []


@pytest.mark.parametrize("phases", [("base packages", 2), (None,), (("nested",),)])
def test_register_rejects_non_string_phase_labels(phases: Any) -> None:
    step = _DummyStep("step1_prerequisites", "Prerequisites", 1, phases=phases)
    with pytest.raises(TypeError):
        register(step)
    assert STEP_REGISTRY == []


@pytest.mark.parametrize("phases", [("",), ("base packages", "   ")])
def test_register_rejects_empty_phase_labels(phases: Any) -> None:
    step = _DummyStep("step1_prerequisites", "Prerequisites", 1, phases=phases)
    with pytest.raises(ValueError):
        register(step)
    assert STEP_REGISTRY == []


def test_register_rejects_an_empty_phases_tuple() -> None:
    # Omitting the attribute is how a step says "one unnamed phase";
    # declaring an empty tuple is an authoring mistake.
    step = _DummyStep("step1_prerequisites", "Prerequisites", 1, phases=())
    with pytest.raises(ValueError):
        register(step)
    assert STEP_REGISTRY == []


def test_validate_phases_names_the_offending_step() -> None:
    step = _DummyStep("step2_deepstream_sdk", "DeepStream SDK", 2, phases=("ok", ""))
    with pytest.raises(ValueError, match="step2_deepstream_sdk"):
        validate_phases(step)


def test_every_step_declares_phases_that_match_the_indices_it_uses():
    """Doc 08 section 3.1 and section 9.

    `ctx.progress.phase(n)` raises on an out-of-range index, deliberately: a
    wrong denominator on screen is worse than a traceback, because the
    operator cannot tell a wrong one from a right one. That makes the
    declaration and the call sites a pair that has to be kept in step, and
    nothing else checks it. A step that grows a phase without declaring it
    would otherwise fail for the first time on a real install.

    Reads the modules directly rather than `STEP_REGISTRY`, which other
    tests in the suite legitimately replace.
    """
    import importlib
    import pathlib as _pathlib
    import re as _re

    from mv3dt_installer import steps as steps_mod

    names = (
        "step1_prerequisites",
        "step2_deepstream_sdk",
        "step3_amc_launcher",
        "step4_calib_output_wiring",
        "step5_per_project_exes",
        "step6_remote_supervision",
        "step7_webapp_integration",
    )

    for name in names:
        module = importlib.import_module(f"mv3dt_installer.steps.{name}")
        step = next(
            obj
            for obj in vars(module).values()
            if isinstance(obj, type) and getattr(obj, "id", None) == name
        )
        labels = steps_mod.step_phases(step)
        assert labels, f"{name} declares no phases"

        source = _pathlib.Path(module.__file__).read_text(encoding="utf-8")
        used = sorted(
            {int(n) for n in _re.findall(r"ctx\.progress\.phase\((\d+)\)", source)}
        )
        assert used == list(range(1, len(labels) + 1)), (
            f"{name} declares {len(labels)} phase(s) but calls phase{used}"
        )
