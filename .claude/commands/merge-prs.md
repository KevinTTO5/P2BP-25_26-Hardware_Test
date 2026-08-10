---
description: Merge a batch of PRs from the parallel-worktree-dev workflow in an explicit, caller-given order — never re-derives or reorders the sequence itself.
argument-hint: <pr-number-or-branch> [pr-number-or-branch ...]
---

Merge the PRs listed in `$ARGUMENTS`, strictly in the order given. This
command is the paired second half of the `parallel-worktree-dev` skill
(`.claude/skills/parallel-worktree-dev/SKILL.md`): that skill builds units
in dependency-ordered waves and opens one PR per unit, stacking wave-2+ PRs
on their dependency's branch instead of `main`, but it never merges
anything. Merge order therefore matters — it is not cosmetic. Follow these
steps in order.

1. **Parse** `$ARGUMENTS` as an ordered, whitespace- or comma-separated
   list of PR numbers or branch names. The order given is the merge order.
   Do not reorder it, deduplicate silently, or try to be smarter than the
   caller about sequencing — if the order looks wrong (e.g. it doesn't
   match the dependency chain implied by each PR's base branch), say so and
   ask before proceeding, but don't substitute your own order.

2. **Validate every entry before merging anything.** For each entry, in
   the given order, run:
   `gh pr view <id> --json number,title,baseRefName,headRefName,mergeable,reviewDecision,state`
   Every entry must be `state: OPEN`, `reviewDecision: APPROVED`, and
   `mergeable: MERGEABLE`. If any entry fails any of those three
   conditions, stop immediately — report exactly which PR and which
   condition failed — and do not merge anything from the list, including
   entries earlier in the order that did pass. A partial merge of a
   dependency chain is worse than no merge.

3. **Print the resolved plan** before touching anything: a table of
   PR # → title → base branch → merge position in this run. Ask the user
   once, up front, whether to merge via `--merge` or `--squash` for the
   whole batch (not per PR — a mixed batch is confusing to reconstruct
   later). Get an explicit go-ahead before merging — this command performs
   real, hard-to-reverse merges against the shared remote.

4. **Merge in order**, one at a time:
   `gh pr merge <id> --merge --delete-branch` (or `--squash --delete-branch`
   per the user's choice in step 3).
   - Before merging any entry after the first: if that PR's base branch was
     the *previous* entry's branch (a stacked PR), confirm GitHub already
     retargeted it to `main` following the previous merge —
     `gh pr view <id> --json baseRefName`. If it's still pointed at the
     now-deleted branch, run `gh pr edit <id> --base main` first, then
     re-check `mergeable` before merging.
   - After each successful merge, move to the next entry. Do not batch all
     the `gh pr merge` calls up front — each one can change the
     mergeability of the next.

5. **Stop on the first failure.** If a merge fails (conflict, CI red,
   retarget didn't resolve cleanly), stop immediately. Report which PRs in
   the list merged successfully, which one failed and why, and which
   remaining entries were never attempted. Never force past a conflict and
   never skip ahead to a later entry — later entries may depend on the one
   that failed.

6. **Final report.** Once the given list completes (or stops early), report
   final status of every entry, then separately list any *other* open PRs
   in the repo that were not part of `$ARGUMENTS` — the goal is to make it
   obvious if the user forgot one, not to merge it for them.
