# Design note — Integration recomposed onto current upstream master

Date: 2026-07-06 · top repo @ `59fab8450a` · micropython @ `18c76f7c` ·
micropython-lib @ `0d42fcd`

Closes: the vendored-snapshot problem raised during migration ("I didn't want just
copies of Jos's code, I wanted mbm merged branches"). Referenced by ROADMAP.md D5.

## Decision

The canonical integration lineage is the **andrewleech branches composed onto current
upstream master**, published as `mpy-debugpy` branches on `andrewleech/micropython` and
`andrewleech/micropython-lib`. Jos's fork branches (`pdb_support_jos`, `debugpy/jos`)
are provenance/reference only. The prior vendored branches are preserved as
`mpy-debugpy-vendored-backup`.

## What was rejected and why

The first migration checked out Josverl's snapshot branches as-is. Those are frozen at
an old master (v1.26.0-preview era), so the "integration" would drift ever further from
upstream and could never become upstream PRs. That defeats the purpose of an mbm-based
integration repo.

## Composition (recorded in mbm.toml; hand-done, not yet `mbm rebase`-reproducible)

- `micropython:mpy-debugpy` = upstream master (`13303f8d2`) +
  **merge of #8767** (`pdb_support`: settrace / gettrace / `_getframe` / `f_trace` —
  merged conflict-free) + **cherry-picked locals commits** from
  `local_names_implementation` (`frame.f_locals`, LOCALNAMES capture) + fixes below.
- `micropython-lib:mpy-debugpy` = upstream master + **merge of #1022**
  (`add-debugpy-support` + enhancement layer) + **EPIC-1 foundations** (DAP framing
  fix, `wait_for_client`, capability probe, readOnly locals). Additive directory,
  clean merge.

Jos's extra commits on his branches were reviewed for cherry-picking and **all
skipped**: his settrace perf optimisation contained dead code and a reentrancy-unsafe
static locals cache, and the rest built on his separate `localnames.c` implementation,
incompatible with the `emitglue`-embedded approach carried by the andrewleech lineage.

**Correction (2026-07-06, ticket s8.2):** "all skipped" holds for the FIRMWARE side
only. On the micropython-lib side, the EPIC-1 foundation commits were based on Jos's
`debugpy/jos` tip, which is `add-debugpy-support` plus three of his commits (ruff
format, cleanup, Special fixup) — so those three ARE in the integration's ancestry.
The ruff-format commit is a rebase hazard for upstreaming against the #1022 lineage;
options are recorded in `tickets/s8.2_upstream-debugpy-foundations.md`.

## Bugs found and fixed during recomposition (on the integration branch)

1. **`frame.f_locals` slot indexing** (`33721cfe44`) — locals were reported under the
   wrong names; fixed to use correct `state[]` slot addressing.
2. **`MICROPY_PY_SYS_SETTRACE_LOCALNAMES_PERSIST` disabled by default**
   (`e5b5fa9a72`) — persisting names into `.mpy` corrupted line numbers for ALL
   compiled code. RAM-side name capture (LOCALNAMES) stays on.

Both fixes need to travel to the canonical branches — STORY-8.6.

## Behavioural facts established

- Real macro names: `MICROPY_PY_SYS_SETTRACE` + `MICROPY_PY_SYS_SETTRACE_LOCALNAMES`.
  The old lineage's `MICROPY_PY_SYS_SETTRACE_SAVE_NAMES` is a no-op on this branch.
- The unix `standard` variant enables both by default on this branch (via #8767's
  `mpconfigvariant.h`); no `CFLAGS_EXTRA` needed for `make firmware-unix`.
- The `line` trace event fires **before** the statement executes (correct CPython/pdb
  semantics; Jos's firmware fired it late). Verified empirically: at a breakpoint on
  `x = 78`, `f_locals` is `{}`; on the following line, `x` is present.
- `frame._set_local` / `sys._set_local_var` exist in no branch — readOnly locals
  gating (STORY-1.3/D4) remains correct.

## Verification

Fresh-clone reproducible: `git clone --recurse-submodules` → `make bootstrap` →
`make firmware-unix` → `make test` = 13 passed, 1 xfailed. micropython's own
settrace/localnames unit tests pass on the integration branch.

## Follow-ups

- STORY-8.5 — express the composition as mbm branch entries so `mbm rebase` rebuilds it.
- STORY-8.6 — upstream the two firmware fixes; answer Q6 (are function parameters
  still omitted from `f_locals` after the slot fix?).
- STORY-3.2 — replace the legacy prebuilt artifacts in `firmware/firmware.toml` with
  CI builds from the pinned integration SHA.
