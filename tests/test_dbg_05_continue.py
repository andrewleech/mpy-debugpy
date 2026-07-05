import time
from concurrent.futures import thread
from pathlib import Path
from typing import TYPE_CHECKING, List

import pytest
from helpers import PerfServer, set_breakpoints, wait_for_msg

# Calculate the repo root at runtime
_REPO_ROOT = Path(__file__).resolve().parents[1]
_TARGET_PY = str(_REPO_ROOT / "src" / "target.py")

# TODO: run a micropython module from the test scripts folder.
@pytest.mark.parametrize(
    "source_file, bp_lines",
    [
        (_TARGET_PY, [78, 89, 90]),
    ],
    indirect=True,
)
def test_debug_continue(attach_server, source_file: str, bp_lines: List, micropython_debuggee):
    """
    Test the debug continue functionality,
    and stoping at the next breakpoint
    """
    server: PerfServer
    server = attach_server

    # Set breakpoints in the debug server
    set_breakpoints(server, source_file, bp_lines)
    wait_for_msg(server, response="setBreakpoints")

    # Check that the debugee responds to the setBreakpoints request
    responses = [msg for msg in server.rcv_messages if msg.type == "response" and msg.command == "setBreakpoints"]
    assert len(responses) == 1, f"Expected 1 setBreakpoints response, got {len(responses)}"

    # Send configurationDone to let the target code start running
    client = server.client
    client.configuration_done()
    wait_for_msg(server, event="stopped")
    server.clear_messages()

    # now we can continue the debugee
    client.continue_(thread_id=1)  # Assuming threadId=1 is the main thread # TODO: Check this
    # and wait for the next breakpoint to be hit
    bp_hit = wait_for_msg(server, event="stopped")

    # check for continue response
    responses = [msg for msg in server.rcv_messages if msg.type == "response" and msg.command == "continue"]  # type: ignore
    assert len(responses) == 1, f"Expected 1 continue response, got {len(responses)}"

    assert bp_hit is True, "Expected breakpoint to be hit"
    hit_msg = server.rcv_messages[-1]
    assert hit_msg.type == "event" and hit_msg.event == "stopped", "Expected stopped event"
    assert hit_msg.body["reason"] == "breakpoint", f"Expected breakpoint hit, got {hit_msg.body['reason']}"  # type: ignore
