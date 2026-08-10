"""STORY-6.1 criterion 3: what killing the bridge mid-session does to the board.

`mpremote debug` is killed outright - no `disconnect` request, no orderly
teardown, the way an editor crash or a closed terminal ends it - while the
target is stopped at a breakpoint over the second CDC. That is the moment the
board has the most to lose: it is inside its own trace function, waiting on a
`continue` from a client that no longer exists.

Three things then have to be true, and the first is the one that needs
hardware to prove. A USB CDC interface has no EOF: an idle one and a
host-less one both read as "no bytes", so a device with nothing else to go on
waits at that breakpoint forever and the board has to be power-cycled. The
board is expected to resume its program instead, which is only observable
here - a pty, which is what the host-side suite has, reaches real EOF and so
never exercises the mechanism that matters.

Needs both device paths; see `conftest.py`.
"""

import contextlib
from pathlib import Path

from conftest import DeviceOutput
from dapdrive import attached, run_to_breakpoint
from debuggee import COMPLETION_LINE, DEVICE_SOURCE_PATH, LOOP_LINE

# Generous against the board, tight against the failure it guards. Resuming
# costs the device one poll of a channel it has already stopped believing in;
# a device that never notices waits out the whole of this and every later
# timeout too.
_RESUME_TIMEOUT = 30


def test_hil_killing_the_bridge_at_a_breakpoint_leaves_the_board_usable(
    hil_serial_dap_runner, hil_device, hil_dap_device
):
    """SIGKILL at a breakpoint: the target finishes, and the board takes a new session.

    Both halves are asserted because either alone would pass for something
    worse. A board that resumes but cannot be debugged again has a wedged
    interface; a board that takes a new session only because that session
    soft-resets it was still frozen in between, which is what a user sees as
    a hung program.
    """
    session = hil_serial_dap_runner()
    proc = session["process"]

    with attached(session) as server:
        stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
        assert stopped.body["reason"] == "breakpoint", stopped

        # Opened before the kill, and a second holder of a port the command
        # still has. A CDC interface only carries stdout while some host holds
        # it, and the killed command is the only holder of the primary one:
        # everything the board prints between its death and this port being
        # reopened is discarded by the device, and the board resumes within a
        # poll and prints its way to the completion line in well under the
        # time reopening takes. Nothing is stolen from the command by reading
        # alongside it, because a target stopped at a breakpoint prints
        # nothing. The DAP interface, the one whose loss is under test, is
        # untouched.
        with contextlib.closing(DeviceOutput(hil_device)) as output:
            proc.kill()
            proc.wait(timeout=15)

            assert output.wait_for(COMPLETION_LINE, timeout=_RESUME_TIMEOUT), (
                "the target never resumed after the bridge was killed while it "
                f"was stopped; the board printed:\n{output.text()}"
            )

    # A vanished interface would fail the next run with a config error rather
    # than the wedge this is about, so it is worth separating.
    assert Path(hil_device).exists(), f"{hil_device} disappeared after the kill"
    assert Path(hil_dap_device).exists(), f"{hil_dap_device} disappeared after the kill"

    fresh = hil_serial_dap_runner()
    assert fresh["caps"]["serial_dap"] is True, fresh["caps"]
    with attached(fresh) as server:
        stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
        assert stopped.body["reason"] == "breakpoint", stopped
        assert server.request("threads").body["threads"], "no threads in the new session"


def test_hil_the_board_reports_the_dap_interface_going_quiet(hil_serial_dap_session):
    """The mechanism the scenario above depends on, asserted on its own.

    `pyb.USB_VCP.isconnected()` is the interface's DTR line. It is the only
    thing standing in for EOF on a CDC channel, so a board whose firmware or
    USB stack stopped tracking it would make the recovery above silently
    conditional on something else - a soft reset, a timeout - rather than on
    the host having gone. Asserted while a session is live, since "False" is
    also what an interface nobody has opened reports.
    """
    with attached(hil_serial_dap_session) as server:
        run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
        connected = server.request(
            "evaluate",
            {
                "expression": "__import__('pyb').USB_VCP(1).isconnected()",
                "frameId": 0,
                "context": "watch",
            },
        )
        assert connected.success, connected
        assert connected.body["result"] == "True", connected.body
