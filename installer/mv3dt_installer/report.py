"""Dependency reporting, pinned-version verification, and failure evidence.

Doc 00 §8.3-8.4 (reporting strings) and doc 08 §6-6.1 (evidence).

Two shared helpers give every step identical wording when reporting
dependency state, plus a single reusable helper for equality-pinned version
verification (porting `require_version_eq` from
laptop/scripts/lib/common.sh). All three route through logs.py's `log.info`
so the lines are colour-aware on stderr and also captured in the transcript
(§8.1-8.2) -- this module writes nothing to a stream itself; every line
it produces goes through `logs`.

Public API:
    report_installed(dependency, version)          -- "installed <dep> version <ver>"
    report_already_installed(dependency, version)   -- "already installed <dep> version <ver>"
    verify_pinned(label, actual, expected) -> bool  -- equality-pinned check

    Evidence(...) / with_evidence(message, evidence) -- doc 08 §6
    FailureContext(...) / render_failure_context / show_failure_context
                                                    -- doc 08 §6.1
    refusal(message, evidence=, user_actions=)      -- USER_ACTION_REQUIRED
    failure(message, context=, evidence=)           -- FAILED

Every step MUST use verify_pinned in its verify() and one of the two
report_* helpers after every dependency it touches, so the transcript stays
uniform and greppable (§8.4).

The second half of this module implements doc
`08-PROGRESS-AND-OBSERVABILITY.md` §6 and §6.1, which exist because of two
observed events (doc 08 §2):

- **Event 5.** A guard refused an operator who was doing exactly what it
  asked, stating its verdict and none of its inputs. §6 makes naming the
  inputs REQUIRED for every `USER_ACTION_REQUIRED` and `FAILED` message that
  is the product of an inference; `Evidence` and `with_evidence` are the
  surface that makes it the cheap thing to do. `step1_prerequisites.py`'s
  session guard already does this by hand and is the shape these follow.
- **The discarded `CompletedProcess`.** A step runs a command, sees a
  nonzero exit, and returns `FAILED` with a sentence -- the captured output
  that says *why* is dropped on the floor. `FailureContext.from_completed`
  turns that same object into the block §6.1 requires: step and phase, the
  exact command, its exit code, the last 20 lines of output, and the
  transcript path.

Neither is re-implemented here. Redaction is `shellout`'s (doc 08 §4.2) and
control-character stripping is `progress.sanitise`'s, for the reason
`shellout` states about itself: one definition of "clean" and one of
"scrubbed", rather than two that agree today and drift later.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from . import logs, progress, shellout
from .logs import log
from .steps import StepResult, StepStatus


def report_installed(dependency: str, version: str) -> None:
    """Report a newly installed dependency (doc 00 §8.3).

    Logs the exact required string: `installed <dependency> version <version>`.
    """
    log.info(f"installed {dependency} version {version}")


def report_already_installed(dependency: str, version: str) -> None:
    """Report a pre-existing dependency (doc 00 §8.3).

    Logs the exact required string:
    `already installed <dependency> version <version>`.
    """
    log.info(f"already installed {dependency} version {version}")


def verify_pinned(label: str, actual: str, expected: str) -> bool:
    """Equality-pinned version verification (doc 00 §8.4).

    Ports `require_version_eq` from lib/common.sh. On match, logs
    `Version OK: <label> == <actual>` and returns True. On mismatch, logs
    `Version check failed: <label> -- expected '<expected>', got '<actual>'`
    (em dash) and returns False. Never calls die() or exits -- the caller
    decides whether to die() or surface a USER_ACTION_REQUIRED.
    """
    if actual == expected:
        log.info(f"Version OK: {label} == {actual}")
        return True
    log.info(
        f"Version check failed: {label} — expected '{expected}', "
        f"got '{actual}'"
    )
    return False


# ---------------------------------------------------------------------------
# Shared cleaning helpers -- one definition of "clean" and one of "scrubbed"
# ---------------------------------------------------------------------------


def _secrets() -> tuple[str, ...]:
    """Plaintext this process is holding under a redacted key.

    Deliberately `shellout`'s collector and `shellout`'s scrubber rather
    than a second pair here: doc 08 §4.2 says redaction is not
    re-implemented, and a private-but-shared definition is the lesser evil
    against two key lists that drift. Everything this module builds prints
    to the terminal and the transcript, which are exactly the two
    destinations §4.2 requires to be scrubbed -- a `CompletedProcess`
    buffer reaches them for the first time here, and it has never been
    scrubbed on its way in.

    Known narrowness: this reads `os.environ`, whereas `shellout` derives
    its secrets from the child environment it actually handed the command
    (`run_streamed` and `run_fragment` both pass `child_env`). A command
    run under an env overlay that carries a redacted key is therefore
    value-scrubbed on shellout's streaming path but only key-scrubbed here,
    because `FailureContext` has no field to carry the environment the run
    used. Adding one is the fix, and it belongs with whichever unit teaches
    the steps to build a context from `ctx.run_root`.
    """
    return shellout._secret_values(os.environ)


def _clean(text: str, secrets: Sequence[str]) -> str:
    """One line of text, made fit for a stream and for the transcript.

    Sanitise first, then scrub: `progress.sanitise` is what drops the
    carriage returns and escapes, and scrubbing before that would let an
    escape sequence sit between a secret's characters. `secrets` is passed
    in rather than collected here so a long buffer scans the environment
    once, not once per line.
    """
    return shellout._scrub(progress.sanitise(text), secrets)


def _decode(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


# ---------------------------------------------------------------------------
# doc 08 §6 -- inferred refusals name their inputs
# ---------------------------------------------------------------------------


def _format_value(value: Any) -> str:
    """Render one evidence value the way an operator reads it.

    `True`/`False` become `yes`/`no` because the inputs to an inference are
    overwhelmingly predicates -- doc 08 §6's worked example reads
    `sshd ancestor: no`, not `sshd ancestor: False`. `None` and a blank
    string both become `unknown`: a probe that could not answer is itself
    evidence, and silently dropping it would leave the operator counting
    which input is missing.
    """
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "unknown"
    text = str(value).strip()
    return text or "unknown"


class Evidence:
    """Ordered label/value pairs behind an inferred verdict (doc 08 §6).

    Renders as `label: value; label: value`, which `with_evidence` puts in
    parentheses after the message:

        Evidence(
            controlling_terminal="/dev/pts/1",
            sshd_ancestor=False,
            active_display_manager="gdm3",
        )
        -> "controlling terminal: /dev/pts/1; sshd ancestor: no; "
           "active display manager: gdm3"

    Keyword labels have their underscores turned into spaces, so the common
    case is one expression with no punctuation to get wrong. `add()` takes
    the label as given and returns `self`, for a guard that collects its
    inputs as it probes them:

        evidence = Evidence()
        evidence.add("controlling terminal", tty or "none detected")
        evidence.add("sshd ancestor", has_sshd_ancestor)

    Insertion order is preserved and never sorted: the order a guard probes
    its inputs in is the order that explains its verdict.
    """

    __slots__ = ("_items",)

    def __init__(
        self,
        items: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
        **labelled: Any,
    ) -> None:
        self._items: list[tuple[str, str]] = []
        if items is not None:
            self.extend(items)
        for label, value in labelled.items():
            self.add(label.replace("_", " "), value)

    def add(self, label: str, value: Any) -> "Evidence":
        """Append one `label: value` pair. Returns self, so calls chain.

        The value is cleaned and scrubbed on the way in, the same way a
        captured output line is (doc 08 §4.2). An evidence value is as
        likely to come from `installer.conf` or the environment as from a
        literal, and it lands in a message `app.py` prints to stderr and
        writes to the transcript -- §6 exists to make naming the inputs the
        cheap thing to do across all seven steps, so the cheap path has to
        be the safe one. A value that cleans away to nothing degrades to
        `unknown` for the same reason a blank one does.

        A blank label is dropped rather than stored, so `len()` and
        `render()` never disagree about how many inputs there are.
        """
        label = str(label).strip()
        if not label:
            return self
        value_text = _clean(_format_value(value), _secrets()) or "unknown"
        self._items.append((label, value_text))
        return self

    def extend(
        self, items: Mapping[str, Any] | Iterable[tuple[str, Any]]
    ) -> "Evidence":
        """Append every pair in a mapping or an iterable of 2-tuples."""
        pairs = items.items() if isinstance(items, Mapping) else items
        for label, value in pairs:
            self.add(label, value)
        return self

    def render(self) -> str:
        """The `label: value; ...` string, or `""` when there is nothing."""
        return "; ".join(f"{label}: {value}" for label, value in self._items)

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self.render())

    def __str__(self) -> str:
        return self.render()

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return f"Evidence({self._items!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Evidence):
            return self._items == other._items
        return NotImplemented


EvidenceLike = Evidence | Mapping[str, Any] | Iterable[tuple[str, Any]] | str | None


def render_evidence(evidence: EvidenceLike) -> str:
    """Render any accepted evidence shape to `label: value; ...`.

    Accepts an `Evidence`, a mapping, an iterable of 2-tuples, or an
    already-rendered string (which is what `step1_prerequisites.py`'s
    session guard returns today, and why it is accepted unchanged). `None`
    and an empty collection both render `""` -- the absent case degrades to
    "no parenthetical", never to an empty `()`.
    """
    if evidence is None:
        return ""
    if isinstance(evidence, Evidence):
        return evidence.render()
    if isinstance(evidence, str):
        # Cleaned and scrubbed like every other evidence value: a
        # pre-rendered string is the one shape that does not pass through
        # `Evidence.add`, and it is the shape a guard builds by hand.
        return _clean(evidence, _secrets()).strip()
    return Evidence(evidence).render()


def with_evidence(message: str, evidence: EvidenceLike = None) -> str:
    """Append the inputs an inference was drawn from to `message` (§6).

    `"<message> (<label: value; ...>)"`, matching doc 08 §6's worked
    example exactly. With no evidence the message is returned unchanged, so
    a caller never has to branch on whether its probes found anything.
    """
    rendered = render_evidence(evidence)
    return f"{message} ({rendered})" if rendered else message


# ---------------------------------------------------------------------------
# doc 08 §6.1 -- the failure context block
# ---------------------------------------------------------------------------

# "the last 20 lines of its output" (§6.1). Named rather than inlined so
# the number is greppable against the doc; it is deliberately not a
# parameter, because §6.1 fixes it and no caller has a reason to differ.
FAILURE_TAIL_LINES = 20

# Frame width matches doc 00 §9.3's ACTION REQUIRED block (privilege.py's
# `_BOX_WIDTH`) so the two blocks line up in one transcript. Only the
# header is boxed on both sides, exactly as that block does it: content
# lines run past the frame rather than being wrapped or truncated, because
# a truncated command line is not a command the operator can re-run.
_BOX_WIDTH = 65
_BORDER = "+" + "-" * (_BOX_WIDTH - 2) + "+"
_HEADER_TEXT = "INSTALL FAILED"
_FIELD_INDENT = "  "
_OUTPUT_INDENT = "      "
_NOT_RECORDED = "(not recorded)"


def _header_line() -> str:
    return "|" + f"  {_HEADER_TEXT}".ljust(_BOX_WIDTH - 2) + "|"


def _current_transcript() -> pathlib.Path | None:
    """The open transcript's path, or None when none is open.

    Read through this one accessor so the day `logs` grows a public one,
    there is a single line to change.
    """
    return logs._transcript_path


def _output_lines(output: Any) -> list[str]:
    r"""Split captured output into cleaned lines, trailing blanks dropped.

    Split on `"\n"` alone, never `str.splitlines()`: splitlines also breaks
    on `\r` (and on `\v`, `\f`, `\x1c`-`\x1e`, `\x85`), which would hand
    `progress.sanitise` lines that no longer contain the carriage returns
    it exists to collapse. That is not cosmetic. curl, wget and dpkg redraw
    one line hundreds of times (doc 08 §2 events 2 and 3), so a splitlines
    split turns one progress bar into one "line" per redraw frame and
    spends the whole 20-line tail on percentages, burying the error the
    block was printed for. Splitting on newlines only leaves each frame
    sequence intact inside its line, and sanitise keeps the last frame.

    A command's output almost always ends in a newline, which would
    otherwise spend one of the 20 tail lines on nothing.
    """
    if output is None:
        return []
    if isinstance(output, (str, bytes, bytearray)):
        raw = _decode(output).split("\n")
    else:
        raw = [_decode(item) for item in output]
    secrets = _secrets()
    lines = [_clean(line, secrets) for line in raw]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _indent_output(lines: Iterable[str]) -> list[str]:
    """Indent output lines under their heading, no trailing whitespace.

    `rstrip` matters for a blank line the command itself printed: indenting
    it would otherwise leave six spaces in the transcript, which is noise a
    diff and a `grep -n '  *$'` both pick up.
    """
    return [f"{_OUTPUT_INDENT}{line}".rstrip() for line in lines]


def _render_command(command: Any) -> str:
    """The failed command, cleaned and quoted so it can be pasted back.

    Cleaned for the same reason a captured output line is: this line goes
    to stderr raw and into the transcript, `logs`' ANSI strip does not
    touch a carriage return (doc 08 §12.2 defect 2), and a CR here would
    clobber the frame the block just drew. The argv branch is
    `shellout._command_dump` rather than a copy of it -- per-argument
    quoting and scrubbing is already defined there, and this module's whole
    premise is not re-implementing it.
    """
    if command is None:
        return _NOT_RECORDED
    secrets = _secrets()
    if isinstance(command, (str, bytes, bytearray)):
        return _clean(_decode(command), secrets) or _NOT_RECORDED
    parts = [progress.sanitise(_decode(part)) for part in command]
    if not parts:
        return _NOT_RECORDED
    return shellout._command_dump(parts, secrets) or _NOT_RECORDED


@dataclass
class FailureContext:
    """What the operator needs in order to act on a `FAILED` (doc 08 §6.1).

    Every field is optional, and every one but the phase degrades to an
    explicit "(not recorded)" rather than a missing line: a block that
    silently omits the exit code reads as though the command never ran.

    The phase is the single exception -- its line is dropped entirely when
    there is none. A step that declared no phases has no phase to report,
    so "Phase: (not recorded)" would claim something was lost when nothing
    was; a missing exit code, by contrast, really is something that was not
    captured. §6.1 names step and phase together, which is why the
    difference is spelled out here rather than left to be discovered.

    `output` is the failed command's own output -- stdout and stderr
    concatenated in that order by `from_completed`, so the tail favours
    stderr, which is where a failing command puts the sentence that
    explains itself.

    `transcript` resolves at render time when left as `None`, picking up
    whichever per-run transcript is open. A step therefore never has to
    thread the log path down to its failure site.
    """

    step: str | None = None
    phase: str | None = None
    command: str | Sequence[str] | None = None
    exit_code: int | None = None
    output: str | bytes | Sequence[str] | None = None
    transcript: pathlib.Path | str | None = None

    @classmethod
    def from_completed(
        cls,
        completed: Any,
        *,
        step: str | None = None,
        phase: str | None = None,
        transcript: pathlib.Path | str | None = None,
    ) -> "FailureContext":
        """Build a context from the `CompletedProcess` a step would discard.

        This is the whole point of §6.1: the object that already holds the
        command, the exit code and the output is the object the step throws
        away on its way to returning `FAILED`. Duck-typed on `.args`,
        `.returncode`, `.stdout` and `.stderr` so a fake in a test, or any
        other result-shaped object, works without an import.
        """
        stdout = getattr(completed, "stdout", None)
        stderr = getattr(completed, "stderr", None)
        parts = [part for part in (stdout, stderr) if part]
        if not parts:
            output: Any = None
        elif all(isinstance(part, (bytes, bytearray)) for part in parts):
            # Each stream's own trailing newline is dropped before the join,
            # so concatenating them does not manufacture a blank line
            # between the two -- which would otherwise spend one of the 20
            # tail slots on a separator nothing printed.
            output = b"\n".join(bytes(part).rstrip(b"\n") for part in parts)
        else:
            output = "\n".join(_decode(part).rstrip("\n") for part in parts)
        return cls(
            step=step,
            phase=phase,
            command=getattr(completed, "args", None),
            exit_code=getattr(completed, "returncode", None),
            output=output,
            transcript=transcript,
        )


def render_failure_context(context: FailureContext | None = None) -> str:
    """Render the §6.1 block as a string.

    Names the step and phase, the exact command, its exit code, the last
    `FAILURE_TAIL_LINES` lines of that command's output, and the transcript
    path -- in that order, which is also the order an operator reads them
    in: what failed, what it ran, what it said, where the rest of it is.

    `None`, and a `FailureContext` with nothing set, both render the full
    frame, with every field except the phase explicitly not recorded (see
    `FailureContext` for why the phase line is the one that is dropped). A
    block that says "we did not capture this" is still worth printing; it
    tells the operator to go to the transcript rather than to guess.
    """
    if context is None:
        context = FailureContext()

    lines = [
        _BORDER,
        _header_line(),
        _BORDER,
        f"{_FIELD_INDENT}Step:       {context.step or '(unknown)'}",
    ]
    if context.phase:
        lines.append(f"{_FIELD_INDENT}Phase:      {context.phase}")
    lines.append(f"{_FIELD_INDENT}Command:    {_render_command(context.command)}")
    exit_code = (
        _NOT_RECORDED if context.exit_code is None else str(context.exit_code)
    )
    lines.append(f"{_FIELD_INDENT}Exit code:  {exit_code}")

    output_lines = _output_lines(context.output)
    lines.append("")
    if not output_lines:
        lines.append(f"{_FIELD_INDENT}Output:     (none captured)")
    elif len(output_lines) > FAILURE_TAIL_LINES:
        lines.append(
            f"{_FIELD_INDENT}Last {FAILURE_TAIL_LINES} lines of output "
            f"({len(output_lines)} total):"
        )
        lines.extend(_indent_output(output_lines[-FAILURE_TAIL_LINES:]))
    else:
        plural = "" if len(output_lines) == 1 else "s"
        lines.append(f"{_FIELD_INDENT}Output ({len(output_lines)} line{plural}):")
        lines.extend(_indent_output(output_lines))

    transcript = context.transcript
    if transcript is None:
        transcript = _current_transcript()
    lines.append("")
    lines.append(
        f"{_FIELD_INDENT}Transcript: "
        f"{transcript if transcript is not None else '(no transcript open)'}"
    )
    lines.append(_BORDER)
    return "\n".join(lines)


def show_failure_context(context: FailureContext | None = None) -> None:
    """Render the §6.1 block and emit it at error level.

    Routed through `logs.log.error` for the same reason
    `privilege.show_user_action_block` is: stderr and the transcript in one
    call, with no second writer to the terminal (doc 08 §4.2).
    """
    log.error(render_failure_context(context))


# ---------------------------------------------------------------------------
# Result constructors -- the surface that makes §6 the cheap path
# ---------------------------------------------------------------------------

# `failure()` records its block on the returned `StepResult` under this
# attribute. `StepResult` (doc 00 §12.2) has no field for it and is not
# this unit's to change, so the dispatch loop reads it with
# `getattr(result, FAILURE_CONTEXT_ATTR, None)` and an older result simply
# has nothing to render.
FAILURE_CONTEXT_ATTR = "failure_context"


def refusal(
    message: str,
    *,
    evidence: EvidenceLike = None,
    user_actions: Iterable[Any] = (),
) -> StepResult:
    """A `USER_ACTION_REQUIRED` result whose message carries its inputs (§6).

    The message reads exactly as doc 08 §6's worked example does:

        the driver installer must stop the desktop session, which would
        kill this installer along with it (controlling terminal:
        /dev/pts/1; sshd ancestor: no; active display manager: gdm3)

    With no evidence the message is passed through untouched, so this is
    also the ordinary constructor for a refusal that infers nothing.
    """
    return StepResult(
        status=StepStatus.USER_ACTION_REQUIRED,
        message=with_evidence(message, evidence),
        user_actions=list(user_actions),
    )


def failure(
    message: str,
    *,
    context: FailureContext | None = None,
    evidence: EvidenceLike = None,
    user_actions: Iterable[Any] = (),
    show: bool = True,
) -> StepResult:
    """A `FAILED` result that keeps the evidence for its own verdict.

    `evidence` appends the inputs behind an inferred message (§6);
    `context` is the §6.1 block, which is both printed (unless
    `show=False`) and attached to the returned result under
    `FAILURE_CONTEXT_ATTR`.

    Printing here is deliberate and interim. The dispatch loop logs only
    `"<step> failed: <message>"` today, so the block has no other way to
    reach the operator; `show=False` is for the caller that renders it
    itself, and for the dispatch loop once it renders the attached context
    on its own.
    """
    result = StepResult(
        status=StepStatus.FAILED,
        message=with_evidence(message, evidence),
        user_actions=list(user_actions),
    )
    setattr(result, FAILURE_CONTEXT_ATTR, context)
    if show and context is not None:
        show_failure_context(context)
    return result
