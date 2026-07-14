# mpy-debugpy — Roadmap to a user-friendly, upstream-mergeable debugging experience

Status: draft for review. This document records both the reasoning and the plan. The
plan is expressed as epics and stories so it can drive implementation and investigation
workflows (some automated, some manual). Read the "Background research" section first if
you want the why; jump to "Epics and stories" for the what.

Target end state: a MicroPython user can debug a script with breakpoints, stepping and
variable inspection across three transports — (1) the unix port, (2) a device over
mpremote serial, (3) a device over the network — driven from a single mental model, with
no hand-maintained IPs/ports, no stale device files, no sleep-based breakpoint race, and
a firmware story that is honest about what each build supports. The orchestration ships as
an `mpremote debug` command (delivered through the `ampremote` integration, ultimately an
upstream micropython PR), with a thin VS Code extension layered on top last.

---

## Status (living section)

Updated as work lands. See per-story acceptance criteria below for detail.

- **Phase 0 DONE (2026-07-15): STORY-8.6 + STORY-8.5.** The canonical locals
  branch `andrewleech/local_names_implementation` is rebuilt as 7 bisect-clean
  commits on `pdb_support` (`2ff9f3cd8` -> `7ae5f769c`, fork PR #5), carrying
  the two integration fixes, the new param-names regression test, and repairs
  for defects the recomposition never surfaced because the firmware tests had
  never been run: a real qstr sentinel bug (`MP_QSTR_NULL` vs `MP_QSTRnull`),
  three locals tests failing on stale expectations / CPython 3.13+ divergence,
  a pre-existing one-extra-line-event tracer artifact on for-loops (now
  documented via static `.exp`), in-tree scratch files, and two latent
  feature-macro compile failures. `frame_f_locals` keeps the `local_NN`
  fallback for code without name data (load-bearing for `.mpy` device
  debugging). Both integration branches are now mbm-rebuildable from
  `mbm.toml` (`make integrate`; always `--local`, pushes stay manual):
  pinned-target proof runs had zero conflicts, micropython recomposed to
  `6863e938a1` (differs from the old tip by exactly the reviewed 13-file
  divergence manifest), micropython-lib to `d7b297dfd1` (tree-identical to the
  old tip); `make test` gate exactly 13 passed / 1 xfailed. Full record:
  `20260715_phase0-canonical-branches-mbm.md`.
- **STORY-3.3 DONE (2026-07-15).** Legacy manifest entries deprecated (provenance
  kept, excluded from selection; collapsed per-board entries land with s3.2),
  `launcher/firmware.py select` resolves required capabilities against manifest
  intent, and `launcher/capcheck.py` hard-errors any claimed-true-but-probed-false
  key against the real MPDBG-READY handshake. Host suite 39 passed / 1 xfailed
  (26 new tests). s3.2 is blocked on the push-to-origin policy call (CI needs
  the repo pushed to iterate Actions); s3.4 waits on s3.2's Release URLs.
- **Integration recomposed onto current upstream master (2026-07-06).** The first
  migration into this repo vendored Josverl snapshot branches frozen at an old master;
  that was rejected and redone. Both submodule integration branches are now composed
  from the andrewleech lineage onto current upstream master: `micropython` @
  `mpy-debugpy` (`18c76f7c`) = master + #8767 (merged) + cherry-picked locals commits +
  two integration bug fixes; `micropython-lib` @ `mpy-debugpy` (`0d42fcd`) = master +
  #1022 + enhancements + EPIC-1 foundations. Jos's extra FIRMWARE commits were reviewed
  and all skipped (dead code / incompatible with the emitglue-embedded locals approach);
  on the LIB side three of his commits (ruff format, cleanup, Special fixup) are in the
  integration's ancestry via the EPIC-1 base — the ruff-format one is a rebase hazard
  for upstreaming, handled in ticket s8.2. See
  `20260706_recomposition.md` for the decision record. Composition is hand-done
  (merge + cherry-pick), not yet `mbm rebase`-reproducible — that is STORY-8.5.
- **Firmware facts corrected during recomposition** (supersede some constraint notes
  below and parts of BACKGROUND.md): the real name-capture macro is
  `MICROPY_PY_SYS_SETTRACE_LOCALNAMES` (`_SAVE_NAMES` is the old lineage's spelling and
  a no-op on the current branch); the unix `standard` variant on the integration branch
  enables settrace + LOCALNAMES by default (no CFLAGS_EXTRA needed); the `line` trace
  event fires BEFORE the statement executes (correct CPython semantics — tests must
  break after an assignment to observe the variable); two real firmware bugs were found
  and fixed on the integration branch (`f_locals` slot indexing reporting variables
  under wrong names; `MICROPY_PY_SYS_SETTRACE_LOCALNAMES_PERSIST` corrupting line
  numbers for all compiled code — now off by default). Upstreaming these fixes to the
  canonical PR branches is STORY-8.6.
- **EPIC-1 (foundations): DONE and verified.** STORY-1.1/1.2/1.3/1.4 implemented in the
  debugpy module + unified `launcher/mpy_launch_debugpy.py`; STORY-1.5 host tests green
  (13 passed, 1 pre-existing unrelated xfail) with behavioural coverage. A real framing bug
  in `common/messaging.py:recv_message` (header stripped before the body arrived →
  desynchronised DAP framing → breakpoints never fired) was found and fixed during review;
  this, not the handler, was why `configurationDone` appeared "unprocessed". Review also
  fixed: leftover debug prints, an ESP32 network-probe crash in the launcher, a `port: 0`
  handshake, and an empty-`f_locals` `save_names` false-positive. Committed and
  published on `andrewleech/micropython-lib` @ `mpy-debugpy` (`0d42fcd`).
- **STORY-3.1 (firmware manifest): DONE.** `firmware/firmware.toml` + `launcher/firmware.py`
  (`list`/`verify`/`fetch`). Found provenance drift — the prebuilt binaries are NOT all from
  the pinned submodule SHA (only `unix_settrace_local_xx` matched the then-pinned `05b7818`);
  download URLs are placeholders pending a CI release step. Reinforces STORY-1.2 (probe,
  don't trust names). NOTE: after the 2026-07-06 recomposition all four manifest entries
  describe legacy pre-recomposition artifacts; the manifest is a provenance record until
  STORY-3.2's CI builds replace them from the pinned integration SHA.
- **EPIC-2 spikes: BOTH RESOLVED (see `SPIKE-2.1-mount.md`, `SPIKE-2.2-serial-dap.md`).**
  - STORY-2.1 (mount under settrace): **CONDITIONAL YES.** Mount's fs-RPC is serviced by the
    host follow-loop regardless of pause state; a paused debuggee issues no filesystem calls,
    so mount + network-DAP coexist. Corrected limitation: mount + `sys.modules` eviction /
    re-import gives a no-upload edit→re-import iteration loop (surfaced STORY-4.5); only an
    already-paused frame can't be live-edited. **Effect:** EPIC-4's heavy sync engine is the
    fallback, not the mainline.
  - STORY-2.2 (DAP over serial): **CONDITIONAL / board-dependent.** Dual-USB-CDC boards can
    give DAP its own channel (yes); single-UART boards (current ESP32 targets) would need an
    unbuilt framing layer (no). **Effect:** network stays the primary device transport; serial
    DAP is a probed multi-CDC subset. Unix gets a dedicated pipe fd for free.
- **Net re-scope from the spikes:** device mainline = **network DAP + `mpremote mount` for
  source + re-import to iterate**; EPIC-4.2/4.3 (hash sync + staleness guard) demoted to the
  single-UART/no-mount fallback and unix. New follow-ups: STORY-4.5, STORY-6.5.

---

## Background research

### The current friction (from the wrapper repo)

To debug one file today a user hand-orchestrates: pick one of four prebuilt firmware
variants via an `MP_DEBUG_VARIANT` env var in `tasks.json`; set `MICROPYPATH` so `src/`
and the debugpy module resolve; run one of two near-duplicate launcher scripts
(`launcher/mpy_launch_debugpy_{unix,esp32}.py`) that call `listen()` → `breakpoint()` →
`debug_this_thread()` → `sleep(2)` → import+call a target; pick one of nine `launch.json`
attach configs, several with hardcoded IPs (`192.168.137.117`, `my-esp32.local`) and
inconsistent ports (5678 vs 5679); and for devices run `compile_debugpy.py` → `mpremote
mip install` → `mpremote cp -r src/ :/` on every run, re-doing everything with no staleness
detection. Config is scattered across `launch.json`, `tasks.json`, `debugpy.json`,
`debugpy_mpy.json`, and the two launcher scripts.

### Hard constraints carried from the firmware findings (see BACKGROUND.md)

- The debugpy server has no background thread. All DAP handling is piggybacked on the
  `sys.settrace` callback; while paused it busy-polls `sleep(0.01)`. Any design has to fit
  this single-threaded, poll-driven model. There is no second socket, no async.
- `frame._set_local` / `sys._set_local_var` exist in **no** branch of either lineage
  (re-confirmed on the recomposed integration, micropython @ `18c76f7c`). setVariable on
  locals degrades to an error; only global edits work. Local editing must not be promised
  in UX; the readOnly presentation hint (STORY-1.3) covers it.
- Variant *names* encode capabilities that the firmware may not have (e.g.
  `unix_settrace_set_local` against firmware with no `_set_local`). Capability must be
  probed at runtime, not inferred from a directory name.
- `f_locals` is a fresh snapshot dict per access, not a live view. `f_back` and
  `f_globals` are live. Name capture is `MICROPY_PY_SYS_SETTRACE_LOCALNAMES` on the
  current branch (the older lineage's `_SAVE_NAMES` spelling is a no-op there); without
  it, names are positional placeholders `local_N`. Function parameters are present in
  `f_locals` under their real names on the current branch — the old omission is gone
  (Q6 DECIDED, verified empirically; `20260706_flocals_params.md`).
- The `line` trace event fires BEFORE the statement executes (correct CPython semantics).
  Jos's old firmware fired it late; anything depending on that timing is wrong on the
  current branch (this already bit one test — break after an assignment, not on it).
- The unix `standard` variant on the integration branch enables settrace + LOCALNAMES by
  default; stock upstream unix does not, and other ports need
  `MICROPY_PY_SYS_SETTRACE=1` + `MICROPY_PY_SYS_SETTRACE_LOCALNAMES=1`. Keep
  `MICROPY_PY_SYS_SETTRACE_LOCALNAMES_PERSIST` off — it corrupts line numbers for all
  compiled code.

### ampremote / mpremote structure (inspected at `~/ampremote/`)

`ampremote` is an `mbm`-managed integration of upstream `mpremote`. The submodule
`~/ampremote/micropython` sits on integration branch `ampremote`; `mbm.toml` lists the
branches composed onto it, in merge order, with `ampremote_rename` always last. `make
install` does `uv tool install --editable` of `micropython/tools/mpremote`, so edits under
`micropython/tools/mpremote/mpremote/` take effect live as the `ampremote`/`ampr` CLI.
The Python module stays named `mpremote`; only the distribution name and console scripts
change (in `ampremote_rename`). Policy (from `~/ampremote/CLAUDE.md`): default to upstream;
keep a branch local-only when it is distribution-specific or needs hardware validation
first, in which case raise it as a draft PR on the `andrewleech/micropython` fork and
switch its base to `micropython:master` once validated.

Command architecture: commands are registered in the `_COMMANDS` dict in
`micropython/tools/mpremote/mpremote/main.py` (line ~295), mapping a command name to
`(handler_func, argparse_func)`. Handlers live in `commands.py` and take `(state, args)`;
`state.transport` is the connected `Transport` (`transport.py` / `transport_serial.py`).
`state.ensure_raw_repl()` gets a raw REPL; `state.did_action()` marks work done. Existing
handlers `do_mount`, `do_run`, `do_exec`, `do_filesystem`, `do_mip` show the pattern a new
`do_debug` should follow. Adding a command is: write `do_debug`/`argparse_debug`, import
them into `main.py`, add an entry to `_COMMANDS`.

### What the already-integrated mpremote PRs give us (reuse, don't reinvent)

> **Correction (2026-07-06):** "already-integrated" means integrated in **ampremote's**
> tree (`~/ampremote/micropython`, e.g. `verify_hash` at its `transport.py:292`, QEMU PTY
> at its `transport_serial.py:108-111`) — NOT in this repo's micropython submodule, whose
> mpremote is plain upstream. Work that relies on these primitives (EPIC-4/5) must either
> register the relevant mpremote branches into this repo's `mpy-debugpy` integration
> branch (consistent with Q1/D5 — recommended) or run against the installed `ampr` CLI.
> Tracked in tickets s4.1/s4.2/s4.4 and the STORY-8.5 composition.

- **#18436 streaming hash verification** (`mpremote_file_cp_hash`): `transport.py`
  `fs_writefile(..., verify_hash=True)` and `fs_readfile(..., verify_hash=True)` compute a
  device-side sha256 during transfer and compare to the local hash. This is most of the
  incremental-sync/staleness engine already — we get per-file content verification for free.
- **#18785 fast encoded file transfer** (`feature/smart-encoding-fs-writefile`): speeds up
  the writes the sync engine issues.
- **#17322 auto-reconnect** (`mpremote_reconnect`): device drop/reconnect handling; useful
  when a debug run resets the board.
- **#19062 socket / rfc2217** (`mpr/fix_socket_rfc2217`): network transport handling —
  relevant to the network-attach flow and to any TCP endpoint work.
- **#18327 QEMU PTY detection** (`mpremote_pty`): lets the same flow target a QEMU PTY,
  useful for CI without hardware.
- **#17485 resume-by-default**, **raw-repl retry** (`mpremote_raw_repl_retry`): connection
  robustness the debug command inherits by building on `state`/`transport`.

### The mount mechanism, and why it is a spike (inspected in `transport_serial.py`)

`do_mount` → `SerialTransport.mount_local` installs a `RemoteFS` hook on the device
(`fs_hook_code`, `__mount()`) and then wraps the host serial object in a `SerialIntercept`.
The device's filesystem calls (open/read/stat over `/remote`) are issued as RPC back over
the **same serial stream**, sniffed out of the byte stream by `SerialIntercept` and served
by `PyboardCommand` on the host. This only works while the device is in a REPL read/write
loop able to service that RPC. Under debugpy the traced program spends its time in the
trace callback, and while paused it busy-polls — it is not servicing serial fs RPC. So an
`import` from a mounted `/remote` during a debug session (or while paused at a breakpoint)
plausibly hangs. Confirming the exact failure mode is a spike (STORY under EPIC-2) because
the answer deletes whole subsystems downstream: **if mount can be made to work under
settrace, live source mount replaces the entire sync/staleness engine**. If DAP can be
tunnelled over serial, the network transport, IP discovery and any TCP proxy disappear.

---

## Design position (summary)

CLI-first orchestration, shipped as an `mpremote debug` command, with a thin VS Code
extension on top. The extension exists only to deliver an F5-style one-keypress launch; it
must not own firmware selection, source sync, target launch or endpoint discovery — that
all lives in the command, which is testable headlessly (the wrapper repo already has a
`fake_vscode` DAP client and pytest harness) and works from CI and other editors.

One mental model for all three transports: **resolve target → ensure runtime → sync →
launch → attach**. A generated on-device boot script prints one machine-readable handshake
line (`MPDBG-READY {json}`) carrying the endpoint and probed capabilities; nobody types an
IP or port. Staleness becomes impossible-without-warning via hash-verified sync (or is
deleted outright if the mount spike succeeds). Capability is probed at runtime and surfaced
honestly; locals are marked read-only in DAP so the edit affordance is greyed out rather
than failing.

Spikes (mount-under-settrace, DAP-over-serial) run **early**, right after the no-regret
foundations, because each can remove a whole epic's worth of work.

---

## Design decisions

### Settled

- **D1 (2026-07-04) — CLI-first orchestration** as `mpremote debug`; the VS Code
  extension is a thin layer added last. (Design position above.)
- **D2 (2026-07-05) — Mount spike: CONDITIONAL YES** — mount fs-RPC and network DAP
  coexist; the sync engine (EPIC-4.2/4.3) is demoted to fallback. `SPIKE-2.1-mount.md`.
- **D3 (2026-07-05) — Serial DAP is board-dependent** — first-class only on multi-CDC
  boards; network stays the primary device transport. `SPIKE-2.2-serial-dap.md`.
- **D4 (2026-07-05) — Locals are read-only in DAP** until a real `_set_local` exists,
  gated on the runtime probe, never on variant names.
- **D5 (2026-07-06) — Canonical lineage is the andrewleech branches composed onto
  current upstream master**; Jos's snapshot branches are provenance only. Composition
  recorded in `mbm.toml`; hand-composed until STORY-8.5. `20260706_recomposition.md`.
- **D6 (2026-07-06) — Depend on the ampremote lineage for mpremote primitives until
  its PRs land upstream.** The prerequisite mpremote capabilities (verify_hash #18436,
  QEMU PTY #18327, auto-reconnect #17322, rfc2217/TCP #19062) are ampremote-only at the
  pinned upstream SHA. Rather than feature-detecting their absence, register the needed
  ampremote branches into this repo's `micropython:mpy-debugpy` composition via mbm
  (STORY-8.5 scope, consistent with Q1's one-branch/two-consumers model). The dependency
  dissolves naturally as each PR merges upstream and drops out of the composition at the
  next `mbm rebase`. Tickets written before this date (s4.x, s6.1/s6.2) hedge with
  "feature-detect" language — supersede that at phase-entry revalidation.

### Open questions

All open questions are now closed; see DECIDED entries below.

**DECIDED (2026-07-06):**

- **Q6 → NO, parameters are NOT omitted** on the current branch (`18c76f7c`). The old
  omission (BACKGROUND area 1, traced on `05b7818`) is gone: `py/profile.c:164/181`
  stores every `local_num` unconditionally with no `param_count` skip, and
  `py/compile.c:3487-3492` captures param names with no `IS_PARAM` exclusion. Proven
  empirically — `def foo(a,b,c=10)` traced via `sys.settrace` yields `f_locals` keys
  `['a','b','c','x','y']`. Real-name path gates on `MICROPY_PY_SYS_SETTRACE_LOCALNAMES`
  (no `py/localnames.c`). No firmware fix needed; a positive regression test is folded
  into STORY-8.6. Full trace: `20260706_flocals_params.md`.

- **Q1 → option (a).** `do_debug` develops on ONE branch on `andrewleech/micropython`,
  registered in ampremote's mbm integration AND merged into this repo's `mpy-debugpy`
  integration branch. One branch, two consumers, no divergence. Governs EPIC-5 / STORY-8.1.
- **Q2 → GitHub Actions on this repo → Releases.** Board set for v1 firmware artifacts:
  **unix, rpi_pico_w, PYBD_SF6, ESP32_GENERIC.** Governs EPIC-3 (STORY-3.1/3.2).
- **Q3 → ship v1 network-only.** Single-UART serial DAP is pushed back, not dropped: the
  framing/escaping details in `SPIKE-2.2-serial-dap.md` are retained as the basis for a
  later follow-up spike. Governs EPIC-6 (STORY-6.1 scopes to multi-CDC boards for v1).
- **Q4 → VS Code extension IS in v1 scope** (the primary marketing win). EPIC-7 stays in
  the v1 roadmap rather than deferring to v2.
- **Q5 → wire mbm reproducibility now.** STORY-8.5 is pulled forward — do it before more
  feature work so upstream-master bumps stay cheap.
Close a question with a dated DECIDED entry and a pointer to the design note that
resolved it; do not delete rows.

---

## Epics and stories

Story fields: id, title, type, description, acceptance criteria, dependencies, component,
effort (S/M/L), risk (low/med/high), and model tier where a coding-workflow tier applies
(implementation→sonnet, automated testing→haiku, review→opus; investigation/design→opus).

**Tickets.** Every substantive not-yet-done story has a self-contained execution brief in
`planning/tickets/s<epic>.<story>_<slug>.md`, written at planning time and revalidated at
phase entry per `planning/00_index.md` (the ticket, not the story text here, is what an
implementation workflow consumes):

| epic | tickets |
|------|---------|
| EPIC-3 | `s3.2_ci-firmware-builds` · `s3.3_variant-matrix-collapse` · `s3.4_firmware-docs` |
| EPIC-4 | `s4.1_device-debugpy-install` · `s4.2_incremental-sync` · `s4.3_staleness-guard-pathmappings` · `s4.4_sync-tests` · `s4.5_hot-reload-reimport` |
| EPIC-5 | `s5.1_do-debug-skeleton` · `s5.2_target-model` · `s5.3_unix-flow` · `s5.4_handshake-plumbing` · `s5.5_command-tests` |
| EPIC-6 | `s6.1_serial-transport` · `s6.2_network-transport` · `s6.3_dap-log` · `s6.4_hardware-tests` · `s6.5_evaluate-exec` |
| EPIC-7 | `s7.1_debug-config-provider` · `s7.2_target-picker` · `s7.4_extension-smoke-test` |
| EPIC-8 | `s8.1_mbm-debug-branch` · `s8.2_upstream-debugpy-foundations` · `s8.4_user-docs` · `s8.5_mbm-reproducible-composition` · `s8.6_upstream-firmware-fixes` |

No tickets (intentional): EPIC-1 and EPIC-2 are DONE; STORY-3.1 is DONE; STORY-7.3 and
STORY-8.3 are trivial — their story entries below are the full brief.

Components: **firmware** = micropython firmware submodule; **debugpy** = micropython-lib
`python-ecosys/debugpy`; **mpremote** = ampremote/mpremote fork; **wrapper** = this repo;
**extension** = VS Code extension; **CI**.

---

### EPIC-1 — No-regret foundations (protocol correctness + capability truth)

**Goal:** remove the fragile bits that are wrong regardless of the eventual architecture:
the sleep(2) breakpoint race, capability guessing, and the promise of local editing. All
of this lands in the debugpy module and is upstream-friendly on its own.

**Rationale:** none of these commit us to the command, the transports or the extension.
They make every later flow more reliable and they are small, self-contained changes with
existing test scaffolding (`tests/` + `fake_vscode`).

**Definition of done:** the sleep(2) is gone and replaced by a deterministic
configuration-done sync; the server reports a capability record at attach time; locals are
presented read-only; the two launcher scripts are merged into one parameterised boot
script that emits the handshake line; host-side pytest covers all of it.

- **STORY-1.1 — `wait_for_client()` gated on `configurationDone`**
  - type: implementation
  - description: Add a `debugpy.wait_for_client()` to the server that spins the existing
    `process_pending_messages()` poll loop until the DAP `configurationDone` request has
    been received, then returns. The launcher calls it instead of `time.sleep(2)`. This is
    the same loop shape as `wait_for_continue()`, so it fits the no-thread model.
  - acceptance criteria:
    - [ ] `configurationDone` is recorded when received by the session.
    - [ ] `wait_for_client()` returns only after `configurationDone`, or after a bounded
      timeout that logs a clear message (no silent 2 s guess).
    - [ ] breakpoints set by the client before `configurationDone` are honoured on the
      first traced line — no race.
    - [ ] no `time.sleep(2)` remains in the launch path.
  - dependencies: none
  - component: debugpy
  - effort: S · risk: low · model: sonnet

- **STORY-1.2 — Runtime capability probe + capability record**
  - type: implementation
  - description: At session start the server probes the actual firmware: settrace present;
    whether `f_locals` names are real or `local_N` placeholders (SAVE_NAMES); whether
    `frame._set_local` exists; `f_back` present. Assemble a capability dict returned to the
    tooling (via the handshake line, STORY-1.4, and/or a custom DAP response field).
  - acceptance criteria:
    - [ ] capability dict reports at least: `settrace`, `save_names`, `set_local`, `f_back`.
    - [ ] values are derived by probing, never from a build/variant name.
    - [ ] probing works on both unix and bare-metal without raising.
  - dependencies: none
  - component: debugpy
  - effort: M · risk: med · model: sonnet

- **STORY-1.3 — Mark locals read-only via DAP presentation hint**
  - type: implementation
  - description: When `set_local` capability is absent, tag local-scope variables with the
    DAP `presentationHint.attributes = ["readOnly"]` so clients grey out the edit
    affordance. Globals stay editable. Removes the "edit then hit an error string" path.
  - acceptance criteria:
    - [ ] locals returned with `readOnly` hint when `set_local` is false.
    - [ ] globals remain editable and setVariable on a global still mutates the program.
    - [ ] no user-visible error path for attempting a local edit in the unsupported case.
  - dependencies: STORY-1.2
  - component: debugpy
  - effort: S · risk: low · model: sonnet

- **STORY-1.4 — Single parameterised boot script with handshake line**
  - type: implementation
  - description: Replace `mpy_launch_debugpy_unix.py` and `..._esp32.py` with one boot
    script that: detects environment (presence of `network`), takes target module/method
    and an optional port (0 = auto), starts the listener, prints exactly one machine
    line `MPDBG-READY {json}` with host, port and the capability record, calls
    `wait_for_client()`, then imports+runs the target. On a network target it reports its
    own DHCP address; on unix it reports the bound port. This script becomes an artifact the
    command uploads/owns; users never edit it.
  - acceptance criteria:
    - [ ] one script covers unix and device; no IP or port literals inside it.
    - [ ] emits exactly one parseable `MPDBG-READY` line with endpoint + capabilities.
    - [ ] uses `wait_for_client()` (STORY-1.1), not sleep.
    - [ ] the two old launcher scripts are deleted.
  - dependencies: STORY-1.1, STORY-1.2
  - component: wrapper (script), later moved into mpremote as an embedded resource
  - effort: M · risk: med · model: sonnet

- **STORY-1.5 — Host-side tests for foundations**
  - type: test
  - description: Extend the `fake_vscode` pytest harness to assert the configurationDone
    handshake (no race), the capability record contents, the read-only locals hint, and
    parsing of the `MPDBG-READY` line.
  - acceptance criteria:
    - [ ] test proves a breakpoint set pre-`configurationDone` stops on first line.
    - [ ] test asserts capability dict fields for a known firmware.
    - [ ] test asserts locals carry readOnly and a global setVariable succeeds.
    - [ ] test parses a sample `MPDBG-READY` line into endpoint+caps.
  - dependencies: STORY-1.1, STORY-1.2, STORY-1.3, STORY-1.4
  - component: wrapper / CI
  - effort: M · risk: low · model: haiku

- **STORY-1.6 — Review foundations**
  - type: review
  - description: Adversarial + standard review of EPIC-1 changes against the no-thread
    model and upstream mergeability (these land in micropython-lib eventually).
  - acceptance criteria: [ ] review findings fed back and resolved; [ ] no regression in
    existing `tests/`.
  - dependencies: STORY-1.5
  - component: debugpy / wrapper · effort: S · risk: low · model: opus

---

### EPIC-2 — Architecture-deciding spikes (run early)

**Goal:** answer the two questions that can delete whole subsystems before we build them.

**Rationale:** the sync engine (EPIC-4) and the network/proxy work (EPIC-6) are large. If
mount works under settrace we don't build a sync engine at all. If DAP tunnels over serial
we don't need network transport, IP discovery, or a TCP proxy for the common case. Spend a
little to possibly avoid a lot.

**Definition of done:** both spikes produce a written yes/no with evidence and a
recommendation that the downstream epics consume.

- **STORY-2.1 — Spike: does mpremote `mount` work under settrace?**
  - type: investigation/spike
  - description: Determine empirically whether a program run from a mounted `/remote`
    filesystem can be debugged: set a breakpoint, pause, step, inspect. Trace the failure
    if it hangs — the hypothesis (from `transport_serial.py`) is that `SerialIntercept`
    fs-RPC is not serviced while the traced program is off the REPL loop or paused in the
    busy-poll. Test whether servicing the fs-RPC from inside the debugpy poll loop, or
    running debugpy's transport on a separate channel, changes the outcome.
  - decision it unblocks: whether EPIC-4 (sync engine) is needed at all, or reduces to a
    thin "ensure debugpy module present" step with mount providing live source.
  - yes/no downstream effect: **yes** → EPIC-4 collapses to STORY-4.1 only (module
    presence); source is always live, staleness problem gone. **no** → build the
    hash-verified sync engine (EPIC-4) in full.
  - acceptance criteria:
    - [ ] reproduce a debug session against mounted source; record pass/hang with logs.
    - [ ] root-cause any hang to a specific interaction (fs-RPC vs trace/pause).
    - [ ] written recommendation with the exact conditions under which mount is/ isn't
      viable for debugging.
  - dependencies: STORY-1.4 (need a working boot path to test against)
  - component: mpremote + debugpy + firmware · effort: M · risk: high · model: opus

- **STORY-2.2 — Spike: DAP over the serial/raw-REPL channel**
  - type: investigation/spike
  - description: Assess tunnelling the DAP byte stream over the existing serial connection
    instead of TCP. The server's poll model only needs a non-blocking read of a stream; it
    is written against a socket today. Evaluate multiplexing DAP with REPL output on one
    serial line (or a dedicated framing), throughput/latency while stepping, and whether
    #18785/#19062 primitives help. Prototype enough to attach VS Code once over serial.
  - decision it unblocks: whether the serial-device flow needs WiFi/IP/network at all, and
    whether a localhost TCP proxy (bridging VS Code↔serial) is the integration point.
  - yes/no downstream effect: **yes** → serial transport becomes first-class (EPIC-5),
    network transport is optional, no IP discovery needed for USB devices; a small
    localhost↔serial DAP bridge is the only proxy we build. **no** → serial devices must
    use the network transport (device joins WiFi, reports IP via handshake), EPIC-6 carries
    the load.
  - acceptance criteria:
    - [ ] measured feasibility of DAP framing over serial alongside/without REPL output.
    - [ ] a prototype attach over serial reaching at least one breakpoint, or a documented
      blocker with root cause.
    - [ ] recommendation on the localhost bridge design (where it lives in mpremote).
  - dependencies: STORY-1.4
  - component: mpremote + debugpy · effort: L · risk: high · model: opus

---

### EPIC-3 — Firmware provenance and capability matrix

**Goal:** make firmware selection and build non-opaque without forcing every user to
compile, and stop encoding capabilities in names.

**Rationale:** prebuilt binaries in-tree have opaque provenance and the variant names lie
about capabilities. This epic can run in parallel with EPIC-1/EPIC-2.

**Definition of done:** each firmware artifact has a manifest (source repo, commit, build
flags, artifact hash, download URL); a `fetch` path downloads+verifies; a documented
reproducible (docker) build path exists; the variant matrix is collapsed to the real set;
the tooling selects firmware by required capability, not by name.

- **STORY-3.1 — Firmware manifest + hash-verified fetch**
  - type: implementation
  - description: Define a per-variant manifest (repo, SHA, flags, artifact sha256, URL).
    Publish binaries to GitHub Releases via CI **on this repo** (Q2 DECIDED); stop
    carrying binaries in-tree. Add a fetch that downloads and verifies against the
    manifest hash. v1 board set: **unix, rpi_pico_w, PYBD_SF6, ESP32_GENERIC.**
  - acceptance criteria:
    - [ ] manifest records provenance for each published artifact.
    - [ ] fetch verifies sha256 and fails loudly on mismatch.
    - [ ] repo no longer stores firmware binaries.
  - dependencies: none
  - component: wrapper + CI · effort: M · risk: med · model: sonnet

- **STORY-3.2 — Reproducible docker build for firmware variants**
  - type: implementation
  - description: Dockerised/CI build producing the v1 artifacts from the pinned
    integration SHA with pinned flags (`MICROPY_PY_SYS_SETTRACE` +
    `MICROPY_PY_SYS_SETTRACE_LOCALNAMES`; PERSIST off). Host-uid/path convention per
    user CLAUDE.md. Boards (Q2 DECIDED): unix, rpi_pico_w, PYBD_SF6, ESP32_GENERIC.
  - acceptance criteria:
    - [ ] each of the four boards builds reproducibly in CI from the pinned SHA.
    - [ ] output artifact hash matches the manifest when inputs match.
    - [ ] the legacy pre-recomposition manifest entries are replaced by these builds.
  - dependencies: STORY-3.1
  - component: firmware + CI · effort: M · risk: med · model: sonnet

- **STORY-3.3 — Collapse variant matrix to the real set**
  - type: implementation
  - description: Remove variants whose names promise unimplemented features (e.g.
    `set_local` against firmware without `_set_local`). Keep `settrace` and
    `settrace_save_names`. Selection driven by the capability the session needs, resolved
    against the probe (STORY-1.2).
  - acceptance criteria:
    - [ ] only real variants remain.
    - [ ] tooling picks a variant by required capability, and a mismatch between a
      variant's claimed and probed capability is a hard error.
  - dependencies: STORY-1.2, STORY-3.1
  - component: wrapper · effort: S · risk: low · model: sonnet

- **STORY-3.4 — Docs: firmware provenance + capability matrix**
  - type: docs
  - description: Document what each variant is, its flags, what capability it yields
    (real local names vs `local_N` placeholders; no local editing), and how to fetch vs
    build. Correct the BACKGROUND note about settrace-by-default (it is not).
  - acceptance criteria: [ ] table of variant→flags→capabilities; [ ] fetch and build both
    documented; [ ] no capability claim that the probe would contradict.
  - dependencies: STORY-3.1, STORY-3.3
  - component: wrapper · effort: S · risk: low · model: sonnet

---

### EPIC-4 — Incremental sync / staleness engine

**Goal:** replace `cp -r src/ :/` on every run with hash-verified incremental sync, and
make device staleness impossible without a warning. **Scope depends on STORY-2.1.**

**Rationale:** full re-upload every run is slow and silently goes stale. mpremote #18436
already gives per-file sha256 verification; build the sync on top of it rather than a
bespoke engine.

**Definition of done (if mount spike is NO):** only changed files are transferred; an
on-device manifest maps path→hash; at attach time a mismatch between local and device hash
is a hard warning before the session starts; path mappings are generated from the sync
record, not hand-written. **If STORY-2.1 is YES:** this epic collapses to STORY-4.1.

- **STORY-4.1 — Ensure debugpy module present on device (compiled, cached)**
  - type: implementation
  - description: Ensure the debugpy package is installed on the device, cross-compiled to
    `.mpy` and cached keyed on (source hash, mpy-cross version, flags); only reinstall when
    the package hash changes. Reuse mpremote `fs_writefile(verify_hash=True)`.
  - acceptance criteria:
    - [ ] first run installs; subsequent runs with unchanged package skip reinstall.
    - [ ] install verified by device-side hash.
  - dependencies: EPIC-2 decision (STORY-2.1)
  - component: mpremote + wrapper · effort: M · risk: med · model: sonnet

- **STORY-4.2 — Hash-verified incremental source sync + device manifest**
  - type: implementation
  - description: (Only if STORY-2.1 = NO.) Maintain `.mpdbg/manifest.json` on the device
    (path→sha256). Each run hashes local files, pushes only diffs via
    `fs_writefile(verify_hash=True)` (+ #18785 encoding), updates the manifest. Record the
    local→device path map for path-mapping generation.
  - acceptance criteria:
    - [ ] unchanged files are not retransferred.
    - [ ] changed files are detected and pushed; manifest updated.
    - [ ] transfers verified by device-side hash.
  - dependencies: STORY-4.1, STORY-2.1 (=NO)
  - component: mpremote + wrapper · effort: L · risk: med · model: sonnet

- **STORY-4.3 — Attach-time staleness guard**
  - type: implementation
  - description: Before a session starts, compare device file hashes to local; on mismatch,
    hard-warn (or block per config) rather than debugging stale code. Generate the DAP
    `pathMappings` as absolute paths from the sync record (fixes multi-root ambiguity).
  - acceptance criteria:
    - [ ] a deliberately stale device file produces a clear warning before attach.
    - [ ] pathMappings are generated, absolute, and correct for a multi-root workspace.
  - dependencies: STORY-4.2 (or STORY-4.1 if mount provides live source)
  - component: mpremote + wrapper · effort: M · risk: med · model: sonnet

- **STORY-4.4 — Sync tests**
  - type: test
  - description: Cover incremental behaviour (no-op on unchanged, diff on changed), hash
    verification failure handling, and the staleness guard. Use QEMU PTY (#18327) where
    possible to run without hardware.
  - acceptance criteria: [ ] tests for no-op/diff/verify-fail/stale-guard; [ ] runnable in
    CI against QEMU.
  - dependencies: STORY-4.2, STORY-4.3
  - component: CI · effort: M · risk: med · model: haiku

- **STORY-4.5 — Hot-reload iteration loop via `sys.modules` eviction + re-import**
  - type: implementation
  - description: (Surfaced by SPIKE-2.1.) With source mounted from the host, an
    edit→re-run loop needs no upload: evict the target from `sys.modules` and re-import
    it (or monkey-patch from the REPL). Wire this into the debug session lifecycle so a
    restart re-imports fresh source from the mount instead of resetting the board. Only
    an already-paused frame cannot pick up edits.
  - acceptance criteria:
    - [ ] editing a mounted source file and restarting the session runs the new code
      with no upload step.
    - [ ] eviction covers the target module and its project-local imports.
  - dependencies: STORY-2.1 (done), STORY-4.1
  - component: mpremote + debugpy · effort: M · risk: med · model: sonnet

---

### EPIC-5 — The `mpremote debug` command + unified target model

**Goal:** one command that resolves a named target and runs resolve→ensure→sync→launch→
attach for unix, serial device and network device behind one mental model.

**Rationale:** this is where the bulk of the friction dies. It is mpremote-native
(reusing transport, raw-REPL, mount, reconnect, hash-cp) and is the artifact we upstream.

**Definition of done:** `mpremote debug <target> [module[:method]]` launches a debug
session on any of the three transports; endpoints and capabilities come from the handshake
line; no IPs/ports are typed; the command is registered in `_COMMANDS` and follows the
mpremote house style; delivered as an mbm-registered branch.

- **STORY-5.1 — `do_debug` command skeleton + argparse, registered in `_COMMANDS`**
  - type: implementation
  - description: Add `do_debug(state, args)` and `argparse_debug()` in `commands.py`,
    import into `main.py`, add to `_COMMANDS`. Follow the `do_run`/`do_mount` pattern
    (`ensure_raw_repl`, `did_action`). Args: target/transport selection, target
    module[:method], optional port, `--dap-log`. Reuse `state.transport` for connection.
  - acceptance criteria:
    - [ ] `mpremote debug --help` lists the command with a real description.
    - [ ] command connects via existing transport handling and reaches the boot script.
    - [ ] follows house style (byte-literal comments, extend existing primitives,
      new-arg defaults preserve old behaviour, underscore-prefix single-caller helpers).
  - dependencies: STORY-1.4
  - component: mpremote · effort: M · risk: med · model: sonnet

- **STORY-5.2 — Named target model (`mpdebug.toml`) + target resolution**
  - type: implementation
  - description: Project config with named targets (`kind = unix|serial|network`, connect
    string via `/dev/serial/by-id/...` for serial, firmware/capability requirement). The
    command resolves a target name to a transport + runtime + firmware requirement.
  - acceptance criteria:
    - [ ] a named target resolves to transport + firmware requirement.
    - [ ] serial connect strings use stable `/dev/serial/by-id/...` references.
    - [ ] absent/ambiguous target gives a clear error, not a stack trace.
  - dependencies: STORY-5.1, STORY-3.3
  - component: mpremote · effort: M · risk: med · model: sonnet

- **STORY-5.3 — Unix flow end-to-end**
  - type: implementation
  - description: For `kind=unix`: ensure firmware (fetch/build), set module path, launch
    the boot script as a subprocess, parse `MPDBG-READY` from stdout (control plane), start
    the client attach to the reported localhost port (data plane).
  - acceptance criteria:
    - [ ] `mpremote debug unix target:main` reaches a breakpoint with zero hand config.
    - [ ] port is auto (0) and taken from the handshake; no literal port anywhere.
  - dependencies: STORY-5.1, STORY-5.2, STORY-3.1, EPIC-1
  - component: mpremote + wrapper · effort: M · risk: med · model: sonnet

- **STORY-5.4 — Handshake plumbing (control plane / data plane split)**
  - type: implementation
  - description: Generalise handshake parsing so the control plane is subprocess stdout
    (unix), raw-REPL output (serial), or serial (network device before it has an IP), and
    the data plane is TCP or the serial DAP tunnel per the EPIC-2 outcome. The device tells
    the tooling its endpoint; the tooling never guesses.
  - acceptance criteria:
    - [ ] one handshake parser serves all transports.
    - [ ] endpoint always comes from the device/subprocess, never from user config.
  - dependencies: STORY-5.1, STORY-2.2
  - component: mpremote · effort: M · risk: med · model: sonnet

- **STORY-5.5 — Command tests (unix + QEMU)**
  - type: test
  - description: Drive `do_debug` for unix and a QEMU PTY target through the `fake_vscode`
    client: attach, breakpoint, step, inspect, continue. Assert no hardcoded endpoints.
  - acceptance criteria: [ ] unix + QEMU flows pass in CI; [ ] handshake-derived endpoints
    asserted.
  - dependencies: STORY-5.3, STORY-5.4
  - component: CI · effort: M · risk: med · model: haiku

---

### EPIC-6 — Device transports: serial and network

**Goal:** the two device transports wired into `mpremote debug`, shaped by EPIC-2.

**Rationale:** serial is the native mpremote experience (USB, no network). Network is
needed when serial DAP tunnelling isn't viable or when the device is only reachable over
WiFi. Which one is primary depends on STORY-2.2.

**Definition of done:** `mpremote debug <serial-target>` and `<network-target>` both work
end-to-end; a device on WiFi reports its own address; serial devices need no IP.

- **STORY-6.1 — Serial device transport**
  - type: implementation
  - description: If STORY-2.2 = YES: run DAP over serial via a localhost↔serial bridge the
    command owns; VS Code attaches to localhost. If NO: bring the serial device onto the
    network and fall through to network transport, documenting the WiFi requirement.
    Reuse #17322 reconnect and #19062 socket handling as applicable.
  - acceptance criteria:
    - [ ] a USB-connected device is debuggable with the connect string only (no IP), or
      the WiFi fallback is clearly required and documented.
    - [ ] board reset/reconnect during a session is handled.
  - dependencies: STORY-2.2, STORY-5.4
  - component: mpremote + debugpy · effort: L · risk: high · model: sonnet

- **STORY-6.2 — Network device transport**
  - type: implementation
  - description: For `kind=network`: device joins WiFi, boot script reports its DHCP
    address in the handshake over the serial/raw-REPL control plane (or via mDNS), tooling
    attaches over TCP. No IP literals; #19062 for socket handling.
  - acceptance criteria:
    - [ ] device address is discovered from the handshake, never typed.
    - [ ] `mpremote debug <network-target>` reaches a breakpoint over TCP.
  - dependencies: STORY-5.4, EPIC-4 (sync for device source)
  - component: mpremote + debugpy · effort: L · risk: high · model: sonnet

- **STORY-6.3 — DAP monitor as a `--dap-log` flag**
  - type: implementation
  - description: Fold `dap_monitor.py` into the command as `--dap-log`, replacing the
    separate task+config. Works across transports.
  - acceptance criteria: [ ] `--dap-log` writes DAP traffic for any transport; [ ] the
    standalone monitor task/config is removed from the wrapper.
  - dependencies: STORY-5.1
  - component: mpremote · effort: S · risk: low · model: sonnet

- **STORY-6.4 — Device transport tests (hardware-in-loop)**
  - type: test
  - description: Serial and network flows on a real ESP32 (referenced by
    `/dev/serial/by-id/...`): attach, breakpoint, step, inspect, staleness guard.
    Hardware-in-loop per ampremote CLAUDE.md testing policy.
  - acceptance criteria: [ ] both device flows pass on real hardware; [ ] results record
    board/port tested.
  - dependencies: STORY-6.1, STORY-6.2
  - component: CI/manual · effort: M · risk: high · model: haiku

- **STORY-6.5 — Extend DAP `evaluate` to statement `exec` (repl/clipboard contexts)**
  - type: implementation
  - description: (Surfaced by SPIKE-2.1.) `evaluate` currently only `eval()`s
    expressions (`evaluate_expression`, `pdb_adapter.py:649-669` at `0d42fcd`; the DAP
    `context` field is currently ignored by the handler). Support statement execution
    for the DAP `repl`/`clipboard` contexts so a paused user can redefine a function or
    mutate globals from the Debug Console — the monkey-patching escape hatch for live
    edits.
  - acceptance criteria:
    - [ ] statements execute against the paused frame's globals from the Debug Console.
    - [ ] expression evaluation is unchanged for `watch`/`hover` contexts.
  - dependencies: EPIC-1
  - component: debugpy · effort: S · risk: low · model: sonnet

---

### EPIC-7 — Thin VS Code extension

**Goal:** F5 one-keypress launch, layered on the command. This is the only epic that
commits to the extension path and is done last, after the command carries the logic.

**Rationale:** VS Code can't give a clean one-keypress launch without either an extension
or the preLaunchTask/problemMatcher contortions the repo has now. Kept thin: it shells out
to `mpremote debug` and starts a `debugpy` attach with the generated config.

> **Fan-out (2026-07-06):** the extension grows beyond this epic into a full
> MicroPython extension (stubs, broker/REPL, fs explorer, mip installer,
> mpflash/mpbuild firmware flows) via its OWN roadmap, seeded by
> `20260706_vscode-extension-direction.md`. EPIC-7 remains the v1 slice here and
> becomes that roadmap's foundation; it does not absorb the new scope.

**Definition of done:** a custom `micropython` debug type resolves to a debugpy attach
using the command's handshake; a target picker in the status bar; nine launch configs
collapse to one; extension depends on ms-python.

- **STORY-7.1 — Debug type + `DebugConfigurationProvider`**
  - type: implementation
  - description: Register debug type `micropython`. In `resolveDebugConfiguration`, spawn
    `mpremote debug`, read the handshake, then start a `debugpy` attach session with the
    generated host/port/pathMappings. Use the return-`undefined`-and-`startDebugging`
    pattern (VS Code can't change a config's type from the resolver). Declare
    `ms-python.debugpy` as an extension dependency. Do not write a TS DAP adapter.
  - acceptance criteria:
    - [ ] F5 on a target launches an attach session with no hand config.
    - [ ] generated pathMappings are absolute (multi-root safe).
    - [ ] fails clearly if ms-python is not installed.
  - dependencies: EPIC-5 (command usable), STORY-4.3 (pathMappings)
  - component: extension · effort: L · risk: high · model: sonnet

- **STORY-7.2 — Target picker + status bar**
  - type: implementation
  - description: Status-bar picker over `mpdebug.toml` targets; capability from the probe
    surfaced (e.g. "locals: placeholders", "local edit: unsupported").
  - acceptance criteria: [ ] picker lists targets; [ ] selected target drives F5; [ ]
    capability shown honestly.
  - dependencies: STORY-7.1
  - component: extension · effort: M · risk: med · model: sonnet

- **STORY-7.3 — Collapse launch.json/tasks.json**
  - type: implementation
  - description: Reduce the nine attach configs to one, delete the `MP_DEBUG_VARIANT` env
    and the fake `problemMatcher`/`beginsPattern: "."` background-task hacks.
  - acceptance criteria: [ ] one debug config remains; [ ] no problemMatcher hacks; [ ] no
    `MP_DEBUG_VARIANT`.
  - dependencies: STORY-7.1
  - component: wrapper · effort: S · risk: low · model: sonnet

- **STORY-7.4 — Extension smoke test**
  - type: test
  - description: Automated extension host test: resolver produces a valid attach config
    and starts a session against a unix target.
  - acceptance criteria: [ ] resolver test green in CI.
  - dependencies: STORY-7.1
  - component: extension/CI · effort: M · risk: med · model: haiku

---

### EPIC-8 — Upstreaming and documentation

**Goal:** land the debugpy changes and the `mpremote debug` command upstream, per the
ampremote default-to-upstream policy, and reconcile the fork lineages.

**Rationale:** the end state is upstream-mergeable, not a private distribution. The command
starts as an mbm-registered branch and, once hardware-validated, retargets to
`micropython:master`.

**Definition of done:** debugpy changes raised against micropython-lib #1022 lineage; the
`debug` command raised as a micropython PR; capability probing/readOnly documented; fork
lineages (Josverl vs andrewleech) reconciled.

- **STORY-8.1 — Register `debug` command branch in mbm.toml**
  - type: implementation
  - description: (Q1 DECIDED — one branch, two consumers.) Create the `debug` command as
    a single branch on `andrewleech/micropython` off upstream master. Register it in
    `~/ampremote`'s `mbm.toml` before `ampremote_rename` (which stays last), AND merge it
    into this repo's `micropython` `mpy-debugpy` integration branch, so both integrations
    consume the same branch with no divergence. Open a draft PR on the fork per policy.
  - acceptance criteria: [ ] one branch on `andrewleech/micropython` is the sole source;
    [ ] it builds into the `ampremote` integration via mbm with `ampremote_rename` last;
    [ ] it is merged into this repo's `mpy-debugpy` integration branch; [ ] draft fork
    PR exists.
  - dependencies: EPIC-5
  - component: mpremote/mbm · effort: S · risk: low · model: sonnet

- **STORY-8.2 — Upstream debugpy foundations**
  - type: implementation
  - description: Raise EPIC-1 (wait_for_client, capability probe, readOnly) against the
    micropython-lib #1022 lineage; reconcile Josverl vs andrewleech debugpy branches.
  - acceptance criteria: [ ] PR(s) opened; [ ] lineage reconciled; [ ] the historical
    double-defined `MICROPY_PY_SYS_SETTRACE` build-glue issue confirmed resolved against
    the live PR (ticket s8.2 found no double definition in the current trees — verify,
    then drop this item).
  - dependencies: EPIC-1
  - component: debugpy/firmware · effort: M · risk: med · model: sonnet

- **STORY-8.3 — Promote `debug` command to upstream PR after hardware validation**
  - type: implementation
  - description: Once EPIC-6 hardware tests pass, switch the fork PR base to
    `micropython:master` for upstream review.
  - acceptance criteria: [ ] base retargeted; [ ] PR references validation evidence.
  - dependencies: STORY-8.1, STORY-6.4
  - component: mpremote · effort: S · risk: med · model: sonnet

- **STORY-8.4 — User-facing docs**
  - type: docs
  - description: One "Debug MicroPython" guide covering the three transports, the target
    model, firmware fetch/build, and the honest capability story (no local editing;
    placeholder vs real local names).
  - acceptance criteria: [ ] guide covers all three transports and firmware; [ ] no
    capability claim the probe contradicts.
  - dependencies: EPIC-5, EPIC-6, EPIC-3
  - component: wrapper · effort: M · risk: low · model: sonnet

- **STORY-8.5 — Wire reproducible mbm composition (`mbm add-pr` / `mbm rebase`)**
  - type: implementation
  - description: The integration branches are hand-composed (merge + cherry-pick;
    `mbm.toml` records them for provenance only). Express the same composition as mbm
    branch entries so `mbm rebase` can rebuild both integration branches on latest
    upstream, with `git rerere` replaying conflict resolutions. The Makefile
    `integrate` target currently stubs to this story.
  - acceptance criteria:
    - [ ] `mbm rebase -s micropython` and `-s micropython-lib` reproduce branches that
      build and pass `make test`.
    - [ ] `make integrate` runs the rebuild.
  - dependencies: **STORY-8.6** — ticket s8.5 found `local_names_implementation` sits on
    stale ancestry superseded by #8767, so it must be rebuilt on top of `pdb_support`
    (with the two fix commits) before it can be an mbm branch entry. Scope also covers
    registering the ampremote-only mpremote branches (#18436 etc.) as EPIC-4/5 needs them.
  - component: wrapper/mbm · effort: M · risk: med · model: sonnet

- **STORY-8.6 — Upstream the two firmware bug fixes to the canonical branches**
  - type: implementation
  - description: The recomposition fixed (a) `frame.f_locals` slot indexing (variables
    reported under wrong names) and (b) `LOCALNAMES_PERSIST` corrupting line numbers
    (now off by default) directly on the integration branch. Attach these onto the
    canonical `local_names_implementation` branch (and #8767 where applicable) so the
    fixes are not stranded in the integration. Q6 is answered (params are NOT omitted —
    `20260706_flocals_params.md`); no fix is needed there, but this story adds the
    positive regression test that was missing (`tests/misc/sys_settrace_locals_params.py`
    + `.exp`) so a future refactor can't silently reintroduce the old omission.
  - acceptance criteria:
    - [ ] both fixes exist as commits on the canonical branch(es) with tests.
    - [ ] a param-name regression test asserts `f_locals` carries parameter names.
  - dependencies: EPIC-1
  - component: firmware · effort: M · risk: med · model: sonnet

---

## Dependency-ordered execution sequence

Topological order suitable for feeding an orchestrator. Items on the same line can run in
parallel.

0. **STORY-8.6 then STORY-8.5** are pulled forward per the 2026-07-06 decisions — do them
   before net-new feature work so upstream-master bumps stay cheap and the fixes are not
   stranded. Order matters: s8.5 found that `local_names_implementation` must first be
   rebuilt on top of `pdb_support` with the two fix commits (that IS STORY-8.6) before
   the composition can be expressed as mbm branch entries. EPIC-1 and STORY-3.1 are
   already DONE; Q6 is closed (no param fix needed, regression test only).
   **DONE 2026-07-15** (both stories; see Status and
   `20260715_phase0-canonical-branches-mbm.md`).
1. **STORY-1.1**, **STORY-1.2** (independent) — and **STORY-3.1** can start here too.
2. **STORY-1.3** (needs 1.2), **STORY-1.4** (needs 1.1+1.2), **STORY-3.2** (needs 3.1),
   **STORY-3.3** (needs 1.2+3.1) — parallel.
3. **STORY-1.5** (needs 1.1–1.4), **STORY-3.4** (needs 3.1+3.3) — parallel.
4. **STORY-1.6** (needs 1.5).
5. **STORY-2.1**, **STORY-2.2** (both need 1.4) — parallel spikes. **Gate:** their yes/no
   decides EPIC-4 scope and EPIC-6 shape.
6. **STORY-4.1** (needs 2.1 decision), **STORY-5.1** (needs 1.4) — parallel.
7. **STORY-4.2** (if 2.1=NO; needs 4.1), **STORY-5.2** (needs 5.1+3.3) — parallel.
8. **STORY-4.3** (needs 4.2/4.1), **STORY-5.4** (needs 5.1+2.2) — parallel.
9. **STORY-5.3** (needs 5.1,5.2,3.1,EPIC-1), **STORY-4.4** (needs 4.2,4.3) — parallel.
10. **STORY-5.5** (needs 5.3,5.4), **STORY-6.3** (needs 5.1) — parallel.
11. **STORY-6.1** (needs 2.2,5.4), **STORY-6.2** (needs 5.4,EPIC-4) — parallel.
12. **STORY-6.4** (needs 6.1,6.2).
13. **STORY-8.1** (needs EPIC-5), **STORY-8.2** (needs EPIC-1) — parallel; can start once
    their epics are green.
14. **STORY-7.1** (needs EPIC-5,4.3).
15. **STORY-7.2**, **STORY-7.3**, **STORY-7.4** (all need 7.1) — parallel.
16. **STORY-8.3** (needs 8.1,6.4), **STORY-8.4** (needs EPIC-5,6,3) — parallel.

Notes:
- The whole of EPIC-3 (firmware) runs in parallel with EPIC-1 and the spikes; it only feeds
  in at STORY-5.3.
- If **STORY-2.1 = YES** (mount works under settrace), skip STORY-4.2/4.3's sync-specific
  parts — EPIC-4 collapses to STORY-4.1 plus a live-mount attach path, and the staleness
  guard becomes trivial.
- If **STORY-2.2 = YES** (DAP over serial), STORY-6.1 is the primary device path and
  STORY-6.2 (network) becomes optional; no TCP proxy beyond the localhost↔serial bridge.
- Added post-spike / post-recomposition: **STORY-4.5** slots in after STORY-4.1;
  **STORY-6.5** any time after EPIC-1; **STORY-8.5** can run at any point (earlier =
  cheaper upstream tracking — Q5); **STORY-8.6** after EPIC-1, ideally with STORY-8.2.

---

## Risk register

| risk | mitigation |
|------|------------|
| ~~Hand-composed integration cannot be rebuilt when upstream master moves~~ RESOLVED 2026-07-15: both branches rebuild from `mbm.toml` (`make integrate`) | Proven by pinned-target runs (zero conflicts, exact tree equivalence). Residual rules: never run `mbm rebase` without `--local` (its pushes target upstream, not the fork); reset local feature branches to the canonical fork tips after each run; backups `mpy-debugpy-pre-mbm` + `mpy-debugpy-vendored-backup` kept |
| Firmware behaviour assumptions drift when branches are recomposed (e.g. Q6 parameter omission, line-event timing) | runtime capability probe (STORY-1.2) + behavioural DAP tests re-run on every recomposition; never trust names or prose over the probe |
| Upstream review churn on #8767 invalidates the composition | keep integration deltas small and upstream-first (STORY-8.2/8.6); rerere replays conflict resolutions |
| Prebuilt firmware provenance drift (legacy artifacts predate the recomposed branch) | STORY-3.1/3.2: CI-built, hash-verified artifacts from the pinned submodule SHA; the manifest never claims what the probe would contradict |
| EPIC-4/5 assume mpremote primitives (verify_hash, QEMU PTY, reconnect, rfc2217) that exist only in ampremote's tree, not this repo's submodule | DECIDED as D6: register the needed ampremote branches into this repo's `mpy-debugpy` integration via mbm (folded into STORY-8.5 scope) until the PRs land upstream; tickets s4.1/s4.2/s4.4/s5.x carry the dependency explicitly |
| Serial DAP framing on single-UART boards unproven | network transport stays mainline (D3); the framing prototype is a gated follow-up spike (Q3), not a dependency |
| Busy-poll pause loop starves WiFi/housekeeping while paused on device | measure during STORY-6.4 hardware-in-loop tests; document the impact and tune the poll interval if measurable |

---

## Progress tracking

New planning documents are written as `planning/YYYYMMDD_<topic>.md` with a header
carrying the date, the top-repo HEAD SHA (`git rev-parse --short=10 HEAD`), and the
relevant submodule SHAs, so every finding is reproducible against a known tree state.
This roadmap — the Status section, the open-questions table, and the risk register — is
updated in place as work lands, never forked. Spike and design documents are decision
records: append corrections, do not rewrite conclusions.

At the entrance to each phase, the workflow planner MUST revalidate that phase's
tickets against everything done since they were written — code drift via
`git log`/`git diff` from the ticket's stamped SHAs (including submodule SHAs),
knowledge drift via planning documents dated after the stamp — updating each ticket in
place and appending a `Revalidated:` line before any workflow consumes it. The full
procedure, conventions, and ticket template live in `planning/00_index.md`; each
executed phase writes its progress and learnings back as `YYYYMMDD_<topic>.md`.
