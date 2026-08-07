"""Device-side pytest coverage for `debugpy.common.stream_transport.StreamTransport`
(STORY-6.1).

Two tiers, both against the real unix firmware (no hardware, no mocks):

- `test_stream_transport_contract` drives `fixtures/stream_transport_probe.py`
  directly against a pair of pipes, isolating `StreamTransport` from the rest
  of `debugpy` to pin down the exact contract `messaging.py` depends on:
  `settimeout` bounding `recv` (`OSError(11)` on expiry, block forever on
  `None`), `recv` returning `b""` on EOF rather than raising, and `send`
  delivering a payload larger than one pipe write - the write end is shrunk
  with `fcntl.F_SETPIPE_SZ` first so a single `write()` genuinely cannot
  accept it all, forcing `send`'s internal retry loop to run.
- `test_reaches_breakpoint_over_stream_transport` promotes the former
  `s6_1_stream_transport_proof.py` script into a collected test: a real
  `debugpy` session run over `listen_stream()` on a pty pair, with no socket
  anywhere in the DAP path, reaching a real breakpoint in `src/target.py`.
"""

import fcntl
import json
import os
import pty
import select
import subprocess
import time
import tty
from pathlib import Path

import pytest

_TOP_DIR = Path(__file__).resolve().parents[1]
_MICROPYTHON = Path(
    os.environ.get(
        "MPY_DEBUG_FIRMWARE",
        _TOP_DIR / "micropython/ports/unix/build-standard/micropython",
    )
)
_MICROPYPATH = "{}:{}:{}".format(
    _TOP_DIR / "src", _TOP_DIR / "micropython-lib/python-ecosys/debugpy", _TOP_DIR / "micropython-lib"
)
_PROBE_SCRIPT = _TOP_DIR / "tests" / "fixtures" / "stream_transport_probe.py"
_STREAM_DEBUGGEE = _TOP_DIR / "tests" / "fixtures" / "stream_debuggee.py"
_TARGET_PY = str(_TOP_DIR / "src" / "target.py")
_BREAKPOINT_LINE = 80  # src/target.py, main(): the `for` loop calling inspect_local_variables()

requires_unix_firmware = pytest.mark.skipif(
    not _MICROPYTHON.exists(),
    reason=f"unix firmware not built at {_MICROPYTHON} (see `make firmware-unix`)",
)


def _env():
    env = os.environ.copy()
    env["MICROPYPATH"] = _MICROPYPATH
    return env


class LineReader:
    """Non-blocking, `select()`-driven line reader over a raw fd.

    Reading `proc.stdout` with `.readline()` can't be interleaved with
    draining a second fd from the same process (e.g. the pipe a probe script
    is simultaneously writing a large payload to) without risking a
    deadlock - both sides end up waiting on each other. Polling the raw fd
    with `select()` lets a caller service two fds in one loop instead.
    """

    def __init__(self, fd):
        self.fd = fd
        self._buf = b""

    def poll_lines(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        chunk = os.read(self.fd, 4096)
        if not chunk:
            return []
        self._buf += chunk
        lines = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            lines.append(line.decode(errors="replace"))
        return lines

    def wait_for(self, predicate, deadline):
        seen = []
        while time.monotonic() < deadline:
            for line in self.poll_lines(max(0.05, deadline - time.monotonic())):
                seen.append(line)
                if line.startswith("FAIL:"):
                    pytest.fail(f"device reported failure: {line}; prior output: {seen}")
                if predicate(line):
                    return line
        pytest.fail(f"device never reported the expected line within the deadline; output so far: {seen}")


@requires_unix_firmware
def test_stream_transport_contract():
    """`StreamTransport`'s settimeout/EOF/send contract, isolated from DAP framing."""
    dev_read_fd, host_write_fd = os.pipe()  # host -> device
    host_read_fd, dev_write_fd = os.pipe()  # device -> host

    # Shrink the device's outbound pipe so a 20000-byte send() cannot
    # complete in one write() - proving send() loops rather than truncating.
    fcntl.fcntl(dev_write_fd, fcntl.F_SETPIPE_SZ, 4096)

    proc = subprocess.Popen(
        [str(_MICROPYTHON), str(_PROBE_SCRIPT), str(dev_read_fd), str(dev_write_fd)],
        env=_env(),
        pass_fds=(dev_read_fd, dev_write_fd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    os.close(dev_read_fd)
    os.close(dev_write_fd)
    stdout = LineReader(proc.stdout.fileno())
    try:
        deadline = time.monotonic() + 15
        stdout.wait_for(lambda ln: ln.startswith("OK:timeout-raises"), deadline)

        os.write(host_write_fd, b"HELLO")
        stdout.wait_for(lambda ln: ln.startswith("OK:recv-blocking"), deadline)

        # The device's send() is now blocked writing into a pipe too small
        # to take the whole payload in one go: this fd and `host_read_fd`
        # must be drained in the same loop, not one after the other, or the
        # device (waiting for buffer space) and this test (waiting for the
        # "send-issued" line the device can't print until send() returns)
        # deadlock each other.
        expected = bytes((i & 0xFF for i in range(20000)))
        received = b""
        send_issued = False
        while time.monotonic() < deadline and (not send_issued or len(received) < len(expected)):
            r, _, _ = select.select([stdout.fd, host_read_fd], [], [], 0.2)
            if stdout.fd in r:
                for line in stdout.poll_lines(0):
                    if line.startswith("FAIL:"):
                        pytest.fail(f"device reported failure: {line}")
                    if line.startswith("OK:send-issued:20000"):
                        send_issued = True
            if host_read_fd in r:
                received += os.read(host_read_fd, 65536)
        assert send_issued, "device never reported OK:send-issued"
        assert received == expected, (
            f"send() did not deliver the payload intact: got {len(received)} of {len(expected)} bytes"
        )

        os.close(host_write_fd)
        host_write_fd = -1
        stdout.wait_for(lambda ln: ln.startswith("OK:eof"), deadline)

        assert proc.wait(timeout=10) == 0
    finally:
        if host_write_fd != -1:
            os.close(host_write_fd)
        os.close(host_read_fd)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@requires_unix_firmware
def test_reaches_breakpoint_over_stream_transport():
    """A real `debugpy` session over `listen_stream()` on a pty, no socket anywhere."""
    master_fd, slave_fd = pty.openpty()
    # Raw mode: a DAP frame is binary-ish, length-prefixed data - canonical
    # mode echo/CR-LF translation would corrupt both directions.
    tty.setraw(slave_fd)
    slave_path = os.ttyname(slave_fd)
    os.close(slave_fd)  # only the path is needed; the device opens it fresh

    proc = subprocess.Popen(
        [str(_MICROPYTHON), str(_STREAM_DEBUGGEE), slave_path, "target", "main"],
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 10
        LineReader(proc.stdout.fileno()).wait_for(lambda ln: "STREAM-READY" in ln, deadline)

        buf = b""

        def send(seq, **kwargs):
            kwargs.setdefault("seq", seq)
            body = json.dumps(kwargs).encode()
            os.write(master_fd, f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

        def wait_for(timeout, **match):
            nonlocal buf
            deadline_local = time.monotonic() + timeout
            while time.monotonic() < deadline_local:
                r, _, _ = select.select([master_fd], [], [], 0.1)
                if r:
                    buf += os.read(master_fd, 4096)
                while True:
                    sep = buf.find(b"\r\n\r\n")
                    if sep < 0:
                        break
                    header = buf[:sep].decode()
                    length = 0
                    for line in header.split("\r\n"):
                        if line.lower().startswith("content-length:"):
                            length = int(line.split(":", 1)[1].strip())
                    if len(buf) < sep + 4 + length:
                        break
                    msg = json.loads(buf[sep + 4 : sep + 4 + length])
                    buf = buf[sep + 4 + length :]
                    if all(msg.get(k) == v for k, v in match.items()):
                        return msg
            return None

        send(1, type="request", command="initialize", arguments={"clientID": "s6.1-test"})
        assert wait_for(10, type="response", command="initialize") is not None

        send(
            2,
            type="request",
            command="setBreakpoints",
            arguments={"source": {"path": _TARGET_PY}, "breakpoints": [{"line": _BREAKPOINT_LINE}]},
        )
        assert wait_for(10, type="response", command="setBreakpoints") is not None

        send(3, type="request", command="configurationDone")
        assert wait_for(10, type="response", command="configurationDone") is not None

        stopped = wait_for(15, type="event", event="stopped")
        assert stopped is not None, "no stopped event over the stream transport"
        assert stopped["body"]["reason"] == "breakpoint"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(master_fd)
