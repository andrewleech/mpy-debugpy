"""`JsonMessageChannel` against the socket contract a real board provides.

The DAP channel always carries a socket timeout (`public_api.py` drops it to
1 ms right after `initialize`, and `debug_session.py` moves it around at
protocol points), so every idle poll ends in an error rather than in data.
Which error, and whether a write completes in one call, is a property of the
network stack underneath - and the unix port, which the rest of the suite
runs on, is the most forgiving of them:

- a timed-out `recv` raises EAGAIN on the unix port and ETIMEDOUT on lwIP,
  which is every WiFi board;
- `sock.send()` may accept a prefix and return the count, which truncates a
  DAP frame mid-`Content-Length` and desynchronises the stream permanently.

Neither is reachable through the pty/unix harness the other suites use, so
these drive `messaging.py` directly against sockets that behave the way a
board's do. They run under the real unix firmware rather than CPython
because the module is MicroPython code (`time.ticks_ms`).
"""

import os
import subprocess
import textwrap

import pytest
from mpremote_debug import MICROPYTHON as _MICROPYTHON, TOP_DIR as _TOP_DIR

_DEBUGPY_DIR = _TOP_DIR / "micropython-lib" / "python-ecosys" / "debugpy"

requires_unix_firmware = pytest.mark.skipif(
    not _MICROPYTHON.exists(),
    reason=f"{_MICROPYTHON} not built (make -C micropython/ports/unix)",
)

_PREAMBLE = """
from debugpy.common.messaging import JsonMessageChannel


def frame(payload):
    body = payload.encode()
    return b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body
"""


def _run(script):
    """Run `script` under the unix firmware with debugpy importable."""
    result = subprocess.run(
        [str(_MICROPYTHON), "-c", _PREAMBLE + textwrap.dedent(script)],
        env={**os.environ, "MICROPYPATH": str(_DEBUGPY_DIR)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result.stdout


@requires_unix_firmware
@pytest.mark.parametrize("errno", [11, 35, 110], ids=["EAGAIN", "EWOULDBLOCK", "ETIMEDOUT"])
def test_idle_poll_errno_does_not_close_the_channel(errno):
    """An idle poll is not a closed peer, whichever errno the stack reports.

    ETIMEDOUT is the one lwIP raises, so before it was accepted here a board
    ended its session on the first poll after `initialize` - which is the
    first poll there is.
    """
    output = _run(f"""
        class IdleSock:
            def recv(self, n):
                raise OSError({errno})

        channel = JsonMessageChannel(IdleSock())
        for _ in range(5):
            assert channel.recv_message() is None
        print("closed:", channel.closed)
    """)
    assert "closed: False" in output


@requires_unix_firmware
def test_unexpected_recv_errno_closes_the_channel():
    """A real socket error still ends the session rather than spinning on it."""
    output = _run("""
        class BrokenSock:
            def recv(self, n):
                raise OSError(104)  # ECONNRESET

        channel = JsonMessageChannel(BrokenSock())
        assert channel.recv_message() is None
        print("closed:", channel.closed)
    """)
    assert "closed: True" in output


@requires_unix_firmware
def test_message_after_idle_polls_is_still_parsed():
    """The buffer survives the idle polls between the parts of one message."""
    output = _run("""
        class TrickleSock:
            def __init__(self, chunks):
                self.chunks = list(chunks)

            def recv(self, n):
                if not self.chunks:
                    raise OSError(110)
                chunk = self.chunks.pop(0)
                if chunk is None:
                    raise OSError(110)
                return chunk

        whole = frame('{"seq": 1, "type": "request", "command": "attach"}')
        sock = TrickleSock([whole[:10], None, whole[10:30], None, whole[30:]])
        channel = JsonMessageChannel(sock)
        message = None
        for _ in range(10):
            message = channel.recv_message()
            if message is not None:
                break
        print("command:", message and message.get("command"))
        print("closed:", channel.closed)
    """)
    assert "command: attach" in output
    assert "closed: False" in output


@requires_unix_firmware
def test_short_write_sends_the_whole_frame():
    """A socket that accepts a few bytes per call still gets the entire frame.

    Truncation here has no protocol-level symptom: the header promises
    `Content-Length` bytes that never arrive, so the client waits forever on
    a message that will never complete and every later frame is misread as
    its body.
    """
    output = _run("""
        class DribbleSock:
            def __init__(self, per_call):
                self.per_call = per_call
                self.written = b""

            def send(self, data):
                chunk = bytes(data)[: self.per_call]
                self.written += chunk
                return len(chunk)

        sock = DribbleSock(7)
        channel = JsonMessageChannel(sock)
        channel.send_event("stopped", reason="breakpoint")
        header, _, body = sock.written.partition(b"\\r\\n\\r\\n")
        length = int(header.split(b":")[1])
        print("complete:", len(body) == length)
        print("closed:", channel.closed)
    """)
    assert "complete: True" in output
    assert "closed: False" in output


@requires_unix_firmware
def test_write_that_never_drains_closes_the_channel():
    """A peer that stops reading ends the session instead of blocking forever.

    The bound matters because `send` runs inside the trace callback: an
    unbounded retry would wedge the debuggee, not just the channel.
    """
    output = _run("""
        class DeafSock:
            def send(self, data):
                raise OSError(110)

        channel = JsonMessageChannel(DeafSock())
        channel.send_event("stopped", reason="breakpoint")
        print("closed:", channel.closed)
    """)
    assert "closed: True" in output
