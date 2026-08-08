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

import os
import sys
import time
from pathlib import Path
from typing import List

import pytest
from fixtures.debuggee import read_mpdbg_ready
from helpers import set_breakpoints, wait_for_msg

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TARGET_PY = str(_REPO_ROOT / "src" / "target.py")

_LAUNCHER_DIR = str(_REPO_ROOT / "launcher")
if _LAUNCHER_DIR not in sys.path:
    sys.path.insert(0, _LAUNCHER_DIR)

import firmware  # noqa: E402


def test_epic1_mpdbg_ready_handshake(attach_server, micropython_debuggee):
    """STORY-1.2/1.4: the handshake line is valid JSON with a boolean caps dict.

    Also checks the live caps keys against `firmware.KNOWN_CAPABILITIES` so a
    probe key rename shows up here rather than as a silent mismatch further
    down the pipeline.
    """
    payload = read_mpdbg_ready(micropython_debuggee)

    assert "host" in payload and "port" in payload and "caps" in payload

    caps = payload["caps"]
    # `serial_dap` is probed and reported like any other capability but is
    # deliberately excluded from `KNOWN_CAPABILITIES` (see mpdebug_config.py):
    # it names a specific dap_device wiring, not a generic interpreter
    # feature a target's `requires`/`--need` can ask for.
    assert set(caps) - {"serial_dap"} == set(firmware.KNOWN_CAPABILITIES), (
        f"caps keys {set(caps)} != {set(firmware.KNOWN_CAPABILITIES)} + {{'serial_dap'}}"
    )
    for key in (*firmware.KNOWN_CAPABILITIES, "serial_dap"):
        assert isinstance(caps[key], bool), f"caps['{key}'] should be a bool, got {caps[key]!r}"
    # This session's DAP channel is a TCP socket, so `serial_dap` must say so.
    # The stream direction is asserted in test_s6_1_stream_transport.py; both
    # are needed, or a constant satisfies whichever one is written alone.
    assert caps["serial_dap"] is False, caps


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

    caps = read_mpdbg_ready(process)["caps"]

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


def _spawn_launcher(root_path, port):
    """Start the launcher directly, without the debuggee fixture.

    The fixture drains stdout and attaches a client during setup; these tests
    need to observe the handshake before anything connects.
    """
    import os
    import subprocess

    micropython_path = Path(
        os.environ.get(
            "MPY_DEBUG_FIRMWARE",
            root_path / "micropython/ports/unix/build-standard/micropython",
        )
    )
    env = os.environ.copy()
    env["MICROPYPATH"] = "{}:{}".format(
        root_path / "src", root_path / "micropython-lib/python-ecosys/debugpy"
    )
    return subprocess.Popen(
        [
            str(micropython_path),
            str(root_path / "launcher/mpy_launch_debugpy.py"),
            "target",
            "main",
            str(port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _read_until(process, predicate, timeout=15):
    """Return the first stdout line satisfying `predicate`, or None on timeout.

    Reads non-blocking so a regression that never emits the awaited line fails
    on the deadline instead of hanging the suite.
    """
    import fcntl

    fcntl.fcntl(process.stdout, fcntl.F_SETFL, os.O_NONBLOCK)
    deadline = time.time() + timeout
    buffered = ""
    while time.time() < deadline:
        try:
            chunk = process.stdout.read(4096)
        except (BlockingIOError, OSError, TypeError):
            chunk = None
        if chunk:
            buffered += chunk
            for line in buffered.splitlines():
                if predicate(line):
                    return line
        elif process.poll() is not None:
            return None
        time.sleep(0.05)
    return None


def _terminate(process):
    import subprocess

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_endpoint_is_published_before_any_client_attaches(pytestconfig, free_tcp_port):
    """Q8: the handshake is readable with nothing attached, so a client can use it.

    Before the bind/accept split, `listen()` only returned - and the launcher
    only printed MPDBG-READY - after a client had connected and sent
    `initialize`, so this read would find nothing and the endpoint was
    unknowable in advance. Reads the handshake first, then connects to the port
    it advertises.

    An explicit port is used because this target has no `getsockname()`; see
    `test_port_zero_refuses_when_the_target_cannot_report_the_port`.
    """
    import json
    import socket

    process = _spawn_launcher(Path(pytestconfig.rootpath), free_tcp_port)
    try:
        # Hand-rolled rather than mpdebug_handshake.read_handshake: this
        # asserts the launcher's raw emission, deliberately not routed
        # through the resolving parser.
        line = _read_until(process, lambda ln: ln.startswith("MPDBG-READY "))
        assert line is not None, "no MPDBG-READY line before any client attached"

        payload = json.loads(line[len("MPDBG-READY ") :])
        assert payload["port"] == free_tcp_port, payload
        assert isinstance(payload["caps"], dict) and payload["caps"], payload

        # The proof: connect to the advertised endpoint, having read it first.
        with socket.create_connection(("localhost", payload["port"]), timeout=10):
            pass
    finally:
        _terminate(process)


def test_port_zero_refuses_when_the_target_cannot_report_the_port(pytestconfig):
    """Q8 sub-decision: refuse rather than advertise an endpoint nothing serves.

    `port=0` needs `getsockname()` to learn what the system assigned. The unix
    port does not implement it, so the request fails loudly instead of
    reporting DEFAULT_PORT, which would send a client to an address the socket
    is not bound to.
    """
    process = _spawn_launcher(Path(pytestconfig.rootpath), 0)
    try:
        line = _read_until(process, lambda ln: "getsockname" in ln or ln.startswith("MPDBG-READY "))
        assert line is not None, "expected either a clean refusal or a handshake"
        assert not line.startswith("MPDBG-READY "), (
            f"port=0 must not advertise an endpoint on a target that cannot report it: {line}"
        )
        assert "pass an explicit port" in line, line
    finally:
        _terminate(process)
