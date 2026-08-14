# Handover: the WiFi TCP stall on the PYBD-SF6W bench

Date: 2026-08-14
Top repo HEAD at writing: f35a1f1
micropython: b3ce072d718ad1e564ab326bdcfed3a8b3ac6a21 (mpy-debugpy)
micropython-lib: 2a9bad0971b7700349d233ea82e1669b42e99eb5 (mpy-debugpy)

Everything known about the network intermittent, written so someone with no
memory of the sessions can reproduce every measurement and carry on. Companion
documents, all of which this one supersedes where they disagree:

- `20260811_network_dap_tx_stall.md` - first evidence record, two of its
  conclusions are wrong (see §7).
- `20260813_wifi_powersave_tcp_stall.md` - the power-save finding and its
  2026-08-14 addendum. The primary record; this document is the working manual
  around it.
- `20260813_console_backpressure.md` - a *different* defect found while chasing
  this one, now fixed. Read it before writing any tool that holds the board's
  console open (§9).
- `ROADMAP.md` risk register, the row beginning "The HIL network suite has an
  intermittent".

---

## 1. The symptom

An established TCP connection to the board stops making progress, permanently,
while the board carries on running and its networking answers everything else.
In the debugger this appears as a DAP request that is never answered:

```
AssertionError: no response to setBreakpoints (seq 2) within 20s; received since: []
--- board stdout during the session ---
Debugger connected from 192.168.0.8:58574
[DAP] Waiting for initialize request...
[DAP] Initialize request handled - returning control immediately
[DAP] Debug session ready - all other messages will be handled in trace function
```

The board is not wedged in any way a user would recognise. During the stall it
answers ICMP without loss and RSTs a SYN to a closed port in about 20 ms, and it
sends its own FIN when the session ends. Only that one connection is affected: a
new connection made while the old one is still being retransmitted into gets
through several exchanges before wedging in turn.

## 2. Current state of knowledge, in one paragraph

The frames are lost **between the access point and lwIP's input**. Measured with
lwIP's own counters compiled in: across a wedge, `tcp.drop` does not move,
`tcp.chkerr`/`lenerr`/`proterr`/`err` stay at zero, and unicast ICMP to the same
station in the same window is received and answered without loss, while the
host's retransmissions of one TCP segment draw no acknowledgement for 34
seconds. Everything above that layer is excluded by measurement: not `debugpy`,
not lwIP's TCP, not MicroPython's accept path, not the DAP pump's socket-timeout
alternation. WiFi power-save mode governs how often it happens - cyw43 PM1
(`network.WLAN.PM_POWERSAVE`) makes it deterministic, the default PM2
(`PM_PERFORMANCE`) makes it rare - but PM1 is an amplifier, not the condition:
every sighting in the wild happened at PM2.

## 3. Timeline of sightings

| when | what | where |
|---|---|---|
| 2026-08-10 | Two failures of `test_hil_stepping_advances_the_program[next]` in one ~5 minute window, then six green runs | risk register |
| 2026-08-11 | 4 of 5 runs of `test_hil_two_consecutive_sessions_each_run_clean` failed; a hand-driven client reproduced 3 of 3; tcpdump taken | `20260811_network_dap_tx_stall.md` |
| 2026-08-13 | One failure of `test_hil_stepping_advances_the_program[next]` in a full-suite run, then 5/5 green alone and 2/2 green immediately after a full suite | ROADMAP Status |
| 2026-08-13/14 | Reproduced deterministically at PM1 with no debugpy; localised below lwIP | `20260813_wifi_powersave_tcp_stall.md` + addendum |

All of these ran at the bench default power mode. The boot script sets no `pm`.

**Rate is not stable across days.** On 2026-08-11 the isolated scenario failed 4
of 5; on 2026-08-13 the same class of run was green 25 of 25, and the raw
reproduction was green 120 of 120. RSSI was -59 then and -58 now, so signal
strength is not the difference. Whatever varies (channel occupancy, the AP's
state, neighbouring traffic) is not under our control or measurement.

## 4. The bench

- Board: PYBD-SF6W, `PYBD-SF6W with STM32F767IIK`.
- Firmware in place: `v1.29.0-preview.717.g4eaafbc5bc` (2026-08-10), a local
  build. Backup image: `micropython/ports/stm32/build-PYBD_SF6/firmware.dfu`,
  sha256 `43429c011e93fe3e2822db8e831c73c0bfe452bcd8fdd7bdf759a40c29cf3be9`.
  **This is not the artifact `firmware/firmware.toml` publishes**, so back it up
  before flashing anything.
- REPL/console: `/dev/serial/by-id/usb-MicroPython_Pyboard_Virtual_Comm_Port_in_FS_Mode_3254335D3037-if01`
- Dedicated DAP interface: same path ending `-if03`
- DFU serial: `3254335D3037`. **A second PYBD-style device is on this host
  (`20693177554D`, a wb55) and must not be touched** - always pass
  `dfu-util -S 3254335D3037`.
- Power cycle: `mpy-dev cycle pybd`. Give it 15-25 s; the boot script's WiFi
  association fails outright often enough that a second cycle is normal.
- Network: host 192.168.0.8 on `wlp2s0`, board 192.168.0.114, RSSI -58 to -60.
- Boot script: `tests/hil/board_boot.py`, copied to the board as `boot.py`. It
  sets `pyb.usb_mode("VCP+VCP+MSC")` and brings up WiFi; it sets no `pm`, so the
  board runs `PM_PERFORMANCE` (0xa11142).

Power-mode constants on this firmware:

| name | value | cyw43 mode |
|---|---|---|
| `PM_NONE` | 0x10 | no power save |
| `PM_POWERSAVE` | 0x11 | PM1, sleeps hardest |
| `PM_PERFORMANCE` | 0xa11142 | PM2, 200 ms sleep-return - **the default, and still a sleeping mode** |

To run at PM1, copy `board_boot.py` with this line added on the success path of
`connect()` (after `wlan.isconnected()` is true, before returning the address):

```python
wlan.config(pm=network.WLAN.PM_POWERSAVE)
```

Setting `pm` before association does not stick - it is reset by
`active()`/`connect()`. Setting it mid-connection from the harness config works
but disturbs the link (switching to `PM_NONE` mid-session lost the first request
6 times out of 6, which is an artefact of the switch, not the fault).

## 5. The reproduction

`planning/repro/20260813_wifi_powersave/`. No `debugpy`, no `sys.settrace`, no
mpremote in the data path.

- `device.py` - board side, accepts connections. Serves one at a time. The
  client's first line is a JSON config, so one device run sweeps a matrix.
  Config keys: `alternate`, `nodelay`, `pm`, `pump_ms`, `run_ms`, `chatter`,
  `replies`, `stop`.
- `client.py` - host side. Flags: `--trials`, `--timeout`, `--gap` (between
  requests), `--pre-gap` (before anything is sent), `--no-alternate`,
  `--uniform N`, `--pm`, `--label`, `--stop`.
- `device_outbound.py` / `client_outbound.py` - the same exchange over a
  connection the **board dials out**, so no pcb is ever accepted. `HOST` is
  hardcoded to 192.168.0.8 in `device_outbound.py`; edit it if the host moves.

The exchange is the wire shape of a real DAP session, taken from a capture:

| step | host sends | board replies |
|---|---|---|
| 1 | 205 bytes | 362 |
| 2 | 206 bytes | 73, then 209 |
| 3 | 83 bytes | 120 |

Request 3 is the one that goes missing. Framing is a 4-byte big-endian length
prefix; the reply is raw bytes of the stated size, whose first 8 characters are
the milliseconds the device spent in `chatter` prints (0 unless `chatter` is
set - that field belongs to the console-backpressure investigation).

### Running it

```bash
cd /home/corona/mpy-debugpy
DEV=/dev/serial/by-id/usb-MicroPython_Pyboard_Virtual_Comm_Port_in_FS_Mode_3254335D3037-if01

# put the harness on the board and start it detached, so nothing holds the console
PYTHONPATH=micropython/tools/mpremote uv run --project . python -m mpremote \
  connect $DEV resume fs cp planning/repro/20260813_wifi_powersave/device.py :stallprobe.py
PYTHONPATH=micropython/tools/mpremote uv run --project . python -m mpremote \
  connect $DEV resume exec --no-follow "import stallprobe"

uv run --project . python planning/repro/20260813_wifi_powersave/client.py 192.168.0.114 \
  --trials 6 --timeout 10
```

Expected at PM1: `STALLED at request 3 (got 0 of 120 bytes)`, every trial.
Expected at PM2: `ok`, every trial.

Outbound arm - the host listens first, then the board is told to dial out:

```bash
uv run --project . python planning/repro/20260813_wifi_powersave/client_outbound.py 10 &
sleep 1
PYTHONPATH=micropython/tools/mpremote uv run --project . python -m mpremote \
  connect $DEV resume fs cp planning/repro/20260813_wifi_powersave/device_outbound.py :outprobe.py
PYTHONPATH=micropython/tools/mpremote uv run --project . python -m mpremote \
  connect $DEV resume exec --no-follow "import outprobe"
```

`exec --no-follow` matters: it leaves the board running the script with **no
host holding the console**. A held-open, unread console stops the board for
reasons of your own making (§9).

## 6. Every measurement taken

### Raw reproduction, accepted connections

| arm | result |
|---|---|
| PM1 from boot | 6/6 wedged (5 at request 3, 1 at request 1) |
| PM1, `--no-alternate` (no `settimeout(None)` between pumps) | 6/6 at request 3 |
| PM1, `--gap 1.0` (1 s between requests) | 5/5 at request 3 |
| PM1, `--pre-gap 1.0` (nothing sent for 1 s after connect) | 3/3, **at request 1** |
| PM1, outbound connection | 3/3 at request 3, plus 2 further runs |
| PM2 (default) from boot | 0/120, `--timeout 8` |
| PM_NONE from boot | 0/8 |
| `--pm 0xa11142` applied mid-session while booted PM1 | 0/6 |
| `--pm 0x10` applied mid-session while booted PM1 | 6/6 at request 1 - artefact of the switch |

### Through the production stack, `test_hil_stepping_advances_the_program`

| arm | result |
|---|---|
| PM1 | 13/13 runs failed, both parametrisations |
| PM2 | 25/25 passed; separately 5/5; 2/2 immediately after a full suite |

### The board's liveness during a wedge

- 91 ICMP replies over a 45 s window spanning two 20 s stalls: no loss, no gap
  over 1 s, 15-83 ms round trip.
- 22 further pings during an outbound wedge: all answered.
- SYN to closed port 9999: `ConnectionRefusedError` (a RST) in 21, 22 and 25 ms,
  three times.
- The board sends its own FIN at the end of the session.

### lwIP counters, instrumented firmware (§8)

Baseline after boot: `{'ip.recv': 32, 'ip.xmit': 10}`.

Accepted-connection wedge, no pings:

```
{'tcp.xmit': 5, 'ip.recv': 97, 'tcp.recv': 8, 'ip.xmit': 15}
```

`tcp.recv` froze at 8 while `ip.recv` kept climbing on background LAN broadcast.

Accepted-connection wedge, with pings:

```
before {'tcp.xmit': 5, 'ip.recv': 160, 'tcp.recv': 8,  'ip.xmit': 15}
after  {'tcp.xmit': 7, 'ip.recv': 256, 'tcp.recv': 17, 'ip.xmit': 49,
        'icmp.xmit': 32, 'icmp.recv': 32, 'tcp.drop': 6}
```

Outbound wedge, with pings - **the important one**:

```
before {'ip.recv': 400, 'tcp.recv': 19, 'ip.xmit': 49, 'icmp.xmit': 32,
        'tcp.drop': 8, 'tcp.xmit': 7,  'icmp.recv': 32}
after  {'ip.recv': 481, 'tcp.recv': 26, 'ip.xmit': 77, 'icmp.xmit': 54,
        'tcp.drop': 8, 'tcp.xmit': 13, 'icmp.recv': 54}
```

`tcp.drop` unmoved, every error counter zero, all 22 pings received and
answered, and the exchange wedged anyway.

### The wire

Accepted connection at PM1, relative to the SYN:

```
+0.000  host  > board  S
+0.028  board > host   S.
+0.029  host  > board  P. 205    request 1
+0.137  board > host   P. 362    reply 1            (108 ms; see §7 note 4)
+0.180  host  > board  P. 206    request 2
+0.198  board > host   P. 209    reply 2, ack 412
+0.241  host  > board  P.  83    request 3  (412:495)
+0.302  board > host   .   ack 412 win 5989
+0.536 … +15.03        P.  83    five retransmissions, none acknowledged
```

Outbound connection at PM1:

```
+0.449  board > host   P. 209   reply 2b, ack 420
+0.499  host  > board  P.  87   request 3, seq 420:507
+0.767 … +34.38        P.  87   eight retransmissions over 34 s
        the board never acknowledges 507
```

### The device's own view (instrumented debugpy, PM1, real session)

```
[PROBE] t=5007  loops=412  sock=<socket state=3 timeout=4294967295 incoming=0 off=0>
        calls=414 bytes=411 msgs=2 errnos={110: 412} buf=0 pumping=False
[PROBE] t=20012 loops=1652 sock=<socket state=3 timeout=4294967295 incoming=0 off=0>
        calls=1654 bytes=411 msgs=2 errnos={110: 1652} buf=0 pumping=False
```

The wait loop turns over 82 times a second, every `recv` returns ETIMEDOUT,
`incoming=0` is `socket->incoming.tcp.pbuf == NULL`, and `bytes=411` is exactly
the two requests that were answered. The application is polling correctly and
nothing is reaching it.

## 7. Hypotheses tried and what happened to them

1. **The DAP pump's `settimeout(0.001)`/`settimeout(None)` alternation.**
   Prescribed by `20260811_network_dap_tx_stall.md` as the first thing to vary.
   **Dead.** The wedge happens 6/6 with the alternation removed at PM1, and does
   not happen 5/5 with it in place at PM2. It is also absent from the
   reproduction's outbound arm in any meaningful sense.

2. **debugpy itself, at any level.** **Dead.** The reproduction is 120 lines of
   MicroPython with no debugger in it.

3. **MicroPython's accept path** - `_lwip_tcp_recv_unaccepted`
   (`extmod/modlwip.c:560`) refuses data on a pcb lwIP has accepted but
   MicroPython has not, and `lwip_socket_accept` drops the lwIP lock between
   taking the pcb off the queue (line 1160) and installing the real receive
   callback (line 1182), so the window is real and is entered on every session
   here - it shows up as the ~108 ms before the first reply. **Dead as the
   cause.** A connection the board dials out never installs that callback and
   wedges identically, 3/3. A client that sends nothing for a second after
   connecting - stepping over the window entirely - wedges *earlier*, at request
   1, 3/3.

4. **lwIP's refused-data drop** (`lib/lwip/src/core/tcp_in.c:422-435`): if
   `pcb->refused_data` is set and a new segment carries data, the segment is
   dropped and nothing is sent unless the announced window is zero. This is the
   only silent drop of in-window data on an established pcb, and the accept arm
   *does* show `tcp.drop` moving (+6), which with `chkerr`/`lenerr`/`proterr` at
   zero can only be that line. **Not the wedge.** The outbound arm wedges with
   `tcp.drop` untouched. The likeliest reading of the +6 is stale pcbs: the
   harness serves one connection at a time, so a client connecting while it is
   busy is queued with `_lwip_tcp_recv_unaccepted` installed, has its first bytes
   refused, and is never accepted.

5. **"The receive window shrank and never reopened, so Python never consumed the
   request."** **Wrong**, and it was the strongest-looking evidence in the
   2026-08-11 note. lwIP only announces a window increase once it reaches
   `LWIP_MIN(TCP_WND/2, mss)` (`tcp_update_rcv_ann_wnd`), which is 800 bytes
   here (`TCP_MSS 800`, `TCP_WND 8*TCP_MSS`), so consuming a 206-byte request
   leaves the advertised window exactly where it was.

6. **"`MICROPY_PY_LWIP_ENTER`/`EXIT` are empty on this port, so no lock is held
   across the receive wait."** **Wrong.** `extmod/modlwip.c:92-95` are the
   fallback definitions for a port that supplies none; stm32 supplies its own at
   `ports/stm32/mphalport.h:66`, mapping them to `MICROPY_PY_PENDSV_ENTER`,
   which raises the interrupt priority so the lwIP background task cannot run.
   Reentrancy is therefore *not* ruled out by that argument, and it is not ruled
   out by anything else either - see §10.

7. **Nagle holding the reply** (`lwip_tcp_send` ends with `tcp_output_nagle`,
   `extmod/modlwip.c:818`). Never directly tested with `TCP_NODELAY`, which the
   harness supports (`nodelay` config key; `modlwip.c:1528`). Made largely
   irrelevant by the outbound capture, which shows the failure is the board not
   *receiving* a segment, not failing to send one. Worth one run if someone
   wants it closed formally.

8. **Console back-pressure** - a host that holds the board's USB CDC console
   open without reading it stops the board dead, because `usbd_cdc_tx_always`
   (`ports/stm32/usbd_cdc_interface.c:393-421`) waits up to 500 ms for room for
   *each byte* while the tty's 4 kB buffer and the board's 1 kB buffer are both
   full. **A real defect, found during this hunt, fixed** (`mpremote debug` now
   drains the console on every stay-attached path), but **not this fault**: the
   HIL harness drains the console throughout, and the reproduction runs
   detached.

9. **"It only fails after a full suite has run."** **Dead.** Two full suites
   followed immediately by the scenario: 2/2 green.

10. **Signal strength.** **Dead.** RSSI -59 on the bad day, -58 on the good one.

11. **A timing race between a reply and the next request.** **Dead.** A 1 s gap
    between requests still wedges 5/5.

12. **PM_NONE as a fix.** **Unproven.** Eight trials against a fault that shows
    about once in twenty is no evidence at all. `docs/debugging.md` says so
    explicitly; do not let that caveat be dropped.

## 8. Instrumentation recipes

### 8a. Device-side debugpy probe

Two edits, both reverted afterwards. In
`micropython-lib/python-ecosys/debugpy/debugpy/common/messaging.py`, add to
`JsonMessageChannel.__init__`:

```python
self.probe_calls = 0
self.probe_bytes = 0
self.probe_errnos = {}
self.probe_last_data_ms = -1
self.probe_messages = 0
```

increment `probe_calls` before `self.sock.recv(4096)`, add `probe_bytes` and
`probe_last_data_ms` after it, record `probe_errnos[errno]` in the `except
OSError` branch, and bump `probe_messages` where a message is parsed. In
`debugpy/server/debug_session.py`, inside `wait_for_client`'s loop, print every
5 s:

```python
print("[PROBE] t={} loops={} sock={} calls={} bytes={} msgs={} errnos={} buf={} last_data={} pumping={}"
      .format(elapsed, probe_loops, ch.sock, ch.probe_calls, ch.probe_bytes,
              ch.probe_messages, ch.probe_errnos, len(ch._recv_buffer),
              ch.probe_last_data_ms, self._pumping))
```

`repr(sock)` is the valuable part: MicroPython's lwIP socket prints
`<socket state=N timeout=T incoming=PTR off=N>` (`extmod/modlwip.c:923-937`),
where `incoming` is `socket->incoming.tcp.pbuf` - non-zero means lwIP is holding
data the application has not read.

Install it with:

```bash
PYTHONPATH=micropython/tools/mpremote uv run --project . python -m mpremote \
  connect $DEV resume debugpy-install micropython-lib/python-ecosys/debugpy/debugpy
```

**`mpremote debug` does not install debugpy.** Only `debugpy-install` and the
HIL `hil_facts` fixture do. A hand-run experiment against a stale copy on the
board has cost hours before.

### 8b. lwIP statistics firmware

Two edits in the `micropython` submodule, both reverted afterwards.

`extmod/lwip-include/lwipopts_common.h`:

```c
#define LWIP_STATS                      1
#define LWIP_STATS_DISPLAY              1
```

`extmod/modlwip.c`: add `#include "lwip/stats.h"` and a function returning the
proto counters as a dict, registered as `print_stats` in
`mp_module_lwip_globals_table`:

```c
static void lwip_stats_add_proto(mp_obj_t dict, const char *prefix, struct stats_proto *p) {
    static const char *const names[] = {"recv", "drop", "chkerr", "lenerr", "memerr", "proterr", "err", "xmit"};
    u16_t values[] = {p->recv, p->drop, p->chkerr, p->lenerr, p->memerr, p->proterr, p->err, p->xmit};
    char key[24];
    for (size_t i = 0; i < MP_ARRAY_SIZE(values); ++i) {
        size_t n = snprintf(key, sizeof(key), "%s.%s", prefix, names[i]);
        mp_obj_dict_store(dict, mp_obj_new_str(key, n), MP_OBJ_NEW_SMALL_INT(values[i]));
    }
}
```

covering `lwip_stats.tcp`, `.ip`, `.link`, `.icmp` plus `lwip_stats.mem.err`.

**Do not use lwIP's own `stats_display()`**: it formats every counter with
`"%hu"` and MicroPython's printf does not implement the length modifier, so it
prints `hu` where each number should be. That was tried first and wasted a
build/flash cycle.

Build, flash and restore:

```bash
# back the current image up first - it is NOT the manifest artifact
cp micropython/ports/stm32/build-PYBD_SF6/firmware.dfu /somewhere/safe.dfu

cd micropython/ports/stm32 && make BOARD=PYBD_SF6 -j8      # ~3 minutes
cd /home/corona/mpy-debugpy
PYTHONPATH=micropython/tools/mpremote uv run --project . python -m mpremote connect $DEV resume bootloader
sleep 4
dfu-util -l                                    # confirm serial="3254335D3037"
dfu-util -S 3254335D3037 -a 0 -D micropython/ports/stm32/build-PYBD_SF6/firmware.dfu
mpy-dev cycle pybd                             # dfu-util's :leave did not work; cycle instead

# read counters
PYTHONPATH=micropython/tools/mpremote uv run --project . python -m mpremote connect $DEV resume exec \
  "import lwip; print({k: v for k, v in lwip.print_stats().items() if v})"

# restore
dfu-util -S 3254335D3037 -a 0 -D /somewhere/safe.dfu
git -C micropython checkout -- extmod/modlwip.c extmod/lwip-include/lwipopts_common.h
```

The toolchain is present at
`/opt/arm-gnu-toolchain-14.3.rel1-x86_64-arm-none-eabi/bin/arm-none-eabi-gcc`
and `dfu-util` is on PATH. The instrumented build identifies itself as
`v1.29.0-preview.726.gb3ce072d71.dirty`.

### 8c. Packet capture

```bash
timeout 45 sudo -n tcpdump -i wlp2s0 -n -U -s 96 host 192.168.0.114 and port 5678 -w out.pcap
```

`-i any` produces a 0-byte file here; name the interface. `-U` matters or the
capture is still buffered when you read it. Prefer `timeout` over killing it
afterwards (see §9).

## 9. Traps that have cost time

- **`pkill -f "<pattern>"` matches the shell running it**, which kills your own
  command (exit 144/143). Capture the PID, or use `timeout`, or match on the
  process name with `pkill -x`.
- **The HIL suite refuses a dirty tree.** Every run writes
  `planning/<date>_hil_<board>[_arrangement].md`, which is untracked, which
  dirties the tree, which refuses the next run. Delete it between iterations, or
  set `MPY_DEBUG_HIL_ALLOW_DIRTY=1` for experiments. Do not commit a record
  written by a partial run: it is named like a full-suite record.
- **A console held open but never read stops the board.** Any bespoke harness
  that opens the tty and stops reading will produce a *different* stall that
  looks like this one. Run device scripts with `exec --no-follow` so nothing
  holds the console, or drain it.
- **`uv` must run from the top repo with `--project /home/corona/mpy-debugpy`.**
  Running it inside `micropython-lib` picks that directory's environment and
  creates a stray `uv.lock`.
- **Boot-time WiFi association fails often.** `board_boot.py` already retries
  three times; a second `mpy-dev cycle pybd` and 25 s of patience is normal, and
  a manual `active(False)/active(True)/connect()` over the REPL always works.
- **The board's filesystem survives a firmware reflash** (it is in external QSPI
  flash), so `boot.py` and any probe scripts you copied are still there
  afterwards. Clean them up.
- **`mpy-dev` has no flash command**; use `mpremote ... bootloader` then
  `dfu-util -S <serial>`.

## 10. Open questions, ranked

1. **Do the PM2 sightings share this mechanism?** This is the one that matters
   for the risk register, and it is answerable without any new tooling: soak the
   raw reproduction at PM2 for thousands of trials (each is about a second) and
   see whether it ever wedges, then the same at PM_NONE. If it does wedge, the
   attribution is complete and the counters will say whether it looks the same.
   If a few thousand trials stay clean, the PM2 failures are something else and
   the row needs reopening on its own terms.
2. **Does the frame reach the cyw43 driver?** `CYW43_VERBOSE_DEBUG` in
   `lib/cyw43-driver` would separate "the chip never delivered it" from "the
   driver discarded it". Note that its output goes to the console, so it must be
   read while it runs or the console-back-pressure defect bites (§7 item 8).
3. **Monitor-mode air capture**, to separate "the AP never sent it" from "the
   chip never received it". Needs an adapter that supports monitor mode; none is
   known to be on this host.
4. **A second access point.** One of the upstream reports of this signature had
   the fault disappear on different AP hardware, and interop with the AP's
   power-save buffering for a sleeping station is exactly what is in question.
5. **Why the third exchange?** The wedge lands on request 3 with these sizes,
   independent of the gap between requests, but on request 1 if the client waits
   a second before sending anything. Whether that is positional, a byte count, a
   frame count, or a count of the board's own transmissions is untested.
   `client.py --uniform N` exists for this (N identical 83/120 exchanges) but the
   one attempt to use it hit a device-harness state problem and was not
   repeated. Restart the harness between shapes.
6. **Reentrancy.** `pyb_lwip_poll` and `cyw43_poll` are both scheduled on PENDSV
   (`ports/stm32/mpnetworkport.c:68-100`), and `MICROPY_PY_LWIP_ENTER` raises the
   interrupt priority to block them while MicroPython is inside modlwip. That
   looks correct on paper and was never disproved; the upstream reports below
   describe the equivalent rp2 arrangement as genuinely unsafe. Not obviously
   consistent with ICMP surviving, but not excluded.

## 11. External references

- [pico-sdk#1079](https://github.com/raspberrypi/pico-sdk/issues/1079) - lwIP is
  not reentrant in `NO_SYS` mode while the cyw43 wrapper calls
  `sys_check_timeouts` from an alarm/IRQ and `netif->input()` from the chip's
  IRQ callback.
- [Raspberry Pi forum: LwIP MQTT client stall](https://forums.raspberrypi.com/viewtopic.php?t=397827) -
  `snd_queuelen` pinned at maximum with both windows open, pings still answered,
  power save already off.
- [Raspberry Pi forum: `[CYW43] STALL` timeouts](https://forums.raspberrypi.com/viewtopic.php?t=390642) -
  disabling power management helped; the fault disappeared entirely on
  enterprise APs.
- [micropython#9455](https://github.com/micropython/micropython/issues/9455) -
  Pico W network becomes inaccessible after idle, association apparently intact.

## 12. Source references collected along the way

| what | where |
|---|---|
| refused-data drop, the only silent drop of in-window data | `micropython/lib/lwip/src/core/tcp_in.c:422-435` |
| where `refused_data` is set | same file, line 518 |
| the `dropped:` label, common to the malformed-packet paths | same file, line 589 |
| no-matching-pcb RST path (also bumps `proterr`) | same file, lines 576-581 |
| refused-data retry, every 250 ms | `lib/lwip/src/core/tcp.c:1482-1525` |
| `tcp_process_refused_data` | same file, from line 1543 |
| refusing recv callback for an unaccepted pcb | `micropython/extmod/modlwip.c:560` |
| accept queue full also refuses | same file, line 598 |
| the ordinary recv callback, which never refuses | same file, lines 602-626 |
| accept: lock dropped between dequeue and callback install | same file, lines 1160-1182 |
| `tcp_recved` on every application read | same file, line 909 |
| Nagle on send | same file, line 818 |
| socket `repr`, including `incoming` | same file, lines 923-937 |
| `TCP_NODELAY` support | same file, line 1528 |
| lwIP lock = raise to PENDSV priority | `micropython/ports/stm32/mphalport.h:61-68` |
| lwIP and cyw43 polling, both via PENDSV | `micropython/ports/stm32/mpnetworkport.c:68-100` |
| `TCP_MSS 800`, `TCP_WND 8*TCP_MSS`, `LWIP_STATS 0` | `micropython/extmod/lwip-include/lwipopts_common.h` |
| console transmit blocking 500 ms per byte | `micropython/ports/stm32/usbd_cdc_interface.c:393-421` |
| CDC transmit buffer size (1024) | `micropython/ports/stm32/mpconfigboard_common.h:368` |
