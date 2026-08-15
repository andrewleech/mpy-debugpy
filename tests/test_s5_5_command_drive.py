"""Host-side pytest coverage for `mpremote debug` DAP client end-to-end flow.

Extends s5.3's handshake/endpoint-reporting tests with the full assertion
sequence: setBreakpoints → configurationDone → stopped(breakpoint) →
stackTrace → scopes → variables (with readOnly hint per caps) → next(step) →
continue → target completes.

The command runs hermetically with the integration's mpremote
(micropython/tools/mpremote), not whatever is on PATH. `mpremote_debuggee`
targets `unix target:main` directly with an explicit --port, so most tests
need no mpdebug.toml; the corrupt-config test writes one to tmp_path, never
committed at repo root.

Coverage:
- Unix flow: attach, breakpoint stop, step, locals inspect (readOnly hint
  per caps), continue to completion.
- Endpoint derived from the command's re-emitted handshake (not hardcoded);
  attach to what the command reported, and that host is the resolved
  loopback, not the device's raw wildcard bind.
- Adversarial: SIGTERM the supervising mpremote process mid-session (no
  orphaned launcher); corrupt mpdebug.toml (error surfaces via exit code
  and output, naming the config path and the TOML failure).
- A serial/PTY target (the unix firmware standing behind a pty pair, exactly
  as QEMU or a USB-serial bridge would) driven through the real `mpremote
  resume debug` subprocess, proving the `is_pty` endpoint-resolution path
  s5.1/s5.4 depend on all the way through a live DAP `initialize`.

Not covered here: a literal QEMU leg. `qemu-system-arm` is not installed on
this machine, so `test_s5_5_mpremote_debug_qemu_pty_leg` always skips here;
even where it is installed, this tree has no settrace-capable firmware built
for the qemu port (`ports/qemu` uses semihosting console I/O, not a UART a
PTY could stand behind), so that test also needs
`MPY_DEBUG_QEMU_FIRMWARE`/`MPY_DEBUG_QEMU_MACHINE` pointing at one - see the
ticket.

Limitation: two concurrent sessions collide by construction (both use
debugpy.DEFAULT_PORT with --port omitted). Distinct ports would be
required to test concurrency; that capability depends on a port with
getsockname() support, which would also unlock Q8's auto-port fallback.
"""

import json
import os
import pty
import re
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest
from helpers import PerfServer, set_breakpoints, wait_for_msg


def _seen_response(server, command):
    """True if `command`'s response is anywhere in the buffer.

    `wait_for_msg` matches only the most recent message, which is right for a
    response the caller just triggered but wrong for `initialize`: the server
    sends `initialized` straight after it, so it is never last by the time a
    test looks.
    """
    return any(m.type == "response" and m.command == command for m in server.rcv_messages)
from mpremote_debug import (
    MICROPYTHON as _MICROPYTHON,
    RESUME as _RESUME,
    SUBMODULE_DIR as _SUBMODULE_DIR,
    TOP_DIR as _TOP_DIR,
    child_pids as _child_pids,
    end_session as _end_session,
    new_launcher_pids as _new_launcher_pids,
    pids_alive as _pids_alive,
    read_until as _read_until,
    requires_settrace_firmware,
    spawn_debug as _spawn_debug,
)

_MICROPYPATH = "{}:{}:{}".format(
    _TOP_DIR / "src", _TOP_DIR / "micropython-lib/python-ecosys/debugpy", _TOP_DIR / "micropython-lib"
)
_TARGET_PY = str(_TOP_DIR / "src" / "target.py")


@pytest.fixture()
def mpremote_debuggee(free_tcp_port):
    """Fixture to spawn `mpremote debug unix target:main` via the command.

    Spawns the command, parses the re-emitted MPDBG-READY handshake for
    host/port, and yields the spawned process and endpoint info. The endpoint
    is derived from the command's output, not from a hardcoded config.
    """
    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(_MICROPYTHON)
    env["MICROPYPATH"] = _MICROPYPATH

    proc = _spawn_debug(["debug", "--port", str(free_tcp_port), "unix", "target:main"], env=env)

    # Read until we see the handshake
    lines, matched = _read_until(proc, "MPDBG-READY ", at_line_start=True)
    if matched is None:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail(f"never saw MPDBG-READY; output: {''.join(lines)}")

    new_pids = _new_launcher_pids(proc)

    # Parse the endpoint from the handshake ("MPDBG-READY {...}").
    try:
        json_start = matched.index("{")
        payload = json.loads(matched[json_start:])
        host = payload["host"]
        port = payload["port"]
        caps = payload["caps"]
    except (ValueError, KeyError, IndexError) as e:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail(f"failed to parse endpoint from handshake: {e}; line: {matched}")

    yield {
        "process": proc,
        "host": host,
        "port": port,
        "caps": caps,
        "new_pids": new_pids,
        "all_output": lines,
    }

    # Cleanup: end session and confirm no orphans. If the process has
    # already exited, just confirm no orphans survived it.
    if proc.poll() is not None:
        still_alive = _pids_alive(new_pids)
        assert not still_alive, f"launcher child(ren) {still_alive} still alive after the session ended"
    else:
        _end_session(proc, new_pids)


@requires_settrace_firmware
def test_s5_5_mpremote_debug_endpoint_resolved_from_wildcard(mpremote_debuggee):
    """The reported host is the command's resolved loopback, not the device's
    raw wildcard bind - the one endpoint fact `mpremote debug` actually derives.

    The unix build has no `network` module, so the launcher always binds
    0.0.0.0 (visible in its own banner, echoed to the command's stdout); the
    command must still hand the DAP client a connectable loopback address.
    """
    debuggee_info = mpremote_debuggee
    banner = "".join(debuggee_info["all_output"])
    assert "0.0.0.0" in banner, f"expected the device's own wildcard bind banner; got: {banner}"
    assert debuggee_info["host"] == "127.0.0.1", (
        f"expected the resolved loopback host; got {debuggee_info['host']!r}"
    )
    _end_session(debuggee_info["process"], debuggee_info["new_pids"])


@requires_settrace_firmware
def test_s5_5_mpremote_debug_full_flow(pytestconfig, mpremote_debuggee):
    """Full DAP flow via `mpremote debug`: attach, breakpoint, locals, continue.

    Mirrors test_epic1_foundations.py assertions but through the mpremote
    command. Breakpoint on line 80 (the `for` header), not 79, because
    sys.settrace fires the `line` event BEFORE the statement runs.
    """
    debuggee_info = mpremote_debuggee
    proc = debuggee_info["process"]
    host = debuggee_info["host"]
    port = debuggee_info["port"]
    caps = debuggee_info["caps"]
    new_pids = debuggee_info["new_pids"]
    root_path = Path(pytestconfig.rootpath)

    server = PerfServer("test-client", host, port)
    try:
        server.start()

        # Attach and configure
        wait_for_msg(server, response="initialize", timeout=10)
        assert _seen_response(server, "initialize"), (
            "no initialize response received"
        )

        # Send attach request (required before setBreakpoints/configurationDone)
        server.client.send_request(
            "attach",
            {
                "name": "Attach to MicroPython",
                "type": "debugpy",
                "request": "attach",
                "connect": {"host": host, "port": port},
                "pathMappings": [
                    {
                        "localRoot": str(root_path / "src"),
                        "remoteRoot": str(root_path / "src"),
                    }
                ],
                "workspaceFolder": str(root_path),
                "justMyCode": True,
                "logToFile": False,
                "clientOS": "unix",
                "debugOptions": ["RedirectOutput", "ShowReturnValue"],
                "showReturnValue": True,
            },
        )
        assert wait_for_msg(server, response="attach", timeout=10), "attach failed"

        # Break on line 80 (the `for` header), not 79 (`x = 78`): sys.settrace
        # fires the `line` event before the statement runs, so at line 79 the
        # local isn't bound yet.
        set_breakpoints(server, _TARGET_PY, [80])
        assert wait_for_msg(server, response="setBreakpoints"), "setBreakpoints failed"

        # Start running and wait for breakpoint stop
        server.client.configuration_done()
        stopped_msg = wait_for_msg(server, event="stopped", timeout=15)
        if stopped_msg is None:
            # Print all received messages for debugging
            msg_summary = ", ".join(
                f"{m.type}:{m.event or m.command}" for m in server.rcv_messages
            )
            launcher_output = "\n".join(debuggee_info.get("all_output", []))
            proc_returncode = proc.poll()
            pytest.fail(
                f"configurationDone produced no stopped event. "
                f"Process returncode: {proc_returncode}\n"
                f"Messages received: {msg_summary}\n"
                f"Launcher output:\n{launcher_output}"
            )
        assert stopped_msg.body.get("reason") == "breakpoint", (
            f"stopped reason not 'breakpoint': {stopped_msg.body}"
        )

        # Get stack trace at the breakpoint
        server.clear_messages()
        server.client.send_request("stackTrace", {"threadId": 1})
        stack_resp = wait_for_msg(server, response="stackTrace")
        assert stack_resp is not None, "stackTrace request failed"
        frames = stack_resp.body.get("stackFrames", [])
        assert frames, "Expected at least one stack frame"
        frame_id = frames[0]["id"]

        # Get scopes for the frame
        server.clear_messages()
        server.client.send_request("scopes", {"frameId": frame_id})
        scopes_resp = wait_for_msg(server, response="scopes")
        assert scopes_resp is not None, "scopes request failed"
        scopes = scopes_resp.body.get("scopes", [])
        locals_ref = next(
            (s["variablesReference"] for s in scopes if s["name"] == "locals"),
            None,
        )
        assert locals_ref is not None, "locals scope not found"

        # Get local variables and check the readOnly hint against the
        # handshake's own caps, per STORY-1.3.
        server.clear_messages()
        server.client.send_request("variables", {"variablesReference": locals_ref})
        vars_resp = wait_for_msg(server, response="variables")
        assert vars_resp is not None, "variables request failed"
        local_vars = [
            v for v in vars_resp.body.get("variables", []) if v["name"] != "Special"
        ]
        assert local_vars, "Expected at least one local variable"

        expect_readonly = not caps.get("set_local", False)
        for var in local_vars:
            attributes = var.get("presentationHint", {}).get("attributes", [])
            if expect_readonly:
                assert attributes == ["readOnly"], (
                    f"expected readOnly hint on {var['name']!r} (caps={caps}): {var}"
                )
            else:
                assert "readOnly" not in attributes, (
                    f"unexpected readOnly hint on {var['name']!r} (caps={caps}): {var}"
                )

        # Step over the breakpoint line: a `next` request should land at a
        # later line in the same frame, with reason "step" not "breakpoint".
        server.clear_messages()
        server.client.send_request("next", {"threadId": 1})
        # Wait on the stopped event rather than the `next` response: the event
        # follows the response, so the response is not the most recent message
        # by the time it is checked.
        step_stopped = wait_for_msg(server, event="stopped", timeout=10)
        assert _seen_response(server, "next"), "no next response"
        assert step_stopped is not None, "next produced no stopped event"
        assert step_stopped.body.get("reason") == "step", (
            f"stopped reason not 'step': {step_stopped.body}"
        )

        server.clear_messages()
        server.client.send_request("stackTrace", {"threadId": 1})
        step_stack = wait_for_msg(server, response="stackTrace", timeout=10)
        assert step_stack is not None, "no stackTrace response after step"
        step_frames = step_stack.body.get("stackFrames", [])
        assert step_frames, "expected at least one stack frame after step"
        assert step_frames[0]["line"] > 80, (
            f"expected the step to move past line 80: {step_frames[0]}"
        )

        # Clear the breakpoint before continuing: it sits on the `for`
        # header, which fires once per loop iteration, so leaving it set
        # would re-stop the target instead of letting it run to completion.
        server.clear_messages()
        set_breakpoints(server, _TARGET_PY, [])
        assert wait_for_msg(server, response="setBreakpoints", timeout=10), "clearing breakpoints failed"

        # Continue and prove completion from the process the command owns:
        # the continue response, the target's own completion print, and the
        # supervising mpremote exiting 0 on its own (not via _end_session).
        server.clear_messages()
        server.client.continue_()
        assert wait_for_msg(server, response="continue", timeout=10), "no continue response"

        completion_lines, matched = _read_until(proc, "Final", timeout=10)
        assert matched is not None, (
            f"target never printed its completion line: {''.join(completion_lines)}"
        )

        returncode = proc.wait(timeout=10)
        assert returncode == 0, f"mpremote exited {returncode} instead of completing cleanly"

    finally:
        try:
            server.stop()
        except Exception:
            pass
        _end_session(proc, new_pids)


@requires_settrace_firmware
def test_s5_5_mpremote_debug_breakpoint_stops_target(pytestconfig, mpremote_debuggee):
    """Breakpoint set before configurationDone actually stops the target.

    The target should not run past the breakpoint while still paused.
    """
    debuggee_info = mpremote_debuggee
    proc = debuggee_info["process"]
    host = debuggee_info["host"]
    port = debuggee_info["port"]
    new_pids = debuggee_info["new_pids"]
    root_path = Path(pytestconfig.rootpath)

    server = PerfServer("test-client", host, port)
    try:
        server.start()
        wait_for_msg(server, response="initialize", timeout=10)
        assert _seen_response(server, "initialize"), "no initialize response"

        # Send attach request
        server.client.send_request(
            "attach",
            {
                "name": "Attach to MicroPython",
                "type": "debugpy",
                "request": "attach",
                "connect": {"host": host, "port": port},
                "pathMappings": [
                    {
                        "localRoot": str(root_path / "src"),
                        "remoteRoot": str(root_path / "src"),
                    }
                ],
                "workspaceFolder": str(root_path),
                "justMyCode": True,
                "logToFile": False,
                "clientOS": "unix",
                "debugOptions": ["RedirectOutput", "ShowReturnValue"],
                "showReturnValue": True,
            },
        )
        assert wait_for_msg(server, response="attach", timeout=10)

        set_breakpoints(server, _TARGET_PY, [80])
        assert wait_for_msg(server, response="setBreakpoints")

        server.client.configuration_done()
        stopped_msg = wait_for_msg(server, event="stopped", timeout=10)
        assert stopped_msg is not None

        # Target should still be paused: give it a moment to (wrongly) keep
        # running, then confirm its own completion print never arrived.
        extra_lines, _ = _read_until(proc, marker=None, timeout=0.3)
        tail = "".join(extra_lines)
        assert "Final" not in tail, f"target ran past the breakpoint while still stopped: {tail!r}"

    finally:
        try:
            server.stop()
        except Exception:
            pass
        _end_session(proc, new_pids)


def test_s5_5_mpremote_debug_corrupt_config_surfaces_error(tmp_path):
    """Corrupt mpdebug.toml surfaces the resolver's own error via exit code and output.

    A malformed config file must fail during config resolution, before any
    child is spawned - so no launcher child ever appears during the run, and
    the error names the config path and the TOML failure, not some other
    failure mode (e.g. "no unix debug binary found") that would also satisfy
    a bare non-zero exit code.
    """
    config_path = tmp_path / "mpdebug.toml"
    config_path.write_text("[target.main\ninvalid toml here\n")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(_SUBMODULE_DIR)

    proc = _spawn_debug(["debug", "main"], env=env, cwd=tmp_path)

    # Poll for exit while watching for a child spawn in the meantime, scoped
    # to this proc so an unrelated launcher elsewhere never counts.
    deadline = time.monotonic() + 10
    spawned_child = False
    while proc.poll() is None and time.monotonic() < deadline:
        if _child_pids(proc.pid):
            spawned_child = True
        time.sleep(0.05)
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("mpremote did not exit after corrupt config")

    assert proc.returncode != 0, "should have exited with error"
    assert not spawned_child, "a launcher child was spawned even though config resolution should fail first"

    output = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
    assert str(config_path) in output, f"error should name the config path; got: {output}"
    assert "invalid TOML" in output, f"error should report invalid TOML; got: {output}"


def test_s5_5_mpremote_debug_terminated_supervisor_leaves_no_orphan(pytestconfig, mpremote_debuggee):
    """SIGTERM to the supervising `mpremote debug` process leaves no orphaned launcher.

    s5.3 found the leak in this direction: an unreaped launcher outlives its
    supervisor and keeps the debug port bound for the next run.
    `_do_debug_unix` installs a SIGTERM handler specifically to reap the
    child before exiting (systemd/CI teardown, `timeout`, Node's
    `child.kill()` all send SIGTERM, not SIGKILL) - this is the signal that
    handler exists for. Killing the launcher child instead (the reverse
    direction) would make the orphan check tautological - the test would be
    asserting that a process it just killed itself is dead. SIGKILL to the
    supervisor is not used here: it gives the process no chance to run any
    handler at all, so no implementation could satisfy it.
    """
    debuggee_info = mpremote_debuggee
    proc = debuggee_info["process"]
    host = debuggee_info["host"]
    port = debuggee_info["port"]
    new_pids = debuggee_info["new_pids"]
    root_path = Path(pytestconfig.rootpath)

    server = PerfServer("test-client", host, port)
    try:
        server.start()
        wait_for_msg(server, response="initialize", timeout=10)
        assert _seen_response(server, "initialize"), "no initialize response"

        # Send attach request
        server.client.send_request(
            "attach",
            {
                "name": "Attach to MicroPython",
                "type": "debugpy",
                "request": "attach",
                "connect": {"host": host, "port": port},
                "pathMappings": [
                    {
                        "localRoot": str(root_path / "src"),
                        "remoteRoot": str(root_path / "src"),
                    }
                ],
                "workspaceFolder": str(root_path),
                "justMyCode": True,
                "logToFile": False,
                "clientOS": "unix",
                "debugOptions": ["RedirectOutput", "ShowReturnValue"],
                "showReturnValue": True,
            },
        )
        assert wait_for_msg(server, response="attach", timeout=10)

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail("mpremote did not exit after SIGTERM")
        assert proc.returncode != 0, f"expected a non-zero exit; got {proc.returncode}"

        deadline = time.monotonic() + 5
        still_alive = _pids_alive(new_pids)
        while still_alive and time.monotonic() < deadline:
            time.sleep(0.1)
            still_alive = _pids_alive(new_pids)
        assert not still_alive, f"launcher child(ren) {still_alive} orphaned after SIGTERM to mpremote"

    finally:
        server.stop()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@requires_settrace_firmware
def test_s5_5_mpremote_debug_serial_pty_leg(free_tcp_port):
    """`mpremote resume debug` against a serial target behind a real pty.

    Stands the unix firmware behind a pty pair, exactly as QEMU or a
    USB-serial bridge would (same technique as s5.1's
    `test_do_debug_over_real_pty_reads_handshake_before_client_attach`), but
    drives it through the `mpremote` CLI subprocess rather than calling
    `do_debug` in-process - proving the `is_pty` endpoint-resolution path
    the ticket's QEMU leg depends on all the way through a live DAP
    `initialize`. `resume` (do_resume) is required first: the unix build
    exits on the raw-REPL soft reset `ensure_raw_repl` would otherwise do,
    unlike a real device that reboots into a fresh REPL.

    Unlike the unix-kind path, `mpremote debug` here does not own or
    supervise the device process - it reports the endpoint and exits - so
    this test owns the pty-backed process directly and there is no launcher
    child to check for orphans.
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    env = os.environ.copy()
    env["MICROPYPATH"] = _MICROPYPATH

    device = subprocess.Popen(
        [str(_MICROPYTHON)], stdin=master_fd, stdout=master_fd, stderr=master_fd, env=env, close_fds=True
    )
    os.close(master_fd)
    # `slave_fd` is held for the whole run: a pty with no open slave makes the
    # device's next read on the master fail EIO, and the unix port treats a
    # failed stdin read as end of input, so the interpreter exits before
    # `mpremote` gets a chance to open the path. See `tests/pty_device.py`.
    try:
        time.sleep(0.3)  # let the interpreter start its REPL before talking to it

        proc = _spawn_debug(
            _RESUME + ["debug", "--port", str(free_tcp_port), slave_path, "target:main"], env=env
        )
        lines, matched = _read_until(proc, "debug server listening on", timeout=15)
        if matched is None:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"never saw the endpoint report; output: {''.join(lines)}")

        returncode = proc.wait(timeout=10)
        assert returncode == 0, f"mpremote exited {returncode} instead of reporting cleanly"

        report = "".join(lines)
        m = re.search(r"debug server listening on ([\d.]+):(\d+)", report)
        assert m is not None, f"could not parse the reported endpoint; output: {report}"
        host, port = m.group(1), int(m.group(2))
        assert host == "127.0.0.1", f"expected the resolved loopback host; got {host!r}"
        assert port == free_tcp_port, f"expected the requested port to be echoed; got {port}"

        client = socket.create_connection((host, port), timeout=5)
        try:
            body = json.dumps({"seq": 1, "type": "request", "command": "initialize", "arguments": {}}).encode()
            client.sendall(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            client.settimeout(5)
            reply = client.recv(4096)
        finally:
            client.close()
        assert b'"initialize"' in reply and b'"success": true' in reply, (
            f"client did not receive a successful initialize response; got: {reply!r}"
        )
    finally:
        device.terminate()
        try:
            device.wait(timeout=5)
        except subprocess.TimeoutExpired:
            device.kill()
            device.wait(timeout=5)
        os.close(slave_fd)


_QEMU_BINARY = shutil.which("qemu-system-arm")
_QEMU_FIRMWARE = os.environ.get("MPY_DEBUG_QEMU_FIRMWARE")
_QEMU_MACHINE = os.environ.get("MPY_DEBUG_QEMU_MACHINE")

if _QEMU_BINARY is None:
    _qemu_skip_reason = "qemu-system-arm not installed"
elif not _QEMU_FIRMWARE:
    _qemu_skip_reason = (
        "no settrace-capable qemu-port firmware configured "
        "(set MPY_DEBUG_QEMU_FIRMWARE/MPY_DEBUG_QEMU_MACHINE)"
    )
else:
    _qemu_skip_reason = None


@pytest.mark.skipif(_qemu_skip_reason is not None, reason=_qemu_skip_reason or "")
def test_s5_5_mpremote_debug_qemu_pty_leg(free_tcp_port):
    """`mpremote resume debug` against a real QEMU process over `-serial pty`.

    `-serial pty` and QEMU's own "char device redirected to <path>" stderr
    line are generic QEMU behaviour, not board-specific; the board/kernel
    are not, so they come from `MPY_DEBUG_QEMU_MACHINE`/
    `MPY_DEBUG_QEMU_FIRMWARE` rather than being guessed here. Always skips
    on this machine - see the module docstring for why.
    """
    qemu = subprocess.Popen(
        [
            _QEMU_BINARY,
            "-machine",
            _QEMU_MACHINE,
            "-nographic",
            "-monitor",
            "none",
            "-serial",
            "pty",
            "-kernel",
            _QEMU_FIRMWARE,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        slave_path = None
        stderr_lines = []
        while time.monotonic() < deadline:
            line = qemu.stderr.readline()
            if not line:
                break
            stderr_lines.append(line)
            m = re.search(r"char device redirected to (\S+)", line)
            if m:
                slave_path = m.group(1)
                break
        if slave_path is None:
            qemu.kill()
            qemu.wait(timeout=5)
            pytest.fail(f"qemu never reported a pty redirect; stderr: {''.join(stderr_lines)}")

        env = os.environ.copy()
        proc = _spawn_debug(
            _RESUME + ["debug", "--port", str(free_tcp_port), slave_path, "target:main"], env=env
        )
        lines, matched = _read_until(proc, "debug server listening on", timeout=15)
        if matched is None:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"never saw the endpoint report; output: {''.join(lines)}")
        returncode = proc.wait(timeout=10)
        assert returncode == 0, f"mpremote exited {returncode} instead of reporting cleanly"
    finally:
        qemu.terminate()
        try:
            qemu.wait(timeout=5)
        except subprocess.TimeoutExpired:
            qemu.kill()
            qemu.wait(timeout=5)
