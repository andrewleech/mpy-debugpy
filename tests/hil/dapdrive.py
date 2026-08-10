"""Driving a DAP session where the peer is a board, not a subprocess.

`helpers.wait_for_msg` matches only the *most recent* message, so a test has
to clear the message list between waits and never lets two messages be in
flight at once. That is workable against the unix port, where a request and
its response are microseconds apart; over WiFi to a board it is not, because
events (`stopped`, `output`) arrive interleaved with responses and each one
overwrites what the next wait would match.

The waits here scan from a cursor taken before the request was sent, so what
a wait matches never depends on what arrived after it, and nothing has to be
cleared.
"""

import contextlib
import threading
import time

from dap import ThreadedServer


class Recorder(ThreadedServer):
    """A DAP client that keeps every message and matches them by content."""

    def __init__(self, *args, session=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rcv_messages = []
        self._lock = threading.Lock()
        self._session = session or {}

    def silence_note(self):
        """What the board itself has to say, for a wait that ended in silence.

        A DAP timeout reports only that nothing came back, which is the same
        text whether the board is wedged, rebooted, or raised inside the
        handler. The board's own stdout separates them: a MicroPython
        traceback goes to the stream `DeviceOutput` is already reading for
        the length of the test, so asking costs nothing and it is the only
        place the answer can be. A `--dap-log` run adds where the transcript
        of the same session landed.
        """
        parts = []
        device = self._session.get("device")
        if device is not None:
            printed = device.text().strip()
            parts.append("--- board stdout during the session ---\n" + (printed or "(nothing)"))
        log_path = self._session.get("dap_log")
        if log_path is not None:
            parts.append(f"--- DAP transcript ---\n{log_path}")
        return ("\n" + "\n".join(parts)) if parts else ""

    def handle_message(self, message):
        with self._lock:
            self.rcv_messages.append(message)

    def mark(self):
        """A cursor into the message log: take one before sending."""
        with self._lock:
            return len(self.rcv_messages)

    def messages_since(self, cursor):
        """Every message logged after `cursor`, as a snapshot."""
        with self._lock:
            return list(self.rcv_messages[cursor:])

    def events_since(self, event, cursor):
        return [m for m in self.messages_since(cursor) if m.type == "event" and m.event == event]

    def wait(self, predicate, timeout=20, since=0):
        """The first message at or after `since` that matches, or None."""
        deadline = time.monotonic() + timeout
        while True:
            for message in self.messages_since(since):
                if predicate(message):
                    return message
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def request(self, command, args=None, timeout=20):
        """Send a request and return its response, matched by request_seq.

        Matching on the sequence number rather than the command name means a
        retry, or two requests of the same kind in flight, cannot be confused
        for one another.
        """
        since = self.mark()
        seq = self.client.send_request(command, args)
        response = self.wait(lambda m: m.type == "response" and m.request_seq == seq, timeout, since)
        if response is None:
            raise AssertionError(
                "no response to {} (seq {}) within {}s; received since: {}{}".format(
                    command, seq, timeout, self.messages_since(since), self.silence_note()
                )
            )
        return response

    def wait_event(self, event, timeout=20, since=0):
        return self.wait(lambda m: m.type == "event" and m.event == event, timeout, since)


@contextlib.contextmanager
def attached(session, name="hil", timeout=20):
    """Connect to the endpoint the board reported and complete `initialize`."""
    server = Recorder(
        name,
        session["host"],
        session["port"],
        connect_timeout=timeout,
        session=session,
    )
    server.start()  # sends `initialize`, as VS Code does
    try:
        if server.wait(lambda m: m.type == "response" and m.command == "initialize", timeout) is None:
            raise AssertionError(
                "no initialize response from {}:{}{}".format(
                    session["host"], session["port"], server.silence_note()
                )
            )
        yield server
    finally:
        server.stop()


def set_breakpoints(server, source_path, lines):
    """Replace the breakpoint set for one file; returns the verified lines.

    DAP has no "remove breakpoint" request: a `setBreakpoints` call is the
    whole set for that source, so an empty list is how a file is cleared.
    """
    response = server.request(
        "setBreakpoints",
        {
            "source": {"name": source_path.rsplit("/", 1)[-1], "path": source_path},
            "breakpoints": [{"line": line} for line in lines],
            "sourceModified": False,
        },
    )
    verified = [bp.get("line") for bp in response.body.get("breakpoints", [])]
    assert verified == list(lines), response
    return verified


def run_to_breakpoint(server, source_path, line, timeout=60):
    """Set one breakpoint, finish configuration, and wait for the stop.

    Returns the `stopped` event. The breakpoint is verified against the
    board's own response rather than assumed: a path the device cannot match
    is accepted as a request and simply never fires, which without this check
    would show up only as a timeout on the wrong assertion.
    """
    set_breakpoints(server, source_path, [line])

    since = server.mark()
    server.request("configurationDone", {})
    stopped = server.wait_event("stopped", timeout=timeout, since=since)
    assert stopped is not None, "target never stopped at {}:{}{}".format(
        source_path, line, server.silence_note()
    )
    return stopped
