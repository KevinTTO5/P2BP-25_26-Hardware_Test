"""Tests for `mv3dt_installer.steps` (doc 00 §12.1-12.2).

Covers: `StepStatus`'s five named values, `StepResult` defaults, a minimal
`UserAction`, and that `STEP_REGISTRY` starts empty and `register()` orders
by `.order`.

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
    register,
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


class _DummyStep:
    """Minimal conforming `Step` implementation, for registry tests only.

    `Any` stands in for `Context`, which does not exist yet (see module
    docstring).
    """

    def __init__(self, id_: str, title: str, order: int) -> None:
        self.id = id_
        self.title = title
        self.order = order

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
