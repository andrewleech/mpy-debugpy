# A mounted debug session never starts its target on esp32 or rp2

Date: 2026-09-23
Top repo HEAD: 1eaf939673 (before this note)
micropython: bfdb690267309ab93fb59991bd1692e6fc45eaf2 (mpy-debugpy; working tree dirty, see "Tree state")
micropython-lib: 788b2a556808957913663bfbfb7672ce01acdd9d (mpy-debugpy)

Status: rp2 ROOT-CAUSED AND FIXED (working tree), esp32 unconfirmed - see
"2026-09-23 correction" at the end. The title and the sections before the
correction are the original capture and are wrong about what happened.

Bench:

- ESP32-C3 (QFN32, rev v0.4, 4 MB), `ESP32_GENERIC_C3`, firmware
  `v1.29.0-41.gbfdb690267.dirty`, built from this tree with settrace enabled by
  an uncommitted edit to `boards/ESP32_GENERIC_C3/mpconfigboard.h`. USB
  Serial/JTAG console, WiFi, 192.168.0.84, `pm=PM_NONE`.
- Raspberry Pi Pico (RP2040), `_build='RPI_PICO-DEBUG_NCM'`, firmware reports
  `1.30.0-preview` - **not** from this tree's pin, which is `v1.29.0-41`, so its
  provenance is unrecorded. Network over `network.USBD_NCM`, board
  169.254.141.1, host 169.254.56.80 (IPv4 link-local, no DHCP from the board).
- Host: WSL2, both boards passed through with `usbipd`.

## Symptom

`mpremote debug --source src target:main` against either board reports a
correct `MPDBG-READY`, with `pathMappings`
`[{"localRoot": "<abs>/src", "remoteRoot": "/remote"}]`. A DAP client then gets
`initialize`, `attach`, `setBreakpoints` (verified) and `configurationDone`
answered within 0.2-1.3 s, and after that **nothing**: no `stopped` event,
no program output, for as long as anyone waited (40 s and 180 s on the C3,
30 s on the Pico). `src/target.py:104` is reached in under a second when the
same file is not mounted.

The last green hardware run, `20260825_hil_PYBD_SF6.md`, is **the same two
pins** and has `test_hil_mount_breakpoint_in_a_file_the_board_never_had`
passing with `mount_breakpoint_seconds = 1.0`. So the code is identical and
the outcome depends on the board: stm32 (PYBD-SF6W) works, esp32 and rp2 hang.

## What was separated out

| arrangement | C3 (WiFi) | Pico (NCM) |
| --- | --- | --- |
| `mpremote mount src exec "import target"`, no debugger | imports in 390 ms | not run |
| `mpremote debug target:main`, file on the board, no mount | stops at 104, locals right, step and continue work | stops at 104 in 0.4 s |
| `mpremote debug --source src target:main` | hangs after `configurationDone` | hangs after `configurationDone` |

So mount alone works and the debugger alone works; only the combination
fails, on two ports that differ from each other in console transport (USB
Serial/JTAG vs TinyUSB CDC) and network (WiFi vs NCM).

Two further observations, not yet explained:

- After a hung C3 session was stopped and restarted, the next connect read
  `b'\r\nInterrupted by user\r\n\x18\x01'` from the console: a `0x18`-marked
  filesystem RPC request from the device that nothing had answered. The host
  side's `_pump_mount` thread was running for the whole session (it discards
  console bytes by design, which is why no program output appears either
  way), so either it did not see the request or it saw it and did not answer.
- A mounted session that fails before the handshake (the Pico case in the
  address-discovery note below) reports
  `could not unmount ... cleanly (could not enter raw repl)`, yet the board
  answered the next `mpremote exec` normally.

## Where to start

The pin is shared with a green stm32 run, so the question is what stm32 does
that esp32 and rp2 do not, on the path from `configurationDone` to the first
RemoteFS call, with `sys.settrace` active. Candidates, none tested:

1. Whether the RemoteFS code on the device is running under the debugger's
   trace function, and whether the trace function's message pump
   (`process_pending_messages`) interacts with RemoteFS's reads of
   `sys.stdin.buffer` differently where the console is not a `dupterm`-held
   `USB_VCP` object (stm32's REPL mechanism, per the STORY-6.7 risk row).
2. Whether `SerialIntercept` on the host ever receives the `0x18` byte: a
   `--dap-log` run changes nothing here, since DAP is on TCP, so instrument
   `_pump_mount` or capture the serial stream directly.
3. The HIL suite drives the production command generically; running
   `tests/hil/test_hil_mount.py` with `MPY_DEBUG_HIL_DEVICE` pointed at either
   board should reproduce this under the suite's own results record, which is
   the reproduction to keep.

## Found on the way

- **Address discovery ignored USB networking.** `_detect_host()` in
  `tools/mpremote/mpremote/mpy_launch_debugpy.py` asked only `network.LAN` and
  `network.WLAN(STA_IF)`, so an NCM-only board bound `0.0.0.0` and mpremote
  refused it (`the device reported no network address ... refusing to
  guess`). `docs/debugging.md` "Custom builds: use networking" says nothing in
  this project changes for `USBD_NCM`, which was false. Fixed in the working
  tree by trying `network.USBD_NCM` first; with it the Pico reports
  169.254.141.1 and the unmounted session above works. **Not committed and
  not on a feature branch** - it belongs on `mpremote_debug_command`, and
  `make integrate` will discard it until it is folded there.
- **The extension cannot bind breakpoints on an unmounted device target.**
  `extension/src/extension.ts` passes only the handshake's `pathMappings`,
  and the handshake carries none without a mount. `pdb_adapter.should_stop`
  matches `co_filename` exactly (the `basename`/`ends_with_path` helpers
  above it are unused), so VS Code's absolute host paths never match a device
  path like `target.py`. With the mount path hung, no F5 flow reaches a
  breakpoint on either board. Workaround, verified on the Pico: put the module
  in `/lib` (so `co_filename` is `/lib/target.py`) and use a plain `debugpy`
  attach config with `pathMappings: [{localRoot: <src>, remoteRoot: "/lib"}]`.
  A device-relative path (`target.py`, from the `''` entry of `sys.path`)
  cannot be mapped at all, since mapping always produces `<remoteRoot>/...`.
- **esptool's RTS reset left the C3 in the ROM bootloader** after
  `write_flash` through usbipd; `--after watchdog-reset` gets it out.
- **dhcpcd on the NCM link adds a default route** through the board (metric
  1001013, so it never won, but it is wrong); delete it after bringing the
  link up.

## Tree state

`micropython` working tree at the time: modified
`ports/esp32/boards/ESP32_GENERIC_C3/mpconfigboard.h` (settrace flags),
`ports/esp32/lockfiles/dependencies.lock.esp32c3`, `lib/micropython-lib`
(submodule pointer), `tools/mpremote/mpremote/mpy_launch_debugpy.py` (the
NCM fix above); untracked `tools/mpremote/mpremote/serial_dap.py`. The host
mpremote was the submodule's, installed with
`uv tool install --editable ./micropython/tools/mpremote`.

## 2026-09-23 correction: nothing hung; the program died and nobody said so

Measured on the Pico by wrapping pyserial's `read`/`write` with timestamps
under the real `mpremote debug --source src target:main` (script at
`/tmp/trace_mpremote.py`, not kept). The mount was fine: the RPC exchange
was answered throughout, `/remote/target.py` was stat'd and read at ~12 s,
and the device printed `Running debuggable code...`. `main()` then calls
`scanner()` → `wifi_scan.run_scan()` → `network.WLAN(...)`, and the Pico has
`network` (for NCM) but no `WLAN`, so it raised
`AttributeError: 'module' object has no attribute 'WLAN'` before ever
reaching line 104. The unmounted run above "worked" only because `wifi_scan`
was not on the board, and `scanner()` catches that `ImportError`.

Two tooling faults turned an ordinary program error into an apparent hang:

1. `_pump_mount` threw away everything `SerialIntercept` handed it. That
   stream is the program's console with the RPC already taken out - clean,
   whatever the old docstring said - so the traceback went nowhere. It also
   never noticed the boot script ending (the raw-REPL `\x04` ... `\x04>`),
   so `mpremote` stayed "attached" to a finished program until Ctrl-C.
2. The boot script never ended the DAP session. With no server thread, a
   finished program leaves an open connection that answers nothing, so a
   client cannot tell a dead program from a busy one.

The `b'\r\nInterrupted by user\r\n\x18\x01'` in the C3 observation fits the
same picture: the boot script caught the next connection's Ctrl-C, and the
STAT request after it came from its own exit path, not the original stall.

Timing, the question that prompted this: neither boot.py nor any handshake
timeout is involved. The handshake was complete at 0.8 s. What is slow is
the target import, since the RemoteFS client code runs under the debugger's
trace function (a 1 ms socket poll on every trace event): 12.5 s from
`configurationDone` to the line-104 stop on the Pico, against 0.4 s
unmounted and 1.0 s on the 2026-08-25 PYBD run. Slow, not stuck, and not
addressed here.

Fixes, in the working trees (not committed, not on a feature branch):

- `mpremote` `commands.py`: `_pump_mount` sends the console to stdout
  through a `ConsoleSink`, and on the exec's second `\x04` prints the REPL's
  own part to stderr and sets a `finished` event. `_stay_attached_mount`
  returns on it ("the program on the device has ended"), and the mounted
  `--dap-log` path passes it as `device_gone`. The usual teardown then
  unmounts. Belongs on `mpremote_debug_command`.
- `mpy_launch_debugpy.py`: at exit the boot script prints the target's full
  traceback (was `Error: {e}` with no location), mirrors it to the client as
  an `output` event, and calls `debugpy.disconnect()` before giving the REPL
  stream back, so the client sees the connection close. Same branch.
- `src/wifi_scan.py`: skips the scan on a board with no `network.WLAN`.

Verified on the Pico, NCM, mounted, with the real `mpremote` command:

- a target that raises: the client got the traceback as `output` and then
  EOF at 1.9 s; `mpremote` printed the output and traceback and exited 0;
  the board answered `mpremote exec` afterwards.
- `src/target.py`, breakpoint 104: stop at 12.5 s in `mathematics`, locals
  `numbers=[3, 4, 5] i=0 num=3`, continue to the end, EOF at 14.0 s,
  `mpremote` printed all three iterations and exited 0.

**esp32 is not confirmed.** The C3 has `WLAN`, so the scan ran there, over
the same station interface the DAP connection uses; whether that, or
something else, is what stopped it cannot be told from the original capture,
because the pump discarded the evidence. With the pump fixed, rerunning the
C3 prints what happened.

## 2026-09-23: committed

Both `mpremote` fixes above (address discovery, and the pump and boot-script
changes) went onto `mpremote_dap_repl`, the registered tip of the stack, as
`e1e74ebbd0` and `81cc708122`, not onto `mpremote_debug_command` as said
above: the changed code sits beside `--dap-repl`'s (`device_gone`,
`_release_repl_stream`), so it does not apply without it. The integration
branch carries them as cherry-picks. The `debug_session.py` docstring
correction is `add-debugpy-support` in `micropython-lib`, likewise
cherry-picked onto its integration branch.
