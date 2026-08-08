"""Clearing a file's breakpoints has to release the debuggee's copy too.

DAP has no remove-breakpoint request: `setBreakpoints` carries the whole set
for a source, so an empty list is how a client removes them. The adapter
stores each set under two keys - the path the client used, and the name the
debuggee reports for the same file - because `should_stop` matches on
`frame.f_code.co_filename`. Those two only differ when a path mapping is in
play, which is why this drives one; with `localRoot == remoteRoot` the second
key is the first and the difference is invisible.
"""

import time
from pathlib import Path

import pytest
from helpers import PerfServer, set_breakpoints, wait_for_msg

_REPO_ROOT = Path(__file__).resolve().parents[1]

# `main()`'s loop header. It is reached once per pass, so a breakpoint left
# armed here stops the program again instead of letting it run out.
_LOOP_LINE = 80
_COMPLETION_LINE = "Target completed successfully!"

# The debuggee imports `target` from `src/`, so `src/` is the root its frames
# report against. Mapping it to the repo root gives the client a different
# path for the same file, which is what makes the two keys diverge.
_CLIENT_PATH = str(_REPO_ROOT / "target.py")


def _read_available(process):
    """Whatever the debuggee has printed since the last read (non-blocking)."""
    try:
        return process.stdout.read(4096) or ""
    except (BlockingIOError, OSError):
        return ""


@pytest.mark.parametrize("local_root", [str(_REPO_ROOT)], indirect=True)
@pytest.mark.parametrize("remote_root", [str(_REPO_ROOT / "src")], indirect=True)
def test_clearing_a_mapped_files_breakpoints_lets_the_program_finish(
    attach_server, micropython_debuggee
):
    server: PerfServer = attach_server
    client = server.client

    set_breakpoints(server, _CLIENT_PATH, [_LOOP_LINE])
    armed = wait_for_msg(server, response="setBreakpoints")
    assert armed is not None and armed.body["breakpoints"][0]["verified"], armed

    client.configuration_done()
    stopped = wait_for_msg(server, event="stopped")
    # Also the check that the mapping is doing something: the client's path
    # does not exist on disk, so a stop here can only come from the mapped
    # name matching the frame.
    assert stopped is not None, f"nothing stopped at {_CLIENT_PATH}:{_LOOP_LINE}"

    server.clear_messages()
    set_breakpoints(server, _CLIENT_PATH, [])
    cleared = wait_for_msg(server, response="setBreakpoints")
    assert cleared is not None and cleared.body["breakpoints"] == [], cleared

    server.clear_messages()
    client.continue_(thread_id=stopped.body["threadId"])

    output = ""
    deadline = time.time() + 20
    while _COMPLETION_LINE not in output and time.time() < deadline:
        server.run_single()
        output += _read_available(micropython_debuggee)
        time.sleep(0.05)

    assert _COMPLETION_LINE in output, output
    assert not [m for m in server.rcv_messages if m.type == "event" and m.event == "stopped"], (
        "stopped again at a breakpoint the client had removed"
    )
