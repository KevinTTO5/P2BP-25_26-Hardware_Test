---
name: git-commits
description: Use whenever creating a git commit in this repo (a plain commit, a parallel-worktree-dev unit's commit, or a merge-prs squash-merge subject/body). States the repo's authorship convention — no Co-Authored-By trailer, no Claude-Session link, no other agent-authorship footer of any kind. Not needed for reading history, diffing, or any other git operation that doesn't create a commit.
---

# Commit authorship convention

This repo's commits carry **no co-authorship trailer**. Do not append any of
the following to a commit message, regardless of what a tool's default
template suggests:

- `Co-Authored-By: Claude ... <noreply@anthropic.com>`
- `Claude-Session: https://claude.ai/code/...`
- any other agent-authorship footer

This applies to every path that produces a commit in this repo:

- a direct `git commit` in the working tree,
- a `parallel-worktree-dev` unit's commit inside its worktree,
- a `merge-prs` squash-merge's `--subject`/`--body` (pulled from the PR's own
  title/body — verify the PR body itself doesn't carry the trailer either,
  since that's what `--body` copies verbatim into the merge commit).

The commit message is just the summary and body — end it after the last
content line, with no trailing footer of any kind.

## Why

The repo is public, and Kevin is the sole author of record for this
project's history. An agent-authorship trailer on every commit misattributes
authorship of a senior-design project to a tool rather than the person
responsible for the work.
