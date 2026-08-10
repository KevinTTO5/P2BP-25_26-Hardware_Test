---
name: parallel-worktree-dev
description: Use whenever a development task in this repo is big enough to split into more than one independently reviewable piece of work — implementing a spec/design doc (e.g. installer/plan/*.md), building out a multi-file feature, or any request to "parallelize this with agents," "build this out," "implement this doc/spec," or "use worktrees for this." Decomposes the work into a dependency-ordered set of units, builds each in its own isolated git worktree/branch, opens one real PR per unit, has a fresh-context agent review and iterate on each PR until approved, then hands the user one consolidated summary before anything merges. Actual merging is deliberately never done by this skill — it hands off to the paired /merge-prs command so merge order stays an explicit, caller-given decision. This is the standing "how we build things here" workflow: it exists so every non-trivial change in this repo leaves the same kind of PR-based paper trail, not a one-off procedure for a single task. Not needed for single-file or single-line changes — the worktree/PR/review overhead isn't worth it below that size.
---

# Parallel worktree development

Decompose → build in parallel worktrees → review with fresh context →
summarize → stop. Merging is a separate, explicit step owned by
`/merge-prs` (see `.claude/commands/merge-prs.md`) — never done here.

## Checklist (apply in order)

1. **When to use** — any implementation task big enough to decompose into
   more than one independently reviewable unit (multiple files/modules, or
   one file with genuinely separable concerns). Skip this workflow for
   one-file/one-line fixes; a plain edit is faster and the PR-per-unit
   overhead buys nothing at that size.

2. **Prerequisite gate** — before doing anything else, check
   `gh auth status`. If it fails, stop and tell the user to run
   `gh auth login` first. Do not silently fall back to local-only commits
   or skip PR creation — a workflow that's supposed to leave a PR paper
   trail is broken if it quietly doesn't.

3. **Decompose into a dependency DAG** — read the target spec (a file path
   the caller gives you, or the request itself if there's no doc) and break
   it into units, where a unit is "one PR worth of work" — usually one
   module/file or one tightly-scoped concern, not "one wave" and not
   "every file individually" if several files are too small or too coupled
   to review separately. For each unit, note which other units it has a
   real import/build dependency on. Group units into topological waves:
   wave 1 has no dependencies on other units in this batch, wave 2 depends
   only on wave 1, and so on. If the caller already supplied a
   decomposition (table of units + waves + dependencies), use it as given
   instead of re-deriving one — don't second-guess a decomposition someone
   already did the analysis for, but do sanity-check it against the spec
   before starting.

4. **Build, wave by wave** — for each wave, in a single message, spawn one
   `Agent` per unit (`isolation: "worktree"`, `run_in_background: true`),
   so units in the same wave run genuinely in parallel. Each implementer's
   prompt must include: the exact spec section(s) it owns, any exact
   required signatures/strings/behavior the spec states (quote them, don't
   paraphrase), a requirement to write tests for non-trivial logic, and the
   scope boundary (what NOT to touch — name adjacent areas explicitly so
   the agent doesn't drift into a neighboring unit's territory).
   - **Wave 1**: the default worktree base (`main`) is correct as-is.
   - **Wave 2+**: the harness's worktree isolation always bases off `main`
     or current HEAD — it cannot be pointed at another agent's branch per
     call. So each wave-2+ agent's prompt must explicitly instruct it,
     immediately after entering its worktree and before writing any code,
     to layer in its dependency branch(es):
     `git fetch origin <dep-branch> && git merge --ff-only origin/<dep-branch>`
     (falling back to a real `git merge` if `--ff-only` fails because
     `main` moved past the base commit). A unit with multiple dependencies
     merges each in turn.
   - Every unit ends the same way: commit, push, and
     `gh pr create --base <correct-base>` — `main` for wave 1, the
     dependency's branch for wave 2+ (this is what makes `/merge-prs`'s
     ordering load-bearing rather than cosmetic: GitHub retargets an open
     PR's base to `main` automatically once its current base branch
     merges, so the dependency chain only resolves if merges happen in
     wave order). The PR body must name the spec section(s) it implements.
   - Don't start wave *N+1* until every wave-*N* PR has been opened
     (pushed branches must exist for wave N+1 to fetch from).

5. **Review each PR as soon as it opens** — don't wait for the whole batch.
   For each PR, spawn one fresh `Agent` with **no knowledge of the
   implementer's prompt or reasoning** — give it only the spec section(s)
   that PR covers and `gh pr diff <n>`. This is deliberate: a reviewer that
   already knows why the code looks the way it does will rubber-stamp it.
   The reviewer checks: exact adherence to any required signatures/strings
   the spec states, scope (flag anything the spec didn't ask for — that's
   a real finding, not nitpicking), and test coverage.
   - **Verdict format — read this before posting anything.** Every agent in
     this workflow authenticates as the *same* `gh` identity as the PR
     author, so GitHub's own self-approval guard rejects
     `gh pr review <n> --approve` / `--request-changes` outright (HTTP 422,
     "Can not approve your own pull request"). Do not attempt formal
     `gh pr review` at all, and do not try to work around the guard (e.g.
     retrying `--comment` as a disguised approval) — that defeats a real
     security control and will get flagged. Instead, post a **plain
     comment** whose first line is exactly one of:
     `REVIEW VERDICT: APPROVE` or `REVIEW VERDICT: REQUEST_CHANGES`,
     followed by the findings — via `gh pr comment <n> --body "..."`. This
     marker line is what `/merge-prs` greps for later, so get it verbatim.

6. **Iterate by continuing agents, not respawning them** — on a
   `REQUEST_CHANGES` verdict, `SendMessage` the reviewer's feedback to the
   *same* implementer agent (it already has the worktree open and the
   context of what it built and why — a fresh agent would have to
   re-derive both). It fixes and pushes a follow-up commit. Then
   `SendMessage` the *same* reviewer agent to re-check and post a fresh
   `REVIEW VERDICT: ...` comment (it already knows what it flagged; the
   newest verdict comment on a PR is the one that counts — don't delete or
   edit the old one, just post a new one). Repeat. Cap at 3 rounds per PR —
   if still unresolved after that, stop looping and surface the specific
   disagreement to the user instead of continuing indefinitely.

7. **Summarize before merge, and stop there** — once every PR in the batch
   is approved, produce one consolidated summary: PR number, branch, files
   changed, spec section(s) covered, how many review rounds it took, and
   final verdict — plus the merge order the summary recommends (derived
   from the dependency DAG in step 3). Hand this to the user. **This skill
   never merges anything.** Merging is the user's explicit call, made with
   `/merge-prs <ordered PR list>` (`.claude/commands/merge-prs.md`) — that
   split exists specifically so "all the work is done and reviewed" and
   "this is now live on main" stay two separate, separately-approved
   moments.

8. **Guardrails**:
   - Always a worktree per unit — never edit the shared checkout directly
     for this kind of task, even for "just one small thing" partway
     through a batch.
   - Never expand scope beyond the target spec. If an implementer or
     reviewer notices the spec is ambiguous or silent on something the
     code needs, it reports that back rather than inventing behavior to
     fill the gap.
   - One branch = one PR = one reviewable unit, always — this is what
     makes the paper trail actually useful later (`git log`/PR history
     shows *why* each piece exists, not one giant undifferentiated commit).
   - If a unit's implementer and reviewer disagree after 3 rounds (step 6),
     that goes to the user — don't have the reviewer unilaterally
     approve just to stop looping, and don't have the implementer give up
     and merge anyway (it can't; merging isn't available to either agent).

## Before starting

- Confirm the repo has a `gh`-authenticated session and a pushable remote
  (`git remote -v`) before spawning any agents — cheaper to fail here than
  after wave 1 is already mid-flight.
- If the target is a spec doc, skim it fully yourself first so the
  decomposition in step 3 is grounded in the actual document, not an
  assumption about what it probably says.
- Know going in that review verdicts are `REVIEW VERDICT: APPROVE` /
  `REQUEST_CHANGES` **comments**, not formal GitHub reviews — see step 5.
  If a stray comment ever gets posted while working out a posting problem
  (e.g. a blocked attempt that partially succeeds), it can't be deleted
  once submitted; post a follow-up comment saying to disregard it and move
  on, don't burn time trying to remove it via the API.

## Self-check before handing back the summary

- Every unit that was built has an open PR, and every open PR has either an
  `REVIEW VERDICT: APPROVE` comment or an explicit user-facing escalation
  (step 6's 3-round cap).
- No PR's diff contains anything outside its assigned spec section(s).
- The recommended merge order in the summary is a valid topological order
  of the dependency DAG — not just "PR-1, PR-2, PR-3..." by number.
- Nothing has been merged. If it has, that's a bug in how this skill was
  run — merging only happens via `/merge-prs`, run by the user.
