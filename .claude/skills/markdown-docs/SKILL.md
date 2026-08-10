---
name: markdown-docs
description: Use whenever creating a new Markdown (.md) file in this repo, or substantially editing an existing one (specs, plan/step docs, design docs, READMEs). Enforces the house doc style established by installer/plan/*.md — owner-tagged titles, numbered cross-linked sections, LOCKED/RESOLVED decision markers, pinned-value tables, and a closing References section — so every doc in the repo stays consistent and legible. Not needed for one-line edits to existing docs that don't touch structure.
---

# Markdown documentation style

This repo's reference style is the spec docs under
[`installer/plan/`](../../installer/plan/) —
[`00-FRAMEWORK-AND-BOOTSTRAP.md`](../../installer/plan/00-FRAMEWORK-AND-BOOTSTRAP.md)
is the fullest example, [`STEP-1-PREREQUISITES.md`](../../installer/plan/STEP-1-PREREQUISITES.md)
and [`STEP-3-AMC-LAUNCHER.md`](../../installer/plan/STEP-3-AMC-LAUNCHER.md) are
good secondary references. These are precise, cross-linked engineering specs,
not narrative guides — match that register. Skim one before writing if you
haven't recently.

## Checklist (apply in order)

1. **Title** — single `#` H1: `<Number or Step name> — <Title> (owner: <Dev>)`,
   e.g. `# Step 3 — AutoMagicCalib launcher (owner: DevC)` or
   `# 00 — Installer Framework and Bootstrap (owner: DevA)`. Omit the
   `(owner: ...)` tag only if the doc genuinely has no single owner. One H1
   per file.
2. **`Status:` line immediately after the title** — one short paragraph
   stating what kind of doc this is (`shared foundation`, `step spec`, ...),
   which doc(s) it depends on, and the non-duplication rule: "does **not**
   restate X — link back to it" when a shared contract lives elsewhere.
3. **1–3 orienting paragraphs** after the status line: what this doc
   specifies, what it supersedes/ports (name the file), and where its facts
   come from if externally sourced (e.g. "cross-checked against Context7
   library `...`"). Bold the one or two terms that carry the doc's central
   constraint (`**single self-contained installer binary**`,
   `**equality pins**`).
4. **`---` horizontal rule** between the title block and the first `##`
   section, and between every top-level `##` section thereafter. Do not
   place `---` between a section and its `###` subsections.
5. **Sequentially numbered `##` sections** — `## 1. Title`, `## 2. Title`, …
   running the full length of the doc (never restart numbering). `###`
   subsections carry the parent number: `### 3.1`, `### 3.2`. Section 1 is
   almost always scope/identity (`Module identity`, `Scope`). The final
   section is always `## References`, unnumbered.
6. **Cross-doc links use the `<doc-id> §N` shorthand** — link text is the
   short doc id (`00`, `STEP-1`, or the doc's own number) plus a section
   symbol and number; the href is the relative path plus the GitHub heading
   anchor: `` [`00` §12](00-FRAMEWORK-AND-BOOTSTRAP.md#12-step-module-interface-the-contract-for-steps-15) ``.
   Anchors are the standard GitHub slug of the heading text (lowercase,
   spaces → hyphens, punctuation stripped). Link on every reference to a
   contract defined elsewhere, not just the first mention — these docs are
   read section-by-section, not top-to-bottom.
7. **Decision state markers, bolded and named**: `**LOCKED**` for a settled
   design choice stated in a heading or first sentence of a section,
   `**RESOLVED**` for an open question that got answered (say what resolved
   it and cite the source), `**REQUIRED**` for a contract every consumer
   must honor exactly. Don't leave a decision unlabeled if it was actually
   contested.
8. **Tables for every pinned value, precondition, or config-key set** —
   header row required, one row per item, terse cells (inline code for
   commands/paths/versions, not prose). This is the default representation
   for anything with more than 2 columns' worth of structured facts (see the
   prerequisite-pin table in `STEP-1-PREREQUISITES.md` §2).
9. **Fenced code blocks with a language tag** for every command, config
   file, JSON schema, or Python interface stub (` ```bash `, ` ```json `,
   ` ```python `). Use a bare ` ``` ` block (no language) only for literal
   required output/log strings the reader should match verbatim. Prefer one
   logical command group per block.
10. **Blockquote `>` callouts** for asides that would otherwise interrupt the
    numbered flow — escalation rules, flagged/optional decisions, first-run
    caveats. Lead with a bold tag: `> **Escalation rule (...):**`,
    `> **Optional self-heal (flagged decision, §9):**`.
11. **Ordered lists with a bold lead-in phrase per item** for any "do these
    in order" sequence (`1. **Preflight**: ...`, `2. **Base deps**: ...`) —
    this is the standard way these docs express procedure, not prose
    paragraphs.
12. **`- [ ]` checklists only for a "developer quick-reference" verify
    section** near the end of a step doc — one checkbox per condition that
    must hold for the step to be `COMPLETE`.
13. **Closing sections, in this order, only the ones that apply**: an
    `## N. Out of scope` / `## N. Out of scope / open decisions` section
    (bullets for settled exclusions, a separate numbered sub-list for open
    decisions each tagged RESOLVED/flagged as in rule 7), then optionally
    `## N. Documented drift from <file>` if this doc changes prior behavior,
    then always `## References` last.
14. **`## References` structure**: a short paragraph naming the authority
    (e.g. "DeepStream 9.0 official documentation... cross-checked via
    Context7 library `...`"), then a bullet list of external links each with
    a bold or parenthetical description of what fact it backs, then a
    `Repo files referenced:` sub-list of every file linked earlier in the
    doc with a one-line note on why it's relevant.
15. **Inline code formatting** for every path, script/module name, env var,
    flag, command, and exact required string — no bare `path/to/file` or
    `--flag` in prose.
16. **Hard-wrap prose lines around 78–80 columns**, matching the source docs.
    Don't hard-wrap table rows or code blocks.
17. **No emoji, no decorative Unicode.** Em dashes are the standard
    appositive/aside punctuation in this style — use them freely in prose.

## Before writing

- Grep `installer/plan/` (and the target doc's own prior section) for an
  existing contract covering the same ground before restating it — link
  back with the `<doc-id> §N` shorthand (rule 6) instead of duplicating.
- Identify the doc's owner (a dev tag or "shared foundation") and its single
  audience before writing — these are specs for implementers, not operator
  walkthroughs; don't drift into narrative "here's how to run this" prose.
- If the doc ports or supersedes existing logic (a script, an earlier doc),
  name the exact file and, where useful, line numbers, so the reader can
  diff intent against the original.

## Self-check before finishing

- Every `##` section is numbered and sequential, `###` subsections carry the
  parent number, and `---` separates every top-level section.
- Every reference to a contract defined elsewhere is a `<doc-id> §N` link
  with a working relative path + anchor.
- Every settled design choice is labeled LOCKED/RESOLVED/REQUIRED where the
  doc actually made a decision; every genuinely open question is called out
  explicitly rather than implied.
- The doc ends with `## References` containing both external links (with
  what fact each backs) and a `Repo files referenced:` list.
