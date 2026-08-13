"""Host-side pytest coverage for `mpremote debug`'s serial DAP bridge (STORY-6.1).

Implements the non-hardware-gated acceptance criteria from
s6.1_serial-transport.md's 2026-08-07 revalidation:

- Criterion 5: host-side unit tests for the bridge byte-pump, no hardware -
  `TestSerialDapBridgePump` drives `SerialDapBridge`/`_SerialDuplex` against a
  loopback pty, exactly as the ticket calls for.
- Criterion 3 (kill the bridge mid-session) and criterion 4's upstream half
  (board reset -> a clear error, no ampremote reconnect present) are exercised
  end-to-end through `do_debug` itself in `TestDoDebugSerialDapBridge`, using a
  real unix-firmware process for the control-plane handshake and a second pty
  standing in for the board's dedicated DAP CDC interface.
- `TestDoDebugSerialDapBridgeRealSession` goes further: the data-plane pty
  carries an actual `debugpy` session (device-side `listen_stream`, not a
  hand-crafted frame), reaching a real breakpoint through the unmodified
  `SerialDapBridge`, including killing the bridge while stopped at a
  breakpoint and confirming the device recovers instead of wedging.

The real second CDC on real hardware (criterion 1) and the negative probe on
a single-UART board (criterion 2) are NOT covered here; both need a bench.

`SerialDapBridge`'s target is opened by path (`dap_device`), matching the
real contract (a `/dev/serial/by-id/...` node); a pty's slave path is used as
that node. `TestDoDebugSerialDapBridge` has the test itself hold the pty's
master fd to play "the device side of the wire" - the same technique
`SerialTransport`'s own pty tests use for the control plane, applied here to
the data plane instead. `TestDoDebugSerialDapBridgeRealSession` instead gives
the master fd to the real device process (inherited via `pass_fds`), since a
genuine `debugpy` session has to run somewhere real.
"""

import errno
import json
import os
import pty
import select
import socket
import sys
import threading
import time
import tty
from pathlib import Path

import pytest

_TOP_DIR = Path(__file__).resolve().parents[1]
_SUBMODULE_DIR = _TOP_DIR / "micropython" / "tools" / "mpremote"

if str(_SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SUBMODULE_DIR))

from mpremote import commands  # noqa: E402
from mpremote import dap_log  # noqa: E402
from mpremote import serial_dap  # noqa: E402
from mpremote.main import State  # noqa: E402
from mpremote.mpdebug_config import Target  # noqa: E402
from mpremote.transport_serial import SerialTransport  # noqa: E402

from helpers import PerfServer, debug_args, set_breakpoints, take_msg, wait_for_msg  # noqa: E402
from pty_device import PtyDevice  # noqa: E402

_MICROPYTHON = Path(
    os.environ.get(
        "MPY_DEBUG_FIRMWARE",
        _TOP_DIR / "micropython/ports/unix/build-standard/micropython",
    )
)

# For the real-debugpy-session tests below: `debugpy` and the `target` fixture
# module both need to be importable inside the control-plane subprocess.
_MICROPYPATH = "{}:{}:{}".format(
    _TOP_DIR / "src", _TOP_DIR / "micropython-lib/python-ecosys/debugpy", _TOP_DIR / "micropython-lib"
)
_TARGET_PY = str(_TOP_DIR / "src" / "target.py")
_BREAKPOINT_LINE = 80  # src/target.py, main(): the `for` header, before the local is bound

requires_unix_firmware = pytest.mark.skipif(
    not _MICROPYTHON.exists(),
    reason=f"unix firmware not built at {_MICROPYTHON} (see `make firmware-unix`)",
)


def _seen_response(server, command):
    return any(m.type == "response" and m.command == command for m in server.rcv_messages)


def _stream_session_boot_script(master_fd, module="target", method="main"):
    """Boot script for a REAL `debugpy` session over `master_fd` (a pty
    inherited via `pass_fds`), standing in for a board's dedicated DAP CDC
    interface - no TCP anywhere in the DAP path, matching
    the shipped boot script's stream mode but folded into the raw-REPL exec
    `do_debug` itself performs, so the whole bridge (`SerialDapBridge`,
    `PumpingProxy`) is exercised unmodified end to end rather than mocked.
    """
    return (
        "import json\n"
        "import debugpy\n"
        f"f = open({master_fd}, 'r+b')\n"
        "debugpy.listen_stream(f)\n"
        "caps = debugpy.get_capabilities()\n"
        "caps['serial_dap'] = True\n"
        "print('MPDBG-READY ' + json.dumps({'host': 'serial', 'port': 1, 'caps': caps}))\n"
        "if debugpy.wait_for_client():\n"
        "    debugpy.debug_this_thread()\n"
        f"    target = __import__({module!r}, None, None, ('*',))\n"
        f"    getattr(target, {method!r})()\n"
        "    print('Target completed successfully!')\n"
        "else:\n"
        "    print('[DAP] no client finished configuring')\n"
        "    debugpy.disconnect()\n"
    )


def _read_master(fd, n, deadline):
    """`os.read(fd, n)` bounded by `deadline`, returning b"" when it passes.

    Every caller loops on a condition until its deadline, so a bounded read
    turns "the bytes never came" into that loop ending and the assertion below
    it reporting what did arrive. A bare `os.read` on a blocking master would
    instead park the test there forever, which is the wedge these tests exist
    to catch reported as a hung run.

    Linux gives EIO reading a pty master while its slave has zero open
    references - the state right after `_open_loopback_pty` closes its own
    handle to hand the path to a bridge, until that bridge's `_connect_target`
    reopens it. A real serial device has no such window; this is a loopback
    pty artifact, not something `SerialDapBridge` itself needs to handle.
    """
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return b""
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return b""
        try:
            return os.read(fd, n)
        except OSError as er:
            if er.errno != errno.EIO or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def _open_loopback_pty():
    """A pty pair with the slave's path and the master fd, both usable directly.

    The slave path is what `SerialDapBridge` opens (matching a real
    `dap_device` connect string); the master fd is kept open here so the test
    can read/write it directly, playing "the device" the bridge is talking to.
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    os.close(slave_fd)  # only its path is needed; the bridge opens it fresh
    return master_fd, slave_path


class _DaemonFuture:
    """Runs `fn` in a daemon thread; `.result(timeout)` behaves like a `Future`'s.

    A `ThreadPoolExecutor` worker is not a daemon thread - if `fn` never
    returns (the wedge these tests exist to catch, or a regression that
    reintroduces it), the interpreter's `_python_exit` atexit hook joins it
    anyway and the whole test run hangs instead of reporting the failure. A
    daemon thread carries no such join-at-exit obligation.
    """

    def __init__(self, fn, *args):
        self._result = None
        self._exc = None
        self._done = threading.Event()

        def _run():
            try:
                self._result = fn(*args)
            except BaseException as e:  # noqa: BLE001 - propagated verbatim by result()
                self._exc = e
            finally:
                self._done.set()

        threading.Thread(target=_run, daemon=True).start()

    def result(self, timeout=None):
        if not self._done.wait(timeout):
            raise TimeoutError("do_debug did not finish within the timeout")
        if self._exc is not None:
            raise self._exc
        return self._result


class TestSerialDapBridgePump:
    """`SerialDapBridge` byte-pumping over a loopback pty - no hardware."""

    def test_binds_auto_assigned_port(self):
        master_fd, slave_path = _open_loopback_pty()
        try:
            bridge = serial_dap.SerialDapBridge(slave_path, dap_log.NullLogger())
            assert bridge.port > 0
            bridge.close()
        finally:
            os.close(master_fd)

    def test_two_bridges_get_independent_ports(self):
        master_fd, slave_path = _open_loopback_pty()
        master_fd2, slave_path2 = _open_loopback_pty()
        try:
            b1 = serial_dap.SerialDapBridge(slave_path, dap_log.NullLogger())
            b2 = serial_dap.SerialDapBridge(slave_path2, dap_log.NullLogger())
            assert b1.port != b2.port
            b1.close()
            b2.close()
        finally:
            os.close(master_fd)
            os.close(master_fd2)

    def test_pumps_both_directions(self):
        """A client<->bridge<->pty round trip carries bytes intact both ways."""
        master_fd, slave_path = _open_loopback_pty()
        bridge = serial_dap.SerialDapBridge(slave_path, dap_log.NullLogger())
        bridge.start()
        try:
            client = socket.create_connection(("127.0.0.1", bridge.port), timeout=5)
            try:
                body = json.dumps({"seq": 1, "type": "request", "command": "initialize"}).encode()
                frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
                client.sendall(frame)

                deadline = time.monotonic() + 5
                received = b""
                while time.monotonic() < deadline and len(received) < len(frame):
                    received += _read_master(master_fd, 4096, deadline)
                assert received == frame, f"device side did not see the client's frame: {received!r}"

                reply_body = json.dumps({"seq": 1, "success": True}).encode()
                reply = f"Content-Length: {len(reply_body)}\r\n\r\n".encode() + reply_body
                os.write(master_fd, reply)

                client.settimeout(5)
                client_received = b""
                while len(client_received) < len(reply):
                    client_received += client.recv(4096)
                assert client_received == reply, f"client did not see the device's reply: {client_received!r}"
            finally:
                client.close()

            assert bridge.wait(5), "bridge never noticed the client disconnecting"
            assert bridge.target_error is None, "a clean client-initiated close must not look like a fault"
        finally:
            bridge.close()
            os.close(master_fd)

    def test_malformed_content_length_does_not_kill_forwarding(self):
        """Garbage framing must not wedge the pump - mirrors DapProxy's own test."""
        master_fd, slave_path = _open_loopback_pty()
        bridge = serial_dap.SerialDapBridge(slave_path, dap_log.NullLogger())
        bridge.start()
        try:
            client = socket.create_connection(("127.0.0.1", bridge.port), timeout=5)
            try:
                client.sendall(b"Content-Length: notanumber\r\n\r\nHELLO")
                good_body = b'{"seq": 1}'
                client.sendall(f"Content-Length: {len(good_body)}\r\n\r\n".encode() + good_body)

                # Both markers are waited for, not just the first: the frame
                # after the malformed one is the whole claim, and it is the one
                # that arrives second, so a wait ending at "HELLO" asserts it
                # against whatever happened to have been read by then.
                deadline = time.monotonic() + 5
                received = b""
                while time.monotonic() < deadline and not (
                    b"HELLO" in received and good_body in received
                ):
                    received += _read_master(master_fd, 4096, deadline)
                assert b"HELLO" in received, f"malformed frame wedged forwarding: {received!r}"
                assert good_body in received, (
                    f"the frame after the malformed one never arrived: {received!r}"
                )
            finally:
                client.close()
        finally:
            bridge.close()
            os.close(master_fd)

    def test_frame_split_across_several_writes_still_arrives_whole(self):
        """A single DAP frame trickled across multiple `sendall()` calls
        arrives at the device intact - the pump must not assume one
        `recv()`/`send()` boundary lines up with one frame.
        """
        master_fd, slave_path = _open_loopback_pty()
        bridge = serial_dap.SerialDapBridge(slave_path, dap_log.NullLogger())
        bridge.start()
        try:
            client = socket.create_connection(("127.0.0.1", bridge.port), timeout=5)
            try:
                body = json.dumps({"seq": 1, "command": "initialize"}).encode()
                frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
                # Dribble it out a few bytes at a time, well below the header
                # length, so the pump's reads never see a complete frame in
                # one chunk.
                for i in range(0, len(frame), 3):
                    client.sendall(frame[i : i + 3])
                    time.sleep(0.01)

                deadline = time.monotonic() + 5
                received = b""
                while time.monotonic() < deadline and len(received) < len(frame):
                    received += _read_master(master_fd, 4096, deadline)
                assert received == frame, f"split frame arrived corrupted: {received!r}"
            finally:
                client.close()
        finally:
            bridge.close()
            os.close(master_fd)

    def test_several_frames_in_one_read_all_arrive(self):
        """Multiple complete frames written in a single `sendall()` (so the
        device's one `recv()` sees them concatenated) all get forwarded,
        none dropped or merged.
        """
        master_fd, slave_path = _open_loopback_pty()
        bridge = serial_dap.SerialDapBridge(slave_path, dap_log.NullLogger())
        bridge.start()
        try:
            client = socket.create_connection(("127.0.0.1", bridge.port), timeout=5)
            try:
                frames = []
                for seq in range(3):
                    body = json.dumps({"seq": seq, "command": "next"}).encode()
                    frames.append(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                batch = b"".join(frames)
                client.sendall(batch)

                deadline = time.monotonic() + 5
                received = b""
                while time.monotonic() < deadline and len(received) < len(batch):
                    received += _read_master(master_fd, 4096, deadline)
                assert received == batch, f"concatenated frames arrived wrong: {received!r}"
            finally:
                client.close()
        finally:
            bridge.close()
            os.close(master_fd)

    def test_target_loss_is_recorded_as_an_error_not_a_clean_close(self):
        """Closing the "device" end mid-session sets `target_error`, not a silent EOF."""
        master_fd, slave_path = _open_loopback_pty()
        bridge = serial_dap.SerialDapBridge(slave_path, dap_log.NullLogger())
        bridge.start()
        client = socket.create_connection(("127.0.0.1", bridge.port), timeout=5)
        try:
            # A pump direction only starts once bytes have actually flowed
            # (see PumpingProxy._accept), so send something first to be sure
            # the "device" pump thread is inside a read before it is closed.
            client.sendall(b"x")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if _read_master(master_fd, 1, deadline) == b"x":
                    break
            os.close(master_fd)

            assert bridge.wait(5), "bridge never noticed the device disappearing"
            assert bridge.target_error is not None, "losing the device must be recorded as an error"
        finally:
            client.close()
            bridge.close()

    def test_survives_an_idle_gap_longer_than_the_poll_timeout(self):
        """Sitting idle at a breakpoint (the normal case) must not look like EOF.

        `_SerialDuplex.recv()`'s read times out every `_POLL_TIMEOUT`
        (0.2s) with no data - that must re-loop, not report a closed
        connection, or every session would die the moment the user paused
        for a fraction of a second.
        """
        master_fd, slave_path = _open_loopback_pty()
        bridge = serial_dap.SerialDapBridge(slave_path, dap_log.NullLogger())
        bridge.start()
        try:
            client = socket.create_connection(("127.0.0.1", bridge.port), timeout=5)
            try:
                client.sendall(b"before-idle")
                deadline = time.monotonic() + 5
                received = b""
                while time.monotonic() < deadline and b"before-idle" not in received:
                    received += _read_master(master_fd, 4096, deadline)
                assert b"before-idle" in received

                time.sleep(3 * serial_dap._SerialDuplex._POLL_TIMEOUT)
                assert not bridge.wait(0), "bridge ended during an ordinary idle gap"

                client.sendall(b"after-idle")
                deadline = time.monotonic() + 5
                received = b""
                while time.monotonic() < deadline and b"after-idle" not in received:
                    received += _read_master(master_fd, 4096, deadline)
                assert b"after-idle" in received, f"nothing arrived after the idle gap: {received!r}"
            finally:
                client.close()
        finally:
            bridge.close()
            os.close(master_fd)

    def test_client_disconnect_does_not_hang_the_bridge(self):
        """A client hanging up must free the device-facing pump too.

        `_SerialDuplex.shutdown()` is what lets the still-blocked
        device-facing pump notice the session is over; if it didn't set the
        closed flag, `bridge.wait()` would never return.
        """
        master_fd, slave_path = _open_loopback_pty()
        bridge = serial_dap.SerialDapBridge(slave_path, dap_log.NullLogger())
        bridge.start()
        try:
            client = socket.create_connection(("127.0.0.1", bridge.port), timeout=5)
            client.close()
            assert bridge.wait(5), "bridge never noticed the client disconnecting"
        finally:
            bridge.close()
            os.close(master_fd)


class TestSerialDapCapabilityNeverInferredFromName:
    """`_maybe_start_serial_dap` decides purely from `dap_device` config and the
    probed `caps["serial_dap"]` - never from the target's name or kind, even
    when the name itself suggests multi-CDC hardware (STORY-3.3's rule that a
    claimed capability is never trusted over the runtime probe, applied here
    to the target's own config too).
    """

    def test_no_dap_device_never_bridges_even_if_caps_claim_it(self):
        """A capability claim alone must never start a bridge nobody asked for."""
        resolved = Target(name="rpi_pico_w", kind="serial", device="whatever")
        handshake = {"host": "serial", "port": 1, "caps": {"serial_dap": True}}
        assert commands._maybe_start_serial_dap(resolved, handshake, False, 0) is None

    def test_suggestive_name_with_unconfirmed_cap_is_still_a_hard_error(self):
        """A board-like target name changes nothing: the probe result is what
        is checked, not whether the name sounds like it should have a second
        CDC interface.
        """
        resolved = Target(name="rpi_pico_w", kind="serial", device="whatever", dap_device="/dev/x")
        handshake = {"host": "serial", "port": 1, "caps": {"serial_dap": False}}
        with pytest.raises(commands.CommandError, match="serial_dap"):
            commands._maybe_start_serial_dap(resolved, handshake, False, 0)

    def test_unsuggestive_name_with_confirmed_cap_still_bridges(self):
        """The reverse: a name giving no hint of multi-CDC hardware still
        bridges once the probe confirms it - the decision reads `caps`, not
        the name.
        """
        master_fd, slave_path = _open_loopback_pty()
        try:
            resolved = Target(name="bench", kind="serial", device="whatever", dap_device=slave_path)
            handshake = {"host": "serial", "port": 1, "caps": {"serial_dap": True}}
            result = commands._maybe_start_serial_dap(resolved, handshake, False, 0)
            assert result is not None
            proxy, reported = result
            try:
                assert reported["host"] == proxy.host and reported["port"] == proxy.port
            finally:
                proxy.close()
        finally:
            os.close(master_fd)


@requires_unix_firmware
class TestDoDebugSerialDapBridge:
    """`do_debug`'s `dap_device`/`serial_dap` wiring, driven end to end.

    The control-plane pty runs the real unix firmware executing a stand-in
    boot script (no `debugpy`/`sys.settrace` needed: it only has to print one
    `MPDBG-READY` line claiming `serial_dap`) - the real probe this claims
    lives on the device side, out of scope here (see the module docstring).
    The data-plane pty stands in for the board's dedicated DAP interface, as
    in `TestSerialDapBridgePump` above.
    """

    def _run_do_debug(self, monkeypatch, boot_script_src):
        """Wire a `state`/`args`/`resolved` triple that reaches the serial_dap
        branch, with `_debug_boot_script` and `_report_debug_result` replaced
        so the test can supply the control-plane script and observe the
        bridge's endpoint as soon as it's reported (both real seams `do_debug`
        already calls through, not new test-only hooks).

        Returns `(future, reported_holder, control_device, device_master_fd,
        transport)`; the caller drives the client against `reported_holder`
        once populated, then resolves `future`.
        """
        control_device = PtyDevice(_MICROPYTHON).start()
        device_master_fd, device_slave_path = _open_loopback_pty()

        transport = SerialTransport(control_device.path, baudrate=115200)
        state = State()
        state.transport = transport
        commands.do_resume(state)  # the unix build exits on a raw-REPL soft reset

        resolved = Target(
            name="bench",
            kind="serial",
            device=control_device.path,
            dap_device=device_slave_path,
        )
        monkeypatch.setattr(commands, "resolve_target", lambda name: resolved)

        def _boot_script(module, method, port, dap_stream=None, mount_point=None, loop=False):
            # A dap_device target must ask the device for its own DAP channel;
            # a device told to bind a port instead would report a TCP endpoint
            # the bridge below has no way to use.
            assert dap_stream == "board", dap_stream
            assert mount_point is None, "this session mounts nothing"
            return boot_script_src

        monkeypatch.setattr(commands, "_debug_boot_script", _boot_script)

        reported_holder = {}
        orig_report = commands._report_debug_result

        def _capture_report(handshake, path_mappings=None):
            reported_holder["value"] = handshake
            return orig_report(handshake, path_mappings)

        monkeypatch.setattr(commands, "_report_debug_result", _capture_report)

        args = debug_args(target="bench", program="mod:main", timeout=15)

        future = _DaemonFuture(commands.do_debug, state, args)
        return future, reported_holder, control_device, device_master_fd, transport

    def _wait_for_report(self, reported_holder, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "value" in reported_holder:
                return reported_holder["value"]
            time.sleep(0.05)
        pytest.fail("do_debug never reported the bridge's endpoint")

    def _cleanup(self, control_device, device_master_fd, transport=None):
        # `transport` closes before the process it talks to is torn down:
        # once the device dies, the control-plane pty's master side goes away
        # and toggling RTS on the (now orphaned) slave through `transport`
        # raises EIO.
        if transport is not None:
            transport.close()
        control_device.close()
        try:
            os.close(device_master_fd)
        except OSError:
            pass

    def test_bridges_dap_traffic_and_reports_localhost_endpoint(self, monkeypatch):
        """A `dap_device` target's endpoint is the bridge's, not the device's own."""
        boot_script = (
            "import json\n"
            'print("MPDBG-READY " + json.dumps('
            '{"host": "serial", "port": 1, "caps": {"serial_dap": True}}))\n'
        )
        future, reported_holder, control_device, device_master_fd, transport = self._run_do_debug(
            monkeypatch, boot_script
        )
        try:
            reported = self._wait_for_report(reported_holder)
            assert reported["host"] == "127.0.0.1"
            assert reported["port"] != 1, "must be the bridge's port, not the device's placeholder"

            client = socket.create_connection((reported["host"], reported["port"]), timeout=5)
            try:
                body = json.dumps({"seq": 1, "type": "request", "command": "initialize"}).encode()
                frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
                client.sendall(frame)
                deadline = time.monotonic() + 5
                received = b""
                while time.monotonic() < deadline and len(received) < len(frame):
                    received += _read_master(device_master_fd, 4096, deadline)
                assert received == frame
            finally:
                client.close()

            final = future.result(timeout=10)
            assert final["host"] == reported["host"] and final["port"] == reported["port"]
        finally:
            self._cleanup(control_device, device_master_fd, transport)

    def test_dap_device_without_serial_dap_cap_is_a_hard_error(self, monkeypatch):
        """A configured `dap_device` the probe doesn't confirm must fail loudly."""
        boot_script = (
            "import json\n"
            'print("MPDBG-READY " + json.dumps('
            '{"host": "serial", "port": 1, "caps": {"serial_dap": False}}))\n'
        )
        future, reported_holder, control_device, device_master_fd, transport = self._run_do_debug(
            monkeypatch, boot_script
        )
        try:
            with pytest.raises(commands.CommandError, match="serial_dap"):
                future.result(timeout=10)
        finally:
            self._cleanup(control_device, device_master_fd, transport)

    def test_board_reset_mid_session_is_a_clear_error_without_reconnect(self, monkeypatch):
        """Losing the device mid-session ends the session with a plain error.

        `do_reconnect` is undefined on this (plain-upstream) tree, so
        `_serial_dap_lost`'s feature-detect always takes the error branch here
        - the ampremote-reconnect branch needs that support present, which
        this checkout doesn't have (see the module docstring).
        """
        boot_script = (
            "import json\n"
            'print("MPDBG-READY " + json.dumps('
            '{"host": "serial", "port": 1, "caps": {"serial_dap": True}}))\n'
        )
        future, reported_holder, control_device, device_master_fd, transport = self._run_do_debug(
            monkeypatch, boot_script
        )
        try:
            reported = self._wait_for_report(reported_holder)
            client = socket.create_connection((reported["host"], reported["port"]), timeout=5)
            try:
                client.sendall(b"x")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if _read_master(device_master_fd, 1, deadline) == b"x":
                        break
                os.close(device_master_fd)
                device_master_fd = -1

                with pytest.raises(commands.CommandError, match="lost"):
                    future.result(timeout=10)
            finally:
                client.close()
        finally:
            # `device_master_fd` is -1 when the test closed it to stage the
            # loss; `_cleanup` tolerates that.
            self._cleanup(control_device, device_master_fd, transport)


@requires_unix_firmware
class TestDoDebugSerialDapBridgeRealSession:
    """`do_debug`'s serial_dap path carrying a REAL `debugpy` session end to
    end - `TestDoDebugSerialDapBridge` above proves the bridge forwards raw
    bytes correctly; this proves those bytes are an actual DAP session that
    reaches a breakpoint, through the production `SerialDapBridge`/
    `PumpingProxy` unmodified. `dap_device` is a second pty's slave path
    (matching a real `/dev/serial/by-id/...` node); the device holds that
    pty's *master* fd, inherited via `pass_fds` and opened directly by
    number (`open(fd, "r+b")` - MicroPython's unix port supports this) -
    reopening it via `/proc/self/fd/N` was tried first and does not work:
    that path re-opens `/dev/ptmx` fresh (a brand new, unconnected pty),
    not the original master, silently self-echoing everything written to it
    instead of delivering to the real slave.
    """

    def _spawn_stream_session(self, monkeypatch, module="target", method="main"):
        """Wire `do_debug`'s serial path to a real `debugpy` session on a
        second pty standing in for a dedicated DAP CDC interface.

        Returns `(future, reported_holder, control_device, transport,
        bridge_holder, device_slave_fd)`.
        Unlike `TestDoDebugSerialDapBridge._run_do_debug`, the *device* (not
        the test) holds the data-plane pty's master fd, since a real session
        has to run somewhere real - the unix firmware process started here.
        `bridge_holder["proxy"]` is populated once `do_debug` starts the
        bridge (a real seam it already calls through, wrapped here purely to
        observe it - not to replace any of its behaviour).
        """
        device_master_fd, device_slave_fd = pty.openpty()
        # Raw mode: a DAP frame is length-prefixed binary-ish data, not
        # terminal input - canonical-mode echo would corrupt it. This is the
        # pty's shared termios state, so setting it via either fd covers both
        # (the device's fd-3 open below and the bridge's later pyserial open
        # of the slave path).
        tty.setraw(device_slave_fd)
        device_slave_path = os.ttyname(device_slave_fd)
        # `device_slave_fd` is kept open (unlike `_open_loopback_pty`) for this
        # whole session: a pty slave with zero open references makes the
        # master's next read/poll return EIO (see `_read_master`'s docstring) -
        # closing it now, before `SerialDapBridge` lazily reopens it on first
        # client connect, would open exactly that window and hand the device a
        # spurious EIO before any real client byte ever arrives. A pty slave
        # tolerates more than one opener, so holding this one alongside the
        # bridge's own is harmless as long as this side never reads/writes it.
        control_device = PtyDevice(
            _MICROPYTHON, _MICROPYPATH, pass_fds=(device_master_fd,)
        ).start()
        os.close(device_master_fd)  # the child now owns its own reference

        transport = SerialTransport(control_device.path, baudrate=115200)
        state = State()
        state.transport = transport
        commands.do_resume(state)  # the unix build exits on a raw-REPL soft reset

        resolved = Target(
            name="bench", kind="serial", device=control_device.path, dap_device=device_slave_path
        )
        monkeypatch.setattr(commands, "resolve_target", lambda name: resolved)

        def _boot_script(mod, meth, port, dap_stream=None, mount_point=None, loop=False):
            assert dap_stream == "board", dap_stream
            assert mount_point is None, "this session mounts nothing"
            return _stream_session_boot_script(device_master_fd, module, method)

        monkeypatch.setattr(commands, "_debug_boot_script", _boot_script)

        reported_holder = {}
        orig_report = commands._report_debug_result

        def _capture_report(handshake, path_mappings=None):
            reported_holder["value"] = handshake
            return orig_report(handshake, path_mappings)

        monkeypatch.setattr(commands, "_report_debug_result", _capture_report)

        bridge_holder = {}
        orig_maybe_start = commands._maybe_start_serial_dap

        def _capture_bridge(resolved_arg, handshake, dap_log_arg, bind_port):
            result = orig_maybe_start(resolved_arg, handshake, dap_log_arg, bind_port)
            if result is not None:
                bridge_holder["proxy"] = result[0]
            return result

        monkeypatch.setattr(commands, "_maybe_start_serial_dap", _capture_bridge)

        args = debug_args(target="bench", program=f"{module}:{method}", timeout=15)

        future = _DaemonFuture(commands.do_debug, state, args)
        return future, reported_holder, control_device, transport, bridge_holder, device_slave_fd

    def _wait_for_report(self, reported_holder, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "value" in reported_holder:
                return reported_holder["value"]
            time.sleep(0.05)
        pytest.fail("do_debug never reported the bridge's endpoint")

    def _cleanup(self, control_device, device_slave_fd, transport=None):
        # `transport` closes before the process it talks to is torn down -
        # see the sibling `_cleanup` above for why order matters here.
        if transport is not None:
            transport.close()
        control_device.close()
        if device_slave_fd != -1:
            os.close(device_slave_fd)

    def test_reaches_breakpoint_over_real_debugpy_stream_session(self, monkeypatch):
        """The end-to-end criterion: a real DAP client, attached to the
        bridge's reported endpoint, drives a real device-side `debugpy`
        session (running over the second pty, not TCP) to a breakpoint stop
        - through the actual `SerialDapBridge`, not a hand-rolled pump.
        """
        future, reported_holder, control_device, transport, _bridge_holder, device_slave_fd = (
            self._spawn_stream_session(monkeypatch)
        )
        try:
            reported = self._wait_for_report(reported_holder)
            assert reported["caps"]["serial_dap"] is True

            server = PerfServer("test-client", reported["host"], reported["port"])
            try:
                server.start()
                wait_for_msg(server, response="initialize", timeout=10)
                assert _seen_response(server, "initialize"), "no initialize response over the bridge"

                set_breakpoints(server, _TARGET_PY, [_BREAKPOINT_LINE])
                assert wait_for_msg(server, response="setBreakpoints", timeout=10), (
                    "setBreakpoints failed over the bridge"
                )

                server.client.configuration_done()
                stopped = wait_for_msg(server, event="stopped", timeout=15)
                assert stopped is not None, "configurationDone produced no stopped event over the bridge"
                assert stopped.body.get("reason") == "breakpoint", (
                    f"stopped reason not 'breakpoint': {stopped.body}"
                )
            finally:
                server.stop()

            final = future.result(timeout=10)
            assert final["host"] == reported["host"] and final["port"] == reported["port"]
        finally:
            self._cleanup(control_device, device_slave_fd, transport)

    def test_killing_the_bridge_while_stopped_leaves_the_device_usable(
        self, monkeypatch, capsys
    ):
        """Killing the bridge while the device is stopped at a breakpoint
        must leave the device recoverable (trace uninstalled or the session
        cleanly closed, per the ticket) - not spinning forever unable to
        ever resume.

        The device's own completion print is the evidence, and it is read
        from `do_debug`'s stdout rather than from the port: killing the
        bridge this way leaves nothing to tell the proxy its session ended,
        so `do_debug` stays attached, and while it does its console pump owns
        that port. A second reader here would take an arbitrary share of the
        bytes and split the line it is looking for.
        """
        future, reported_holder, control_device, transport, bridge_holder, device_slave_fd = (
            self._spawn_stream_session(monkeypatch)
        )
        try:
            reported = self._wait_for_report(reported_holder)
            server = PerfServer("test-client", reported["host"], reported["port"])
            try:
                server.start()
                wait_for_msg(server, response="initialize", timeout=10)
                set_breakpoints(server, _TARGET_PY, [_BREAKPOINT_LINE])
                wait_for_msg(server, response="setBreakpoints", timeout=10)
                server.client.configuration_done()
                stopped = wait_for_msg(server, event="stopped", timeout=15)
                assert stopped is not None and stopped.body.get("reason") == "breakpoint"

                assert "proxy" in bridge_holder, "do_debug never started the serial DAP bridge"
                bridge_proxy = bridge_holder["proxy"]
                # Kill the bridge abruptly (not proxy.close()'s orderly
                # shutdown - a process getting SIGKILLed doesn't run that
                # either): sever the socket the device-facing pump is
                # blocked reading, without telling anyone. The pty's master
                # (held by the device) only sees a hangup once every open
                # slave reference is gone - `_spawn_stream_session` kept its
                # own `device_slave_fd` open until now purely to avoid a
                # spurious EIO before the bridge got a chance to connect, so
                # it must close that one too, or the bridge's own close()
                # leaves one reference standing and the device never notices.
                os.close(device_slave_fd)
                device_slave_fd = -1
                bridge_proxy._server.close()
                bridge_proxy._client.close()

                # If the device recovers, its own completion print appears
                # within a bounded time; if it is wedged (the bug above),
                # this never arrives.
                marker = "Target completed successfully!"
                echoed = ""
                deadline = time.monotonic() + 15
                while marker not in echoed and time.monotonic() < deadline:
                    echoed += capsys.readouterr().out
                    time.sleep(0.1)
                assert marker in echoed, (
                    "device never recovered after the bridge was killed while "
                    f"stopped: {echoed!r}"
                )
            finally:
                server.stop()
        finally:
            self._cleanup(control_device, device_slave_fd, transport)

    def test_a_talkative_target_does_not_wedge_on_the_console_nobody_reads(
        self, monkeypatch, capsys
    ):
        """A session that stays attached keeps the board's console emptied.

        The console is a second connection here, not the one DAP rides, and
        for as long as `do_debug` stays attached it is the only process
        holding it. Held open and never read, it back-pressures into the
        device - the tty stops accepting, the device's own buffer fills
        behind it, and the next `print` waits for room that is not coming.
        The program stops, so the DAP channel stops answering, and it reads
        as a link that went quiet rather than as a full console.

        Driven through `evaluate`, because what the target prints of its own
        accord is bounded and this needs to cross the tty's buffer: each one
        prints a line and is answered, so an unanswered one is the device
        having stopped, and how many had gone through by then says how far it
        got.
        """
        future, reported_holder, control_device, transport, _bridge_holder, device_slave_fd = (
            self._spawn_stream_session(monkeypatch)
        )
        try:
            reported = self._wait_for_report(reported_holder)
            server = PerfServer("test-client", reported["host"], reported["port"])
            try:
                server.start()
                wait_for_msg(server, response="initialize", timeout=10)
                set_breakpoints(server, _TARGET_PY, [_BREAKPOINT_LINE])
                wait_for_msg(server, response="setBreakpoints", timeout=10)
                server.client.configuration_done()
                stopped = wait_for_msg(server, event="stopped", timeout=15)
                assert stopped is not None and stopped.body.get("reason") == "breakpoint"

                # A kilobyte at a time, past 100 kB in total: a tty will
                # absorb tens of kilobytes into its own buffers before the
                # writer feels anything, so crossing that is what makes a
                # console nobody empties stop the device rather than merely
                # slow it down.
                printed = 0
                for _ in range(100):
                    server.client.evaluate('print("P" * 1000)', frame_id=0, context="repl")
                    answered = take_msg(server, response="evaluate", timeout=10)
                    assert answered is not None, (
                        f"the device stopped answering after printing {printed} bytes; "
                        "a console this session holds open is not being read"
                    )
                    printed += 1001
                assert printed > 65536, printed
            finally:
                server.stop()
            future.result(timeout=15)
        finally:
            self._cleanup(control_device, device_slave_fd, transport)
