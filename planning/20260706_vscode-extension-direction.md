# Design note — VS Code extension fan-out: scope, positioning, decided questions

Date: 2026-07-06 · top repo @ `7708098c74` (micropython `18c76f7c56`,
micropython-lib `0d42fcd5bc`)

Captures the direction discussion for expanding the VS Code work beyond EPIC-7's
thin debug adapter into a full MicroPython extension, plus the decisions taken.
This note seeds a future dedicated roadmap (`planning/vscode/ROADMAP.md`, to be
produced via the phased-roadmap procedure with its own research fan-out — NOT yet
written). EPIC-7 stays in the main roadmap as the v1 debug-adapter slice; the
extension roadmap will treat completed EPIC-7 as its foundation.

## Positioning

- The debug adapter is the differentiator — no existing MicroPython extension
  debugs. Everything else (REPL, run, fs view, stubs) is table stakes users expect
  once an extension is installed; they are the minimum shell around the debug win,
  not optional garnish.
- Prior art: MicroPico (paulober) does REPL/run/fs/stubs well but is RP2-only and
  has no debugging; Pymakr is dead (its remembered failure mode: flaky serial-port
  contention between features).
- Constitutional rule (extends D1 CLI-first): **every feature must exist as an
  mpremote-level capability first; the extension is a thin TS wrapper.** If a
  feature can't be expressed as "spawn mpremote / call its module API", it doesn't
  go in the extension. Hard work lands in mpremote (upstreamable), not TypeScript.

## Decided (2026-07-06)

- **Build new extension, do not extend MicroPico.** MicroPico is RP2-only by
  design; we need multi-port, and the debug adapter architecture (config provider
  over `mpremote debug`) is ours. Record the rationale publicly — "why not
  MicroPico" will be the community's first question.
- **mbm-adopt PRs where features overlap** (consistent with D6): needed mpremote
  primitives (rfc2217/TCP #19062, reconnect #17322, etc.) are pulled into the
  composition rather than feature-detected or blocked on upstream-first.
- **Broker integrates into mpremote** (preferred over a long-lived CLI process
  wrapper or TS-side implementation): the single-owner serial arbitration /
  connection-multiplexing layer (one session per device serving DAP control
  plane, REPL terminal, fs ops, mount fs-RPC) is mpremote work, upstreamable.
- **Stubs: follow the micropython-stubs project's best-practices guidance**
  (Josverl's documented Pylance/venv setup — per-port `micropython-<port>-stubs`
  PyPI packages installed as project dev-deps; do not invent a parallel
  mechanism). Addition: ship `.pyi` for OUR added APIs (`frame.f_locals`,
  settrace extensions, the `debugpy` module) from the micropython-lib package,
  since public stubs don't know them.
- **Use mpflash** (Josverl) for board detection (USB VID/PID database) and
  flashing — wrap, don't reimplement.
- **Use mpbuild** (mattytrentini, containerized per-board toolchain builds,
  integrable as a library) for firmware builds — both the extension's
  "build custom debug firmware without a local toolchain" flow and candidate
  build engine for STORY-3.2 CI builds (evaluate there at phase entry).

## Feature set for the extension roadmap (research fan-out pending)

1. **Connection broker** — the architectural keystone; phase 0 of the extension
   roadmap. Generalizes SPIKE-2.1's control-plane/data-plane split. Everything
   below stacks on it.
2. **Debug adapter** — EPIC-7, already ticketed (s7.1/s7.2/s7.4) in the main
   roadmap.
3. **REPL terminal** — VS Code pseudoterminal over the broker; Ctrl-C/Ctrl-D
   pass-through; run-current-file drops into it.
4. **Stubs integration** — probe board (`sys.implementation` over raw-REPL, same
   pattern as the caps handshake) → matching stubs package/version → offer venv
   dev-dep + Pylance settings, per stubs-project guidance.
5. **Firmware awareness + flashing** — read the caps handshake, diagnose "your
   firmware lacks settrace" plainly, offer a debug-capable build: fetch from this
   repo's CI releases (Q2/STORY-3.2, provenance via `firmware.toml`) or build
   locally via mpbuild; flash via mpflash.
6. **Device filesystem explorer** — VS Code `FileSystemProvider` over
   `mpremote fs` through the broker.
7. **mip installer** — wraps `mpremote mip install`; discovery via MIM
   (checkmim.com — "mim indexes mip", community index of micropython-lib +
   third-party GitHub/GitLab mip packages; site is fully client-rendered, so the
   ticket must establish whether it exposes a queryable JSON API or is
   link-out-only). Companion helpers: scaffold/validate `package.json` (the mip
   third-party package format) and scaffold `manifest.py` (the freeze /
   micropython-lib format — connects to firmware builds, item 5).
8. **No-hardware mode** — debug the unix port locally (EPIC-5 flow); best
   onboarding/demo story, works in Codespaces.
9. **Status-bar device presence** — selected device (persist by
   `/dev/serial/by-id`, never raw device nodes), port/variant, mem/fs stats.

Rejected/deferred (record in the extension roadmap so they don't creep):
Jupyter/notebook support (micropython-magic exists; different audience), project
scaffolding templates, on-device test runners (v2 at best), WebREPL transport
(superseded by TCP DAP + rfc2217 REPL via D6/#19062).

## Mechanics

- New roadmap document `planning/vscode/ROADMAP.md`, produced per the
  phased-roadmap procedure. Research fan-out dimensions: MicroPico/Pymakr
  feature-and-failure audit; micropython-stubs best-practices as actually
  documented; mpflash capabilities/API; mpbuild capabilities/API; MIM API
  existence; VS Code `FileSystemProvider`/pseudoterminal API constraints; broker
  design study.
- Its phase 0 depends on main-roadmap EPIC-7 completing; later phases push work
  back into mpremote (broker, transports) per the constitutional rule.
- When the extension gets its own repo (marketplace release cadence), the
  sub-roadmap migrates with it, per the repo's "pieces leave as they find homes"
  rule.
