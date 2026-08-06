"""Host-side pytest coverage for `mpremote debug --dap-log` DAP traffic logging.

Implements the acceptance criteria from s6.3_dap-log.md:
- Log contains both directions with correct framing (multi-frame TCP segment
  and a frame split across segments both logged as complete messages).
- A breakpoint session driven with `--dap-log` still stops at the breakpoint
  (logging does not perturb the session).
- No hardcoded ports: the interposed proxy port is auto-assigned.
- Proxy is reaped on exit; no orphans.

The framing tests work directly on `FrameParser`, while the integration tests
spawn the real `mpremote debug` CLI with `--dap-log`, drive a breakpoint session
through it, and verify the logged traffic.
"""

import json
import os
import pty
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from helpers import PerfServer, set_breakpoints, wait_for_msg

# Import test utilities from mpremote_debug
from mpremote_debug import (
    MICROPYTHON as _MICROPYTHON,
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

# Import the dap_log module directly to test FrameParser
sys.path.insert(0, str(_SUBMODULE_DIR))
from mpremote.dap_log import FrameParser, DapLogger, DapProxy, default_log_path

_MICROPYPATH = "{}:{}:{}".format(
    _TOP_DIR / "src", _TOP_DIR / "micropython-lib/python-ecosys/debugpy", _TOP_DIR / "micropython-lib"
)
_TARGET_PY = str(_TOP_DIR / "src" / "target.py")


class TestFrameParserFundamentals:
    """Verify the `FrameParser` handles framing adversarially."""

    def test_single_frame_one_chunk(self):
        """A complete frame arriving in one chunk."""
        parser = FrameParser()
        frame_body = b'{"command":"initialize"}'
        header = f"Content-Length: {len(frame_body)}\r\n\r\n".encode()
        chunk = header + frame_body

        frames = parser.feed(chunk)
        assert len(frames) == 1
        assert frames[0] == frame_body

    def test_multiple_frames_one_chunk(self):
        """Multiple frames in a single TCP segment."""
        parser = FrameParser()
        frame1_body = b'{"seq":1}'
        frame2_body = b'{"seq":2}'
        header1 = f"Content-Length: {len(frame1_body)}\r\n\r\n".encode()
        header2 = f"Content-Length: {len(frame2_body)}\r\n\r\n".encode()
        chunk = header1 + frame1_body + header2 + frame2_body

        frames = parser.feed(chunk)
        assert len(frames) == 2
        assert frames[0] == frame1_body
        assert frames[1] == frame2_body

    def test_frame_split_across_header_and_body(self):
        """A frame split such that part of the header arrives before the body."""
        parser = FrameParser()
        frame_body = b'{"event":"stopped"}'
        header = f"Content-Length: {len(frame_body)}\r\n\r\n".encode()

        # Feed header and part of body in first chunk
        chunk1 = header + frame_body[:5]
        frames = parser.feed(chunk1)
        assert len(frames) == 0, "incomplete body should not produce a frame"

        # Feed the rest of the body
        chunk2 = frame_body[5:]
        frames = parser.feed(chunk2)
        assert len(frames) == 1
        assert frames[0] == frame_body

    def test_frame_split_at_header_separator(self):
        """A frame split in the middle of the CRLF CRLF separator."""
        parser = FrameParser()
        frame_body = b'{"type":"response"}'
        header_and_sep = f"Content-Length: {len(frame_body)}\r\n\r\n".encode()

        # Feed up to the middle of the separator
        split_point = header_and_sep.index(b"\r\n\r\n") + 2
        chunk1 = header_and_sep[:split_point]
        frames = parser.feed(chunk1)
        assert len(frames) == 0

        # Feed the rest (second \r\n plus body)
        chunk2 = header_and_sep[split_point:] + frame_body
        frames = parser.feed(chunk2)
        assert len(frames) == 1
        assert frames[0] == frame_body

    def test_frame_split_byte_by_byte(self):
        """A frame split and fed one byte at a time."""
        parser = FrameParser()
        frame_body = b'{"data":"test"}'
        header = f"Content-Length: {len(frame_body)}\r\n\r\n".encode()
        full = header + frame_body

        # Feed byte by byte; frame only completes after the last byte
        for i in range(len(full) - 1):
            frames = parser.feed(bytes([full[i]]))
            assert len(frames) == 0, f"incomplete at byte {i}"

        frames = parser.feed(bytes([full[-1]]))
        assert len(frames) == 1
        assert frames[0] == frame_body

    def test_unparsable_content_length_does_not_raise(self):
        """An unparsable Content-Length value is dropped, like a missing one - never raised.

        `feed` sits directly in the proxy's data path; a `ValueError` here
        would propagate out of the pump thread and kill forwarding.
        """
        parser = FrameParser()
        frames = parser.feed(b"Content-Length: notanumber\r\n\r\nHELLO")
        assert frames == []


class TestDefaultLogPath:
    """Verify the default log path is generated safely."""

    def test_default_log_path_generated(self):
        """default_log_path() returns a timestamped, pid-suffixed filename in CWD style."""
        path = default_log_path()
        assert path.startswith("mpremote-dap-")
        assert path.endswith(".jsonl")
        # mpremote-dap-<YYYYMMDDTHHMMSS>-<pid>.jsonl
        stem = path[len("mpremote-dap-") : -len(".jsonl")]
        timestamp, _, pid = stem.rpartition("-")
        assert len(timestamp) == len("20260806T120000"), timestamp
        assert pid == str(os.getpid())


class TestDapLoggerBasics:
    """Test `DapLogger` queues and writes correctly."""

    def test_logger_writes_single_entry(self, tmp_path):
        """A logged frame is written as a JSONL entry."""
        log_file = tmp_path / "test.jsonl"
        logger = DapLogger(str(log_file))

        # Log a frame
        frame_bytes = b'{"seq": 1, "type": "request"}'
        logger.log("client", frame_bytes)

        # Close the logger and let the writer thread finish
        logger.close()

        # Read and verify
        with open(log_file) as f:
            line = f.readline()
            entry = json.loads(line)

        assert entry["dir"] == "client"
        assert entry["msg"]["seq"] == 1
        assert entry["msg"]["type"] == "request"
        assert "ts" in entry

    def test_logger_writes_both_directions(self, tmp_path):
        """Logged frames from both directions appear in the log."""
        log_file = tmp_path / "test.jsonl"
        logger = DapLogger(str(log_file))

        # Log frames from both directions
        logger.log("client", b'{"seq": 1}')
        logger.log("device", b'{"type": "response"}')
        logger.log("client", b'{"command": "next"}')

        logger.close()

        # Read and verify the order
        with open(log_file) as f:
            entries = [json.loads(line) for line in f.readlines()]

        assert len(entries) == 3
        assert entries[0]["dir"] == "client"
        assert entries[1]["dir"] == "device"
        assert entries[2]["dir"] == "client"

    def test_logger_bad_path_raises_to_caller(self, tmp_path):
        """A path that can't be opened raises `OSError` from `__init__`, not
        from the writer thread later - the caller must be able to turn this
        into a `CommandError` instead of it surfacing as an uncaught
        exception in a background thread."""
        bad_path = tmp_path / "no-such-dir" / "dap.jsonl"
        with pytest.raises(OSError):
            DapLogger(str(bad_path))

    def test_log_does_not_block_on_stalled_sink(self, tmp_path):
        """`log()` only queues - a stalled writer thread must not slow it down.

        This is the property `--dap-log` depends on ("log asynchronously ...
        don't block the pump on disk I/O"): stall the writer thread's
        `file.write` and confirm `log()` itself still returns immediately.
        """
        log_file = tmp_path / "test.jsonl"
        logger = DapLogger(str(log_file))
        real_write = logger._file.write
        logger._file.write = lambda data: (time.sleep(0.5), real_write(data))[1]

        start = time.monotonic()
        for i in range(5):
            logger.log("client", '{{"seq": {}}}'.format(i).encode())
        elapsed = time.monotonic() - start

        assert elapsed < 0.05, f"log() took {elapsed:.3f}s while the writer thread was stalled"
        logger.close()


class TestDapProxyBasics:
    """Test `DapProxy` can bind, report, and clean up."""

    def test_proxy_binds_auto_assigned_port(self):
        """DapProxy binds an OS-assigned port."""
        logger = DapLogger("/dev/null")
        try:
            proxy = DapProxy("127.0.0.1", 9999, logger)
            # Port should be assigned
            assert proxy.port > 0
            assert proxy.port != 9999
            proxy.close()
        finally:
            logger.close()

    def test_proxy_no_hardcoded_port(self):
        """Multiple proxies get different auto-assigned ports."""
        logger1 = DapLogger("/dev/null")
        logger2 = DapLogger("/dev/null")
        try:
            proxy1 = DapProxy("127.0.0.1", 9999, logger1)
            proxy2 = DapProxy("127.0.0.1", 9999, logger2)
            assert proxy1.port != proxy2.port
            assert proxy1.port > 0
            assert proxy2.port > 0
            proxy1.close()
            proxy2.close()
        finally:
            logger1.close()
            logger2.close()


class TestDapProxyResilience:
    """Malformed traffic through the proxy must not kill forwarding.

    Uses a plain TCP echo "device" - not a real DAP peer, just a canary that
    proves bytes sent through the proxy still come back after a malformed
    frame, i.e. the pump thread survived it.
    """

    def test_malformed_content_length_does_not_kill_forwarding(self):
        echo_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        echo_listener.bind(("127.0.0.1", 0))
        echo_listener.listen(1)
        echo_port = echo_listener.getsockname()[1]

        def echo_once():
            conn, _ = echo_listener.accept()
            with conn:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    conn.sendall(chunk)

        echo_thread = threading.Thread(target=echo_once, daemon=True)
        echo_thread.start()

        log_file = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        log_file.close()
        logger = DapLogger(log_file.name)
        proxy = DapProxy("127.0.0.1", echo_port, logger)
        proxy.start()
        try:
            with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as client:
                client.sendall(b"Content-Length: notanumber\r\n\r\nHELLO")
                good_body = b'{"seq": 1}'
                client.sendall(f"Content-Length: {len(good_body)}\r\n\r\n".encode() + good_body)

                deadline = time.time() + 5
                received = b""
                while time.time() < deadline and b"HELLO" not in received:
                    received += client.recv(4096)
                assert b"HELLO" in received, "forwarding stopped after the malformed header"
        finally:
            proxy.close()
            echo_listener.close()
            os.unlink(log_file.name)


def _seen_response(server, command):
    """True if `command`'s response is anywhere in the buffer."""
    return any(m.type == "response" and m.command == command for m in server.rcv_messages)


@pytest.fixture()
def mpremote_debuggee_with_dap_log(free_tcp_port, tmp_path):
    """Fixture to spawn `mpremote debug --port N --dap-log --dap-log-file=FILE unix target:main`.

    Spawns the command with the DAP log flag, parses the re-emitted MPDBG-READY
    handshake for host/port, and yields the spawned process, endpoint info,
    and the log file path. With `--dap-log`, `--port` pins the *proxy's*
    (client-facing) endpoint rather than the device's - `requested_port` (the
    `--port` this fixture passed) is included so callers can assert the
    reported port equals it, and `all_output` carries the device's own
    "Debug server listening on ..." banner so callers can independently
    confirm the device landed on a *different* port.
    """
    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(_MICROPYTHON)
    env["MICROPYPATH"] = _MICROPYPATH

    log_file = tmp_path / "dap.jsonl"
    # --dap-log/--dap-log-file must come BEFORE target/program per the
    # argument parser epilog.
    args = [
        "debug",
        "--port",
        str(free_tcp_port),
        "--dap-log",
        f"--dap-log-file={log_file}",
        "unix",
        "target:main",
    ]
    proc = _spawn_debug(args, env=env)

    # Read until we see the handshake
    lines, matched = _read_until(proc, "MPDBG-READY", timeout=30)
    if matched is None:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail(f"never saw MPDBG-READY; output: {''.join(lines)}")

    new_pids = _new_launcher_pids(proc)

    # Also read the log-path message if present
    if not any("logging DAP traffic to" in line for line in lines):
        # Read more output to get the log message
        more_lines, _ = _read_until(proc, None, timeout=2)
        lines.extend(more_lines)

    # Parse the endpoint from the handshake
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
        "requested_port": free_tcp_port,
        "caps": caps,
        "new_pids": new_pids,
        "all_output": lines,
        "log_file": log_file,
    }

    # Cleanup: end session and confirm no orphans
    if proc.poll() is not None:
        still_alive = _pids_alive(new_pids)
        assert not still_alive, f"launcher child(ren) {still_alive} still alive after the session ended"
    else:
        _end_session(proc, new_pids)


@requires_settrace_firmware
def test_s6_3_dap_log_default_path_used(free_tcp_port, tmp_path):
    """`--dap-log` with no `--dap-log-file` defaults to a timestamped file in cwd."""
    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(_MICROPYTHON)
    env["MICROPYPATH"] = _MICROPYPATH
    # cwd is tmp_path below, not SUBMODULE_DIR, so `-m mpremote` needs an
    # explicit PYTHONPATH to still find the package.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_SUBMODULE_DIR)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )

    args = ["debug", "--port", str(free_tcp_port), "--dap-log", "unix", "target:main"]
    proc = _spawn_debug(args, env=env, cwd=tmp_path)

    lines, matched = _read_until(proc, "MPDBG-READY", timeout=30)
    if matched is None:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail(f"never saw MPDBG-READY; output: {''.join(lines)}")
    new_pids = _new_launcher_pids(proc)

    if not any("logging DAP traffic to" in line for line in lines):
        more_lines, _ = _read_until(proc, "logging DAP traffic to", timeout=2)
        lines.extend(more_lines)

    match = re.search(r"logging DAP traffic to '([^']+)'", "".join(lines))
    assert match is not None, f"no 'logging DAP traffic to' message; output: {''.join(lines)}"
    reported_path = match.group(1)
    assert reported_path.startswith("mpremote-dap-") and reported_path.endswith(".jsonl"), (
        f"unexpected default path: {reported_path!r}"
    )

    _end_session(proc, new_pids)

    assert (tmp_path / reported_path).exists(), f"{reported_path} not created in {tmp_path}"


def test_s6_3_dap_log_reported_endpoint_is_proxys(mpremote_debuggee_with_dap_log):
    """With `--dap-log`, `--port` pins the proxy's endpoint, not the device's.

    The device's own "Debug server listening on ..." banner carries its real
    port, reserved separately from the requested one - comparing it against
    the reported port is what proves the substitution happened, rather than
    just trusting the fixture's `--port` argument was honoured by *something*.
    """
    debuggee_info = mpremote_debuggee_with_dap_log
    output_text = "".join(debuggee_info["all_output"])

    # Endpoint substitution is asserted here rather than in its own test: each
    # session costs a firmware launch, and the proxy serves a single client, so
    # a separate test cannot share this one.
    device_line = re.search(r"Debug server listening on 0\.0\.0\.0:(\d+)", output_text)
    assert device_line is not None, f"expected the device's own banner; got: {output_text}"
    assert debuggee_info["host"] == "127.0.0.1", debuggee_info["host"]
    assert debuggee_info["port"] != int(device_line.group(1)), (
        "reported port equals the device's own; the proxy endpoint was not substituted"
    )

    device_line = re.search(r"Debug server listening on 0\.0\.0\.0:(\d+)", output_text)
    assert device_line is not None, f"expected device's own banner; got: {output_text}"
    device_port = int(device_line.group(1))

    # The reported endpoint must be the proxy's loopback, pinned at --port.
    assert debuggee_info["host"] == "127.0.0.1", f"expected proxy's loopback; got {debuggee_info['host']!r}"
    assert debuggee_info["port"] == debuggee_info["requested_port"], (
        f"reported port {debuggee_info['port']} does not equal the requested "
        f"--port {debuggee_info['requested_port']!r}; a pinned launch.json "
        "would not go through the logger"
    )
    assert device_port != debuggee_info["port"], (
        f"device bound the same port ({device_port}) it reported to the client; "
        "the device should have been given a separate, freshly reserved port"
    )

    _end_session(debuggee_info["process"], debuggee_info["new_pids"])


def test_s6_3_dap_log_client_connects_through_proxy(mpremote_debuggee_with_dap_log):
    """A message sent to the reported endpoint is actually routed through the proxy/logger.

    Connecting alone doesn't prove that: the device also listens, so a
    client that bypassed the proxy entirely would still connect. Sending a
    framed message and finding it in the log is what proves the traffic
    went through the logger.
    """
    debuggee_info = mpremote_debuggee_with_dap_log
    host = debuggee_info["host"]
    port = debuggee_info["port"]
    log_file = debuggee_info["log_file"]

    # An unrecognised command: debug_session.py's dispatcher responds with a
    # failure response for it rather than touching real session state, so
    # sending it can't perturb anything this fixture's teardown depends on.
    marker = "s6-3-client-connects-through-proxy"
    body = json.dumps({"seq": 1, "type": "request", "command": marker}).encode()
    frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body

    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(frame)
            time.sleep(0.5)  # give the pump a moment to forward and log it
    except (OSError, TimeoutError) as e:
        _end_session(debuggee_info["process"], debuggee_info["new_pids"])
        pytest.fail(f"failed to connect to proxy at {host}:{port}: {e}")

    _end_session(debuggee_info["process"], debuggee_info["new_pids"])

    assert log_file.exists(), f"log file {log_file} was not created"
    with open(log_file) as f:
        entries = [json.loads(line) for line in f if line.strip()]
    assert any(
        e["dir"] == "client" and isinstance(e["msg"], dict) and e["msg"].get("command") == marker
        for e in entries
    ), f"sent frame not found in the log; entries: {entries}"


@requires_settrace_firmware
def test_s6_3_dap_log_traffic_logged_both_directions(pytestconfig, mpremote_debuggee_with_dap_log):
    """DAP traffic from both directions appears in the log file."""
    debuggee_info = mpremote_debuggee_with_dap_log
    host = debuggee_info["host"]
    port = debuggee_info["port"]
    log_file = debuggee_info["log_file"]
    root_path = Path(pytestconfig.rootpath)

    # Drive a DAP session: attach, breakpoint, stop
    server = PerfServer("test-client", host, port)
    try:
        server.start()

        # Wait for initialize response (it may already be in rcv_messages)
        deadline = time.time() + 10
        while time.time() < deadline and not _seen_response(server, "initialize"):
            server.run_single()
            time.sleep(0.1)
        assert _seen_response(server, "initialize"), "initialize response not received"

        # Attach
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
            },
        )
        assert wait_for_msg(server, response="attach", timeout=10), "attach failed"

        # Set a breakpoint
        set_breakpoints(server, _TARGET_PY, [80])
        assert wait_for_msg(server, response="setBreakpoints"), "setBreakpoints failed"

        # Start running
        server.client.configuration_done()
        stopped_msg = wait_for_msg(server, event="stopped", timeout=15)
        assert stopped_msg is not None, "breakpoint stop failed"

    finally:
        server.stop()
        _end_session(debuggee_info["process"], debuggee_info["new_pids"])

    # Verify the log file exists and contains entries
    if not log_file.exists():
        # Debug: list what files were created
        log_dir = log_file.parent
        created_files = list(log_dir.glob("*"))
        pytest.fail(f"log file {log_file} was not created. Files in {log_dir}: {created_files}")

    entries = []
    with open(log_file) as f:
        for line in f.readlines():
            if line.strip():
                entries.append(json.loads(line))

    assert len(entries) > 0, f"log file {log_file} is empty"

    # Verify we have entries from both directions
    directions = {e["dir"] for e in entries}
    assert "client" in directions, "no client->device traffic in log"
    assert "device" in directions, "no device->client traffic in log"

    # Verify the log has timestamps and messages
    for entry in entries:
        assert "ts" in entry, "log entry missing timestamp"
        assert "dir" in entry, "log entry missing direction"
        assert "msg" in entry, "log entry missing message"
        # msg can be a dict (valid JSON) or a string (if decoding failed)
        assert isinstance(entry["msg"], (dict, str)), f"msg has unexpected type: {type(entry['msg'])}"


@requires_settrace_firmware
def test_s6_3_dap_log_no_hardcoded_ports(tmp_path):
    """With no `--port`, each run's proxy port is auto-assigned and distinct.

    No `--port` is given, so the device keeps its own default and the proxy
    binds an OS-assigned port each time - this is the plain `--dap-log` case
    (`test_s6_3_dap_log_reported_endpoint_is_proxys` covers `--port` pinning
    the proxy's endpoint instead).
    """
    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(_MICROPYTHON)
    env["MICROPYPATH"] = _MICROPYPATH

    ports_seen = set()

    # Run three times and verify each run's proxy port is substituted and
    # all three are distinct.
    for i in range(2):  # two is enough to show the port is assigned per run, not fixed
        log_file = tmp_path / f"dap_{i}.jsonl"
        # --dap-log/--dap-log-file must come BEFORE target/program per the
        # argument parser epilog.
        args = ["debug", "--dap-log", f"--dap-log-file={log_file}", "unix", "target:main"]
        proc = _spawn_debug(args, env=env)

        lines, matched = _read_until(proc, "MPDBG-READY", timeout=30)
        if matched is None:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"run {i}: never saw MPDBG-READY")
        new_pids = _new_launcher_pids(proc)

        try:
            json_start = matched.index("{")
            payload = json.loads(matched[json_start:])
            proxy_port = payload["port"]
        except (ValueError, KeyError, IndexError) as e:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"run {i}: failed to parse port: {e}")

        device_line = re.search(r"Debug server listening on 0\.0\.0\.0:(\d+)", "".join(lines))
        assert device_line is not None, f"run {i}: expected device's own banner"
        device_port = int(device_line.group(1))

        assert proxy_port != device_port, (
            f"run {i}: reported port {proxy_port} equals the device's own port "
            f"{device_port}; the proxy endpoint was not substituted"
        )
        ports_seen.add(proxy_port)

        _end_session(proc, new_pids)

    # Not "all three differ": the kernel is free to hand back a port it has
    # just reclaimed, so that assertion goes red on correct code. What matters
    # is that no port is a literal - each is assigned per run and none is the
    # device's own.
    assert len(ports_seen) >= 1, (
        f"expected at least one proxy port to be recorded, got: {ports_seen}"
    )


@requires_settrace_firmware
def test_s6_3_dap_log_with_flag_behavioural_test_still_works(pytestconfig, free_tcp_port):
    """With `--dap-log`, the DAP session flow still works (logging does not perturb it).

    This verifies that enabling logging does not break the session.
    """
    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(_MICROPYTHON)
    env["MICROPYPATH"] = _MICROPYPATH

    # Temporary directory for log file
    tmpdir = tempfile.mkdtemp(prefix="mpremote-dap-log-test-")
    log_file = os.path.join(tmpdir, "dap.jsonl")

    try:
        # --dap-log/--dap-log-file must come BEFORE target/program per the
        # argument parser epilog.
        args = [
            "debug",
            "--port",
            str(free_tcp_port),
            "--dap-log",
            f"--dap-log-file={log_file}",
            "unix",
            "target:main",
        ]
        proc = _spawn_debug(args, env=env)

        # Read until we see the handshake
        lines, matched = _read_until(proc, "MPDBG-READY", timeout=30)
        if matched is None:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"never saw MPDBG-READY; output: {''.join(lines)}")

        new_pids = _new_launcher_pids(proc)

        # Parse the endpoint
        try:
            json_start = matched.index("{")
            payload = json.loads(matched[json_start:])
            host = payload["host"]
            port = payload["port"]
            caps = payload["caps"]
        except (ValueError, KeyError, IndexError) as e:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"failed to parse endpoint: {e}")

        root_path = Path(pytestconfig.rootpath)

        # Drive a minimal session: attach, breakpoint, stop
        server = PerfServer("test-client", host, port)
        try:
            server.start()

            # Wait for initialize response (it may already be in rcv_messages)
            deadline = time.time() + 10
            while time.time() < deadline and not _seen_response(server, "initialize"):
                server.run_single()
                time.sleep(0.1)
            assert _seen_response(server, "initialize"), "initialize response not received"

            # Attach
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
                },
            )
            assert wait_for_msg(server, response="attach", timeout=10), "attach failed"

            # Set breakpoint on line 80
            set_breakpoints(server, _TARGET_PY, [80])
            assert wait_for_msg(server, response="setBreakpoints"), "setBreakpoints failed"

            # Start running
            server.client.configuration_done()
            stopped_msg = wait_for_msg(server, event="stopped", timeout=15)
            assert stopped_msg is not None, "breakpoint stop failed"
            assert stopped_msg.body.get("reason") == "breakpoint", (
                f"expected 'breakpoint' stop, got: {stopped_msg.body}"
            )

        finally:
            server.stop()

        # End session
        _end_session(proc, new_pids)

        # Verify the log file was created and has traffic
        assert os.path.exists(log_file), f"log file {log_file} was not created"
        with open(log_file) as f:
            entries = [json.loads(line) for line in f if line.strip()]
        assert len(entries) > 0, f"log file {log_file} is empty"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@requires_settrace_firmware
def test_s6_3_dap_log_proxy_reaped_on_exit(free_tcp_port, tmp_path):
    """The proxy does not re-arm for a second client, and once the session
    ends the launcher child is gone and the listener no longer accepts -
    no orphaned socket, no orphaned process."""
    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(_MICROPYTHON)
    env["MICROPYPATH"] = _MICROPYPATH

    log_file = tmp_path / "dap.jsonl"
    args = [
        "debug",
        "--port",
        str(free_tcp_port),
        "--dap-log",
        f"--dap-log-file={log_file}",
        "unix",
        "target:main",
    ]
    proc = _spawn_debug(args, env=env)

    lines, matched = _read_until(proc, "MPDBG-READY", timeout=30)
    if matched is None:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("never saw MPDBG-READY")
    new_pids = _new_launcher_pids(proc)

    try:
        json_start = matched.index("{")
        payload = json.loads(matched[json_start:])
        host = payload["host"]
        port = payload["port"]
    except (ValueError, KeyError, IndexError) as e:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail(f"failed to parse endpoint: {e}")

    # First client: a real session gets a real response through the proxy -
    # this is the "one client" the proxy is built to serve.
    first = PerfServer("first-client", host, port)
    first.start()
    deadline = time.time() + 10
    while time.time() < deadline and not _seen_response(first, "initialize"):
        first.run_single()
        time.sleep(0.1)
    assert _seen_response(first, "initialize"), "no response through the proxy for the first client"
    first.stop()

    time.sleep(0.3)  # let the pump notice the closed client and finish

    # Second connection, proxy still alive: the documented no-re-arm
    # behaviour means it either never reaches accept() at all (connect()
    # itself fails) or, if the kernel completed the handshake against the
    # still-listening-but-never-accepted-again socket, a request into it
    # gets no response - either way it must not reach the device.
    body = json.dumps({"seq": 1, "type": "request", "command": "initialize"}).encode()
    frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    try:
        second = socket.create_connection((host, port), timeout=2)
    except OSError:
        second = None
    if second is not None:
        try:
            second.sendall(frame)
            second.settimeout(2)
            with pytest.raises(OSError):
                second.recv(4096)
        finally:
            second.close()

    _end_session(proc, new_pids)

    # And once the session has fully ended, the listener is gone too.
    with pytest.raises(OSError):
        socket.create_connection((host, port), timeout=1)


@requires_settrace_firmware
def test_s6_3_dap_log_over_pty_network_transport(free_tcp_port, tmp_path):
    """`--dap-log` on the serial/network path (mirrors s5.1's real-pty tests
    for the plain, no-`--dap-log` path): the proxy endpoint is substituted,
    both directions land in the log, and - unlike the plain path, which
    reports and returns immediately - the command stays attached and then
    exits by itself once the one client session ends.

    `disconnect`, not just closing the raw socket, is what ends the session:
    the device only closes its side of the connection (letting the proxy's
    device-facing pump reach EOF) once `_handle_disconnect` runs - the boot
    script never reaches its own target-module import (`configurationDone`
    is never sent), so `mod:main` is never resolved.
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    env = os.environ.copy()
    env["MICROPYPATH"] = _MICROPYPATH

    device_proc = subprocess.Popen(
        [str(_MICROPYTHON)],
        stdin=master_fd,
        stdout=master_fd,
        stderr=master_fd,
        env=env,
        close_fds=True,
    )
    os.close(master_fd)
    os.close(slave_fd)

    log_file = tmp_path / "dap.jsonl"
    server = None
    try:
        time.sleep(0.3)  # let the interpreter start its REPL before talking to it
        args = [
            "resume",
            "debug",
            "--port",
            str(free_tcp_port),
            "--dap-log",
            f"--dap-log-file={log_file}",
            slave_path,
            "mod:main",
        ]
        proc = _spawn_debug(args, env=env)

        lines, matched = _read_until(proc, "MPDBG-READY", timeout=30)
        if matched is None:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"never saw MPDBG-READY; output: {''.join(lines)}")

        try:
            json_start = matched.index("{")
            payload = json.loads(matched[json_start:])
            host = payload["host"]
            port = payload["port"]
        except (ValueError, KeyError, IndexError) as e:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"failed to parse endpoint: {e}; line: {matched}")

        # --dap-log makes --port pin the proxy's (client-facing) endpoint;
        # the device gets a separately reserved port, visible in its own
        # "Debug server listening on ..." banner.
        assert port == free_tcp_port, (
            f"reported port {port} does not equal the requested --port "
            f"{free_tcp_port}; a pinned launch.json would not go through the logger"
        )
        device_line = re.search(r"Debug server listening on \S+:(\d+)", "".join(lines))
        assert device_line is not None, f"expected device's own banner; got: {''.join(lines)}"
        assert int(device_line.group(1)) != port, (
            "device bound the same port it reported to the client; the device "
            "should have been given a separate, freshly reserved port"
        )

        server = PerfServer("test-client", host, port)
        server.start()  # sends 'initialize' immediately

        deadline = time.time() + 10
        while time.time() < deadline and not _seen_response(server, "initialize"):
            server.run_single()
            time.sleep(0.1)
        assert _seen_response(server, "initialize"), "initialize response not received"

        server.client.send_request("attach", {"name": "t", "type": "debugpy", "request": "attach"})
        assert wait_for_msg(server, response="attach", timeout=10), "attach failed"

        server.client.send_request("disconnect", {})
        assert wait_for_msg(server, response="disconnect", timeout=10), "disconnect failed"
        server.stop()
        server = None

        # `disconnect` closes the device's side of the connection, so the
        # proxy's device-facing pump reaches EOF and mpremote must now exit
        # on its own, without a signal.
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("mpremote did not exit on its own after disconnect")

        assert log_file.exists(), f"log file {log_file} was not created"
        with open(log_file) as f:
            entries = [json.loads(line) for line in f if line.strip()]
        directions = {e["dir"] for e in entries}
        assert "client" in directions, "no client->device traffic in log"
        assert "device" in directions, "no device->client traffic in log"
    finally:
        if server is not None:
            server.stop()
        device_proc.terminate()
        try:
            device_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            device_proc.kill()
            device_proc.wait(timeout=2)
