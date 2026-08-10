# A board reset in the middle of a debug session

- Date: 2026-08-10
- Top repo HEAD: `c5d8e5b`
- `micropython`: `5ac1cda066` (`mpy-debugpy`)
- `micropython-lib`: `3bd6c44` (`mpy-debugpy`)

STORY-6.1's criterion 4 reads "board reset/reconnect during a session is
handled", and the story description points at ampremote's reconnect work
(#17322) as the thing to reuse. This settles what "handled" means for a debug
session, and records the defect that proving it on hardware turned up.

## Reconnecting is the wrong answer, not the unfinished one

`do_reconnect(state)` in the ampremote tree
(`~/ampremote/micropython/tools/mpremote/mpremote/commands.py:80`) returns
`False` unless `state.reconnect_enabled` and `state.connect_device` are both
set, then loops - `while True`, no deadline - until a device matching the
original connect string comes back, and builds a fresh `SerialTransport` for
it. Everything it restores is the primary REPL connection.

That is exactly right for `mpremote repl` or a file transfer, and it cannot be
right for a debug session. The device it hands back is not the device the
session was talking to. A reset board runs a fresh `debugpy` with no memory of
anything the client established: no breakpoints, no frame or variable
references, no sequence numbers, no `initialize`. The client, meanwhile, has
lost nothing - it is still stopped at a breakpoint it set, holding
`variablesReference` handles for frames that no longer exist. Reviving the byte
pump between the two would hand the client a peer that never received its
`initialize` and let it keep addressing objects the peer has never heard of.
The client cannot detect this; it looks like the session continuing.

So there is no partial recovery to build. Either the client is told the session
is over, or it is lied to. `_serial_dap_lost`
(`micropython/tools/mpremote/mpremote/commands.py:793`) tells it, and the next
`mpremote debug` builds both ends from scratch.

The bridge already forces the issue from the other side. When the target
connection fails, `DapProxy._accept`/`_pump`
(`micropython/tools/mpremote/mpremote/dap_log.py:170`) records `target_error`,
shuts both pump directions and closes the client socket - so the DAP client's
session ends whatever the command decides afterwards. A reconnect would be
reopening a socket the client has already seen close.

## What the criterion delivers

`mpremote debug` ends, naming the cause and exiting non-zero:

```
mpremote: the serial DAP connection to '/dev/serial/by-id/usb-MicroPython_Pyboard_Virtual_Comm_Port_in_FS_Mode_...-if03' was lost (board reset?); reset the device and run 'mpremote debug' again
```

Proven on hardware by `tests/hil/test_hil_board_reset.py`: a client is stopped
at a breakpoint over the serial bridge, the board is power-cycled under it, and
the command is asserted to report the loss, exit non-zero, and print no
traceback. The reset command is not the suite's business - there is no portable
way to reset a board, so `MPY_DEBUG_HIL_RESET_CMD` supplies whatever the bench
has (here `mpy-dev cycle pybd`, which is `uhubctl` cutting power to one hub
port) and the scenario skips where it has none.

## The defect the mutation check found

Asserting the report alone passed against a command that printed it and then
died:

```
mpremote: the serial DAP connection to '...-if03' was lost (board reset?); ...
Traceback (most recent call last):
  File "mpremote/main.py", line 757, in main
    do_disconnect(state)
  File "mpremote/commands.py", line 105, in do_disconnect
    state.transport.close()
  File "mpremote/transport_serial.py", line 132, in close
    raise er
```

`SerialTransport.close()` clears RTS/DTR before closing the port - a workaround
for a Windows host resetting an ESP target on the way out - and tolerated only
`ENOTTY`, for devices that have no such signals. A device that has *gone away*
fails the ioctl with `EIO` instead, pinned by a standalone probe (open the
port, cycle the board's hub port, `close()`): `OSError: [Errno 5] Input/output
error`. `do_disconnect` calls `close()` outside the `try` that already exists
for "the serial port disappeared", and `main()` calls `do_disconnect` from a
`finally` outside its `CommandError` handler, so the exception escaped as an
unhandled traceback - and the port was never closed, since the raise happens
before `self.serial.close()`.

Clearing the signals is best-effort by nature, so it no longer stops the close.
The fix is general upstream mpremote behaviour - any command whose board
vanishes hits it, debugging or not - so it is its own branch,
`mpremote_close_lost_device`, registered in `mbm.toml`, rather than an
unrelated passenger on `mpremote_debug`'s upstream PR.

The scenario now asserts the absence of a traceback, and that assertion was
confirmed by reverting the fix and watching it fail.

## Left undone

The integration was not rebuilt on latest upstream for this. `mbm rebase
--target 13303f8d29` (the base the current integration already sits on) still
refetches the PR-numbered branches live, and #8767's head has moved on to a
newer master: a rebuild pulls in unrelated `py/gc.c`, `py/emitnative.c`,
`py/mpz.c` and `py/modmicropython.c` commits, which would put the flashed
firmware out of step with the source tree in the middle of hardware
verification. The fix was merged onto the integration branch on its own instead;
`mbm.toml` carries the registration, so the next deliberate rebuild composes it
in the ordinary way, and that rebuild should be paired with a firmware rebuild,
a reflash and a HIL rerun.
