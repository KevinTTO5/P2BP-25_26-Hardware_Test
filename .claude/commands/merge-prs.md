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
   `gh pr view <id> --json number,title,baseRefName,headRefName,mergeable,state,comments`
   Every entry must be `state: OPEN` and `mergeable: MERGEABLE`. For
   approval, do **not** check `reviewDecision` — every agent in this
   workflow shares one `gh` identity with the PR author, so GitHub blocks
   formal self-approval and `reviewDecision` is never populated here.
   Instead, scan `.comments[].body` in chronological order and take the
   **last** one that is exactly `LGTM` or that starts with
   `REQUEST_CHANGES` (a later verdict comment supersedes an earlier one on
   the same PR — that's how the re-review loop in `parallel-worktree-dev`
   records an iteration). The entry must resolve to `LGTM`. If any entry
   fails on state, mergeable, or verdict — including "no verdict comment
   found at all" — stop immediately, report exactly which PR and which
   condition failed, and do not merge anything from the list, including
   entries earlier in the order that did pass. A partial merge of a
   dependency chain is worse than no merge.

3. **Print the resolved plan** before touching anything: a table of
   PR # → title → base branch → merge position in this run. Get an
   explicit go-ahead from the user before merging — this command performs
   real, hard-to-reverse merges against the shared remote. There is no
   merge-strategy choice to make: this command always squash-merges with
   the branch deleted (see step 4) — never a plain merge commit.

4. **Retarget to `main` before every single merge, unconditionally —
   this is not optional and not conditional on anything.** For each
   entry, immediately before merging it: check `baseRefName`
   (`gh pr view <id> --json baseRefName`). If it is not already `main`,
   retarget it:
   `gh api repos/<owner>/<repo>/pulls/<id> -X PATCH -f base=main`
   (do **not** use `gh pr edit <id> --base main` — it fails outright on
   this repo with a GraphQL error about deprecated Projects Classic
   fields; the REST endpoint above works). Then re-check `mergeable`.

   Do this for *every* stacked-base entry, every time, even if you expect
   GitHub to have already auto-retargeted it. Reasoning: GitHub only
   auto-retargets a PR when the literal branch it's pointed at is deleted
   as part of merging *that exact branch's own* PR. Two things break this
   silently: (a) a merge whose `--delete-branch` step fails for any reason
   (e.g. the branch is checked out in another local worktree — this
   happens routinely in this workflow) leaves the branch alive and
   auto-retargeting never fires; (b) if a dependency's code ever reaches
   `main` through a *different* PR/branch than the one this entry is
   literally based on (for example, after a recreated PR — see below),
   GitHub has no way to know they're "the same" and will never retarget.
   In both cases the PR silently stays pointed at its stale base, and
   `gh pr merge --squash` will merge onto *that base*, not `main` — with
   no error, and `gh pr view` will still happily report the PR as
   `MERGED`. This is exactly how four modules ended up correct in the
   final tree but invisible as separate commits in `main`'s history the
   first time this command ran for real: their PRs were still based on
   stacked branches nobody had explicitly retargeted, so their
   squash-merges landed on those standalone branches instead of `main`,
   and only became part of `main` because a later, larger PR happened to
   already contain the same content. Retargeting explicitly, every time,
   before every merge, is what makes each entry's contribution actually
   show up as its own commit on `main` — the property this whole workflow
   exists to guarantee.

   **`--delete-branch` itself is the hazard for a stacked chain — not just
   manual deletion.** Before merging any entry, find every *open* PR based
   on its `headRefName` by querying GitHub directly, not just scanning this
   run's own list:
   `gh pr list --state open --json number,baseRefName --jq '.[] | select(.baseRefName == "<headRefName>") | .number'`.
   This catches a dependent PR that isn't part of this run at all — a
   teammate's PR, or one opened after this run's argument list was
   assembled — which a list-only check would miss and which would still be
   auto-closed by the branch deletion below. If that query returns any PR
   number:
   - Merge the current entry **without** `--delete-branch`:
     `gh pr merge <id> --squash --subject "<pr title>" --body "<pr body>"`.
   - Immediately after that merge succeeds — before doing anything else,
     and before moving to the next entry — retarget every PR number the
     query returned to `main` via the API
     (`gh api repos/<owner>/<repo>/pulls/<id> -X PATCH -f base=main`) and
     re-check each one's `mergeable`. For any that are *not* in this run's
     own list, note them in the final report (step 6) rather than merging
     them yourself — retargeting keeps them alive and correct; merging a PR
     nobody asked this run to merge is not this command's call to make.
   - Only then delete the just-merged branch, explicitly:
     `git push origin --delete <branch>`. It is safe now because no open
     PR anywhere still depends on it as a base.

   If the query returns nothing, `--delete-branch` on the merge call itself
   is fine — nothing depends on that branch surviving.

   The reason this matters: GitHub does not auto-retarget a dependent PR
   just because its base branch's content later lands on `main` through
   some other path. Deleting a branch while an open PR still points at it
   as `base` doesn't retarget that PR to wherever the content went — it
   auto-*closes* it, and a PR whose base branch is gone cannot be reopened
   or retargeted through the API at all (a hard GitHub limitation, not a
   bug to route around). Retargeting "on that entry's own turn" (this
   step, applied earlier in this same list) is too late for a stacked
   entry specifically *because* its own turn comes after the branch it's
   based on was already deleted as a side effect of merging the entry
   before it — by the time you'd retarget it, GitHub has already closed
   it. Pre-emptive retargeting, right after the dependency's merge and
   before its branch is deleted, is what actually prevents this.

   If a PR has already been auto-closed this way before you notice: don't
   try to reopen it. Open a fresh PR from the same, still-existing head
   branch, targeted at `main`; verify its `headRefOid` matches the closed
   PR's, so you know no code or review work was lost, review the diff for
   conflicts against the now-updated `main` (a stacked branch cut before
   its dependency merged will very likely show textual conflicts — usually
   cosmetic doc/status-marker duplication from both branches independently
   fixing the same lines — resolve, re-run the full test suite, and push
   before merging the fresh PR), and merge that instead.

   With the merge itself, always pass an explicit commit message:
   `gh pr merge <id> --squash [--delete-branch] --subject "<pr title>" --body "<pr body>"`
   (omit `--delete-branch` per the stacked-chain rule above; include it
   otherwise). Never omit `--subject`/`--body` and never use `--merge`.
   Individual commits on a feature branch may carry trailers or wording
   that violate the content style rules (they are working history, not the
   final record), so the default squash message (which can fold in raw
   commit messages) is not safe to use as-is. Always pass the PR's own
   already-cleaned title and body explicitly, so the commit that lands on
   `main` is exactly that text, never a concatenation of the branch's
   individual commits. If `--delete-branch` reports a local deletion
   failure (again, typically a worktree conflict) but `gh pr view` shows
   the PR as `MERGED`, the merge itself succeeded — separately delete the
   now-safe remote branch with `git push origin --delete <branch>`.

   After each successful merge, move to the next entry. Do not batch all
   the `gh pr merge` calls up front — each one can change the
   mergeability and the correct base of the next.

5. **Stop on the first failure.** If a merge fails (conflict, CI red,
   retarget didn't resolve cleanly), stop immediately. Report which PRs in
   the list merged successfully, which one failed and why, and which
   remaining entries were never attempted. Never force past a conflict and
   never skip ahead to a later entry — later entries may depend on the one
   that failed.

6. **Final report.** Once the given list completes (or stops early), report
   final status of every entry, then separately list any *other* open PRs
   in the repo that were not part of `$ARGUMENTS` — the goal is to make it
   obvious if the user forgot one, not to merge it for them. Before
   declaring success, confirm `main`'s first-parent history actually has
   one commit per entry in the given order
   (`git log origin/main --oneline --first-parent -N`, where N is the
   list length) — don't just trust that every `gh pr view` call reported
   `MERGED`, since step 4 exists precisely because that alone doesn't
   guarantee the commit landed where you think it did.
