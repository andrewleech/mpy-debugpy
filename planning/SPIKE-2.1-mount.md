# SPIKE-2.1 — Does mpremote `mount` work while a MicroPython program is being traced/paused under `sys.settrace`?

Status: code-analysis complete, no live reproduction obtained (no hardware, no QEMU binary
available in this environment — see "Reproduction attempt" below). Confidence: high for the
mechanism claims below; medium for the overall verdict, pending the hardware check this
document specifies.

## Question

The roadmap's hypothesis (`ROADMAP.md`, "The mount mechanism, and why it is a spike"): mount
serves the device filesystem via an RPC (`SerialIntercept`) over the same serial line, and
only while the device is sitting in its REPL loop; a program paused/traced under `settrace`
is not servicing that RPC, so mount reads would hang or fail while debugging.

## Mechanism analysis

Source: `~/ampremote/micropython/tools/mpremote/mpremote/transport_serial.py` (all line
numbers from this file unless stated otherwise).

### How the RPC actually gets serviced (host side)

`do_mount` (`commands.py:583-587`) calls `state.transport.mount_local(path)`
(`transport_serial.py:363-370`), which: uploads `fs_hook_code` to the device if not already
present, calls the device-side `__mount()`, then replaces `self.serial` with a
`SerialIntercept(self.serial, self.cmd)` wrapper (`:370`).

From that point on, **every host-side serial read goes through `SerialIntercept`**
(`:1063-1111`). `SerialIntercept.read`/`inWaiting` call `_check_input` (`:1070-1086`), which
sniffs the byte stream: if it sees `0x18`, it reads the command type byte, acks with `0x18`,
and dispatches into `PyboardCommand.cmd_table[c]` (`:1046-1060`) — the actual host-side file
op (`do_stat`, `do_open`, `do_read`, …, `:898-1044`). Anything else goes into `self.buf` as
ordinary program output.

Crucially, `SerialIntercept` is not a background thread or a separate poll loop — it only
runs when *something on the host* calls `.read()`/`.inWaiting()` on it. The thing that calls
it, for the scenario this spike cares about (`mpremote mount src run target.py`, or `mount`
chained before a `debug`-style launch), is `follow()` (`:246-260`), which is used by
`_do_execbuffer`/`do_run` to wait for the target script to finish. `follow()` calls
`read_until()` (`:135-176`), which loops with `time.sleep(0.01)` between polls
(`:175`) until it sees the EOF marker `\x04` — i.e. **until the raw-REPL exec of the target
script terminates**. This loop runs continuously for the entire lifetime of the on-device
process, independent of what that process is doing internally.

### What the device does when paused (debugpy side)

Source: `/home/anl/mp_debugpy/micropython-lib/python-ecosys/debugpy/debugpy/`.

- `listen()` (`public_api.py`) binds a `socket.socket()` — a TCP socket, not `sys.stdin`/
  `sys.stdout`. This is a channel entirely separate from the raw-REPL serial connection
  mpremote used to start the process.
- The trace callback (`debug_session.py:453`, `_trace_function`) calls
  `process_pending_messages()` on every `call`/`line`/`return` event, and on a breakpoint
  match calls `wait_for_continue()` (`pdb_adapter.py:284`), a busy-poll:
  `while not continue_event: process_pending_messages(); sleep(0.01)`.
- `process_pending_messages()` only touches the debugpy TCP socket. It never reads/writes
  `sys.stdin`/`sys.stdout`, and it does not call into `os`/VFS.

So while the device is paused, it is spinning a `sleep(0.01)` loop polling its own TCP
socket. It issues **no filesystem calls at all** while paused — a paused program does not
execute new statements, and therefore never triggers a `RemoteFS`/`RemoteCommand` round
trip (`fs_hook_code`, `transport_serial.py:484-829`) during the pause. The device-side mount
hook (`RemoteCommand.rd_into` etc.) is only invoked synchronously, in-line, as part of an
active `open`/`read`/`write`/`stat` call — never from a background task, never from the
trace/pause loop.

On the host side, meanwhile, `follow()`'s `read_until` loop is still running (it doesn't
know or care that the device is "paused" — from its point of view the raw-REPL exec simply
hasn't produced the `\x04` terminator yet), so if the device *did* issue an fs RPC at that
moment, the host would still service it.

### Where this actually matters: import time, not pause time

Mount's function is to let the device `import` files directly from the host filesystem
instead of a device-local copy. Imports happen when a module is first loaded (program
startup, or a lazy `import` statement executed as code runs) — i.e. while the device is
actively executing, which is exactly when the host's `follow()` loop is guaranteed to be
polling. Once a module is imported, its bytecode lives in device RAM; the interpreter never
re-reads the source file to execute subsequent lines. So:

- Mount can serve the **initial import** of the target script and the debugpy package
  itself, whether that happens before `debug_this_thread()`/`sys.settrace()` is installed,
  or via a later lazy `import` while stepping (that import executes as an ordinary traced
  statement — the trace fires `call`/`line` events for it like anything else, and the host
  read-loop is not blocked, so it still gets serviced).
- Mount **cannot** by itself retroactively re-execute or re-link already-loaded bytecode:
  editing the file on the host does not change the code of a module already imported. This is
  an ordinary Python/MicroPython import-caching property, unrelated to serial RPC servicing or
  to `settrace`/pause state.

  **Correction (see follow-ups below).** The earlier framing that this makes live iteration
  impossible was too strong. Because mount makes the edited file *already present* on the
  device filesystem, the import cache is straightforward to defeat:
  - **Between runs / re-import:** `del sys.modules["target"]; import target` (or simply
    re-running the launcher, which re-imports) picks up the edited file with **no upload** —
    mount already put it there. This is the fast edit→debug loop and it works on any board
    that can mount, not only the network-DAP case.
  - **Mid-pause, in-session:** the device is *not at the REPL* while paused — it is spinning
    inside `wait_for_continue()` under the trace callback (`pdb_adapter.py:284`), so the REPL
    is unavailable for live monkey-patching during a pause. The in-session lever is the DAP
    `evaluate` request, but it currently calls `eval()` (`pdb_adapter.py:652`), so a name can
    be rebound (`g = lambda: ...`) but a multi-line function cannot be redefined. Switching
    `evaluate` to `exec` for the `repl`/`clipboard` contexts would make in-session live
    redefinition real.
  - **Between runs, at the REPL:** ordinary REPL monkey-patching / redefinition applies as
    usual, since the REPL is free once the traced program has exited.
  - In all cases, redefinition never affects the **currently executing frame** (already-
    compiled bytecode on the stack); it takes effect on the next call.

### The real conflict case (not what the roadmap's hypothesis describes)

The one scenario where mount's serial RPC and a debug session genuinely collide is if DAP
traffic is *also* multiplexed onto the same serial line the mount RPC uses (`SerialIntercept`
sniffs `0x18`-prefixed bytes to detect fs commands; unrelated DAP JSON framing on the same
wire would corrupt or be swallowed by that sniffing, and vice versa). That is exactly
STORY-2.2's scope (DAP over serial), and it does not exist today — `debugpy.listen()` always
opens a TCP socket. So today, with debugpy over the network transport (ESP32 WiFi) and
source mounted over USB serial, the two channels are physically and logically independent;
there is no contention to resolve. If STORY-2.2 lands and DAP is later tunnelled over the
same serial line as an active mount, that combination would need an explicit multiplexing
protocol — a new problem for STORY-2.2/6.1 to solve, not evidence against mount-while-paused.

## Reproduction attempt

Timeboxed: no `/dev/serial/by-id/*` device present in this environment
(`ls /dev/serial/by-id/` → no such directory), and no `qemu-system-arm`/`qemu-system-xtensa`
binary installed (checked `PATH` and `/`). The micropython `ports/qemu` source tree exists
in the submodule, and PR #18327 (QEMU PTY detection, already integrated — see
`3f8cf5d1f2`/`e515d3e3ce` etc. in `git log` of the `ampremote` `micropython` submodule) would
let a built QEMU firmware be mounted-and-debugged exactly like a real board via its PTY, but
building a QEMU target from scratch was out of scope for this timebox (cross-toolchain +
QEMU-arm build, not a quick step). A prebuilt `firmware/esp32_settrace_save_names/firmware.bin`
exists in this repo, but there is no ESP32 hardware attached to flash and exercise it against.
No live repro was performed; the verdict below is from code analysis only.

**What a hardware/QEMU check would need to confirm** (concrete, falsifiable):
1. Flash `firmware/esp32_settrace_save_names/firmware.bin` (or build the QEMU target) to a
   board reachable at `/dev/serial/by-id/...`, with WiFi configured.
2. `mpremote connect <dev> mount src run boot_debug.py` where `boot_debug.py` does
   `debugpy.listen()` (network), `debug_this_thread()`, sets a breakpoint, and imports a
   second module lazily mid-function (to force an import-time mount RPC after tracing is
   active).
3. Attach VS Code/`fake_vscode` over TCP, hit the breakpoint, confirm the process is paused
   (`stopped` event received), leave it paused for several seconds.
4. From the host, independently touch the mounted directory (e.g. `mpremote fs ls :/remote`
   in a second connection, or watch mpremote's own log/trace) to confirm the fs RPC channel
   is still alive and responsive during the pause — this is the one thing code reading alone
   cannot prove with certainty (e.g. unforeseen host-buffer starvation, USB-CDC quirks under
   long idle periods, or WiFi/serial ISR interaction specific to the ESP32 port).
5. Resume, step to the lazy import, confirm it succeeds by reading from `/remote` at that
   moment (i.e. it doesn't fail as "stale copy" but hits the RPC).

## Verdict

**CONDITIONAL YES** for the case that matters to the roadmap's near-term plan
(serial-mounted source + network-transport debugpy), confidence: **high** on the mechanism,
**medium** on the overall verdict pending the hardware check above.

- Mount's fs-RPC servicing depends only on the host's `follow()`/`read_until()` loop still
  being active (i.e. the on-device raw-REPL exec of the target process hasn't returned), not
  on whether that process is "in its REPL loop" vs. paused inside a trace callback. A paused
  debugpy program issues zero filesystem calls during the pause (`wait_for_continue` only
  touches its own TCP socket), so there is nothing pending to service, and nothing about
  `settrace`/pause blocks mount from serving an import that happens before or after the
  pause.
- The middle path the roadmap anticipated is real, but for a different reason than stated:
  mount does not, and cannot, give **live source editing while paused at a breakpoint** —
  not because of an RPC-servicing conflict, but because MicroPython (like CPython) does not
  re-read/re-link already-imported bytecode. That limitation exists independent of mount,
  settrace, or serial.
- The one genuine conflict (shared-wire contention between mount's `SerialIntercept` framing
  and DAP bytes) only arises if DAP is also tunnelled over the same serial line — STORY-2.2
  territory, not built yet, and orthogonal to the "paused under settrace" question this spike
  was scoped to answer.

## Downstream consequence for EPIC-4

- **If confirmed by hardware** (expected, high confidence from the mechanism): mount is
  viable for serial devices whenever debugpy uses network transport. EPIC-4 collapses close
  to the roadmap's YES branch: STORY-4.1 (ensure the debugpy module itself is present,
  compiled/cached) remains needed, but the target *script* source no longer needs
  hash-verified incremental sync — `mount` serves it directly and always-live for anything
  not yet imported in the current run. STORY-4.2/4.3 (incremental sync + staleness guard) are
  **not needed for the mount-capable path**; a "device is running an mounted, always-current
  copy" fact replaces the staleness guard entirely for that path.
- **Still needed regardless of this spike's outcome:** sync/staleness machinery for the unix
  port (no serial, mount is not applicable there — confirmed in `BACKGROUND.md`) and for any
  future combined serial-mount + serial-DAP configuration (STORY-2.2 territory), where the
  shared-wire conflict above would force a choice between mount and serial-tunnelled DAP, or
  a multiplexing scheme neither currently has.
- **Live *iteration* is in scope; live-edit of an already-paused frame is not.** The roadmap's
  design-position text ("if mount can be made to work under settrace, live source mount
  replaces the entire sync/staleness engine") holds more strongly than first written: mount
  removes the need to pre-copy files before each *run*, AND — combined with `sys.modules`
  eviction + re-import — gives a fast edit→re-import→debug loop with no upload step at all.
  What still does not hold is that an edit takes effect inside the *currently paused frame*
  (already-compiled bytecode on the stack); that is ordinary import-caching semantics, not a
  serial/RPC or settrace limitation. See the "Correction" under Limitations above.

## Follow-up stories surfaced by this spike

- **STORY-4.5 (proposed) — Hot-reload iteration loop via `sys.modules` eviction + re-import.**
  A `mpremote debug`-side affordance (and/or launcher support) to re-run the target after a
  file edit by evicting the target module(s) from `sys.modules` and re-importing, so a mounted
  edit is picked up without re-uploading or a full soft-reset. Makes mount + re-import the
  mainline device iteration loop. Component: ampremote/mpremote + wrapper. Depends on
  STORY-2.1 (this), STORY-4.1. Effort S/M, risk low-med.
- **STORY-6.5 (proposed) — Extend DAP `evaluate` to `exec` for repl/clipboard contexts.**
  Let the `evaluate` request run `exec()` (not just `eval()`) when the DAP `context` is
  `repl` or `clipboard`, enabling in-session live redefinition of multi-line functions /
  monkey-patching from the debug console. Never affects the currently executing frame; takes
  effect on next call. Component: micropython-lib debugpy (`pdb_adapter.py:635-655`). Depends
  on nothing structural; upstream-friendly. Effort S, risk low.

## Recommendation

Proceed on the assumption of CONDITIONAL YES for STORY-4.1 scoping (network-transport
debugging + serial mount), but do not close this spike's confidence to high without the
hardware check above — specifically item 4 (fs RPC responsiveness during an actual multi-
second pause on real ESP32 hardware), since idle-USB-CDC/ISR starvation is the one class of
failure code reading cannot rule out. Treat STORY-2.2 (DAP over serial) as the place the
real mount conflict lives, not this story.
