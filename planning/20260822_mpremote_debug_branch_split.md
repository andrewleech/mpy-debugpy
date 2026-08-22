# The `mpremote debug` branch, split three ways and reviewed

- Date: 2026-08-22
- Top repo: `1436df3856`
- Pinned `micropython`: `b80cc3d585` · `micropython-lib`: `13bb67cb19`
- Base for all three branches: `5f2181f938` (upstream master)
- Superseded: `mpremote_debug` at `d9d9350047`, kept as tag `pre-split-20260821`

`/mpy-rules:review` over the 17-commit `mpremote_debug` branch returned 42 findings
(41 kept, 1 questionable, **0 invalid**), 11 of them blocking. Two were merge-stoppers
in the sense that matters here - MicroPython does not squash-merge, so the commit tree
submitted is the tree that lands:

- **`read_until` had lost its non-strict `timeout_overall` bound.** The base checked
  both bounds in the quiet branch; the branch moved the check behind a new
  `timeout_overall_strict` parameter, so at its default the parameter was dead and
  `enter_raw_repl(timeout_overall=10)` - which runs on every mpremote invocation - was
  unbounded. The docstring still described the old behaviour, which is why it read as
  correct.
- **`mpremote mount` was broken for seven commits.** The first commit reads
  `self.serial.timeout` outside the `is_pty` guard, and after `mount_local` that object
  is a `SerialIntercept` with no such attribute until the property arrives seven commits
  later.

## What the branch became

Three stacked branches, each based on the one before it:

| branch | commits | scope |
|---|---|---|
| `mpremote_transport_fixes` (`9ee81df4b7`) | 2 | `transport_serial.py` only; affects `mount`/`fs`/`mip`/`romfs` |
| `mpremote_debug_command` (`d36c5e6f78`) | +10 | the `debug` command over TCP |
| `mpremote_dap_repl` (`b80cc3d585`) | +1 | the REPL-multiplexed DAP channel |

17 commits became 13. The split is clean at file level - the transport branch touches
one file and nothing else - which is what made it worth doing before the fixes rather
than after: the fixes then land on the branch that owns them.

## The correction worth carrying

Finding 18 asked for the pty read timeout to be *clamped* rather than overwritten, so it
could not undercut `SerialIntercept`'s 5 s filesystem-RPC floor. Implemented literally
(`if self.is_pty and saved_timeout is None`) it broke two mount teardown tests: a mounted
pty's polling reads went from 0.1 s to 5 s, so the pump missed its stop event and the
session reported `did not stop in time`.

The finding was right about the hazard and wrong about where to fix it. A polling read
*should* be short - that is what lets the loop notice its own deadline and a stopping
pump notice its event. What must not be short is the multi-read RPC exchange. The floor
now goes around the exchange, in `_check_input`, and the poll is lowered as before:

```python
self.rpc_timeout = 5.0          # __init__
...
poll_timeout = self.orig_serial.timeout
self.orig_serial.timeout = self.rpc_timeout
try:
    ...                          # command byte, ack, dispatch
finally:
    self.orig_serial.timeout = poll_timeout
```

Generalise it: when a review says "do not let X undercut Y", check whether X and Y are
the same operation before constraining X. Here they were two, and constraining the
wrong one traded a latent fault for a live one.

## Two findings that were wrong on the facts, and what they showed instead

- **"Remove `escape()`, it has no caller."** True inside the package, and the wrong
  conclusion. It is the host-side mirror of the device's `repl_mux.escape`, and the
  wrapper repo's drift test feeds identical bytes to both. Documented rather than
  deleted.
- **"Remove the `# fmt: off` pair, the file already satisfies `ruff format`."** True,
  but the exemption's stated reason was that the file is vendored byte-for-byte between
  repos with different ruff settings. No such copy exists in `micropython-lib` - the
  copy is `launcher/mpy_launch_debugpy.py` in *this* repo, pinned by a byte-identity
  test. So the marker guarded a real relationship, misdescribed, and one an upstream
  branch cannot be asked to carry. Removed, and the wrapper copy is resynced instead.

## The CLI change

`--target`/`-t` is an option; `module[:method]` is the only positional. This exists for
one shape: `mpremote connect <device> debug app:main` previously tried to open a device
named `app:main`. Resolution order is now `--target`, then a preceding `connect`, then
the sole target in `mpdebug.toml`.

It buys less than it looks. `main.py`'s `next_command` is `nargs=REMAINDER`, so once a
positional is consumed everything after it is the next chained command:
`mpremote debug app:main -t /dev/ttyACM0` still fails. The win is the chained form, not
free option placement, and the `--help` epilog says so.

## Per-commit invariants, and how they are checked

Prose about per-commit hygiene does not survive a rebase; these are mechanical and were
re-run after every fold:

- `ruff format --check` over a clean `git archive` extract of each commit - not a
  working-tree checkout, which leaves files from commits that do not contain them and
  reports them against the wrong commit.
- `import mpremote.main, mpremote.commands` at each commit.
- **Every `--flag` documented in `docs/reference/mpremote.rst`'s debug section exists in
  that commit's own `argparse_debug()`, and every flag it defines is documented.** This
  is what forced the docs to be split across seven commits rather than written once at
  the tip, and it caught two real gaps: `--dap-log` declared-but-undocumented in four
  commits (it errors "not implemented yet" there, which is worth saying), and a note
  naming `--dap-log-file` three commits before that flag exists.
- `tools/mpremote/tests/test_mount.sh` on the bench PYBD-SF6 at **all 13 commits**. This
  is the check the history fold exists to satisfy, so it is the one that must be run per
  commit rather than at the tip.

## Tests, where there were none

Nothing in this diff was gated by mpremote's own harness. Two files now are, both
device-free so they run in the same harness without a board:

- `test_transport_timeouts.sh` (transport branch) - `read_until`'s quiet and strict
  bounds over a pty pair, and `SerialIntercept`'s timeout round-trip.
- `test_debug_units.sh` (DAP branch) - the `MPDBG-READY` parser and the REPL-stream
  demux, including byte-at-a-time feeding, which is the case its incremental state
  machine exists for.

Writing the first one surfaced that the obvious assertion is wrong: a non-strict
`read_until` against a peer that never goes quiet is *deliberately* unbounded, because
that is what keeps a verbose board's boot printing intact. The test hung until it was
written to match the semantics rather than the intuition.

## Verification

Host suite 390 passed / 19 skipped / 1 xfailed; extension 98 passed; `make lint` and
`make lint-submodules` clean; per-commit format, import and doc/flag checks clean across
all 13; `test_mount.sh` green at every commit on hardware; full HIL suite 17/17 on the
bench PYBD-SF6, including the five `repl_dap` scenarios that exercise the new bounded
console sink.

The HIL run used a throwaway merge of the stack onto the integration branch, because
`debugpy_install` lives there and these branches are based on bare upstream master. The
tracked record `20260821_hil_PYBD_SF6.md` therefore still describes the pre-split tree;
a real re-record needs the integration branch rebuilt (see below).

## Left open

- **The integration branch is not rebuilt.** `mbm.toml` now registers the three branches
  in dependency order in place of `mpremote_debug`, and `mbm config -s micropython` reads
  them, but `make integrate` has not been run. Until it is, `mpy-debugpy` still merges
  the superseded branch. Rebuilding moves the submodule pin, so it is a deliberate step,
  not a tidy-up.
- **Fork PR #51** still has head `mpremote_debug` (the superseded branch) into
  `review/mpremote_debug`. Three stacked branches want either three stacked PRs or a
  replacement of #51; either is outward-facing.
- `Claude-Session` trailers were dropped from all 13 commits rather than resolved to a
  URL: the placeholders carried no information, the work spans several sessions so any
  single id would be fabricated provenance, and upstream micropython has no such
  convention - disclosure belongs in the PR body's Generative AI section.
