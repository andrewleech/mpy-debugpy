"""STORY-6.1 criterion 4: what a board reset in the middle of a session does.

The board is reset while a client is stopped at a breakpoint over the bridge,
which is the worst moment for it: the device is holding a paused frame, the
client is waiting on nothing in particular, and both of mpremote's channels to
the board vanish at once.

What the command must do then is end, saying what happened. It must not
reconnect: the rebooted device runs a fresh `debugpy` with no memory of the
session the client still believes it has - no breakpoints, no frames, no
sequence numbers - so anything the client sends next is addressed to a peer
that never received its `initialize`. Reviving the byte pump would hand the
client that peer and call it a recovery. See
`planning/20260810_board-reset-mid-session.md`.

Needs `MPY_DEBUG_HIL_RESET_CMD` alongside the usual two device paths; see
`conftest.py`.
"""

from dapdrive import attached, run_to_breakpoint
from debuggee import DEVICE_SOURCE_PATH, LOOP_LINE
from mpremote_debug import read_until

# The board has to be gone, noticed, reported and the command finished inside
# this. A USB power cycle takes seconds; the rest is one failed read on the
# bridge's device side and a teardown of ports whose device is already gone.
_REPORT_TIMEOUT = 60


def test_hil_board_reset_mid_session_ends_the_command_with_a_clear_error(
    hil_serial_dap_session, hil_reset_board, hil_dap_device
):
    """A reset at a breakpoint ends `mpremote debug` naming the cause.

    All three halves are asserted, because any one alone would pass for
    something worse: an exit code with no message is a session that died
    silently, a message with no exit is a command still holding the board's
    ports, and a message followed by a traceback is a teardown that gave up
    partway and left them open.
    """
    session = hil_serial_dap_session
    proc = session["process"]

    with attached(session) as server:
        stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
        assert stopped.body["reason"] == "breakpoint", stopped

        hil_reset_board(also=[hil_dap_device])

        # Read to EOF rather than stopping at the report. The command exits
        # right behind it, so everything it has to say arrives in one chunk,
        # and `read_until` keeps only the lines up to the one that matched.
        lines, _ = read_until(proc, None, timeout=_REPORT_TIMEOUT)
        said = "".join(lines)
        reported = [line for line in lines if "was lost" in line]
        assert reported, "mpremote never reported the lost DAP connection:\n" + said
        assert "board reset" in reported[0], reported[0]

        # The command has already said what happened, so an exception on the
        # way out is a second and contradictory answer - and it means the
        # teardown that raised it stopped before closing the ports.
        assert "Traceback" not in said, "mpremote reported the loss, then raised:\n" + said

    proc.wait(timeout=_REPORT_TIMEOUT)
    assert proc.returncode != 0, f"the command exited 0 after losing the board:\n{said}"
