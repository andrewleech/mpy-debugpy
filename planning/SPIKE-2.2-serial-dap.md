# SPIKE-2.2 — Can the DAP debug channel run over the mpremote serial / raw-REPL link instead of a TCP socket?

Status: complete (desk investigation; no hardware attach attempted — see confidence note).

## Question

STORY-2.2 asks whether the DAP byte stream can be tunnelled over the existing
mpremote serial connection instead of requiring the device to join a network and expose
a TCP socket. The answer decides whether EPIC-6's serial path can be a first-class,
IP-free device transport (STORY-6.1) or whether every device flow must fall through to
WiFi + IP discovery + a TCP/localhost proxy (STORY-6.2 carrying the load).

## 1. Transport-interface analysis (what debugpy assumes of "the socket")

`JsonMessageChannel` (`micropython-lib/python-ecosys/debugpy/debugpy/common/messaging.py`)
is written directly against a `socket` object, not an abstract stream, and the coupling is
not confined to the constructor:

- `self.sock = sock` (`messaging.py:11`) — stored once.
- `self.sock.send(header + content)` (`messaging.py:51`) — write, expects `OSError` on
  failure (caught, sets `self.closed = True`).
- `self.sock.recv(1)` / `self.sock.recv(1024)` / `self.sock.recv(content_length - len(buf))`
  (`messaging.py:93,114,144`) — read up to N bytes, must raise `OSError` with
  `e.errno in (11, 35)` (EAGAIN/EWOULDBLOCK) when no data is available rather than
  blocking, and must return `b""` (falsy) to signal a closed connection.
- `self.sock.close()` (`messaging.py:179`).

The non-blocking contract is established by the caller, not the channel — `settimeout` is
called from three places outside `messaging.py`:

- `public_api.py:62` — `_debug_session.channel.sock.settimeout(0.001)` once, right after
  `initialize` is handled synchronously on the blocking socket.
- `debug_session.py:93,113,121,131,142,155` — `debug_session.py` reaches through
  `self.channel.sock.settimeout(...)` repeatedly (0.001 / 0.5 / 1.0 / `None`) to shift
  between blocking and non-blocking modes at specific protocol points (e.g. waiting longer
  for the first message after `attach`, or blocking briefly during disconnect).

So the required interface is not just "recv/send" — it is a *socket-shaped* object:
`recv(n) -> bytes` (non-blocking-capable, EAGAIN via `OSError.errno`), `send(bytes) -> None`
(raises `OSError` on failure), `settimeout(seconds_or_None)` (mutated at runtime, not just
at construction), `close()`. Any replacement stream object must implement all four, including
tolerating repeated `settimeout` calls with different values during a session — this rules
out a fire-and-forget wrapper that only supports "always non-blocking".

`public_api.py:27-46` also does the initial `listener.accept()` synchronously/blocking to get
the first client socket — the equivalent for a serial transport is "raw REPL is already
connected", so there is no accept-equivalent needed; the analogue of "listen" is just
"start reading/writing on the already-open serial fd instead of binding a TCP listener."

**Conclusion:** the transport is swappable in principle — wrap a serial file object in a
small adapter class exposing `recv`/`send`/`settimeout`/`close` with EAGAIN/`OSError`
semantics matching a MicroPython non-blocking socket — but every one of the ~9 call sites
above (all outside `messaging.py`, reaching through `.channel.sock`) needs the adapter to
behave indistinguishably from a real `socket`, including the `settimeout` state machine.
This is a from-scratch adapter, not a config flag.

## 2. Host/device serial multiplexing — the actual crux

### What already proves non-blocking bidirectional traffic over serial while code runs

The `mpremote mount` mechanism is existing evidence that a second logical channel can be
carried over one physical serial line concurrently with normal REPL traffic, **while a
program is executing (not idle at the REPL)**:

- Device side (`fs_hook_code`, `transport_serial.py:484-598`, uploaded and exec'd on the
  device): `RemoteCommand.__init__` (`:489-496`) builds a `select.poll()` registered on
  `sys.stdin.buffer` and does `self.poller.ipoll(1000)` / `self.poller.poll(1000)`
  (`:498-502`, `:528-532`) to check for input **without blocking indefinitely** — this is
  the same non-blocking-poll shape `debugpy` needs, and it works from inside a running
  script, proving `sys.stdin`/`sys.stdout` remain live, pollable streams during raw-REPL
  code execution, not just at the `>>>` prompt.
- Framing: the device writes a 2-byte sentinel `0x18 <type>` (`RemoteCommand.begin`,
  `:521-526`) directly to `sys.stdout.buffer`, then blocks on `sys.stdin.buffer` for the
  matching `0x18` echo-back (`:527-532`) before exchanging a binary RPC (stat/open/
  read/write/…, `PyboardCommand.cmd_table`, `:1046-1060`).
- Host side (`SerialIntercept`, `:1063-1111`): wraps the real serial object and scans every
  byte for the `0x18` sentinel (`_check_input`, `:1070-1086`); on seeing it, it hands off to
  `PyboardCommand.cmd_table[c]` to service the RPC instead of passing bytes to the terminal.
  Non-sentinel bytes go into `self.buf` for normal terminal display.

This is *device-initiated*, half-duplex, single-sentinel-byte framing: the device decides
when an RPC happens (inside its own `open()`/`read()` call), disables Ctrl-C
(`micropython.kbd_intr(-1)`, `:522`) for the duration, and blocks until the host responds.
It is not comparable to DAP's requirement, which is host-initiated, asynchronous, and must
be serviceable while the device is *also* printing arbitrary user output and while paused
in a busy-poll trace callback.

### Why DAP is a harder multiplexing problem than mount's fs-RPC

1. **Framing collision with `print()`.** A single UART has one byte stream. DAP frames
   need a delimiter or an escape scheme distinguishable from arbitrary program output.
   Mount's `0x18` sentinel is *not* free of collision risk in general (any user code that
   writes a raw `0x18` byte — binary protocols, `sys.stdout.buffer.write(some_bytes)`,
   garbled UART noise, a stack-overflow crash dump — can desync the host's scanner); mount
   tolerates this because filesystem RPC is comparatively rare and short, and a
   desynchronised session is recoverable (worst case: a hung read that times out at 5 s,
   `SerialIntercept.__init__:1068`, or a corrupted terminal line). DAP would run this
   scanner permanently for the whole debug session, on every line of program output.
2. **Escaping incomplete output paths.** To make plain `print()` output safe against
   collision, every byte a user's code could send to the wire would have to be intercepted
   and escaped (e.g. doubling `0x18`, or switching to a proper SLIP/COBS-style framing)
   before it reaches the UART. Doable at the Python `sys.stdout` layer only if MicroPython
   lets you redirect `stdout` on that port (not uniformly true; C-level tracebacks, hard
   faults, and panic dumps write to the UART below the Python level and would bypass any
   Python-side escaping wrapper). A single corrupted DAP frame from an unescaped low-level
   write can hang `JsonMessageChannel.recv_message`'s header/body length parsing
   (`messaging.py:112-124`, `:142-154`) since it trusts `Content-Length` and does not
   resynchronise on garbage.
3. **Non-blocking read has to happen inside the trace callback, at very high frequency.**
   `debug_session.py:453`'s trace function calls `process_pending_messages()` on every
   traced `call`/`line`/`return` event, and `wait_for_continue()` busy-polls every 10 ms
   while paused (`pdb_adapter.py:284`). A serial-backed channel would need a genuinely cheap
   non-blocking `recv()` (a `select.poll(0)` + `readinto`) at this frequency — feasible per
   the mount precedent (`RemoteCommand.rd_into`, same primitives), but it means the serial
   RX buffer must be drained fast enough on both ends not to lose bytes at typical
   115200–921600 baud, competing with any REPL/print traffic on the same line.

### Where the problem disappears: boards with a second USB CDC/ACM interface

Some ports/boards expose more than one USB serial interface from the same physical USB
connection — a second, dedicated ACM endpoint that carries its own independent byte stream,
with no relation to the REPL/stdout stream at all. Evidence in this checkout
(`micropython/ports/`):

- `ports/stm32/usbd_cdc_interface.h`, `usbd_conf.c` — STM32's USB device stack supports
  multiple CDC instances (`MICROPY_HW_USB_CDC_NUM`-style board config); several `nrf` boards
  (`PCA10056`, `PCA10059`, `NRF52840_MDK_USB_DONGLE`, `SEEED_XIAO_NRF52`) define USB CDC via
  `ports/nrf/drivers/usb/usb_cdc.c/.h`.
- `ports/esp32/usb.c/.h`, `ports/esp32/boards/sdkconfig.usb`, `ARDUINO_NANO_ESP32` — native
  USB (TinyUSB) on ESP32-S2/S3, capable of multiple CDC interfaces in the composite USB
  descriptor.
- `ports/alif/tinyusb_port/tusb_config.h` — TinyUSB-based, same multi-interface capability.
- RP2040 (`rp2` port, TinyUSB-based) is not separately grepped above but is architecturally
  the same TinyUSB composite-device case as `alif`/native-USB ESP32.

On these boards a second CDC interface, if configured and exposed to Python (e.g. as a
second `machine.USBDevice`/raw `machine.UART`-like object, or via a custom TinyUSB
descriptor), removes the multiplexing problem entirely: DAP frames go on the dedicated
interface, user `print()`/REPL stays on the primary one, and no framing/escaping is needed
because there is no shared byte stream. This is the same shape debugpy already uses over
TCP (`public_api.py`'s socket is a channel wholly separate from any device stdout).

**Boards where this does NOT apply:** anything using an external USB↔UART bridge chip
(CP2102/CH340/FTDI on most ESP32 dev boards, most non-native-USB STM32 Nucleo/Discovery
boards via an on-board ST-Link VCP, most classic AVR/8-bit-era-descended boards) — there is
exactly one physical UART exposed over USB, wired only to `UART0`/console. No amount of
firmware cleverness adds a second wire; a second CDC interface can only exist where the MCU
itself does native USB with a composite descriptor. Most currently-common ESP32 dev boards
(the devkit-v1/WROOM boards this repo's `firmware/esp32_*` variants target) are in this
single-UART bucket — they use CP2102/CH340, not native USB.

## 3. On-device server-side change required

Symmetrical to §1: the debugpy server-side would need a stream adapter over
`sys.stdin.buffer`/`sys.stdout.buffer` (or a second CDC interface's stream object where
available) implementing the same `recv`/`send`/`settimeout`/`close` shape `messaging.py`
already assumes, using `select.poll()` for the non-blocking peek — directly mirroring
`RemoteCommand.poll_in`/`rd_into` (`transport_serial.py:498-519`). No change to
`messaging.py`'s framing (`Content-Length` + JSON) is needed if the adapter presents a
clean, already-demuxed byte stream; the change is confined to what feeds it. On a
single-UART board this demuxing/framing/escaping work is new device-side code that must run
underneath the debuggee's own program output at trace-callback frequency (§2.2), which is
where the real engineering risk sits, not in `messaging.py`.

## Per-port feasibility summary

| Port/board class | Physical channels | DAP-over-serial feasible? |
|---|---|---|
| Native-USB, multi-CDC-capable (RP2040, ESP32-S2/S3 native USB, some STM32 w/ custom USB descriptor, some nRF52 boards) | 2+ independent USB CDC/ACM interfaces available | **Yes, cleanly** — dedicate a second CDC interface to DAP; no framing/escaping needed; same shape as TCP today. |
| External USB-UART bridge boards (most common ESP32 WROOM/DevKit boards via CP2102/CH340, many classic STM32/AVR boards via ST-Link/FTDI VCP) | 1 UART, shared with REPL/stdout | **Conditional, high engineering risk** — requires a framing/escaping scheme covering *all* device output paths (Python `print`, C-level tracebacks/panics), a scanner running for the whole session at trace-callback frequency, and acceptance that a corrupted frame can hang `recv_message`'s length-prefixed parser. Not proven; not attempted on hardware in this spike. |
| Unix port | Arbitrary number of OS pipes/fds (not physically constrained) | **Effectively free, but not "serial"** — see note below. |

### Unix port note

The "single shared wire" constraint that makes serial hard on real boards does not exist for
the unix port: a `subprocess.Popen` can be given as many independent pipe file descriptors
as wanted (stdin/stdout plus extra `pass_fds`), so a dedicated pipe pair for DAP, distinct
from the debuggee's own stdout, is trivial and requires zero framing/escaping work — it is
exactly the multi-CDC case (§2, "where the problem disappears"), not the single-UART case.
Today's TCP socket already achieves this same separation from a different mechanism. Doing
this over pipes on unix would validate the *transport-interface swap* in §1 cheaply (proves
the adapter behind `messaging.py` works against a non-socket, non-blocking stream) without
touching the hard multiplexing problem at all — it says nothing about whether single-UART
boards are viable, because the unix case never has the shared-stream constraint that defines
the real risk.

## Verdict

**CONDITIONAL — board-dependent. Confidence: medium** (desk analysis from source; no
hardware attach was run in this spike, and STORY-2.2's own acceptance criteria call for a
prototype reaching at least one breakpoint over serial, which was not attempted here).

- On boards with a second, independently-addressable USB CDC interface (native-USB ports:
  RP2040, ESP32-S2/S3 native USB, some STM32/nRF52 boards): **YES**. DAP over serial is
  straightforward — a stream adapter over the second interface satisfying §1's interface,
  no framing needed. This should be prototyped next (pick one such board) before committing
  further design.
- On single-UART boards, which includes the ESP32 dev boards this repo's prebuilt firmware
  variants currently target: **NO, not without substantial new device-side plumbing** (a
  full-session framing/escaping layer intercepting all output paths) whose reliability is
  unproven and which the mount-RPC precedent only partially de-risks (it demonstrates the
  non-blocking-poll mechanics work mid-execution, not that a permanent full-duplex framed
  channel can coexist safely with unconstrained `print()`/traceback output).
- Unix port: the transport-interface swap (§1) is free to prototype via extra pipe fds, but
  it validates only the socket-to-stream adapter, not the hard multiplexing question — it is
  not evidence either way for real serial devices.

## Minimal proposed transport interface (if pursued)

A `Transport` adapter class the server constructs instead of a raw socket, implementing
exactly the surface `messaging.py`/`debug_session.py`/`public_api.py` currently reach for:

```python
class StreamTransport:
    def __init__(self, reader, writer):   # reader/writer: objects with readinto/write
        self._poller = select.poll()
        self._poller.register(reader, select.POLLIN)
        self._timeout = None              # seconds, or None = blocking

    def settimeout(self, seconds):
        self._timeout = seconds

    def recv(self, n):
        # poll for n ms (self._timeout), else raise OSError(errno=11) on no data
        ...

    def send(self, data):
        # write(data); raise OSError on failure
        ...

    def close(self):
        ...
```

On multi-CDC boards, `reader`/`writer` are the second CDC interface's stream. On
single-UART boards, they would additionally need to sit behind a framing/de-escaping layer
that does not exist yet and is the unresolved risk above.

## Downstream consequence for EPIC-6

Because the answer is board-dependent rather than a clean yes/no, EPIC-6 cannot uniformly
collapse to "serial only" (STORY-2.2's simple YES branch) nor uniformly require network for
every device (the simple NO branch). Recommendation:

- **STORY-6.1 (serial transport)** should scope to multi-CDC-capable boards first, gated on
  a runtime capability check (does this board/port expose a second CDC interface the
  firmware routes DAP to) — mirroring the existing capability-probing philosophy in EPIC-1
  (STORY-1.2) rather than assuming serial-DAP universally.
- **STORY-6.2 (network transport)** remains required, not optional, for single-UART boards
  — which includes this repo's current ESP32 firmware targets — until/unless a
  framing/escaping prototype is built and proven on real hardware (a follow-up spike, not
  covered by this investigation).
- The localhost↔serial DAP bridge in STORY-6.1 is worth building for the multi-CDC subset;
  it should not be presented as the general device story until a second board class is
  validated.
- Given most of this repo's currently-targeted hardware (ESP32 WROOM/DevKit via
  CP2102/CH340) is single-UART, EPIC-6 should treat network transport as the primary,
  broadly-applicable device path in the near term, with serial-DAP as an optimisation for
  a specific board subset once prototyped.
