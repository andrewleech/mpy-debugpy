import time
from pathlib import Path

from dap import ThreadedServer


def firmware_variant(id, port="unix", board="standard", deprecated=False, sha256="0" * 64, url="", **caps):
    """Build a `firmware.toml` `[[variant]]`-shaped dict for `launcher/firmware.py` tests."""
    return {
        "id": id,
        "port": port,
        "board": board,
        "deprecated": deprecated,
        "artifact": f"{id}/micropython",
        "artifact_sha256": sha256,
        "download_url": url,
        "capabilities": caps,
    }


class PerfServer(ThreadedServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rcv_messages: list[dict] = []  # Instance variable, not class variable
        self.cursor = 0  # how far take_msg() has consumed

    def handle_message(self, message):
        """Handle a message from the client or adapter."""
        self.rcv_messages.append(message)
        print("Message received:", message)

    def clear_messages(self):
        """Clear all received messages."""
        self.rcv_messages.clear()
        self.cursor = 0


def take_msg(server, *, event="", response="", match=None, timeout=15):
    """Consume messages in arrival order until one matches; return it, or None.

    `wait_for_msg` requires the awaited message to be the *most recent* one, so
    anything that lands while the test is looking elsewhere is missed and the
    same event cannot be awaited twice without a `clear_messages()` in between.
    This walks a cursor forward instead: every message is offered to the match
    exactly once, in the order it arrived, whenever the test gets round to
    asking. A session that stops several times per run needs that.

    Messages the cursor passes are consumed, so a later call never rematches an
    earlier message - which is what makes "the next stop after this continue"
    expressible at all.

    `match` is a predicate on the message, for anything `event`/`response`
    cannot express (a particular event body, say, or either of two events).
    """
    if match is None:
        if event:

            def match(msg):
                return msg.type == "event" and msg.event == event

        elif response:

            def match(msg):
                return msg.type == "response" and msg.command == response

        else:
            raise ValueError("take_msg needs one of event, response or match")

    deadline = time.time() + timeout
    while True:
        while server.cursor < len(server.rcv_messages):
            message = server.rcv_messages[server.cursor]
            server.cursor += 1
            if match(message):
                return message
        if time.time() > deadline:
            return None
        time.sleep(0.02)


def drain_lines(stream, sink):
    """Background-thread target: append each decoded line from `stream` to `sink`.

    `readline()` blocks until either a full line or EOF, so this needs its own
    thread per stream - the alternative, polling both of a subprocess's pipes
    non-blockingly from one thread, is exactly what this avoids having to write
    twice for stdout and stderr.
    """
    for line in iter(stream.readline, ""):
        sink.append(line)
    stream.close()


def wait_for_prefixed_line(sink, prefix, timeout):
    """Poll `sink` (grown by `drain_lines` on another thread) for a line starting with `prefix`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in sink:
            if line.startswith(prefix):
                return line
        time.sleep(0.05)
    return None


def wait_for_msg(server, *, count=0, event="", response="", timeout=5):
    """Wait for a specific message, or number of messages, to be received.

    Returns the matching message for an `event`/`response` wait (None if it
    never arrives), so callers can use what the wait matched rather than
    re-reading `rcv_messages[-1]` and racing an event that landed after it.
    Count mode still returns a bool.

    Matching itself is unchanged: this still waits for the awaited message to
    be the most recent one, so a test triggering the same event or response
    twice must `clear_messages()` in between. Making the match order-insensitive
    is the open harness question in the risk register - two attempts measured
    no better than this, so it is not folded in here.
    """
    t1 = time.time()
    server.run_single()
    while not server.rcv_messages and time.time() - t1 < timeout:
        time.sleep(0.1)
        server.run_single()
    if count > 0:
        while time.time() - t1 < timeout and len(server.rcv_messages) < count:
            time.sleep(0.1)
            server.run_single()
    elif event:
        while time.time() - t1 < timeout and not (
            server.rcv_messages[-1].type == "event" and server.rcv_messages[-1].event == event
        ):
            time.sleep(0.1)
            server.run_single()
    elif response:
        while time.time() - t1 < timeout and not (
            server.rcv_messages[-1].type == "response" and server.rcv_messages[-1].command == response
        ):
            time.sleep(0.1)
            server.run_single()
    if event or response:
        if not server.rcv_messages:
            return None
        last = server.rcv_messages[-1]
        matched = (
            last.type == "event" and last.event == event
            if event
            else last.type == "response" and last.command == response
        )
        return last if matched else None
    return len(server.rcv_messages) >= count


def set_breakpoints(
    server: PerfServer,
    source_file: str,
    bp_lines: list[int],
    wait=False,
):
    """Set breakpoints in the debug server.
    This fixture uses the attach_server fixture to set
    breakpoints in the debug server.
    It can be parameterized with:
    - source_file: The source file to set breakpoints in.
    - bp_lines: The lines to set breakpoints on.
    """
    client = server.client

    #     interface SetBreakpointsArguments {
    #   /**
    #    * The source location of the breakpoints; either `source.path` or
    #    * `source.sourceReference` must be specified.
    #    */
    #   source: Source;

    #   /**
    #    * The code locations of the breakpoints.
    #    */
    #   breakpoints?: SourceBreakpoint[];

    #   /**
    #    * Deprecated: The code locations of the breakpoints.
    #    */
    #   lines?: number[];

    #   /**
    #    * A value of true indicates that the underlying source has been modified
    #    * which results in new breakpoint locations.
    #    */
    #   sourceModified?: boolean;
    # }

    # {
    #   "source": {
    #     "name": "target.py",
    #     "path": "<repo-root>/src/target.py"
    #   },
    #   "lines": [
    #     79,
    #     81,
    #     86
    #   ],
    #   "breakpoints": [
    #     {
    #       "line": 79
    #     },
    #     {
    #       "line": 81
    #     },
    #     {
    #       "line": 86
    #     }
    #   ],
    #   "sourceModified": false
    # }

    breakpoints = [{"line": line} for line in bp_lines]
    client.set_breakpoints(
        source={"name": Path(source_file).name, "path": source_file},
        breakpoints=breakpoints,
        # lines=bp_lines,   # Deprecated
        source_modified=False,
    )

    # Wait for the setBreakpoints response
    if not wait:
        return True
    return wait_for_msg(server, response="setBreakpoints")
