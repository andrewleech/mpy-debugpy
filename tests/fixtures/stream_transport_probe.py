"""Device-side self-test for `debugpy.common.stream_transport.StreamTransport`.

Runs inside a MicroPython (unix port) subprocess against a pair of host-
supplied pipe fds (inherited, opened by number - the same technique
the boot script uses for a `dap_device` path). Exercises the socket-shaped
contract directly: `settimeout` governing both `recv` and `send`, `recv`'s
EAGAIN-vs-EOF distinction, and `send` reporting a short write rather than
claiming the whole buffer. The last step puts the real `JsonMessageChannel`
on top of the same narrow pipe to prove the framing survives being written
in several pieces under a timeout too short to finish in one.

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
    # cannot be taken by one write(). send() reports how much it took, the
    # way a socket does; a whole-buffer answer here would be a lie the caller
    # cannot detect, and losing the count is what desynchronises a frame.
    payload = bytes((i & 0xFF for i in range(20000)))
    transport.settimeout(0.5)
    first = transport.send(payload)
    if not 0 < first < len(payload):
        print("FAIL:send-not-partial:{}".format(first))
        return
    view = memoryview(payload)
    sent = first
    while sent < len(payload):
        try:
            sent += transport.send(view[sent:])
        except OSError as er:
            # EAGAIN is the writer having no room this instant, which is the
            # normal answer here and the caller's cue to try again - the same
            # thing `_send_all` does with it.
            if er.args[0] != 11:
                print("FAIL:send-errno:{!r}".format(er.args))
                return
    print("OK:send-partial:{}:{}".format(first, sent))

    # The same narrow pipe, now carrying a real DAP frame written by the
    # production channel under the 1 ms timeout `public_api.py` sets once a
    # session is live. Several writes are needed and most of them find no
    # room, so this fails unless every partial write is accounted for: a
    # resend from the wrong offset repeats a prefix, which is not a visible
    # error but a stream the peer can never parse again.
    from debugpy.common.messaging import JsonMessageChannel

    transport.settimeout(0.001)
    JsonMessageChannel(transport).send_message("event", "output", output="B" * 20000)
    print("OK:frame-issued")

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
