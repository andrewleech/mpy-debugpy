"""STORY-6.1: DAP over the board's own second CDC interface, on real hardware.

The criterion these exist for is that a debug session needs no address of any
kind: `mpremote debug <target>` names a board, the board takes its dedicated
DAP interface instead of binding a socket, and mpremote bridges that interface
to a loopback port for the client. Nothing in the path has an IP the user
chose, and the board needs no network at all.

Set `MPY_DEBUG_HIL_DAP_DEVICE` to the board's second `/dev/serial/by-id/...`
path alongside `MPY_DEBUG_HIL_DEVICE` to run them (see `conftest.py`). The
board must enumerate both interfaces: on stm32 that is a `usb_mode` call in
`boot.py`, since the firmware's `MICROPY_HW_USB_CDC_NUM` is only a build-time
maximum and the default mode enumerates one.
"""

import ipaddress
import re
import time

from dapdrive import attached, run_to_breakpoint
from debuggee import DEVICE_SOURCE_PATH, LOOP_LINE

# The device's own report of where it is listening, echoed by mpremote before
# the handshake line it consumes. `_run()` prints "serial:0" for a stream.
# The board's line endings survive the echo, so the capture stops short of
# them rather than including a stray CR in the endpoint.
_DEVICE_ENDPOINT_RE = re.compile(r"^Debug server listening on (\S+)\s*$", re.M)

# Large enough that the response crosses the link in many reads rather than
# one, which is what the throughput measurement is about.
_PAYLOAD_SIZE = 16384

# The floor the measurement has to clear, well under what a working link
# achieves and well over what a per-byte round trip would. A regression to
# reading the stream one byte per `read()` call shows up here.
_MIN_BYTES_PER_SECOND = 2000


def test_hil_serial_dap_takes_the_stream_instead_of_a_port(hil_serial_dap_session):
    """The board binds nothing; the only endpoint is mpremote's own loopback.

    Three separate claims, because any one of them alone would still admit a
    session that quietly went over the network: the device reported a stream
    rather than an address, the probe agrees the session took one, and the
    endpoint handed to the client belongs to this host.
    """
    session = hil_serial_dap_session

    endpoints = _DEVICE_ENDPOINT_RE.findall(session["command_output"])
    assert endpoints == ["serial:0"], session["command_output"]

    assert session["caps"]["serial_dap"] is True, session["caps"]

    assert session["host"] == "127.0.0.1", session
    assert isinstance(session["port"], int) and session["port"] > 0, session
    assert ipaddress.ip_address(session["host"]).is_loopback, session


def test_hil_serial_dap_reaches_a_breakpoint(hil_serial_dap_session):
    """The whole criterion in one scenario: attach, stop, read the frame back.

    Driven against the bridge exactly as the network scenarios drive the
    board's own listener - the client cannot tell the difference, which is
    the point of putting the bridge behind a socket.
    """
    with attached(hil_serial_dap_session) as server:
        stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
        assert stopped.body["reason"] == "breakpoint", stopped
        thread_id = stopped.body["threadId"]

        frames = server.request("stackTrace", {"threadId": thread_id}).body["stackFrames"]
        assert (frames[0]["name"], frames[0]["line"]) == ("main", LOOP_LINE), frames[0]
        assert frames[0]["source"]["path"] == DEVICE_SOURCE_PATH, frames[0]

        # Resuming works over the stream too, and lands on the loop header
        # again (see the network suite for why a header stops on every pass).
        since = server.mark()
        server.request("continue", {"threadId": thread_id})
        again = server.wait_event("stopped", timeout=60, since=since)
        assert again is not None, "no second stop after continue"
        frames = server.request("stackTrace", {"threadId": thread_id}).body["stackFrames"]
        assert frames[0]["line"] == LOOP_LINE, frames[0]


def test_hil_serial_dap_carries_a_large_response(hil_serial_dap_session, record_property):
    """A response many times the CDC packet size arrives whole, and how fast.

    The risk register asks what the serial transport costs on real hardware:
    `StreamTransport.recv` reads the stream a byte at a time, which is cheap
    per call and expensive per kilobyte. The rate is recorded rather than
    merely bounded, so a later change can be compared against a number from a
    board instead of from a desktop pty.
    """
    with attached(hil_serial_dap_session) as server:
        run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)

        started = time.monotonic()
        response = server.request(
            "evaluate",
            {"expression": f"'A' * {_PAYLOAD_SIZE}", "frameId": 0, "context": "watch"},
            timeout=120,
        )
        elapsed = time.monotonic() - started

        assert response.success, response
        assert response.body["result"] == "A" * _PAYLOAD_SIZE, len(response.body["result"])

        rate = _PAYLOAD_SIZE / elapsed
        record_property("serial_dap_bytes_per_second", round(rate))
        record_property("serial_dap_payload_seconds", round(elapsed, 3))
        assert rate > _MIN_BYTES_PER_SECOND, f"{rate:.0f} B/s for {_PAYLOAD_SIZE} bytes"

        # The link is still in step afterwards: a short write would truncate
        # the frame rather than fail, and only the next message would notice.
        assert server.request("threads").body["threads"], "session unusable after a large response"
