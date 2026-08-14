# The network intermittent is not debugpy: WiFi power save wedges one TCP connection

Date: 2026-08-13
Top repo HEAD: c793ac4 (before this note)
micropython: b3ce072d718ad1e564ab326bdcfed3a8b3ac6a21 (mpy-debugpy)
micropython-lib: 2a9bad0971b7700349d233ea82e1669b42e99eb5 (mpy-debugpy)

Bench: PYBD-SF6W, firmware `v1.29.0-preview.717.g4eaafbc5bc`, RSSI -58 to -60, board 192.168.0.114, host 192.168.0.8.

Working manual for anyone picking this up: `20260814_wifi_stall_investigation_handover.md` -
every measurement, every hypothesis and how it died, the instrumentation recipes, and the
traps. This note is the evidence record; that one is how to carry on.

Reproduction: `repro/20260813_wifi_powersave/` - `device.py` on the board, `client.py` on the host. No `debugpy`, no `sys.settrace`, no mpremote.

## The claim

A PYBD-SF6W in cyw43 power-save mode PM1 (`network.WLAN.PM_POWERSAVE`) stops
accepting data on an established TCP connection after a few small
request/response exchanges, permanently, while the rest of its networking keeps
working. Nothing in this repo's code is involved: the reproduction is a 120-line
MicroPython script that echoes fixed-size replies.

The HIL network intermittent in the risk register has that signature. It is not
proven to be the same fault, and the reason to be careful is that **every
sighting in that row happened at PM2**: the bench boot script sets no `pm`, so
it runs the default `PM_PERFORMANCE` (0xa11142), which is cyw43 PM2 and still a
sleeping mode. PM1 is therefore an amplifier - it turns a fault seen about once
in twenty runs into one that happens every time, which is what made it possible
to strip the session down to this script - not the condition for the fault.

What links the two is a signature, a board and a transport. What does not yet
link them is a measurement: the capture and the device probe below both come
from PM1 runs, and 2026-08-11's capture points the other way on direction
(nothing left the board there; every PM1 wedge is inbound). A long soak of this
reproduction at PM2 is what would settle it.

## Measurements

The exchange in every arm below is the wire shape of a real session, taken from
a capture: request 205 bytes -> reply 362; request 206 -> replies 73 and 209;
request 83 -> reply 120. Only the third one is ever missed.

| arm | result |
|---|---|
| PM1 (`PM_POWERSAVE`, 0x11) from boot | 6/6 wedged, five of them at request 3 |
| PM1, no `settimeout` alternation in the server loop | 6/6 wedged at request 3 |
| PM1, 1 s gap between requests instead of 50 ms | 5/5 wedged at request 3 |
| PM2 (`PM_PERFORMANCE`, 0xa11142 - the default) | 0/120 |
| PM_NONE (0x10) from boot | 0/8 |

Through the production stack, `test_hil_stepping_advances_the_program`:

| arm | result |
|---|---|
| PM1 | 13/13 runs failed, both parametrisations, always `no response to ... within 20s` |
| PM2 (default) | 25/25 passed; separately 5/5, and 2/2 immediately after a full suite |

## The board is not asleep, and not dead

Measured during a wedge, with the connection stalled for the whole window:

- **ICMP**: 91 replies over 45 s spanning two 20 s stalls, no loss, no gap over
  1 s, 15-83 ms round trip.
- **A SYN to a closed port**: RST in 21, 22 and 25 ms, three times.
- **The board's own transmit**: it sends its FIN at the end of the session.

So frames reach the board, its IP and TCP layers answer them synchronously, and
it can transmit. One established pcb stops taking data.

## The wire

From a capture of a failing session under PM1, times relative to the SYN:

```
+0.000  host  > board  S
+0.028  board > host   S.
+0.029  host  > board  P. 205    request 1
+0.137  board > host   P. 362    reply 1              (108 ms - see below)
+0.180  host  > board  P. 206    request 2
+0.198  board > host   P. 209    reply 2, ack 412
+0.241  host  > board  P.  83    request 3  (412:495)
+0.302  board > host   .   ack 412 win 5989
+0.536  host  > board  P.  83    retransmit
+1.528  host  > board  P.  83    retransmit
+3.448  host  > board  P.  83    retransmit
+7.288  host  > board  P.  83    retransmit
+15.03  host  > board  P.  83    retransmit
        ... never acknowledged ...
```

Request 3 sits exactly at `rcv_nxt` with 5989 bytes of window open. It is
retransmitted five times over 15 s and never acknowledged, and no RST is sent,
so lwIP matched it to the pcb and dropped it.

## What the device saw

`debugpy` instrumented to report from inside `wait_for_client` every 5 s, on the
real session under PM1:

```
[PROBE] t=5007  loops=412  sock=<socket state=3 timeout=4294967295 incoming=0 off=0>
        calls=414 bytes=411 msgs=2 errnos={110: 412} buf=0 pumping=False
[PROBE] t=20012 loops=1652 sock=<socket state=3 timeout=4294967295 incoming=0 off=0>
        calls=1654 bytes=411 msgs=2 errnos={110: 1652} buf=0 pumping=False
```

The loop runs at 82 iterations a second and every `recv` times out. `incoming=0`
is `socket->incoming.tcp.pbuf == NULL`: lwIP is holding nothing for the socket
to read. `bytes=411` is exactly the two requests that were answered. So the
application is polling correctly and the data is not reaching the socket layer.

## Two earlier conclusions that were wrong

- **The `settimeout(0.001)`/`settimeout(None)` alternation is not involved.**
  `20260811_network_dap_tx_stall.md` named it as the thing to vary first. Under
  PM1 the wedge happens 6/6 with the alternation removed; under PM2 it does not
  happen 5/5 with it in place.
- **The receive window staying at 5989 proves nothing.** That note read it as
  "the board never consumed the request". lwIP only announces a window increase
  once it reaches `LWIP_MIN(TCP_WND/2, mss)` (`tcp_update_rcv_ann_wnd`), which
  here is 800 bytes; a consumed 206-byte request leaves the advertised window
  where it was.

## Where the drop is, and what is not yet known

**Superseded by the 2026-08-14 addendum below: measured with lwIP's own
counters, the wedge involves no lwIP drop at all. The reasoning here is kept
for the enumeration of `tcp_input`'s silent-drop paths, which is what made the
counters worth reading, not for its conclusion.**

In `tcp_input` the only silent drop of in-window data on an established pcb is
the refused-data path (`lib/lwip/src/core/tcp_in.c:422-435`): if
`pcb->refused_data` is still set and the new segment carries data, the segment
is dropped and nothing is sent unless the announced window is zero. Everything
else either accepts the data or answers with an ACK or a RST.

The only callback in `extmod/modlwip.c` that refuses is
`_lwip_tcp_recv_unaccepted` (line 560), installed on a pcb between lwIP
accepting the connection and MicroPython's `accept()` returning. That window is
real and is entered on every session here: `lwip_socket_accept` takes the pcb
off the queue at line 1160, drops the lwIP lock at 1162, and installs the real
receive callback at 1182, so anything arriving in between is refused. It shows
up as the 108 ms before the first reply in the capture above - the refusal
waiting for the next `tcp_fasttmr`, which retries every 250 ms.

What is not explained is why a refusal would then *persist*: after `accept()`
the callback is `_lwip_tcp_recv`, which never refuses (`SOCKET_SINGLE_PBUF` is
not defined anywhere in the tree), and both `tcp_fasttmr` and every incoming
segment retry it. Either the refusal survives the callback swap in a way this
reading misses, or the segment is being dropped below lwIP while ICMP and RSTs
still get through. Answering that is the first step of an upstream report, and
the reproduction is small enough to hand over as-is.

## What to do here

- **Documented, not worked around.** `docs/debugging.md` now says that WiFi
  power save can stall a DAP session and how to turn it off. Anything more
  automatic is a question, not a decision: the boot script `mpremote debug`
  generates could set `pm=PM_NONE` when it finds an active WLAN, but that would
  change the user's radio configuration behind their back for the length of a
  session, and a debugger is not obviously entitled to do that.
- **The bench keeps the default.** `tests/hil/board_boot.py` is deliberately not
  changed to disable power save. The risk row exists to catch this intermittent,
  and a bench configured to avoid it stops being able to.

## Addendum (2026-08-14): where the frames die, measured with lwIP's own counters

The board was flashed with an instrumented build of the current pin -
`LWIP_STATS`/`LWIP_STATS_DISPLAY` turned on in
`extmod/lwip-include/lwipopts_common.h`, plus a `lwip.print_stats()` in
`extmod/modlwip.c` returning the proto counters as a dict. lwIP's own
`stats_display()` is no use here: it formats every counter with `"%hu"` and
MicroPython's printf does not implement the length modifier, so it prints the
format string. The bench firmware was backed up first and restored afterwards;
both patches are reverted.

`repro/20260813_wifi_powersave/` gained the outbound variant used below:
`device_outbound.py` dials out to `client_outbound.py`, so no connection is ever
accepted on the board.

### The wedge involves no lwIP drop at all

Outbound arm, wedged, with pings running throughout:

| counter | before | after |
|---|---|---|
| `tcp.recv` | 19 | 26 |
| `tcp.drop` | 8 | **8** |
| `icmp.recv` | 32 | 54 |
| `icmp.xmit` | 32 | 54 |

Every one of the 22 pings sent during the stall was received and answered, and
lwIP dropped nothing. `tcp.chkerr`, `tcp.lenerr`, `tcp.proterr` and `tcp.err`
are zero for the whole session, as are the IP ones.

The accept arm does show `tcp.drop` moving (+6 across one wedge), and with the
other TCP error counters at zero the only line that can be is
`tcp_in.c:432`, the refused-data drop. That is not the wedge: the outbound arm
wedges identically with the counter untouched. The likeliest reading is stale
pcbs - the harness serves one connection at a time, so a client connecting while
it is busy is queued with `_lwip_tcp_recv_unaccepted` installed, has its first
bytes refused, and is never accepted.

### The wire says the board never receives the segment

An outbound wedge, captured:

```
+0.449  board > host   P. 209   reply 2b, ack 420
+0.499  host  > board  P.  87   request 3, seq 420:507
+0.767  host  > board  P.  87   retransmit
+1.295  host  > board  P.  87   retransmit
+2.383  host  > board  P.  87   retransmit
+4.495  host  > board  P.  87   retransmit
+8.784  host  > board  P.  87   retransmit
+17.49  host  > board  P.  87   retransmit
+34.38  host  > board  P.  87   retransmit
        the board never acknowledges 507
```

The board's last acknowledgement is 420. Eight deliveries of `420:507` over 34
seconds produce no ACK, no RST and no counter movement, while unicast ICMP to
the same station in the same window is answered without loss.

So the frames are lost between the access point and lwIP's input. Everything
above that is now excluded by measurement: not `debugpy`, not lwIP's TCP, not
MicroPython's accept path, not the socket-timeout alternation. It is
per-connection rather than per-station - a fresh connection gets through two
exchanges again while the wedged one is still being retransmitted into.

### What is left to try

- **cyw43 driver logging.** The only remaining instrument on this side is
  `CYW43_VERBOSE_DEBUG`, which would say whether the driver is handed the frame
  and discards it or never sees it. Note the trap: that output goes to the
  console, and an undrained console stops the board (see
  `20260813_console_backpressure.md`), so it has to be read while it runs.
- **A monitor-mode capture of the air**, which would separate "the AP never
  sent it" from "the chip never delivered it". Needs an adapter that can do it.
- **A second access point.** The upstream reports of this signature
  ([pico-sdk#1079](https://github.com/raspberrypi/pico-sdk/issues/1079) and the
  Raspberry Pi forum threads on `[CYW43] STALL`) include one where the fault
  disappeared on a different AP, and interop with the AP's power-save buffering
  is exactly what is in question here.
