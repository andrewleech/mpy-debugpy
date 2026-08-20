"""STORY-6.7 on real hardware: DAP over the one stream that carries the REPL.

The channel a board with one UART and no network has. Everything the host
suite (`tests/test_s6_7_repl_dap.py`) can prove is proved there, against a pty
and a real unix interpreter; three things it cannot are proved here and only
here:

- that the REPL really is the whole of `dupterm` slot 1 on this board class, so
  installing the framing wrapper there diverts program output rather than
  losing it beside a console the runtime writes to directly;
- that the wrapper is given back at the end of a session, since a board left
  with it installed answers a plain REPL in framed bytes and only a reset
  clears that;
- what the channel costs, which is criterion 4 and is a number, not a claim.

Run against the bench board as it boots. There is one bench arrangement now,
so nothing gates these on a USB mode: a second CDC interface is not built,
not enumerated and not supported.
"""

import contextlib
import ipaddress
import json
import os
import re
import subprocess
import threading
import time

import pytest
from conftest import BAUDRATE
from dapdrive import attached, run_to_breakpoint
from debuggee import DEVICE_SOURCE_PATH, LOOP_LINE, TARGET_MODULE
from mpremote_debug import (
    SUBMODULE_DIR as _SUBMODULE_DIR,
    read_until as _read_until,
    spawn_debug as _spawn_debug,
)

# The device's own report of where it is listening, which for any stream
# channel is "serial:0" - it bound nothing. Echoed through mpremote before the
# handshake line it consumes, so it arrives here demultiplexed out of the same
# stream the DAP frames are on, which is itself part of what is being checked.
_DEVICE_ENDPOINT_RE = re.compile(r"^Debug server listening on (\S+)\s*$", re.M)

# Byte sequences a program can print that the framing has to survive: a bare
# marker, a doubled one, and a marker followed by a plausible frame header
# (code 14, length 64 little-endian). The third is the one a content heuristic
# would take for a DAP frame and swallow 64 bytes of console output behind.
_MARKER = 0x18
_DAP_CODE = 14
_ADVERSARIAL = bytes((_MARKER, _MARKER, _MARKER, _MARKER, _DAP_CODE, 64, 0))
# Trailing bytes so a demux that did read the header above as a frame would be
# left short of its payload rather than merely early, which is visible as the
# console text never arriving at all.
_ADVERSARIAL_TAIL = b"marker-survived"

# Large enough that the response crosses the link in many reads rather than
# one, and the same size STORY-6.1 measured the second-CDC channel with, so the
# two numbers are comparable.
_PAYLOAD_SIZE = 16384

# The floor the measurement has to clear. Below what a working link achieves
# and far above what a per-byte round trip would, as in the serial-DAP suite;
# this channel pays framing and credit-window acks on top, so it is not the
# same number.
_MIN_BYTES_PER_SECOND = 1000


class _CommandOutput:
    """Everything mpremote printed after the handshake, as raw bytes.

    On this channel the board's stdout *is* mpremote's stdout: the device
    writes program output into the framing wrapper, the host demultiplexes it
    back out and writes it on, so a `print()` on the board lands here. Bytes
    rather than text, because the thing being checked is which bytes survived.

    Reads the raw fd for the same reason `read_until` does - a buffered reader
    would strand already-flushed output in Python-land where nothing sees it.
    """

    def __init__(self, proc):
        self._proc = proc
        self._chunks = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        fd = self._proc.stdout.fileno()
        while not self._stop.is_set():
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                return
            if chunk == b"":
                return
            with self._lock:
                self._chunks.append(chunk)

    def data(self):
        with self._lock:
            return b"".join(self._chunks)

    def text(self):
        return self.data().decode("utf-8", "replace")

    def wait_for(self, needle, timeout):
        deadline = time.monotonic() + timeout
        while needle not in self.data():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)


@pytest.fixture()
def hil_repl_dap_runner(hil_device, hil_facts, tmp_path):
    """Call to start a `mpremote debug <target>` run with DAP on the REPL stream.

    `dap_repl` reaches the command through an `mpdebug.toml` rather than the
    `--dap-repl` flag: the file is the form a user of a single-UART board
    keeps, and it is the one that also has to survive config validation. It is
    written per test from the environment, so nothing about the bench is
    committed.

    Like the second-CDC runs and unlike the network ones, mpremote *is* the
    DAP endpoint here, so the process outlives the handshake and is reaped by
    this fixture. It also holds the board's only port for the whole session,
    which is why nothing here opens a second reader on it - the board's stdout
    arrives through mpremote instead, via `_CommandOutput`.
    """
    (tmp_path / "mpdebug.toml").write_text(
        "[target.hil]\n"
        'kind = "serial"\n'
        f'device = "{hil_device}"\n'
        "dap_repl = true\n"
        f'program = "{TARGET_MODULE}:main"\n'
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_SUBMODULE_DIR), env.get("PYTHONPATH")]))
    runs = []

    def _run(timeout=60):
        proc = _spawn_debug(["debug", "hil"], env=env, cwd=tmp_path)
        lines, matched = _read_until(proc, "MPDBG-READY ", timeout=timeout, at_line_start=True)
        if matched is None:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"never saw MPDBG-READY; output:\n{''.join(lines)}")
        payload = json.loads(matched[matched.index("{") :])
        payload["command_output"] = "".join(lines)
        payload["process"] = proc
        # `dapdrive` reaches for `device` when a wait ends in silence, and on
        # this channel that is where the board's own traceback would be.
        payload["device"] = _CommandOutput(proc)
        runs.append(payload)
        return payload

    try:
        yield _run
    finally:
        for payload in runs:
            proc = payload["process"]
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
                proc.wait(timeout=5)
            payload["device"].close()


@pytest.fixture()
def hil_repl_dap_session(hil_repl_dap_runner):
    """One REPL-stream run, for the tests that only need one."""
    return hil_repl_dap_runner()


def test_hil_repl_dap_takes_the_stream_it_was_launched_over(hil_repl_dap_session, hil_facts):
    """No port bound, no second interface in play, and the probe says which.

    Four claims, because no one of them rules out a session that had quietly
    gone somewhere else: the device reported a stream rather than an address,
    the board it ran on had only the one interface to offer, the handshake
    names this channel specifically, and the endpoint the client is given
    belongs to this host.

    `serial_dap` is true alongside `repl_dap` and that is not a contradiction -
    a shared stream is still a stream, and everything downstream of
    `listen_stream` cannot tell the two apart. `repl_dap` is the key that says
    which one it was.
    """
    session = hil_repl_dap_session

    endpoints = _DEVICE_ENDPOINT_RE.findall(session["command_output"])
    assert endpoints == ["serial:0"], session["command_output"]

    assert session["caps"]["repl_dap"] is True, session["caps"]

    # The board really has no second interface to have taken instead: not
    # enumerated, and not built either since the second-CDC path was removed.
    usb_mode = hil_facts["usb_mode"]
    assert not (usb_mode and "xVCP" in usb_mode), (
        f"the board enumerated a second interface (usb_mode {usb_mode!r}), so this "
        "run does not show what a single-UART board can do"
    )

    assert session["host"] == "127.0.0.1", session
    assert isinstance(session["port"], int) and session["port"] > 0, session
    assert ipaddress.ip_address(session["host"]).is_loopback, session


def test_hil_repl_dap_reaches_a_breakpoint(hil_repl_dap_session):
    """Criterion 1: a board with one UART stops at a breakpoint over that UART.

    Driven exactly as the network and second-CDC scenarios drive their
    sessions - the client attaches to a loopback port and cannot tell which
    channel is behind it, which is the point of putting the bridge behind a
    socket. What differs is only what that socket is bridged to.
    """
    with attached(hil_repl_dap_session) as server:
        stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
        assert stopped.body["reason"] == "breakpoint", stopped
        thread_id = stopped.body["threadId"]

        frames = server.request("stackTrace", {"threadId": thread_id}).body["stackFrames"]
        assert (frames[0]["name"], frames[0]["line"]) == ("main", LOOP_LINE), frames[0]
        assert frames[0]["source"]["path"] == DEVICE_SOURCE_PATH, frames[0]

        # Resuming works over the shared stream too, and lands on the loop
        # header again (see the network suite for why a header stops on every
        # pass).
        since = server.mark()
        server.request("continue", {"threadId": thread_id})
        again = server.wait_event("stopped", timeout=60, since=since)
        assert again is not None, "no second stop after continue"
        frames = server.request("stackTrace", {"threadId": thread_id}).body["stackFrames"]
        assert frames[0]["line"] == LOOP_LINE, frames[0]


def test_hil_program_output_carrying_the_marker_arrives_intact(hil_repl_dap_session):
    """Criteria 2 and 3, on the wire that actually has the problem.

    The program's stdout and the DAP channel share one stream, and the marker
    that separates them is an ordinary byte a program may print. Written from
    inside the debuggee's own frame while it is stopped, so the bytes are
    interleaved with DAP traffic rather than sent on an idle link, which is
    the case a desync would show up in.

    That the fake frame header is *not* read as a frame is criterion 3: a
    demux keying on content would take `0x18 0x0e 0x40 0x00` for a 64-byte DAP
    frame and swallow the console text behind it, so the text arriving is what
    proves the rule applied was structural.

    Escaping is asserted to be invisible: what the program wrote is what
    reaches mpremote's stdout, byte for byte, with no doubled marker left in
    it.
    """
    session = hil_repl_dap_session
    printed = _ADVERSARIAL + _ADVERSARIAL_TAIL
    with attached(session) as server:
        stopped = run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)
        thread_id = stopped.body["threadId"]

        expression = "sys.stdout.write({!r})".format(printed.decode("latin-1"))
        response = server.request("evaluate", {"expression": expression, "frameId": 0, "context": "watch"})
        assert response.success, response

        assert session["device"].wait_for(printed, timeout=20), (
            "the marker-bearing output never arrived whole; mpremote's stdout "
            f"since the handshake was {session['device'].data()!r}"
        )

        # Still in step afterwards, which a lost or half-read frame would not
        # be: the next request is answered rather than the link stalling on a
        # body that never comes.
        frames = server.request("stackTrace", {"threadId": thread_id}).body["stackFrames"]
        assert frames[0]["line"] == LOOP_LINE, frames[0]


def test_hil_repl_dap_carries_a_large_response(hil_repl_dap_session, record_property):
    """Criterion 4: what the shared stream costs, measured rather than claimed.

    The same payload the second-CDC channel was measured with, so the two
    numbers stand beside each other and the difference is what the framing and
    the credit window cost. Recorded into the run's results record, which is
    where `docs/debugging.md` takes the published figure from.
    """
    with attached(hil_repl_dap_session) as server:
        run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)

        started = time.monotonic()
        response = server.request(
            "evaluate",
            {"expression": f"'A' * {_PAYLOAD_SIZE}", "frameId": 0, "context": "watch"},
            timeout=180,
        )
        elapsed = time.monotonic() - started

        assert response.success, response
        assert response.body["result"] == "A" * _PAYLOAD_SIZE, len(response.body["result"])

        rate = _PAYLOAD_SIZE / elapsed
        record_property("repl_dap_bytes_per_second", round(rate))
        record_property("repl_dap_payload_seconds", round(elapsed, 3))
        assert rate > _MIN_BYTES_PER_SECOND, f"{rate:.0f} B/s for {_PAYLOAD_SIZE} bytes"

        assert server.request("threads").body["threads"], "session unusable after a large response"


def test_hil_the_repl_comes_back_after_the_session(hil_repl_dap_runner, hil_device):
    """The wrapper is given back, so the board is usable without a power cycle.

    The failure this rules out is the expensive one. A framing wrapper left in
    `dupterm` slot 1 answers every later REPL in framed bytes, and while it is
    installed the interface is detached from the REPL, so Ctrl-C is ordinary
    data and cannot break out of anything - on a board reached only over this
    one UART, the debugger would have taken away the way to fix it.

    Its own run, and its own connection taken after mpremote has exited and
    let go of the port. The board has to notice the host left for any of this
    to happen: DTR drops when the last opener goes, which is the only
    end-of-session signal a USB CDC stream has.

    No soft reset in the check, which is why it uses `enter_raw_repl` directly
    rather than `hil_serial`. A reset re-runs `pyb_usb_vcp_init0` and would put
    the REPL back in the slot by itself, so a board that never released it
    would pass. Ctrl-C followed by a raw-REPL handshake is the narrowest thing
    that only a released stream can answer.
    """
    from mpremote.transport_serial import SerialTransport

    session = hil_repl_dap_runner()
    with attached(session) as server:
        run_to_breakpoint(server, DEVICE_SOURCE_PATH, LOOP_LINE)

    proc = session["process"]
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("mpremote did not exit after being asked to end the session")

    # The target resumes when its peer goes and has a few seconds of work left
    # before the boot script returns, so the handshake is retried to a deadline
    # rather than demanded at the first attempt.
    deadline = time.monotonic() + 60
    while True:
        transport = SerialTransport(hil_device, baudrate=BAUDRATE)
        try:
            transport.enter_raw_repl(soft_reset=False)
            assert transport.eval("1 + 1") == 2
            transport.exit_raw_repl()
            return
        except Exception as er:
            if time.monotonic() >= deadline:
                pytest.fail(
                    "the board never answered a plain raw-REPL handshake after the "
                    f"session; the framing wrapper was not given back. Last error: {er!r}"
                )
            time.sleep(1)
        finally:
            with contextlib.suppress(Exception):
                transport.close()
