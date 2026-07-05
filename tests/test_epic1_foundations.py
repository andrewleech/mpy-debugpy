"""Behavioural tests for EPIC-1 foundations: protocol correctness + capability truth.

These drive a real DAP session against the running MicroPython debuggee
rather than grepping source files, so they fail if the underlying behaviour
regresses even when the surrounding code is refactored:

- STORY-1.1: a breakpoint set before configurationDone actually stops the
  target with a `stopped`/`breakpoint` event, and the target does not run
  past it while still paused.
- STORY-1.2/1.4: the launcher emits exactly one `MPDBG-READY` line whose
  JSON payload carries a `caps` dict with boolean settrace/save_names/
  set_local/f_back entries.
- STORY-1.3: local-scope variables at a breakpoint carry a
  `presentationHint.attributes == ["readOnly"]` hint when the firmware
  reports no `set_local` support, while global-scope variables never do.
"""

import json
import time
from pathlib import Path
from typing import List

import pytest
from helpers import set_breakpoints, wait_for_msg

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TARGET_PY = str(_REPO_ROOT / "src" / "target.py")


def _read_mpdbg_ready(process, timeout=5):
    """Drain `process.stdout` (already non-blocking, per the debuggee fixture)
    until the launcher's `MPDBG-READY <json>` handshake line shows up, and
    return its decoded payload.
    """
    deadline = time.time() + timeout
    stdout_data = ""
    while time.time() < deadline:
        try:
            chunk = process.stdout.read(4096)
            if chunk:
                stdout_data += chunk
        except (BlockingIOError, OSError):
            pass
        if "MPDBG-READY " in stdout_data:
            break
        time.sleep(0.05)

    ready_lines = [
        line for line in stdout_data.splitlines() if line.startswith("MPDBG-READY ")
    ]
    assert len(ready_lines) == 1, (
        f"Expected exactly one MPDBG-READY line, got {len(ready_lines)}: {ready_lines!r}"
    )
    return json.loads(ready_lines[0][len("MPDBG-READY ") :])


def test_epic1_mpdbg_ready_handshake(attach_server, micropython_debuggee):
    """STORY-1.2/1.4: the handshake line is valid JSON with a boolean caps dict."""
    payload = _read_mpdbg_ready(micropython_debuggee)

    assert "host" in payload and "port" in payload and "caps" in payload

    caps = payload["caps"]
    for key in ("settrace", "save_names", "set_local", "f_back"):
        assert key in caps, f"caps missing '{key}': {caps}"
        assert isinstance(caps[key], bool), f"caps['{key}'] should be a bool, got {caps[key]!r}"


@pytest.mark.parametrize("source_file, bp_lines", [(_TARGET_PY, [78])])
def test_epic1_breakpoint_stops_target(
    attach_server, source_file: str, bp_lines: List[int], micropython_debuggee
):
    """STORY-1.1: configurationDone drains to a real breakpoint stop, not a hang."""
    server = attach_server
    process = micropython_debuggee

    set_breakpoints(server, source_file, bp_lines)
    wait_for_msg(server, response="setBreakpoints")

    server.client.configuration_done()
    assert wait_for_msg(server, event="stopped"), "configurationDone produced no stopped event"

    stopped_events = [m for m in server.rcv_messages if m.type == "event" and m.event == "stopped"]
    assert stopped_events, "Expected a stopped event"
    assert stopped_events[0].body.get("reason") == "breakpoint", stopped_events[0].body

    # The target should still be paused at the breakpoint - give it a moment
    # to (wrongly) keep running, then confirm it never reached the end.
    time.sleep(0.3)
    try:
        tail = process.stdout.read(4096) or ""
    except (BlockingIOError, OSError):
        tail = ""
    assert "Final" not in tail, f"Target ran past the breakpoint while still stopped: {tail!r}"


def test_epic1_readonly_locals_hint(attach_server, micropython_debuggee):
    """STORY-1.3: locals are read-only exactly when the firmware lacks set_local."""
    server = attach_server
    process = micropython_debuggee
    client = server.client

    caps = _read_mpdbg_ready(process)["caps"]

    # Break on line 80 (the `for` header) rather than line 79 (`x = 78`):
    # sys.settrace fires the `line` event BEFORE the statement runs (correct
    # CPython/pdb semantics), so at line 79 `x` is not yet bound. By line 80,
    # `x = 78` has executed and the local is present to carry the hint.
    set_breakpoints(server, _TARGET_PY, [80])
    wait_for_msg(server, response="setBreakpoints")
    server.client.configuration_done()
    assert wait_for_msg(server, event="stopped"), "configurationDone produced no stopped event"

    server.clear_messages()
    client.send_request("stackTrace", {"threadId": 1})
    wait_for_msg(server, response="stackTrace")
    frames = server.rcv_messages[-1].body["stackFrames"]
    assert frames, "Expected at least one stack frame"
    frame_id = frames[0]["id"]

    client.send_request("scopes", {"frameId": frame_id})
    wait_for_msg(server, response="scopes")
    scopes = server.rcv_messages[-1].body["scopes"]
    locals_ref = next(s["variablesReference"] for s in scopes if s["name"] == "locals")
    globals_ref = next(s["variablesReference"] for s in scopes if s["name"] == "globals")

    # clear_messages() between each variables request: both requests share
    # the same response command name, so wait_for_msg's "last message
    # matches" check can't otherwise tell the locals response apart from a
    # still-pending globals response.
    server.clear_messages()
    client.send_request("variables", {"variablesReference": locals_ref})
    wait_for_msg(server, response="variables")
    local_vars = [v for v in server.rcv_messages[-1].body["variables"] if v["name"] != "Special"]

    server.clear_messages()
    client.send_request("variables", {"variablesReference": globals_ref})
    wait_for_msg(server, response="variables")
    global_vars = [v for v in server.rcv_messages[-1].body["variables"] if v["name"] != "Special"]

    assert local_vars, "Expected at least one local variable at the breakpoint"
    assert global_vars, "Expected at least one global variable"

    expect_locals_readonly = not caps.get("set_local", False)
    for var in local_vars:
        attributes = var.get("presentationHint", {}).get("attributes", [])
        if expect_locals_readonly:
            assert attributes == ["readOnly"], f"local '{var['name']}' missing readOnly hint: {var}"
        else:
            assert "readOnly" not in attributes, f"local '{var['name']}' unexpectedly readOnly: {var}"

    for var in global_vars:
        attributes = var.get("presentationHint", {}).get("attributes", [])
        assert "readOnly" not in attributes, f"global '{var['name']}' unexpectedly readOnly: {var}"
