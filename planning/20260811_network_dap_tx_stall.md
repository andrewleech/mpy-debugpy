# The HIL network intermittent is a TCP send that never leaves the board

Date: 2026-08-11
Top repo HEAD: ddadd12 (before this note)
micropython: 55103ee80a50eb08130bde79c2004e2c5fcb866b (mpy-debugpy)
micropython-lib: 2a9bad0971b7700349d233ea82e1669b42e99eb5 (mpy-debugpy)

Bench: PYBD-SF6W, firmware `v1.29.0-preview.717.g4eaafbc5bc`, associated to the bench AP at RSSI -59, board address 192.168.0.114, host 192.168.0.8 over `wlp2s0`.

## What was known before

The risk register carried it as an intermittent seen twice on 2026-08-10 and green on the six runs after: `test_hil_stepping_advances_the_program[next]` failing with `no response to setBreakpoints (seq 2) within 20s; received since: []`. The row said the next occurrence should be caught with the session's `--dap-log` on, and that a longer timeout would be papering over it.

## What is known now

It is not intermittent at the rate the row records. On 2026-08-11 it failed 4 of 5 runs of `test_hil_two_consecutive_sessions_each_run_clean` and roughly half of full network-suite runs, and a hand-driven client reproduces it 3 times out of 3. The signature is identical to the 2026-08-10 sightings.

**It is not caused by the STORY-6.7 work.** Reverting the only change to `public_api._accept_and_initialize` on the device and reinstalling, the same test still failed 4 of 5. The other device-side change (`StreamTransport`) is not on the network path at all.

The failure is a send the board believes it has made:

- `sock.send()` returns the full byte count for the `setBreakpoints` response. Instrumenting `JsonMessageChannel._send_all` prints `send_all 209` / `send_all done 209`, so `tcp_write` accepted every byte.
- `tcpdump` on the host shows nothing from the board after that point: no data segment carrying those 209 bytes, and no retransmission of anything, for the whole 20 s until the client gives up and sends FIN. The board ACKs the FIN, so the pcb is alive.
- The board's TCP had already ACKed the request itself (`ack 412`), and its advertised window shrank from 6195 to 5989 and never reopened, although Python had read those bytes out.
- The device's own loop is alive throughout: it prints `[DAP] wait_for_client: timed out after 30s waiting for configurationDone` at the end, and instrumented `recv` calls tick over at about 80 a second with `ETIMEDOUT` the whole time.
- The board answers ICMP throughout the silence: 54 replies over the 26.3 s of a failing run, no loss, worst gap 2 s.

So the input path lives and the output path is dead for that pcb. Nothing in `debugpy` is in a position to cause that: it read the request, handled it, and handed the response to the socket, which took it.

The connection setup in the same capture is worth carrying: the host's SYN went unanswered for a second, was retransmitted, and the board then sent two SYN-ACKs. The board's networking was already sluggish before any DAP traffic.

The captured exchange, times relative to the SYN:

```
+0.000  host  > board  S
+1.006  host  > board  S      (retransmit; the board has not answered)
+2.013  board > host   S.
+2.014  board > host   S.     (duplicate)
+2.014  host  > board  P. 205 bytes   initialize
+2.022  board > host   P. 362 bytes   initialize response
+2.025  board > host   P.  73 bytes   initialized event
+2.065  host  > board  P. 206 bytes   setBreakpoints
+2.278  host  > board  P. 206 bytes   (retransmit; no ACK yet)
+2.327  board > host   .   ack 412 win 5989
                            ... 20 s of nothing ...
+22.257 host  > board  F.
+22.492 board > host   .   ack 413
```

## What has been ruled out

- **The DAP layer.** Instrumented at every step from `recv` to `send`, and each one reports success.
- **The exception swallowed by `process_pending_messages`.** Replacing its `except Exception: pass` with a print produces nothing; nothing raises.
- **The board being wedged, or the VM stopped.** It prints its own 30 s timeout and answers ICMP.
- **The air.** No packet loss to the board during a failing run, RSSI -59.
- **A lock held across the receive wait.** `MICROPY_PY_LWIP_ENTER`/`EXIT` are empty on this port (`extmod/modlwip.c:93-95`), so the receive loop's `poll_sockets()` holds nothing.

## What to do next

A minimal reproduction with no `debugpy` in it: a device script that listens, accepts, reads a frame, writes a large reply, then pumps with `settimeout(0.001)` / `settimeout(None)` around each `recv` the way `process_pending_messages` does, against a host that sends a second request immediately after reading the first reply. If that reproduces, the defect belongs upstream in `extmod/modlwip.c` or the stm32 networking port, and this repo's part is to report it and to decide whether the DAP pump should stop alternating the socket timeout.

The `settimeout(0.001)` / `settimeout(None)` alternation is the one thing the DAP pump does that ordinary socket code does not, and it runs about 80 times a second while a session waits. It is the first thing to vary in that reproduction.

## What this does not change

The network transport still works: sessions that get past `setBreakpoints` run to completion, and the rest of the network suite passes in the same runs. The stall is at the first request after `initialize` on a connection whose reply follows the previous send closely.

## Correction (2026-08-13): the prescribed step was taken, and two of the claims above are wrong

See `20260813_console_backpressure.md` for the work and the measurements.

1. **The `settimeout` alternation is not it.** The minimal reproduction "What to do next" asks for was built - a device script with no `debugpy` in it, pumping at 80 Hz with the same alternation, answering the same two-send-then-one-send shape. Five trials, 5/5 answered, second reply in 6-16 ms. Removing the alternation changes nothing measurable.

2. **"A lock held across the receive wait" was not ruled out.** The reason given - that `MICROPY_PY_LWIP_ENTER`/`EXIT` are empty on this port - reads the fallback definitions at `extmod/modlwip.c:92-95` as the effective ones. stm32 defines them, at `ports/stm32/mphalport.h:66`, raising the interrupt priority to PENDSV so the lwIP background task cannot run. It is still not the cause, but nothing here tested it.

The same work found a different defect with a mechanism that accounts for every symptom recorded above - a send the application makes that never leaves the board, a receive window that never reopens although the request was handled, ICMP answered throughout, the board's own 30 s timeout eventually printed. A console a host holds open and never reads stops MicroPython in `print` (up to 500 ms per byte on a stm32 CDC) while interrupts, and so lwIP, keep running; `mpremote debug` was holding one open on every path that stays attached without a mount.

## Correction (2026-08-13, second): the signature reproduces with no debugpy in it, and power save governs it

See `20260813_wifi_powersave_tcp_stall.md`. A 120-line MicroPython script replaying this session's wire shape wedges the same way, so nothing in `debugpy` is involved. WiFi power-save mode is the variable: cyw43 PM1 (`network.WLAN.PM_POWERSAVE`) wedges 6 times out of 6, the bench default PM2 is clean 120 times out of 120, and `PM_NONE` is clean.

Two claims above are superseded by it. The `settimeout` alternation is ruled out from both directions (the wedge happens without it, and does not happen with it at PM2). And "its advertised window shrank from 6195 to 5989 and never reopened, although Python had read those bytes out" does not show what it was read as showing: lwIP only announces a window increase once it reaches `LWIP_MIN(TCP_WND/2, mss)`, 800 bytes here, so a consumed 206-byte request leaves the advertised window exactly where it was.

That does **not** retire this note. The HIL harness drains the board's console for the whole of every run, so the scenario measured here at 4 failures in 5 is not exposed to that defect, and it now passes 5/5 in both `--dap-log` modes. What the two notes together say is that the signature described here has at least one cause that is fully understood, and that this particular sighting is not yet attributed to it.
