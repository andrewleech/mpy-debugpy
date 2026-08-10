"""Device-side pytest coverage for `debugpy.common.stream_transport.StreamTransport`
(STORY-6.1).

Two tiers, both against the real unix firmware (no hardware, no mocks):

- `test_stream_transport_contract` drives `fixtures/stream_transport_probe.py`
  directly against a pair of pipes, isolating `StreamTransport` from the rest
  of `debugpy` to pin down the exact contract `messaging.py` depends on:
  `settimeout` bounding `recv` (`OSError(11)` on expiry, block forever on
  `None`), `recv` returning `b""` on EOF rather than raising, and `send`
  reporting a short write instead of claiming the whole buffer - the write
  end is shrunk with `fcntl.F_SETPIPE_SZ` first so a single `write()`
  genuinely cannot accept it all. The same narrow pipe then carries a real
  `JsonMessageChannel` frame while this side deliberately stops draining,
  so the frame arrives intact only if every partial write is accounted
  for - a sender that discards the count it reached resends a prefix, and
  the frame's declared length then covers a body that will not parse.
- `test_stream_transport_peer_gone_signal` drives
  `fixtures/stream_liveness_probe.py` over a pipe the host never closes, so
  the transport has no EOF to find, and checks when the caller-supplied
  host-present signal is allowed to stand in for one: not before the channel
  has carried a byte, not while a connected peer is merely idle, promptly
  once a peer that was talking goes away, and never at all for a stream that
  supplies no signal.
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
_LIVENESS_PROBE = _TOP_DIR / "tests" / "fixtures" / "stream_liveness_probe.py"
# The shipped boot script, not a stand-in: given a `dap_device` it runs the
# DAP channel over that stream, which is the product path under test here.
_BOOT_SCRIPT = _TOP_DIR / "launcher" / "mpy_launch_debugpy.py"
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
        # Whole lines already parsed out of an earlier read but not yet
        # handed to a caller. A device that prints several steps faster than
        # this side reads delivers them in one chunk, and a caller waiting
        # for them one at a time would otherwise never see the later ones.
        self._pending = []

    def poll_lines(self, timeout):
        if self._pending:
            lines, self._pending = self._pending, []
            return lines
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
            lines = self.poll_lines(max(0.05, deadline - time.monotonic()))
            for i, line in enumerate(lines):
                seen.append(line)
                if line.startswith("FAIL:"):
                    pytest.fail(f"device reported failure: {line}; prior output: {seen}")
                if predicate(line):
                    self._pending = lines[i + 1 :] + self._pending
                    return line
        pytest.fail(f"device never reported the expected line within the deadline; output so far: {seen}")


_CORRUPT = object()


def _parse_frame(data):
    """The first complete `Content-Length` frame in `data`.

    None while the frame is still incomplete, and `_CORRUPT` once the
    declared length has arrived but the body will not parse - the signature
    of a sender that lost track of a partial write and resent a prefix.
    """
    sep = data.find(b"\r\n\r\n")
    if sep < 0:
        return None
    length = None
    for line in data[:sep].decode(errors="replace").split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1])
    if length is None or len(data) < sep + 4 + length:
        return None
    try:
        return json.loads(data[sep + 4 : sep + 4 + length])
    except ValueError:
        return _CORRUPT


def _payload_then_frame(data, payload_len):
    """True once the raw payload and the framed message after it have arrived."""
    return len(data) >= payload_len and _parse_frame(data[payload_len:]) is not None


@requires_unix_firmware
def test_stream_transport_contract():
    """`StreamTransport`'s settimeout/EOF/send contract, isolated from DAP framing."""
    dev_read_fd, host_write_fd = os.pipe()  # host -> device
    host_read_fd, dev_write_fd = os.pipe()  # device -> host

    # Make the device's outbound pipe behave like the CDC interface this
    # transport exists for: small, so one write cannot take a whole frame,
    # and non-blocking, so the write reports the short count instead of
    # looping inside the kernel until the host drains.
    fcntl.fcntl(dev_write_fd, fcntl.F_SETPIPE_SZ, 4096)
    fcntl.fcntl(dev_write_fd, fcntl.F_SETFL, fcntl.fcntl(dev_write_fd, fcntl.F_GETFL) | os.O_NONBLOCK)

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

        # The device is now writing into a pipe too small to take either
        # payload in one go: this fd and `host_read_fd` must be drained in
        # the same loop, not one after the other, or the device (waiting for
        # buffer space) and this test (waiting for a line the device cannot
        # print until its write completes) deadlock each other.
        expected = bytes((i & 0xFF for i in range(20000)))
        received = b""
        steps = []
        # Draining stops for a moment once the raw payload is in, so the pipe
        # is full for far longer than the 1 ms timeout the device sends its
        # frame under. Without the stall the device can finish a whole frame
        # inside one timeout on a fast host, and a transport that mishandles
        # partial writes goes unnoticed; with it, the frame can only arrive
        # intact if each partial write resumes from the right offset.
        stall_until = None
        while time.monotonic() < deadline and not _payload_then_frame(received, len(expected)):
            r, _, _ = select.select([stdout.fd, host_read_fd], [], [], 0.05)
            if stdout.fd in r:
                for line in stdout.poll_lines(0):
                    if line.startswith("FAIL:"):
                        pytest.fail(f"device reported failure: {line}")
                    steps.append(line)
            if host_read_fd in r:
                if len(received) >= len(expected):
                    if stall_until is None:
                        stall_until = time.monotonic() + 0.2
                    remaining = stall_until - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(0.05, remaining))
                        continue
                received += os.read(host_read_fd, 65536)

        partial = next((s for s in steps if s.startswith("OK:send-partial:")), None)
        assert partial is not None, f"device never reported a partial send; steps: {steps}"
        first, total = (int(v) for v in partial.split(":")[2:4])
        # The premise of the whole step: one write really could not take it
        # all, so the short count is the interesting case and not an accident
        # of a pipe that happened to be big enough.
        assert 0 < first < len(expected), partial
        assert total == len(expected), partial

        assert received[: len(expected)] == expected, (
            "send() did not deliver the payload intact: got "
            f"{len(received)} bytes, first mismatch at "
            f"{next((i for i, b in enumerate(received) if i < len(expected) and b != expected[i]), None)}"
        )

        frame = _parse_frame(received[len(expected) :])
        assert frame is not None, f"no complete DAP frame after the payload; steps: {steps}"
        assert frame is not _CORRUPT, (
            "the framed message arrived corrupt, so a partial write was not resumed "
            "from the offset it reached"
        )
        assert frame["event"] == "output", frame
        assert frame["body"]["output"] == "B" * 20000, len(frame["body"]["output"])

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
def test_stream_transport_peer_gone_signal():
    """When a caller-supplied host-present signal may stand in for EOF.

    Both pipe ends stay open for the whole run, so the transport can never
    find an EOF of its own - the situation a USB CDC interface is permanently
    in, and the reason the signal exists. Each step is asserted separately
    because the two ways of getting this wrong fail in opposite directions:
    trusting the signal too early ends a session that was only waiting for
    its client to attach, and not trusting it at all leaves a board stopped
    at a breakpoint until someone power-cycles it.
    """
    dev_read_fd, host_write_fd = os.pipe()  # host -> device
    host_read_fd, dev_write_fd = os.pipe()  # device -> host

    proc = subprocess.Popen(
        [str(_MICROPYTHON), str(_LIVENESS_PROBE), str(dev_read_fd), str(dev_write_fd)],
        env=_env(),
        pass_fds=(dev_read_fd, dev_write_fd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    os.close(dev_read_fd)
    os.close(dev_write_fd)
    stdout = LineReader(proc.stdout.fileno())
    try:
        deadline = time.monotonic() + 20
        stdout.wait_for(lambda ln: ln.startswith("OK:quiet-before-traffic"), deadline)

        # The device is blocked in recv() waiting for this, which is what
        # makes the step above a real "nothing has arrived yet" and not a
        # race with data already in the pipe.
        os.write(host_write_fd, b"HELLO")

        for step in (
            "OK:traffic",
            "OK:idle-is-not-gone",
            "OK:peer-gone",
            "OK:peer-gone-sticky",
            "OK:no-signal-no-eof",
        ):
            stdout.wait_for(lambda ln, s=step: ln.startswith(s), deadline)

        assert proc.wait(timeout=10) == 0
    finally:
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
        [str(_MICROPYTHON), str(_BOOT_SCRIPT), "target", "main", "0", slave_path],
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 10
        # The boot script's own handshake line, on stdout as always - the DAP
        # channel is the pty, so stdout carries only the human-readable output.
        ready = LineReader(proc.stdout.fileno()).wait_for(
            lambda ln: ln.startswith("MPDBG-READY "), deadline
        )
        payload = json.loads(ready[len("MPDBG-READY ") :])
        assert payload["host"] == "serial" and payload["port"] == 0, payload
        assert payload["caps"]["serial_dap"] is True, payload

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
