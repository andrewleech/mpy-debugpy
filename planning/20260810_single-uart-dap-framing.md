# The no-network path is in-band framing on the REPL UART, not a second CDC

Date: 2026-08-10
Top repo HEAD: 47b752b0d9ff52635644c12172d224dca47e9e07
micropython: 19410568d68fabe5d3fcf8594a4e30919205a7cd (mpy-debugpy)
micropython-lib: b829073c393f9ba1027fe8d526ea58c2c8eb7fe4 (mpy-debugpy)

## The correction

Prompted by a user correction (2026-08-10): *"No board ever comes with dual CDC
available by default so it's pointless to suggest support for it as an 'easier
option'. The easy option for users without network is to interleave the protocol
just like mpremote mount does, though in the medium term usb-ncm networking will
be promoted as well once that open PR is merged."*

The roadmap's own position was already right - D3 makes serial DAP a probed
board subset and network the mainline, and `SPIKE-2.2-serial-dap.md` names the
framing prototype as a follow-up spike. What was wrong was the framing in
`docs/debugging.md` and in how the serial transport had been described in
conversation: as the low-setup path, the one that needs no credentials and no
address. That reading survives only if the second interface is there for the
taking. It is not.

`ports/stm32/main.c:688-695` initialises USB to `USBD_MODE_CDC_MSC` - one CDC -
whenever `boot.py` has not already called `pyb.usb_mode()`. `MICROPY_HW_USB_CDC_NUM
(2)` in `boards/PYBD_SF6/mpconfigboard.h` raises the build's ceiling and changes
nothing about what boot brings up. The bench PYBD reports `2xVCP+MSC` only
because a `boot.py` written in an earlier session sets it; the HIL records have
been carrying "USB mode: 2xVCP+MSC" as provenance since 2026-08-08 without
anything stating it as a precondition, and the published guide never told a
reader to do it. So the serial path costs a user a `boot.py` edit and a
re-enumeration *before* it costs them anything else - strictly more device-side
setup than the network path, not less.

This does not disturb Q12. `second_cdc` still measures the build's maximum, which
is still the right thing for a firmware manifest to advertise, and the three-way
distinction it rests on is unchanged. What changes is the sentence around it: a
`true` there means "this build could enumerate a second interface", never "this
board has one ready".

## The intended shape for single-UART boards

`mpremote mount` already interleaves a request/response protocol with the REPL
on one stream, and that is the technique to copy rather than invent. From
`tools/mpremote/mpremote/transport_serial.py:551-600`:

- the device writes `0x18` then a one-byte command code on stdout, and waits for
  the host to echo `0x18` back before sending the payload;
- `micropython.kbd_intr(-1)` is in force for the transaction, so a `0x03` in the
  payload is data rather than a KeyboardInterrupt;
- the host's read path watches the same stream for `0x18` and services the call
  when it sees one, passing everything else through as console output.

Two things it does not solve, which any DAP version has to:

1. **Only the device initiates.** Every fs-hook exchange starts with the device
   asking the host for something. DAP has requests going the other way at
   arbitrary times - `setBreakpoints` while the program runs, `pause`,
   `evaluate` - so the device needs a poll point for an inbound marker. Under
   `settrace` there is an obvious one, since the trace callback already checks
   for pending DAP messages; between trace events there is not.
2. **The program's own output shares the stream.** `0x18` is a bare marker with
   no escaping, so a program printing that byte would be read as a command. The
   filesystem RPC tolerates this; a debug session that stays up for the life of
   the program is exposed to it for much longer. The alternative to escaping is
   to stop the program's stdout going out in band at all and route it through
   the DAP `output` event instead, which is where a client wants it anyway.
   `DebugSession.send_output` (`debug_session.py:648`) already exists and already
   has one caller's worth of reason to: on a mounted serial session the
   filesystem pump discards device stdout, so run-boundary markers go through it
   today. Cost: the REPL is not usable as a REPL during a session.

Neither is a research problem. Both are why this is a story rather than a patch.

## Medium term: USB NCM

`micropython/micropython#16459` (*shared/tinyusb, extmod: Add USB NCM network
driver*, open) gives a board a USB network interface. On a board with it, the
existing network transport is the cable path: an IP over USB, no WiFi
credentials, no association, and nothing in this project changes - the transport
is already written and already the mainline. That makes NCM the more likely
medium-term answer for "no network" than the framing layer is, and the framing
layer the answer for boards that get neither.

`micropython/micropython#19103` (*shared/tinyusb, extmod: Add runtime USB class
selection*, open) is adjacent and worth tracking for a different reason: a
runtime-selectable CDC would remove the `boot.py`-and-reboot precondition above,
turning the second interface into something `mpremote debug` could ask for when
it wants one.

Neither is a dependency. Both are open PRs on upstream master with no merge date,
so nothing here is scheduled against them.

## What changed in the tree

- `docs/debugging.md`, the `### serial` section: states the enumeration
  precondition with the `boot.py` line, stops implying the path is a shortcut
  around the network one, and replaces the "do not infer the second interface
  from `pyb.USB_VCP(1)`" sentence - which read as forbidding exactly what the
  `second_cdc` probe does - with the three questions and which instrument
  answers each.
- ROADMAP: D3 carries a dated correction, D7 records the direction, the Q3
  entry and the framing risk row name this note.
- STORY-6.7 added for the framing layer itself.
- `tests/hil/conftest.py`: the `hil_dap_device` skip said only that a board
  might have one CDC interface. It now names the likelier cause on a capable
  board - a boot that was never told to enumerate the second one - so an
  operator does not go looking for a firmware problem.
- Two documents had drifted from the code in the direction Q12's own finding
  predicted, and were found by re-reading them rather than by any check:
  `docs/debugging.md`'s `requires` vocabulary and `docs/firmware.md`'s variant
  table and glossary all predated `second_cdc`. The published `requires` list
  was missing a key a user could legally write. `tests/test_docs_capabilities.py`
  now holds both documents to `KNOWN_CAPABILITIES`, which is the fifth and
  sixth copy of a vocabulary the Q12 work had already found four of.

## What is not done

No framing layer, no prototype, no measurement. STORY-6.1 criterion 2 - the
single-UART negative probe - is still unmet and still needs a board that is not
on the bench, and it is a *negative*: it asserts such a board refuses the serial
path and falls back, not that it debugs over one UART.

## Correction, 2026-08-10: the program's stdout cannot be diverted from Python

Obstacle 2 above offers two ways out - escape the marker byte, or "stop the
program's stdout going out in band at all and route it through the DAP `output`
event instead". The second is not implementable on the device in Python. Three
independent reasons, any one of which is enough:

- `print()` does not read `sys.stdout`. Its `file` keyword defaults to a ROM
  pointer at the `mp_sys_stdout_obj` object itself
  (`py/modbuiltins.c:393`), so the destination is fixed at build time and a
  rebound name could not change it.
- `sys.stdout` is not assignable anyway. The `sys` globals are a const dict, and
  the attribute-store delegation covers exactly `path`, `ps1`, `ps2` and
  `tracebacklimit` (`py/modsys.c:298-320`). On the unix build
  `sys.stdout = sys.stderr` raises `AttributeError: 'module' object has no
  attribute 'stdout'` - the store path, not a missing attribute; reading it
  yields `<io.TextIOWrapper 1>`.
- `os.dupterm` duplicates rather than diverts. `mp_hal_stdout_tx_strn` writes
  the stdio UART, then the CDC, then dupterm
  (`ports/stm32/mphalport.c:82-105`), so adding a dupterm destination adds a
  copy and removes nothing from the stream the framing layer is trying to keep
  clean.

Rebinding `print` in the debuggee's own globals is not a substitute: a traceback
and any direct `sys.stdout.write` still go out in band, and those are exactly
the output a debug session most needs to see.

So STORY-6.7 inherits a narrower choice than this note recorded. Either the
marker byte is escaped in the device's stdout path, or the divert is made in C -
a port-level stdout hook, or a `dupterm` variant that replaces rather than
adds - which puts it in `micropython` rather than in `micropython-lib` and
changes which repo the story lands in.

## Correction, 2026-08-10: STORY-6.1 criterion 2 is met

"What is not done" above states that criterion 2 "is still unmet and still needs
a board that is not on the bench". It was closed the same day, by this note's own
finding: if an stm32 enumerates one VCP unless `boot.py` calls `pyb.usb_mode()`,
then the bench PYBD booted without that line is a board with no dedicated DAP
interface, and no second chip is needed to ask the criterion's question. See
`tickets/s6.1_serial-transport.md` and
`20260810_hil_PYBD_SF6_no-dap-device.md`. The rest of that section stands: no
framing layer, no prototype, no measurement.

## Correction, 2026-08-10: on stm32 with the legacy USB stack, `os.dupterm` does divert

Measured at top-repo HEAD `3139524b74` (micropython `19410568d6`, micropython-lib
`b829073c39`) on the bench PYBD_SF6. The correction above concludes that
`os.dupterm` "duplicates rather than diverts" and therefore that the divert has
to be made in C. That is right for the code path it cites and wrong for this
board, which does not compile that path.

`mp_hal_stdout_tx_strn` (`ports/stm32/mphalport.c:81-103`) has three
destinations: `pyb_stdio_uart`, a USB CDC block guarded by
`#if MICROPY_HW_USB_CDC && MICROPY_HW_TINYUSB_STACK`, and dupterm. PYBD_SF6
builds the legacy STM USB stack, so the middle block is not compiled at all,
and `ports/stm32/usb.c:653-654` puts `USB_VCP(0)` **into dupterm slot 1** -
"Activate USB_VCP(0) on dupterm slot 1 for the REPL". The USB REPL on this
board is not a destination beside dupterm; it *is* a dupterm slot. With
`pyb.repl_uart()` reporting `None` (measured), that slot is the whole stdout
path, and `os.dupterm(obj, 1)` replaces it and hands back what was there.

Measured with a wrapper in slot 1 that counts bytes and can either forward to
the displaced `USB_VCP` or swallow:

| what was written | bytes the slot saw | reached the host |
| --- | --- | --- |
| `print("PROBE-FORWARDED")` | 17 | yes |
| `print("PROBE-COUNTED")` | 15 | yes |
| `sys.print_exception(ValueError(...))` | 101 | yes |
| `print(...)` with the wrapper swallowing | 33 | **no** |

The third row is the one that decides it. The correction above rules out
rebinding `print` because "a traceback and any direct `sys.stdout.write` still
go out in band"; a traceback goes through `mp_hal_stdout_tx_strn` like anything
else, so the slot sees it. The fourth row is the divert itself: 33 bytes
written, nothing on the wire. The displaced object came back as `USB_VCP`, was
restored, and the REPL survived.

One implementation detail, and it is why the first attempt failed:
`os.dupterm` calls `mp_get_stream_raise(obj, READ|WRITE|IOCTL)`
(`extmod/os_dupterm.c:231`), so the object needs the native stream protocol. A
plain class raises `OSError: stream operation not supported`; subclassing
`io.IOBase` is what gives a Python class that protocol, and is how WebREPL has
always sat in a dupterm slot.

So the choice STORY-6.7 inherits is wider again, not narrower, and the two
options are the same mechanism rather than two:

- **Escape**, in Python. The wrapper sees every stdout byte before the VCP
  does, so escaping a marker in program output needs no C.
- **Divert**, in Python. The wrapper swallows program output and the DAP
  `output` event carries it, per the fourth row.

Scope, because this is a property of one port and one USB stack rather than of
MicroPython. It holds on stm32 boards built with the legacy STM USB stack and
no stdio UART. It does **not** hold on rp2 (`ports/rp2/mphalport.c:112-118`,
`mp_usbd_cdc_tx_strn` unconditional under `MICROPY_HW_USB_CDC`), on esp32
(`ports/esp32/mphalport.c:166-173`, USB-JTAG and UART unconditional), or on an
stm32 built with the TinyUSB stack - on all of those dupterm is genuinely
additive and the C hook the correction above describes is still the general
answer. What changes is that the story no longer *starts* in `micropython`: it
can be built and measured end to end on the bench board in
`micropython-lib`, and the C hook becomes the port-expansion step rather than
the precondition.
