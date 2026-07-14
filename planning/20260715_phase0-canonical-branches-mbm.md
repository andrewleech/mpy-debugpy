# Phase 0 execution: canonical locals branch (STORY-8.6) + mbm-reproducible composition (STORY-8.5)

- Date: 2026-07-15
- Top-repo HEAD at execution: `6d91cca` (this note lands in the phase-0 commit)
- micropython submodule: `6863e938a1` (branch `mpy-debugpy`, recomposed this phase; was `18c76f7c5`)
- micropython-lib submodule: `d7b297dfd1` (branch `mpy-debugpy`, recomposed this phase; was `0d42fcd`)

Phase 0 (the two stories pulled forward by the 2026-07-06 decisions) is done. Both
stories ran as multi-agent workflows (implementation on sonnet, test batteries on
haiku, standard + adversarial review on opus, looped until clean), with the
contested design calls settled by the orchestrator between rounds. This note is
the progress record; the tickets (`tickets/s8.6_upstream-firmware-fixes.md`,
`tickets/s8.5_mbm-reproducible-composition.md`) carry the full execution outcomes
and the mbm 2.0.2 semantics reference.

## STORY-8.6: canonical locals branch

`andrewleech/local_names_implementation` was rebuilt from scratch as 7 commits on
`pdb_support` (#8767, `3280c2a5e`) and force-pushed (`2ff9f3cd8` -> `7ae5f769c`).
Fork PR #5 (base `pdb_support`) carries it. The stale gettrace/_getframe ancestry
is gone.

Final series: `e6b77bc6e` (Jos: local-variable tracing) -> `7d282e191` (name
preservation) -> `a54527a98` (bytecode persistence, off by default from birth) ->
`79a3b41f5` (f_locals slot-indexing fix) -> `b2aff0c74` (RAM-lookup regression
test) -> `ffe6670b6` (modernised locals tests) -> `7ae5f769c` (param-names
regression test). Every commit builds and passes every settrace test that exists
at that commit, `tools/verifygitlog.py` reports ok across the range, and the
four-way feature-macro matrix (SETTRACE alone / +LOCALNAMES / +PERSIST / both)
compiles.

The interesting part: the first workflow's adversarial reviewer proved a faithful
replay was achievable (byte-identical deltas, zero payload divergence), and the
test round then showed the faithful content itself was broken. Nobody had ever
run these firmware tests on the composed integration branch, the host `make test`
harness only exercises the DAP side. What that surfaced:

- Three of the misc locals tests fail as originally composed, a mix of stale
  `local_NN` index expectations (the code now produces real names) and dynamic
  CPython comparison breaking on Python 3.13+ (`FrameLocalsProxy`).
- `tests/basics/sys_settrace_localnames_comprehensive.py` fails its CPython
  comparison everywhere: MicroPython legitimately emits one extra `line` event
  entering a range-for loop (the loop's trailing test block has no line-table
  entry of its own and inherits the preceding statement's line). Reproduced on
  the unmodified pdb_support base, so it is a pre-existing tracer artifact, not
  a defect of the locals work. Fixed with a committed static `.exp`, the same
  treatment `sys_settrace_localnames.py` already had.
- A real sentinel bug: the code compared local names against `MP_QSTR_NULL`
  (the interned qstr for the literal string "NULL", a nonzero value) where
  `MP_QSTRnull` (index 0, what `m_new0` actually zero-fills the array with) was
  meant, in `py/emitglue.h`, `py/profile.c` and `py/persistentcode.c`. Unnamed
  slots leaked out under an invalid key instead of falling back to `local_NN`.
- A 276-line scratch harness (`test_local_names.py`) and a planning markdown
  were committed in-tree; both are now excised from the whole history.
- Two latent compile failures: PERSIST-only builds (struct fields guarded too
  narrowly in `py/emitglue.h`) and, after the restructure, SETTRACE-only builds
  (named-loop helper only declared under the name-tracking macros). Both fixed
  with widened `#if` guards.

One design call needed settling after the reviewers deadlocked on it: the first
implementation pass "improved" `frame_f_locals` to return an empty dict when no
name data is present. That reads sensible but would break variable inspection
for `.mpy`-loaded code on LOCALNAMES firmware (devices run cross-compiled `.mpy`
with no persisted names, and the tooling's capability story depends on the
`local_NN` placeholder fallback). Decision: keep the integration branch's
runtime fallback structure, take only the sentinel fix and the `#if` widenings.

The PERSIST adversarial build re-confirmed total line-number corruption (every
line reports as 1), so persistence stays off by default and the branch documents
why. A proper fix (skip-aware line-number reader) stays open in the s8.6 ticket,
it belongs with the s8.2/s8.3 upstreaming conversation.

### Divergence manifest (canonical branch vs old integration content)

The s8.6 ticket's original "recomposition must be content-identical" constraint
was superseded once the defects above turned up; the replacement contract is
this reviewed manifest. `tree(local_names_implementation)` differs from
`tree(18c76f7c5)` (old integration tip) in exactly these files, everything else
is upstream master movement:

| file | nature |
|------|--------|
| `py/emitglue.h` | sentinel `MP_QSTRnull`; struct-field `#if` widened to LOCALNAMES \|\| PERSIST |
| `py/profile.c` | sentinel; named-loop `#if` guard; comment wording (structure unchanged) |
| `py/persistentcode.c` | sentinel in save/load of local names (masked in file-level diffs by an unrelated upstream rename touching the same file) |
| `test_local_names.py` | deleted (scratch harness, excised from history) |
| `tests/basics/sys_settrace_localnames_comprehensive.py.exp` | new static `.exp` (pre-existing tracer/CPython divergence documented above) |
| `tests/misc/sys_settrace_locals.py` + `.exp` | modernised to real-name assertions; static `.exp` added |
| `tests/misc/sys_settrace_locals_edge_cases.py` + `.exp` | modernised; static `.exp` added |
| `tests/misc/sys_settrace_locals_simple.py` + `.exp` | modernised; `.exp` regenerated |
| `tests/misc/sys_settrace_locals_params.py` + `.exp` | new param-names regression test (mutation-checked) |

## STORY-8.5: mbm-reproducible composition

Both integration branches now rebuild from `mbm.toml` via `mbm rebase`. The
pinned-target proof runs (micropython onto `13303f8d2`, micropython-lib onto
`68f8fe4`, the same bases the hand-composition used) completed with zero
conflicts, and the equivalence checks came out exact:

- micropython `mpy-debugpy_update` vs old tip `18c76f7c5`: precisely the 13
  manifest files above, each byte-identical to the canonical branch; the
  composed tree as a whole is byte-identical to the rebased canonical branch.
- micropython-lib `mpy-debugpy_update` vs old tip `0d42fcd`: tree-identical.

Gates: `make firmware-unix` green, settrace suite 11/11, host DAP harness
exactly `13 passed, 1 xfailed`. Composed tips force-pushed to the fork
(`mpy-debugpy`: micropython `6863e938a1`, micropython-lib `d7b297dfd1`); the
old tips remain as `mpy-debugpy-pre-mbm` backup branches in each submodule.

Config shape (see `mbm.toml`): per submodule, one PR-number entry fetched live
from upstream at each rebase (`pdb_support` #8767, `add-debugpy-support` #1022)
plus one local-only entry (`local_names_implementation`,
`mpy-debugpy-foundations` @ `e359a95`, both pushed on the fork). The D6 mpremote
branches are deliberately not registered yet; they get added when the EPIC-4/5
ticket that needs them starts.

Operational rules learned from the mbm 2.0.2 source (full reference in the s8.5
ticket):

- Both submodules now carry an `upstream` remote, mbm hardcodes that name.
- Always `mbm rebase --local` (via `make integrate`); mbm's own push routing
  targets the upstream repo, not the fork. Fetch first, push by hand after
  verifying.
- The rebuild lands on `mpy-debugpy_update`; moving `mpy-debugpy` and pushing
  is manual.
- mbm force-moves the local feature branches to rebased versions as a side
  effect; reset them to the canonical fork tips after each run (done this
  phase: `local_names_implementation` @ `7ae5f769c`, `pdb_support` @
  `3280c2a5e`, `mpy-debugpy-foundations` @ `e359a95`).

## Carried forward

- First default-target (`upstream/master`) rebase moves every pin; do it
  deliberately alongside the CI firmware builds (s3.2), not as a side effect.
- The integration branches until this phase carried the sentinel bug and the
  silently-failing firmware tests; anything anchored on pre-recomposition
  behaviour of `frame_f_locals` edge cases should re-verify against
  `6863e938a1`.
- Commit-hygiene nits for the upstreaming pass (s8.2/s8.3), from the final
  review: `b2aff0c74`'s subject/filename mismatch (`_persist` test named as
  RAM-lookup regression) and its no-op Phase-2 stub.
- `extmod/select_poll_fd.py` fails on this master snapshot independent of our
  branches (proven on the unmodified base); expect it until the next master
  bump.
- Fork PR #5 body refreshed to describe the rebuilt branch.

## Correction (2026-07-15, roadmap review)

The reset-to-fork-tips rule above was applied incompletely: micropython-lib's
`add-debugpy-support` was left at the mbm-rebased copy (`7c27698`) after the
s6.5 recompose instead of the canonical PR #1022 head (`5d491e0`). Composition
correctness was never affected — `pr_number` entries fetch `pull/N/head` live
at every rebase and never read the local branch, and `7c27698` was verified to
be exactly the fork head's commits replayed onto the pinned target — but the
stale local branch misrepresents the PR state. Reset to `5d491e0`. The rule
applies to every branch mbm force-moves, `pr_number`-tracked ones included.
Full detail: `20260715_roadmap-review.md`.
