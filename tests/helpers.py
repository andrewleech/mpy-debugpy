import sys
import time
from pathlib import Path

from dap import ThreadedServer

_MPREMOTE_DIR = str(Path(__file__).resolve().parents[1] / "micropython" / "tools" / "mpremote")
if _MPREMOTE_DIR not in sys.path:
    sys.path.insert(0, _MPREMOTE_DIR)


def debug_args(**overrides):
    """The argument namespace `mpremote debug` builds, with `overrides` applied.

    Taken from the command's own parser rather than hand-rolled, so every
    option `do_debug` reads is present with the default a real invocation
    would give it. A hand-rolled stand-in only carries the options that
    existed when it was written, and adding an option to the command then
    breaks every test that built one.

    An override naming something the parser does not define is an error: the
    parser is the whole vocabulary, so a misspelt name is a test setting
    nothing and asserting against the default.
    """
    from mpremote.main import argparse_debug

    args = argparse_debug().parse_args([])
    for name, value in overrides.items():
        if not hasattr(args, name):
            raise AttributeError(f"'mpremote debug' has no argument {name!r}")
        setattr(args, name, value)
    return args


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


def wait_for_msg(server, *, event="", response="", timeout=5):
    """Wait for a specific message; return it, or None if it never arrives.

    Callers use what the wait matched rather than re-reading
    `rcv_messages[-1]` and racing an event that landed after it.

    A wait naming neither an event nor a response is a `ValueError`, not a wait
    for anything that arrives: a success value that does not say what was found
    makes `assert wait_for_msg(...)` pass on a timeout.

    Matching still requires the awaited message to be the *most recent* one, so
    a test triggering the same event or response twice must `clear_messages()`
    in between; `take_msg` is the cursor-based alternative. Making the match
    order-insensitive is the open harness question in the risk register - two
    attempts measured no better than this, so it is not folded in here.
    """
    if not event and not response:
        raise ValueError("wait_for_msg needs event or response")
    t1 = time.time()
    server.run_single()
    while not server.rcv_messages and time.time() - t1 < timeout:
        time.sleep(0.1)
        server.run_single()
    if event:
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
    if not server.rcv_messages:
        return None
    last = server.rcv_messages[-1]
    matched = (
        last.type == "event" and last.event == event
        if event
        else last.type == "response" and last.command == response
    )
    return last if matched else None


def set_breakpoints(
    server: PerfServer,
    source_file: str,
    bp_lines: list[int],
    wait=False,
):
    """Send a `setBreakpoints` request naming `bp_lines` as the whole set for
    `source_file`.

    Returns the response only when `wait` is set, and None if it never arrived;
    a fire-and-forget call returns None too, so `assert set_breakpoints(...)`
    cannot pass on a request nobody confirmed.
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

    if not wait:
        return None
    return wait_for_msg(server, response="setBreakpoints")
