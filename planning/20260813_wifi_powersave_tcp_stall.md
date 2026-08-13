# The network intermittent is not debugpy: WiFi power save wedges one TCP connection

Date: 2026-08-13
Top repo HEAD: c793ac4 (before this note)
micropython: b3ce072d718ad1e564ab326bdcfed3a8b3ac6a21 (mpy-debugpy)
micropython-lib: 2a9bad0971b7700349d233ea82e1669b42e99eb5 (mpy-debugpy)

Bench: PYBD-SF6W, firmware `v1.29.0-preview.717.g4eaafbc5bc`, RSSI -58 to -60, board 192.168.0.114, host 192.168.0.8.

Reproduction: `repro/20260813_wifi_powersave/` - `device.py` on the board, `client.py` on the host. No `debugpy`, no `sys.settrace`, no mpremote.

## The claim

A PYBD-SF6W in cyw43 power-save mode PM1 (`network.WLAN.PM_POWERSAVE`) stops
accepting data on an established TCP connection after a few small
request/response exchanges, permanently, while the rest of its networking keeps
working. Nothing in this repo's code is involved: the reproduction is a 120-line
MicroPython script that echoes fixed-size replies.

The HIL network intermittent in the risk register has that signature. It is not
proven to be the same fault - the bench default is PM2, where the reproduction
is clean 120 times out of 120 - but power-save mode is now a measured variable
that governs it, which nothing before this identified.

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
