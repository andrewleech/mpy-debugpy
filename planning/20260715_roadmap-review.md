# Roadmap review: verification of landed results, refinements applied

Date: 2026-07-15. HEAD `518388a3dd` (micropython `6863e938a1`, micropython-lib
`5f74950e9e`). Checkpoint review after phase 0 (STORY-8.6 + STORY-8.5),
STORY-3.3 and STORY-6.5 landed: four parallel verification passes (test gate,
fork/composition state, ROADMAP-vs-tree audit, upcoming-ticket drift), findings
verified before acting.

## What holds

Everything the planning docs record about the landed state checked out against
the tree, the fork and GitHub:

- Submodule pins at HEAD match the recorded SHAs exactly.
- Canonical branch tips match the fork on all of `pdb_support` (`3280c2a5e`),
  `local_names_implementation` (`7ae5f769c`), `mpy-debugpy-foundations`
  (`4fabcb3`), and both `mpy-debugpy` integration branches.
- PR #5 on andrewleech/micropython is open, base `pdb_support`, head exactly
  `7ae5f769c`.
- `mbm.toml` matches the CLAUDE.md composed-branch-set description; both
  submodules carry the required `upstream` remote.
- `make test` gate: 46 passed / 1 xfailed (on re-run; first run hit the known
  harness flake, below).
- Local `main` is 8 commits ahead of `origin/main` @ `59fab84` (Q7).

## Findings, and what was done about them

### Harness flake reproduced, mechanism pinned down

First `make test` run failed `test_epic1_readonly_locals_hint` with
`KeyError: 'variables'`: an asynchronous `stopped` event (seq 9) was appended
to `rcv_messages` immediately after the `variables` response (seq 8), and the
test reads `server.rcv_messages[-1]` after `wait_for_msg(response='variables')`
returns. Second run: 46 passed / 1 xfailed.

This generalises the race noted in the s6.5 ticket (there seen at startup as a
`stopped` event during `wait_for_client`): DAP events interleave with responses,
so any harness code that indexes `rcv_messages` positionally instead of using
the message object `wait_for_msg` returns is racy. That makes the fix
deterministic, not a timing tune — sweep the tests for positional reads. Scoped
to the s5.5 fix pass; risk-register row added.

### `add-debugpy-support` left un-reset after the s6.5 recompose (fixed)

The reset-to-fork-tips rule (phase-0 record) was applied to `pdb_support`,
`local_names_implementation` and `mpy-debugpy-foundations` but missed
micropython-lib's `add-debugpy-support`: after the s6.5 recompose it pointed at
the mbm-rebased copy (`7c27698`) instead of the canonical PR #1022 head
(`5d491e0`). Verified before resetting: `7c27698` is exactly the fork head's
five commits replayed onto the pinned target `68f8fe4` (identical subject
list), and that replay being the commit merged into the integration is correct
mbm behaviour — `pr_number` entries fetch `pull/N/head` live at every rebase
and never read the local branch, so composition correctness was never
affected. The raw `git diff` between the two (140 files) is a year of master
movement between their bases, not content divergence. Reset to `5d491e0`;
the rule applies to every branch mbm force-moves, `pr_number`-tracked ones
included (correction appended to the phase-0 record).

## Roadmap refinements applied (same commit as this note)

- Header line: `draft for review` → living plan in execution.
- Status section gains a bullet for this review.
- DONE annotations on the STORY-3.3 / 6.5 / 8.5 / 8.6 story bodies (their
  point-in-time present-tense text — notably s8.5's "Makefile integrate target
  currently stubs" — now carries a dated superseded marker instead of being
  rewritten) and in the tickets table.
- Execution sequence: steps 1–5 annotated DONE (step 2 except STORY-3.2, step 3
  except STORY-3.4), step 6 marked as the current frontier.
- Open questions: **Q7 (push-to-origin / CI iteration policy)** added as the
  first OPEN question since Q1–Q6 closed; the "all questions closed" line
  corrected. Blocks s3.2's Actions/Release path (and transitively s3.4's
  fetch-URL sections); s3.2's local dockerised build half is not blocked.
- Risk register: rows added for the harness interleaving race and for
  `launcher/firmware.py` `KNOWN_CAPABILITIES` being a hand-maintained mirror of
  `debugpy.get_capabilities()`.
- `00_index.md`: document-map row for this note; D1–D5 → D1–D6; Q7 noted open.

## Upcoming-ticket drift (input to phase-entry revalidation)

Recorded here rather than editing each ticket now; the phase-entry procedure
reads planning docs dated after each ticket's stamp. All tickets below are
stamped 2026-07-06, before the composition became mbm-rebuildable and before
s3.3/s6.5 landed reusable tooling.

- **s5.1**: registration of the new `do_debug` fork branch must be an mbm.toml
  local-only `/tree/` entry (mirroring `local_names_implementation`), not the
  ticket's hand-merge into the integration branch — a hand-merged commit is
  force-reset away on the next rebuild. EPIC-5 entry is the D6 trigger point
  for registering the mpremote primitive branches too, not s8.1. Not gated on
  Q7 (fork branch + local `make test`). Caveat found in ampremote's tree: its
  `mbm.toml` currently violates the "`ampremote_rename` last" invariant s5.1
  assumes (`mpr/fix_socket_rfc2217` #19062 sits after the rename entry) —
  resolve in ampremote before inserting the debug branch. The
  `mpremote_debug_command` branch does not exist on the fork yet (expected;
  s5.1 creates it).
- **s4.1**: its open question (register #18436 vs feature-detect) was already
  closed by D6 → register. First execution step is registering the ampremote
  `verify_hash` branch (#18436) into this repo's `mbm.toml` so
  `fs_writefile(verify_hash=True)` exists in the composed submodule; drop the
  feature-detect fallback.
- **s3.2**: needs a Prerequisites section — (1) Q7 gates the Actions/Release
  half only; the local dockerised build + sha256 recipe can proceed now, so
  split the acceptance criteria into those two sets; (2) s3.2 is the deliberate
  moment for the first `upstream/master` default-target rebase (moves every
  pin — phase-0 carried-forward item), including re-verifying device-port
  flag-injection anchors and the `extmod/select_poll_fd.py` baseline.
- **s3.4**: its capability-truth section is confirmed draftable now against the
  shipped `KNOWN_CAPABILITIES`/`capcheck` and a live unix MPDBG-READY caps
  dict, citing the phase-0 finding that `frame_f_locals` keeps the `local_NN`
  placeholder fallback (save_names=false ⇒ placeholders, not an empty dict).
  Only the variant table and fetch URLs wait on s3.2.
- **s5.2**: validate `mpdebug.toml` `requires` names against the single
  vocabulary shipped by s3.3 (`launcher/firmware.py` `KNOWN_CAPABILITIES` /
  `debugpy.get_capabilities()`), not the ticket's cited test-file assertion —
  the MPDBG-READY parser moved into `tests/fixtures/debuggee.py` and a third
  hand copy of the list would drift.
- **s5.3**: its resolve/ensure step should consume `firmware.py select` and
  `capcheck.check_capabilities` (built by s3.3 explicitly for this path)
  instead of the ticket's ad-hoc "validate requires" prose; re-stamp the
  `public_api.py` anchors at `5f74950e9e`.
- **s8.2**: re-stamp commit anchors to the recomposed `mpy-debugpy-foundations`
  SHAs (`c1e48b4` etc.); add the s6.5 statement-exec commit (`194d0ef` on the
  foundations branch) to the upstreaming scope; carry the s6.5 docstring
  snapshot-coherence tidy and the phase-0 `b2aff0c74` subject/filename nit.
