# Killing the debug bridge while the board is stopped

- Date: 2026-08-10
- Top repo HEAD: `30a9442`
- `micropython`: `6fcf5bac9b` (`mpy-debugpy`)
- `micropython-lib`: `1eb7c82` (`mpy-debugpy`)

STORY-6.1's criterion 3 is "kill the bridge mid-session - device recovers to a
usable state, no wedged CDC". The host-side suite had asserted it since
2026-08-07 and it passed. On hardware it failed, and the reason is a property
of USB CDC that the host-side stand-in does not have.

## A CDC interface has no EOF

stm32's `pyb_usb_vcp_ioctl` (`micropython/ports/stm32/usb.c:913`) sets
`MP_STREAM_POLL_RD` when `usbd_cdc_rx_num(itf) > 0` and never reports POLLHUP
or POLLERR. An interface nobody is holding and an interface whose peer is
simply not talking are therefore the same interface: no bytes, no error, no
end of file.

Everything downstream was already correct for a channel that *can* end.
`PdbAdapter.wait_for_continue` (`pdb_adapter.py:366`) checks
`session.channel.closed` each time round its wait, and on a closed channel
uninstalls the trace function, sets `continue_event` and breaks - the target
runs on. `JsonMessageChannel.closed` is set when `recv` returns `b""`. On a
CDC interface `recv` never returns `b""`, so none of that could fire. A user
whose editor crashed at a breakpoint had a board that needed its power cycled.

The pty the host-side scenario uses reaches a real EOF the moment the bridge
dies, so it exercised the recovery path and told us nothing about the path a
board actually takes. This is the second time in this story that a pty has
flattered a mechanism; the first was criterion 4's board reset.

## DTR is what the interface has instead

Measured directly on a PYBD-SF6W, `pyb.USB_VCP(1).isconnected()` is False
before the host opens the node, True while a pyserial handle is open, and
False again after it closes. It is per-interface
(`usbd_cdc_is_connected(self->cdc_itf)`, `usb.c:739`), which matters because
the REPL is on interface 0 and stays held for the whole session either way.

That is a host-present signal, not an end-of-stream, and the difference
decides where it belongs. `debugpy` gets no knowledge of `pyb`: `listen_stream`
takes an optional `is_connected` callable, `StreamTransport` treats it going
false as EOF, and the boot script - which already knows it is holding a
`USB_VCP` - passes `getattr(stream, "isconnected", None)`. The unix port's
`dap_device` is an ordinary file with a real EOF, has no `isconnected`, and
passes nothing, so that path is byte-for-byte what it was.

## The latch is on traffic, not on having seen DTR high

The obvious form of the check - "DTR was up and is now down" - is wrong here.
`serial_dap.check_device()` opens the DAP node and immediately closes it
before the bridge starts, and the kernel raises DTR on tty open regardless of
what pyserial then sets, so that probe alone would arm and disarm the latch
before a client ever attached. The session would end on the flicker.

`StreamTransport._peer_gone` therefore requires that the channel has carried a
byte before it will believe the signal. An open/close that moves no data
cannot trip it, and by the time it can, there is a session to lose. The other
direction is covered too: down before any traffic is exactly the state between
`listen_stream()` and the client's first connect, which must read as idle.

Once it fires, EOF is sticky. A host that comes back is a new session, not a
continuation of one whose framing state both ends have already lost.

## What proves it

`tests/hil/test_hil_bridge_kill.py`, on the bench PYBD_SF6W:

- SIGKILL at a breakpoint, then the target is asserted to print its completion
  line within 30 s, both device nodes to still exist, and a fresh `mpremote
  debug` to reach a breakpoint and list threads over the same interface. Both
  halves are asserted because either alone passes for something worse - a
  board that resumes but cannot be debugged again has a wedged interface, and
  a board that only takes a new session because that session soft-resets it
  was still frozen in between.
- A second scenario evaluates `pyb.USB_VCP(1).isconnected()` through the live
  DAP session and asserts True, so a firmware or USB stack that stopped
  tracking DTR fails as itself instead of making the recovery above silently
  conditional on a soft reset or a timeout.

The fix was confirmed causal by removing the `is_connected` argument from the
boot script's `listen_stream` call and watching the first scenario fail again
with the board silent for the full 30 s.

Host-side, `test_stream_transport_peer_gone_signal` drives a probe over a pipe
whose ends the host never closes - no EOF available, which is the only CDC
property that matters here - and pins each case separately: EAGAIN before any
traffic even with the signal down, EAGAIN while a connected peer is idle,
`b""` within a second of a peer that was talking going away (against a 5 s
timeout, so a transport that merely waited it out fails), stickiness, and no
EOF at all for a transport constructed without a signal. Both halves of the
latch were mutation-checked: dropping the traffic requirement fails the first
case, dropping the check in `recv` fails the fourth.

`LineReader.wait_for` in that test module dropped any lines that arrived in
the same read as the one it matched, which no earlier caller had noticed
because each waited on a step the host itself triggered. It now keeps them.

## Left undone

Stale bytes in the board's second-CDC rx buffer desynchronise DAP framing for
the next session and survive a soft reset - a power cycle clears them. This
was hit while probing DTR by hand (a stray `b"x"` written into interface 0),
not by any product path, and is recorded here rather than chased: a session
that ends by the mechanism above leaves no partial frame behind, since the
peer stops writing before the device stops reading.
