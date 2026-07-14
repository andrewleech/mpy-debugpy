# planning/ — index and operating instructions

This folder is self-describing: everything needed to action the roadmap and its
tickets lives here. A fresh agent (new session, different machine, a plain workflow
subagent) starts at this file and needs nothing else.

Reading order: this index → `../CLAUDE.md` (repo design rules) → `ROADMAP.md` →
the current phase/epic → its tickets under `tickets/`.

## Document map

| file | what it is |
|------|------------|
| `00_index.md` | this file — conventions + phase-entry procedure |
| `ROADMAP.md` | the living roadmap: epics/stories, status, settled decisions D1–D6, questions Q1–Q7 (all closed), risk register, dependency-ordered execution sequence |
| `BACKGROUND.md` | architecture orientation, commit-anchored to the ORIGINAL Josverl tree (`05b7818`); superseded facts are marked inline — trust the anchor notes |
| `SPIKE-2.1-mount.md` | decision record: mount works under settrace (D2); source of STORY-4.5/6.5 |
| `SPIKE-2.2-serial-dap.md` | decision record: serial DAP is board-dependent (D3); retains the single-UART framing detail deferred by Q3 |
| `20260706_recomposition.md` | design note: canonical lineage = andrewleech branches composed onto current upstream master (D5) |
| `20260706_flocals_params.md` | trace: params are NOT omitted from `f_locals` on the current branch (closes Q6) |
| `20260706_vscode-extension-direction.md` | design note: VS Code extension fan-out — positioning (build new), decided questions (mbm-adopt overlaps, broker in mpremote, stubs best-practices, mpflash, mpbuild), feature set; seeds a future `vscode/ROADMAP.md` |
| `20260715_phase0-canonical-branches-mbm.md` | progress record: phase-0 execution (STORY-8.6 canonical locals branch + STORY-8.5 mbm composition), the defects it surfaced, the divergence manifest, mbm operational rules |
| `20260715_roadmap-review.md` | checkpoint review: landed results verified against tree/fork/GitHub, harness-flake mechanism, `add-debugpy-support` reset correction, Q7 opened, upcoming-ticket drift notes for phase-entry revalidation |
| `tickets/s<epic>.<story>_<slug>.md` | one self-contained execution brief per substantive work item, all phases, written upfront at peak knowledge |

## Conventions

- Every document is stamped with its date and the top-repo HEAD SHA
  (`git rev-parse --short=10 HEAD`), plus the relevant submodule SHAs when it
  anchors into a submodule. New research/design docs: `YYYYMMDD_<topic>.md`.
- Tickets carry an immutable `Written:` stamp and an append-only `Revalidated:`
  history — never overwrite the Written stamp.
- The open-questions table uses Qk numbering; questions close with dated DECIDED
  entries and are never deleted.
- `ROADMAP.md` is updated in place, never forked. Each executed phase writes its
  progress and learnings back here as `YYYYMMDD_<topic>.md`.
- Decision records (spikes, design notes) are append-only: add dated corrections,
  do not rewrite conclusions.
- Trivial work items (STORY-7.3, STORY-8.3) have no ticket; their roadmap entry is
  the full brief.

## Phase-entry procedure (mandatory, before authoring a phase's workflow)

Revalidate each of the phase's tickets:

1. **Code drift**: `git log --oneline <ticket SHA>..HEAD` for what landed since,
   and `git diff <ticket SHA>..HEAD -- <the ticket's anchored files>` for exactly
   how the anchors moved. For submodule anchors, run the same against the
   submodule SHAs recorded in the ticket stamp. Re-resolve every file:line
   reference against the current tree.
2. **Knowledge drift**: read planning documents dated after the ticket's Written
   stamp, any Qk decisions closed or reopened since, and completed phases'
   progress reports.
3. **Update the ticket in place**: refresh anchors, adjust scope/approach/tests,
   then append a `Revalidated:` line (date + new HEAD SHA + one line on what
   changed, or "no drift"). Drift big enough to reshape a ticket is a roadmap
   update, not a silent rewrite.

Only tickets whose latest Revalidated SHA is the current HEAD feed a workflow.

## Execution model

Each phase's revalidated tickets are the input to a dynamic multi-agent workflow.
Default coding tiering: **implementation → sonnet, test authoring/running → haiku,
standard + adversarial review → opus**, looped (review findings → implementer
fixes → re-test → re-review) until reviews are clean and tests pass. A ticket's
own "Workflow shape" section overrides the default when present.

## Ticket template

```
# <component>: <imperative one-line title>

Phase: EPIC-N / STORY-N.M
Depends on: <other tickets / phases / external prerequisites>
Written: <YYYY-MM-DD> at HEAD <short SHA> (micropython <sha>, micropython-lib <sha>)
Revalidated: <YYYY-MM-DD> at HEAD <short SHA> - <what changed / "no drift">

## Context
Why this exists; link design notes/reports and the Qk/Dk decisions that constrain it.

## Scope
In scope: the exact change. Out of scope: what is deferred, and to where.

## Files and anchors
Concrete files and file:line references the implementer touches or must respect,
with the behaviour at each anchor that matters.

## Design constraints
Settled rules and decisions that bind this item; what must not break.

## Approach sketch
The direction and the non-obvious choices already made during planning.

## Acceptance criteria and tests
The checks that prove done, including adversarial ones and hardware needed.

## Workflow shape
Model-tiered decomposition, or "runs inside the phase's larger workflow".

## Open questions
Anything genuinely undecided, so the planner surfaces it rather than guessing.
```
