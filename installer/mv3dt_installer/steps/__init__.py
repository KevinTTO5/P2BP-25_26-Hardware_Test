"""Step-module interface and registry.

Implements doc `00-FRAMEWORK-AND-BOOTSTRAP.md` §12.1 (the `Step` protocol)
and §12.2 (`StepStatus`, `StepResult`, and the `UserAction` type referenced
from §9.3). This module is framework-only: it defines the contract that
`stepN_*.py` modules (step1_prerequisites.py … step5_per_project_exes.py)
implement and register against. No step business logic lives here.

`Context` (§12.3) is built later in `app.py`, which depends on nearly every
other module in this package and is therefore integrated last. To avoid a
circular/premature import, this module uses `from __future__ import
annotations` (PEP 563) so `Context` can appear as an unevaluated type hint
in the `Step` protocol's method signatures without being imported or
stubbed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

__all__ = [
    "StepStatus",
    "UserAction",
    "StepResult",
    "Step",
    "STEP_REGISTRY",
    "register",
]


class StepStatus(Enum):
    """Status recorded by the state machine for a step's effective result.

    Doc 00 §12.2.
    """

    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    REBOOT_REQUIRED = "REBOOT_REQUIRED"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
    FAILED = "FAILED"


@dataclass
class UserAction:
    """One instruction line in a USER-ACTION block.

    Doc 00 §9.3. `command` (if set) is rendered verbatim and is meant to be
    copy-pasteable; `path` (if set) names the file the operator should edit.
    """

    text: str
    command: str | None = None
    path: str | None = None


@dataclass
class StepResult:
    """Result returned by each of a step's four lifecycle methods.

    Doc 00 §12.2. The dispatch loop (§3.2) takes the first non-`COMPLETE`
    result across `preflight -> run -> verify` as the step's effective
    result, else `COMPLETE`. Steps never write `state.json` directly — they
    only return a `StepResult`; the framework owns all persistence.
    """

    status: StepStatus
    message: str = ""
    user_actions: list[UserAction] = field(default_factory=list)


class Step(Protocol):
    """The contract every `stepN_*.py` module implements (doc 00 §12.1).

    `ctx` is typed as the bare name `Context` (doc 00 §12.3), which is
    intentionally never imported here — see the module docstring.
    """

    id: str  # e.g. "step2_deepstream_sdk"  (matches state.json key)
    title: str  # human title for logs/USER-ACTION blocks
    order: int  # 1..5

    def preflight(self, ctx: Context) -> StepResult: ...

    def run(self, ctx: Context) -> StepResult: ...

    def verify(self, ctx: Context) -> StepResult: ...

    def report(self, ctx: Context) -> None: ...


# Ordered (by `.order`) registry of step implementations. Populated at
# import time by each `stepN_*.py` module via `register()`; empty until
# those modules exist (they are separate, out-of-scope work — doc 00 is
# framework-only). Iterating `STEP_REGISTRY` always yields steps in
# ascending `.order`.
STEP_REGISTRY: list[Step] = []


def register(step: Step) -> None:
    """Register a `Step` implementation into `STEP_REGISTRY`.

    Call this once at module scope in each `stepN_*.py` module, e.g.:

        from mv3dt_installer.steps import register

        class Step2DeepStreamSdk:
            id = "step2_deepstream_sdk"
            title = "DeepStream SDK"
            order = 2
            ...

        register(Step2DeepStreamSdk())

    `STEP_REGISTRY` is kept sorted by `.order` after every call, so callers
    never need to sort it themselves.
    """

    STEP_REGISTRY.append(step)
    STEP_REGISTRY.sort(key=lambda s: s.order)
