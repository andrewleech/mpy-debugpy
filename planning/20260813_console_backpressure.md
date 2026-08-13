# A console nobody reads stops the board, and `mpremote debug` was holding one

Date: 2026-08-13
Top repo HEAD: acb2e5a (before this note)
micropython: b3ce072d718ad1e564ab326bdcfed3a8b3ac6a21 (mpy-debugpy, carrying the fix below)
micropython-lib: 2a9bad0971b7700349d233ea82e1669b42e99eb5 (mpy-debugpy)

Bench: PYBD-SF6W, firmware `v1.29.0-preview.717.g4eaafbc5bc`, two-interface arrangement, 192.168.0.114.

## What this started as

`20260811_network_dap_tx_stall.md` ends with a prescribed next step: a minimal
reproduction with no `debugpy` in it, varying the `settimeout(0.001)` /
`settimeout(None)` alternation the DAP pump does about 80 times a second,
because that is "the one thing the DAP pump does that ordinary socket code does
not". That reproduction was built and the alternation is not it. What it found
instead is a different defect, in this repo's own command, with a mechanism that
accounts for every observation that note recorded as unexplained.

## The alternation is not it

`device.py` (kept with this note's working files) listens, accepts, reads a
length-prefixed request, answers it with two sends close together the way an
`initialize` response and an `initialized` event go out, then pumps with
`settimeout(0.001)` / `settimeout(None)` around each `recv` at about 80 Hz and
answers the second request with a single reply. No `debugpy`, no `settrace`.

Five trials, faithful alternation: 5/5 answered, second reply in 6-16 ms. The
same harness with the alternation removed is indistinguishable. Whatever the
2026-08-11 measurements were of, the socket timeout is not carrying it.

## What the earlier note got wrong

Its "ruled out" list says the lwIP concurrency hooks "are empty on this port
(`extmod/modlwip.c:93-95`)". Those lines are the fallback for a port that
defines nothing; stm32 defines them, at `ports/stm32/mphalport.h:66`, where
`MICROPY_PY_LWIP_ENTER` raises the interrupt priority to PENDSV so the lwIP
background task cannot run. Reading the default as the effective definition
retired a hypothesis that was never tested. It is not the cause either, but it
was not ruled out.

## The defect

A USB CDC console that a host holds open and never reads back-pressures all the
way into the device:

- The tty's line discipline stops accepting once its own buffer fills (4096
  bytes for N_TTY), and the CDC driver then stops draining the device.
- `usbd_cdc_tx_always` (`ports/stm32/usbd_cdc_interface.c:393-421`) waits for
  room for **each byte**, up to 500 ms, whenever `connect_state` is not
  DISCONNECTED - that is, whenever a host holds the port open. The transmit
  buffer it is waiting on is 1024 bytes (`mpconfigboard_common.h:368`).
- It waits in `__WFI()`, so interrupts keep running. lwIP keeps acknowledging,
  ICMP keeps being answered, and the board keeps looking alive to everything
  except the code that would have answered.

So a board printing into a held-open, unread console stops running Python, and
what an observer sees is a DAP channel that went quiet, a receive window that
never reopens because nothing is reading the socket, and a device that
nonetheless ACKs. That is the whole of the earlier note's evidence.

And `mpremote debug` was holding exactly such a console. `do_debug` starts a
reader for the primary connection only when the session also mounts a directory
(`commands.py`, the `if mounted` guard on the pump thread), so a `serial_dap`
session and a `--dap-log` session both sit in `_stay_attached` holding the port
with nothing reading it. The unix path never had the problem: it owns its child
and streams the child's stdout for the whole session.

## Measurements

Device-side mechanism, with the harness above and the console in three states
(the board's own prints, 40 lines of about 52 bytes per request):

| console | result |
|---|---|
| closed | 3/3 answered; the printing costs 7-8 ms |
| closed, 400 lines (21 kB) | 3/3 answered; 73-77 ms, linear |
| open, not read | 1/3 answered - the two failures answer nothing at all within 20 s |
| open, drained | 4/4 answered; printing back to 8 ms |

Product path, `mpremote debug` against the same board, driving a real session
and making the target print one line per `evaluate`:

| arm | result |
|---|---|
| `--dap-log` (stays attached, console held) | stopped answering after **5124 bytes**, twice, at the same byte |
| no flag (mpremote exits, console closed) | 9150 bytes, no stall, worst round trip 0.10 s |
| `--dap-log`, with the fix | 9150 bytes, no stall, worst round trip 0.10 s |

5124 is 4096 + 1024, the tty's buffer plus the board's, which is what makes the
mechanism identification rather than correlation.

## The fix

`_stay_attached` takes a `console` transport and drains it for exactly as long
as it waits, printing what it reads. Passed by the two paths that stay attached
without a mount; None from a `repl_dap` session, whose channel reader owns the
port and would lose DAP frames to a second reader, and None when a mount's own
RPC pump is already emptying it.

Printing rather than discarding is a deliberate second effect: on a `serial_dap`
session the console is the only place the debugged program's output can appear,
and today it is thrown away by the board when its buffer overflows. It also
matches what the unix path already does. The caveat is that a caller which pipes
mpremote's stdout and stops reading it moves the same back-pressure one level
up, at the pipe's 64 kB rather than the tty's 4 kB.

Pinned by `test_a_talkative_target_does_not_wedge_on_the_console_nobody_reads`,
which drives a real `debugpy` session over a pty and prints 100 kB through it.
Without the drain the device stops answering after 20020 bytes; a pty absorbs
more than a CDC before the writer feels it, which is why the test prints in
kilobyte lines rather than the 60-byte ones the bench used.

## What this does not settle

The 2026-08-11 intermittent is **not** explained by this. The HIL harness drains
the console throughout (`DeviceOutput`, started per run in `tests/hil/conftest.py`),
so the scenario that failed 4 of 5 that day is not exposed to this defect, and
today it passes 5/5 with `--dap-log` off and 5/5 with it on. Either the bench
conditions differed - that note records a board taking two seconds to answer a
SYN, before any DAP traffic - or the hand-driven reproduction it describes was
holding the console the way `--dap-log` does. The risk register keeps the row.

One thing is worth carrying either way: the row prescribed `--dap-log` as the
instrument for catching the intermittent, and `--dap-log` was, until this fix,
the one flag that could cause a stall of its own.
