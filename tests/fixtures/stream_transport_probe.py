"""Device-side self-test for `debugpy.common.stream_transport.StreamTransport`.

Runs inside a MicroPython (unix port) subprocess against a pair of host-
supplied pipe fds (inherited, opened by number - the same technique
the boot script uses for a `dap_device` path). Exercises the socket-shaped
contract directly, independent of any DAP framing on top: `settimeout`
governing both `recv` and `send`, `recv`'s EAGAIN-vs-EOF distinction, and
`send` delivering a payload larger than one pipe write in full.

Usage: stream_transport_probe.py <read-fd> <write-fd>

Prints one `OK:<step>` line per step to stdout as it passes, or a single
`FAIL:<reason>` line and exits before printing further steps.
"""

import sys
import time


def _run():
    from debugpy.common.stream_transport import StreamTransport

    read_fd = int(sys.argv[1])
    write_fd = int(sys.argv[2])
    reader = open(read_fd, "rb")
    writer = open(write_fd, "wb")
    transport = StreamTransport(reader, writer)

    # settimeout() bounds recv(): no data arrives, so this must raise
    # OSError(11) (EAGAIN) once the timeout elapses, not block forever or
    # return an empty result silently.
    transport.settimeout(0.2)
    start = time.ticks_ms()
    try:
        transport.recv(16)
    except OSError as er:
        if er.args[0] != 11:
            print("FAIL:timeout-wrong-errno:{!r}".format(er.args))
            return
    else:
        print("FAIL:timeout-did-not-raise")
        return
    elapsed = time.ticks_diff(time.ticks_ms(), start)
    if elapsed < 150:
        print("FAIL:timeout-too-fast:{}".format(elapsed))
        return
    print("OK:timeout-raises")

    # settimeout(None) blocks until data actually arrives - the host writes
    # after this point.
    transport.settimeout(None)
    data = transport.recv(16)
    if data != b"HELLO":
        print("FAIL:recv-mismatch:{!r}".format(data))
        return
    print("OK:recv-blocking")

    # A payload bigger than the host has sized the pipe to (see the test)
    # cannot be delivered by one write() - send() must loop until it is all
    # accepted, not silently truncate it.
    payload = bytes((i & 0xFF for i in range(20000)))
    transport.send(payload)
    print("OK:send-issued:{}".format(len(payload)))

    # The host closes its write end next: recv() must report that as EOF
    # (b""), the same as a closed socket - not raise, and not block forever.
    transport.settimeout(2.0)
    eof = transport.recv(16)
    if eof != b"":
        print("FAIL:eof-mismatch:{!r}".format(eof))
        return
    print("OK:eof")


if __name__ == "__main__":
    _run()
