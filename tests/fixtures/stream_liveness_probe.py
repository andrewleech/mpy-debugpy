"""Device-side self-test for `StreamTransport`'s peer-gone signal.

Runs inside a MicroPython (unix port) subprocess against a pair of host-
supplied pipe fds, both of whose far ends the host holds open for the whole
run. That is deliberate: a pipe nobody has closed never reaches EOF, which
is the only property of a USB CDC interface this needs to reproduce. The
host-gone signal a CDC interface has instead is a callable
(`pyb.USB_VCP.isconnected()` on stm32), and what is under test is when
`StreamTransport` is allowed to believe it.

Usage: stream_liveness_probe.py <read-fd> <write-fd>

Prints one `OK:<step>` line per step to stdout as it passes, or a single
`FAIL:<reason>` line and exits before printing further steps.
"""

import sys
import time


class _Link:
    """A host-holds-the-port signal this probe can raise and drop at will."""

    def __init__(self):
        self.up = False
        self._drop_at = None

    def drop_after_ms(self, ms):
        """Drop the line `ms` from now, from inside whatever asks next.

        The only way to change the signal underneath a call that is blocking
        on it, which is the case that matters: a transport waiting with no
        timeout has nothing but this line to end the wait.
        """
        self._drop_at = time.ticks_add(time.ticks_ms(), ms)

    def is_up(self):
        if self._drop_at is not None and time.ticks_diff(time.ticks_ms(), self._drop_at) >= 0:
            self.up = False
        return self.up


def _run():
    from debugpy.common.stream_transport import StreamTransport

    reader = open(int(sys.argv[1]), "rb")
    writer = open(int(sys.argv[2]), "wb")
    link = _Link()
    transport = StreamTransport(reader, writer, link.is_up)

    # Down and nothing has ever been received: the state between
    # `listen_stream()` and the client's first connect, and also what a host
    # that opens the port briefly just to check it exists leaves behind. Read
    # as "gone" it would end the session before it began, so it must be
    # indistinguishable from idle.
    transport.settimeout(0.2)
    try:
        transport.recv(16)
    except OSError as er:
        if er.args[0] != 11:
            print("FAIL:quiet-wrong-errno:{!r}".format(er.args))
            return
    else:
        print("FAIL:quiet-before-traffic-read-as-gone")
        return
    print("OK:quiet-before-traffic")

    # The host attaches and sends; blocking until it does, since the write is
    # what the host does on seeing the line above.
    link.up = True
    transport.settimeout(None)
    data = transport.recv(16)
    if data != b"HELLO":
        print("FAIL:recv-mismatch:{!r}".format(data))
        return
    print("OK:traffic")

    # Connected and simply not saying anything. A session sits here for as
    # long as the user takes to press continue.
    transport.settimeout(0.2)
    try:
        transport.recv(16)
    except OSError as er:
        if er.args[0] != 11:
            print("FAIL:idle-wrong-errno:{!r}".format(er.args))
            return
    else:
        print("FAIL:idle-read-as-gone")
        return
    print("OK:idle-is-not-gone")

    # Down after the two ends were talking: the host has left. This has to be
    # answered from the signal rather than by waiting out the timeout, because
    # the timeout a stopped session waits under is unbounded - answering late
    # here is answering never there.
    link.up = False
    transport.settimeout(5.0)
    start = time.ticks_ms()
    eof = transport.recv(16)
    elapsed = time.ticks_diff(time.ticks_ms(), start)
    if eof != b"":
        print("FAIL:peer-gone-not-eof:{!r}".format(eof))
        return
    if elapsed > 1000:
        print("FAIL:peer-gone-waited:{}".format(elapsed))
        return
    print("OK:peer-gone:{}".format(elapsed))

    # Sticky, and not because the signal stayed down: a host that comes back
    # is a new session, not a continuation of the one whose framing state
    # both ends have already lost.
    link.up = True
    again = transport.recv(16)
    if again != b"":
        print("FAIL:peer-gone-not-sticky:{!r}".format(again))
        return
    print("OK:peer-gone-sticky")

    # A stream with no such signal at all - every port but stm32 today - is
    # left exactly as it was: EAGAIN forever on a channel that cannot reach
    # EOF, since inventing a disconnect for it would end healthy sessions.
    plain = StreamTransport(reader, writer)
    plain.settimeout(0.2)
    try:
        plain.recv(16)
    except OSError as er:
        if er.args[0] != 11:
            print("FAIL:no-signal-wrong-errno:{!r}".format(er.args))
            return
    else:
        print("FAIL:no-signal-read-as-gone")
        return
    print("OK:no-signal-no-eof")

    # A channel whose host was already holding it when the transport was
    # built: DAP sharing the stream the host is driving the board over. No
    # byte ever crosses it, so nothing arms the signal the way traffic does
    # above, and the wait is the unbounded one the accept/initialize step
    # does - the host leaves without the client it launched ever attaching.
    # Getting this wrong on a shared stream costs the console: the wait never
    # ends, so the stream is never given back.
    held = _Link()
    held.up = True
    shared = StreamTransport(reader, writer, held.is_up)
    shared.settimeout(None)
    held.drop_after_ms(300)
    start = time.ticks_ms()
    eof = shared.recv(16)
    elapsed = time.ticks_diff(time.ticks_ms(), start)
    if eof != b"":
        print("FAIL:held-not-eof:{!r}".format(eof))
        return
    if elapsed > 3000:
        print("FAIL:held-waited:{}".format(elapsed))
        return
    print("OK:held-before-traffic:{}".format(elapsed))


if __name__ == "__main__":
    _run()
