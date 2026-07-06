# mpy-debugpy contributor notes

An [mbm](https://github.com/andrewleech/micropython-branch-manager)-managed
integration of the MicroPython debug-support work: `sys.settrace`/frame
inspection in `micropython`, and the `debugpy` DAP server in `micropython-lib`.
The integration branch in both submodules is `mpy-debugpy`; the composed branch
set lives in `mbm.toml`.

## Where a change belongs

Default to upstream. The end state is:

- debugpy DAP server → `micropython-lib` (`python-ecosys/debugpy`), upstream
  PR #1022 lineage.
- settrace / frame locals / `_set_local` → `micropython`, upstream PR #8767
  lineage plus the local-variable work.
- orchestration (`mpremote debug`) → the mpremote source tree, delivered via the
  [ampremote](https://github.com/andrewleech/ampremote) mbm integration, then an
  upstream micropython PR.
- VS Code convenience → a thin extension, built last.

This repo holds only what has no upstream home yet: the integration spec, the
roadmap/planning, the host-side test harness, samples, and the firmware manifest.
As each piece is upstreamed, it should leave this repo.

## Working with the integration

```bash
mbm config -s micropython           # show the composed branch set
mbm add-pr <N> -s <submodule>       # add an upstream PR to an integration branch
mbm rebase -s <submodule>           # rebuild the integration on latest upstream
```

`git rerere` records conflict resolutions so rebuilds replay them. After a
manual conflict resolution during `add-pr`, fast-forward the integration branch
to the working tip (see the ampremote CLAUDE.md for the exact sequence).

### Composing the debug branches (first integration task)

`mbm.toml` starts with both submodules registered and no branches. Populate it:

- `micropython-lib`: PR #1022 (base debugpy), then the local `debugpy`
  foundations branch (EPIC-1 work), then Jos's follow-up updates.
- `micropython`: PR #8767 (settrace), then the local-variable branches. Note
  #8767 is old relative to master; expect conflicts and record them via rerere.

The exact fork/branch refs for the local branches depend on where they are
pushed (see planning/ROADMAP.md). Register local-only branches by hand in
`mbm.toml` with `pr_url` pointing at the fork branch, as ampremote does.

## Firmware

Binaries are not committed. `firmware/firmware.toml` records provenance (source
commit, build flags, artifact sha256) and `launcher/firmware.py` verifies/fetches.
Do not trust a variant name for capabilities — the runtime probe in the debugpy
server (`caps` in the MPDBG-READY handshake) is the source of truth.

## Tests

`make test` runs the host-side DAP harness (a fake VS Code client drives a real
session against a built unix firmware). Tests must stay machine-independent (no
hardcoded absolute paths) and drive real behaviour, not source-string checks.

## Planning

**Entry point: `planning/00_index.md`** — the folder's operating manual (document
map, conventions, phase-entry ticket-revalidation procedure, execution model,
ticket template). Then `planning/ROADMAP.md` (epics/stories + status +
open-questions table + risk register), `planning/BACKGROUND.md` (architecture,
commit-anchored), `planning/SPIKE-*.md` / `planning/YYYYMMDD_*.md` (decision
records / research reports), and `planning/tickets/` (per-work-item execution
briefs). Tracked here deliberately — keep them current as work lands.

Capture discipline (per the phased-roadmap procedure):

- New research/design documents go in `planning/` as `YYYYMMDD_<topic>.md` with a
  header carrying the date, the top-repo HEAD SHA, and the relevant submodule SHAs.
- The roadmap's Status, open-questions table (Q1..Qn), and risk register are updated
  in place, never forked. A question closes with a dated DECIDED entry pointing at
  the design note that resolved it; rows are never deleted.
- Decision records are append-only: add dated corrections, don't rewrite conclusions.

## Python tooling

Use `uv` with PEP 723 inline deps so scripts run via `uv run script.py`.

## Session attribution

End commit messages with the Claude-Session trailer and PR bodies with the
session link, per the global convention.
