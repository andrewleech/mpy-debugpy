"""STORY-6.4: the network DAP flow, end to end on a real board.

Every scenario here runs the production command (`mpremote debug <device>
target:main`) against hardware and drives the resulting session over TCP with
a DAP client, exactly as VS Code would. Nothing is stubbed, and no address is
written down anywhere: the endpoint comes from the device's own MPDBG-READY
handshake on each run.

Set `MPY_DEBUG_HIL_DEVICE` to a board's `/dev/serial/by-id/...` path to run
them; without it the whole file skips (see `conftest.py`). The board needs a
working network connection of its own - these tests provision nothing, which
is the "fail with instructions" decision from the STORY-6.2 ticket.
"""

import os
import socket
import time

import pytest
from dapdrive import attached, run_to_breakpoint, set_breakpoints
from debuggee import (
    COMPLETION_LINE,
    DEVICE_SOURCE_PATH,
    LOCALS_LINE,
    LOOP_BODY_MARK,
    LOOP_ITERATIONS,
    LOOP_LINE,
)

# How long a paused session is held before the link is checked. The measurement
# the risk register asks for; overridable so a quick run does not pay for it.
PAUSE_SECONDS = int(os.environ.get("MPY_DEBUG_HIL_PAUSE_SECONDS", "60"))


@pytest.mark.hil_direct_endpoint
def test_hil_handshake_reports_a_reachable_lan_endpoint(hil_debug_session, hil_facts):
    """The reported endpoint is the board's own address, and it answers.

    A wildcard or loopback address here would still look like a successful
    handshake to the command while being unusable by any client, so the
    address is both inspected and connected to. The connection is the debug
    client as far as the board is concerned - it is waiting in `accept()` -
    so this consumes the run, which is why it gets one of its own.
    """
    host, port = hil_debug_session["host"], hil_debug_session["port"]
    assert host not in ("0.0.0.0", "127.0.0.1", "localhost", "::", "serial"), hil_debug_session
    assert isinstance(port, int) and port > 0, hil_debug_session

    # Not this host's own address either: the point of the network transport
    # is that the listener is on the board.
    assert socket.gethostbyname(socket.gethostname()) != host, hil_debug_session

    with socket.create_connection((host, port), timeout=15):
        pass


def test_hil_handshake_caps_match_a_live_probe(hil_debug_session, hil_facts):
    """What the handshake claims is what the running firmware reports.

    STORY-3.3's rule, checked against hardware: the capability set is probed
    from the interpreter, so an independent probe over the serial control
    plane has to agree with the one the launcher reported.

    `serial_dap` and `repl_dap` are excluded because neither is a firmware
    property - each records whether the *session* took that channel, and
    this session took TCP, which is asserted directly. A probe run over the
    REPL took no channel at all, so it reports both false and would disagree
    with a handshake from a session that did.
    """
    channel_keys = ("serial_dap", "repl_dap")

    claimed = dict(hil_debug_session["caps"])
    for key in channel_keys:
        assert claimed.pop(key, False) is False, hil_debug_session

    probed = dict(hil_facts["capabilities"])
    for key in channel_keys:
        probed.pop(key, None)
    assert claimed == probed


def test_hil_breakpoint_stops_the_target_on_the_board(hil_debug_session):
    """Attach, break, and read the stack back over WiFi.

    The whole criterion-1 flow for STORY-6.2 in one scenario: everything up
    to and including a frame the client can act on.
    """
    with attached(hil_debug_session) as server:
        stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
        assert stopped.body["reason"] == "breakpoint", stopped
        thread_id = stopped.body["threadId"]

        frames = server.request("stackTrace", {"threadId": thread_id}).body["stackFrames"]
        assert frames, "stopped with an empty stack"
        assert frames[0]["line"] == LOOP_LINE, frames[0]
        assert frames[0]["name"] == "main", frames[0]
        assert frames[0]["source"]["path"] == DEVICE_SOURCE_PATH, frames[0]

        scopes = server.request("scopes", {"frameId": frames[0]["id"]}).body["scopes"]
        locals_scope = next(s for s in scopes if s["name"].lower().startswith("local"))
        variables = server.request(
            "variables", {"variablesReference": locals_scope["variablesReference"]}
        ).body["variables"]
        assert {v["name"]: v["value"] for v in variables}.get("x") == "78", variables


def test_hil_locals_are_readable_at_a_breakpoint_in_a_deep_frame(hil_debug_session, hil_facts):
    """Every local of a paused function comes back, named, over the link.

    `inspect_local_variables` binds roughly two dozen names, which is more
    than the 32-slot limit is far from but enough for the per-variable
    plumbing to matter. Values are 30-character previews by design
    (`pdb_adapter._get_preview`), so this scenario is about names and count,
    not payload size - `test_hil_a_large_response_arrives_whole` covers the
    link.
    """
    with attached(hil_debug_session) as server:
        stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOCALS_LINE)
        assert stopped.body["reason"] == "breakpoint", stopped

        frames = server.request("stackTrace", {"threadId": stopped.body["threadId"]}).body["stackFrames"]
        assert frames[0]["name"] == "inspect_local_variables", frames[0]
        assert [f["name"] for f in frames[1:]][:1] == ["main"], frames

        scopes = server.request("scopes", {"frameId": frames[0]["id"]}).body["scopes"]
        locals_scope = next(s for s in scopes if s["name"].lower().startswith("local"))
        variables = server.request(
            "variables", {"variablesReference": locals_scope["variablesReference"]}, timeout=30
        ).body["variables"]
        by_name = {v["name"]: v["value"] for v in variables}

        if hil_facts["capabilities"].get("save_names"):
            assert by_name["dead_parrot"] == "'Norwegian Blue'", by_name
            assert by_name["life_of_brian"] == "1979", by_name
            assert "Cheddar" in by_name["cheese_market"], by_name
        else:
            # The honest fallback: placeholder names, not missing variables.
            assert any(n.startswith("local_") for n in by_name), by_name

        # `Special` is not a local: it is the synthetic group the adapter
        # emits to hold the dunder names (`pdb_adapter._special_vars`), and it
        # carries no presentation hint because there is nothing to edit.
        locals_only = [v for v in variables if v["name"] != "Special"]
        assert len(locals_only) < len(variables), variables

        # Locals are offered as read-only unless the firmware can write them
        # back, which is the STORY-1.3 contract this board's caps decide.
        if hil_facts["capabilities"].get("set_local"):
            assert [v for v in locals_only if not v.get("presentationHint")], variables
        else:
            assert all(
                v.get("presentationHint", {}).get("attributes") == ["readOnly"] for v in locals_only
            ), variables


def test_hil_a_large_response_arrives_whole(hil_debug_session):
    """A response many times the link's window is not truncated.

    A single lwIP send takes a couple of kilobytes at best, and `sock.send`
    reports the short write rather than blocking, so a 16 KB frame is written
    across many calls. Getting that wrong truncates the frame
    mid-`Content-Length`, which does not fail visibly - it desynchronises the
    stream, and every later message is read at the wrong offset. Both halves
    are checked: the whole result arrives, and the session still works after.
    """
    size = 16384
    with attached(hil_debug_session) as server:
        run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)

        response = server.request(
            "evaluate", {"expression": f"'A' * {size}", "frameId": 0, "context": "watch"}, timeout=60
        )
        assert response.success, response
        assert response.body["result"] == "A" * size, len(response.body["result"])

        assert server.request("threads").body["threads"], "session unusable after a large response"


def test_hil_a_loop_breakpoint_fires_on_every_iteration(hil_debug_session):
    """Resuming from a loop-header breakpoint stops again on the next pass.

    `LOOP_LINE` is the `for` statement, so it is reached again on every pass
    and the breakpoint is re-armed for each of them: one `continue` buys one
    pass of the body, not the rest of the program. Resuming until the stops
    run out is what proves both halves - that it keeps firing, and that it
    eventually stops firing rather than trapping the program forever.

    The stop count exceeds the iteration count by one: the header is reached
    once to set the loop up before the first pass, so the body lags the stops
    by one throughout. That is a property of where the compiler puts the line
    number, not of the debugger, hence the range rather than an equality.
    """
    with attached(hil_debug_session) as server:
        stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
        thread_id = stopped.body["threadId"]
        device = hil_debug_session["device"]

        stops = 1  # the one `run_to_breakpoint` already caught
        for _ in range(LOOP_ITERATIONS + 2):  # bounded: a breakpoint that never lets go is the failure
            since = server.mark()
            server.request("continue", {"threadId": thread_id})
            again = server.wait_event("stopped", timeout=60, since=since)
            if again is None:
                break
            assert again.body["reason"] == "breakpoint", again
            frames = server.request("stackTrace", {"threadId": thread_id}).body["stackFrames"]
            assert (frames[0]["name"], frames[0]["line"]) == ("main", LOOP_LINE), frames[0]
            stops += 1
        else:
            pytest.fail(f"still stopping at {DEVICE_SOURCE_PATH}:{LOOP_LINE} after {stops} stops")

        assert LOOP_ITERATIONS <= stops <= LOOP_ITERATIONS + 1, f"{stops} stops"
        assert device.wait_for(COMPLETION_LINE, timeout=60), device.text()
        assert device.text().count(LOOP_BODY_MARK) == LOOP_ITERATIONS, device.text()


def test_hil_target_runs_to_completion_after_continue(hil_debug_session):
    """With no breakpoint left armed, `continue` runs the program to the end.

    The breakpoint has to be cleared first or the loop header catches the
    program again on its next pass. Clearing is itself worth exercising here:
    DAP expresses it as a `setBreakpoints` with an empty list, and if the
    board kept the old set the program would never finish.

    The DAP session emits no terminated or exited event, so the launcher's
    completion line is the only evidence that the target ran to the end
    rather than dying under the tracer.
    """
    with attached(hil_debug_session) as server:
        stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
        set_breakpoints(server, DEVICE_SOURCE_PATH, [])

        since = server.mark()
        server.request("continue", {"threadId": stopped.body["threadId"]})

        device = hil_debug_session["device"]
        assert device.wait_for(COMPLETION_LINE, timeout=180), device.text()
        assert "Final" in device.text(), device.text()
        assert device.text().count(LOOP_BODY_MARK) == LOOP_ITERATIONS, device.text()
        assert not server.events_since("stopped", since), "stopped again after clearing"


@pytest.mark.hil_direct_endpoint
def test_hil_a_long_pause_does_not_drop_the_link(hil_debug_session):
    """A session held at a breakpoint stays usable, and so does the network.

    The risk-register measurement: while stopped, the board is spinning in
    the debug session's pause loop rather than running the scheduler, so the
    question is whether the WiFi link survives it.

    Two things are checked after the wait, because a stack that had stalled
    could leave the established socket looking alive. The session's own
    connection still answers a request, and the board still answers an
    unrelated connection attempt - with a refusal, which is the correct
    response: `_accept_and_initialize` closes the listening socket once a
    client is accepted, so nothing is bound to that port any more. A refusal
    is the board's TCP stack replying; a timeout would mean the link itself
    had gone away.
    """
    with attached(hil_debug_session) as server:
        stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
        thread_id = stopped.body["threadId"]

        time.sleep(PAUSE_SECONDS)  # deliberately idle: no DAP traffic at all

        frames = server.request("stackTrace", {"threadId": thread_id}, timeout=30).body["stackFrames"]
        assert frames[0]["line"] == LOOP_LINE, frames[0]

        started = time.monotonic()
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection((hil_debug_session["host"], hil_debug_session["port"]), timeout=15)
        assert time.monotonic() - started < 5, "the refusal took long enough to suggest a retry, not a reply"

        set_breakpoints(server, DEVICE_SOURCE_PATH, [])
        server.request("continue", {"threadId": thread_id})
        assert hil_debug_session["device"].wait_for(COMPLETION_LINE, timeout=180)


@pytest.mark.hil_direct_endpoint
def test_hil_two_consecutive_sessions_each_run_clean(hil_debug_runner):
    """The board is left in a state a second run can use.

    The device-side listener outlives a finished session, so a second
    `mpremote debug` fails with `Already listening for debugger` unless the
    VM is reset first. The command's own soft reset handles that, and this is
    what proves it: two full runs back to back, with nothing done in between.

    Each run is taken all the way to the target's completion line so that the
    second run starts from a board that finished rather than one abandoned at
    a breakpoint - the harder of the two states to recover, and the one a
    developer actually leaves behind.
    """
    endpoints = []
    for _ in range(2):
        session = hil_debug_runner()
        endpoints.append((session["host"], session["port"]))
        with attached(session) as server:
            stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
            assert stopped.body["reason"] == "breakpoint", stopped
            set_breakpoints(server, DEVICE_SOURCE_PATH, [])
            server.request("continue", {"threadId": stopped.body["threadId"]})
            assert session["device"].wait_for(COMPLETION_LINE, timeout=180), session["device"].text()

    assert endpoints[0] == endpoints[1], endpoints  # same board, same address


@pytest.mark.parametrize("command", ["next", "stepIn"])
def test_hil_stepping_advances_the_program(hil_debug_session, command):
    """A step request produces another stop, not a hang or a dropped session."""
    with attached(hil_debug_session) as server:
        stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
        thread_id = stopped.body["threadId"]

        since = server.mark()
        server.request(command, {"threadId": thread_id})
        stepped = server.wait_event("stopped", timeout=60, since=since)
        assert stepped is not None, "no stop after a step"
        assert stepped.body["reason"] == "step", stepped

        frames = server.request("stackTrace", {"threadId": thread_id}).body["stackFrames"]
        assert frames[0]["line"] != LOOP_LINE or frames[0]["name"] != "main", frames[0]
