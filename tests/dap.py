"""Minimal fake-VS-Code DAP test client for the host-side pytest harness.

The original private `dap` test dependency this harness was written against
is not available in this environment. This module reimplements the small
slice of its API the fixtures and tests actually use (`ThreadedServer`, plus
a request-sending `client`) directly against the wire format the debugpy
server speaks (`debugpy/common/messaging.py`: `Content-Length: N\\r\\n\\r\\n`
framed JSON), so the existing test suite is runnable without depending on
that package.

Despite the name (kept for source compatibility with `helpers.PerfServer`),
`ThreadedServer` is the DAP *client* side of the connection: like VS Code, it
connects out to the debugpy server's already-listening socket, sends
`initialize` immediately, and drains incoming messages on a background
thread so tests can poll `rcv_messages` without blocking.
"""

import json
import socket
import threading


class Message:
    """Attribute-style view over a decoded DAP message dict."""

    def __init__(self, data):
        self.raw = data
        self.type = data.get("type")
        self.command = data.get("command")
        self.event = data.get("event")
        self.body = data.get("body", {})
        self.seq = data.get("seq")
        self.request_seq = data.get("request_seq")
        self.success = data.get("success", True)
        self.message = data.get("message")

    def __repr__(self):
        return f"Message({self.raw!r})"


class DAPClient:
    """Sends DAP requests over a connected socket."""

    def __init__(self, sock):
        self._sock = sock
        self._lock = threading.Lock()
        self.seq = 0

    def _send(self, message):
        content = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(content)}\r\n\r\n".encode("utf-8")
        with self._lock:
            self._sock.sendall(header + content)

    def send_request(self, command, args=None):
        self.seq += 1
        message = {"seq": self.seq, "type": "request", "command": command}
        if args:
            message["arguments"] = args
        self._send(message)
        return self.seq

    def set_breakpoints(self, source, breakpoints, source_modified=False):
        return self.send_request(
            "setBreakpoints",
            {
                "source": source,
                "breakpoints": breakpoints,
                "sourceModified": source_modified,
            },
        )

    def continue_(self, thread_id=1):
        return self.send_request("continue", {"threadId": thread_id})

    def configuration_done(self):
        return self.send_request("configurationDone", {})

    def evaluate(self, expression, frame_id=0, context="watch"):
        """Send a DAP `evaluate` request with an explicit `context`.

        `context` is one of the DAP-defined values (`watch`, `hover`,
        `repl`, `clipboard`, ...); the debugpy server dispatches statement
        execution only for `repl`/`clipboard` (STORY-6.5).
        """
        return self.send_request(
            "evaluate",
            {"expression": expression, "frameId": frame_id, "context": context},
        )


class ThreadedServer:
    """Fake VS Code: connects to a debugpy listener and drives it over DAP."""

    def __init__(self, name, host, port, connect_timeout=10):
        self.name = name
        self.host = host
        self.port = port
        self.running = False
        self.rcv_messages = []
        self.client = None
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._connect_timeout = connect_timeout

    def clear_messages(self):
        self.rcv_messages = []

    def handle_message(self, message):
        """Overridable hook called with a `Message` for each received message."""

    def start(self):
        sock = socket.create_connection((self.host, self.port), timeout=self._connect_timeout)
        sock.settimeout(0.2)
        self._sock = sock
        self.client = DAPClient(sock)
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        # A real DAP client sends `initialize` as its first message.
        self.client.send_request(
            "initialize",
            {
                "clientID": self.name,
                "adapterID": "debugpy",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "pathFormat": "path",
            },
        )

    def run_single(self):
        """No-op: the background thread drains the socket continuously.

        Kept so call sites that poll with `run_single()` in a sleep loop work
        unmodified against this always-running implementation.
        """

    def stop(self):
        self._stop.set()
        self.running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _read_loop(self):
        buf = b""
        while not self._stop.is_set():
            try:
                data = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while True:
                sep = buf.find(b"\r\n\r\n")
                if sep < 0:
                    break
                header = buf[:sep].decode("utf-8", "replace")
                length = 0
                for line in header.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1].strip())
                if len(buf) < sep + 4 + length:
                    break
                body = buf[sep + 4 : sep + 4 + length]
                buf = buf[sep + 4 + length :]
                try:
                    data_dict = json.loads(body.decode("utf-8"))
                except ValueError:
                    continue
                self.handle_message(Message(data_dict))
        self.running = False
