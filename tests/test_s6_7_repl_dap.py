"""Host-side pytest coverage for the DAP channel that shares the REPL stream (STORY-6.7).

The framing lives twice - `debugpy/common/repl_mux.py` on the device and
`mpremote/repl_dap.py` on the host - because `mpremote` cannot depend on a
`micropython-lib` package being installed on the machine it runs on.
`TestCrossImplementation` is what stops the two copies drifting: identical byte
streams go into both readers, in-process for the host copy and under the real
unix firmware for the device copy, and the results have to match.

`TestFraming` drives the reader against the streams a shared wire actually
produces - a marker arriving at the end of one read and its code at the start
of the next, program output that contains the marker, a frame longer than one
length byte can describe. `TestReplDapChannel` puts the host channel on a
loopback pty and plays the device by hand, which is where the credit window and
the end-of-session frame are observable.

`TestRealSession` runs the whole thing: `do_debug --dap-repl` against a real
`debugpy` session on the unix firmware, over one pty carrying the raw REPL, the
program's output and the DAP traffic at once, to a real breakpoint - with the
program deliberately printing the marker byte while the session is live
(STORY-6.7 criterion 2). What it cannot cover is the *installation*: the unix
port has no `os.dupterm`, so the boot script here hands the mux the stream
directly where the shipped one takes it out of dupterm slot 1. That the slot
really is the whole stdout path, and that it is given back afterwards, is
criterion 1 and needs the bench.

`TestReplDapRefusals` covers the combinations `do_debug` must reject before it
touches a device, and the config loader's own validation of `dap_repl`.
"""

import errno
import json
import os
import pty
import subprocess
import sys
import threading
import time
import tty
from pathlib import Path

import pytest
import serial

_TOP_DIR = Path(__file__).resolve().parents[1]
_SUBMODULE_DIR = _TOP_DIR / "micropython" / "tools" / "mpremote"

if str(_SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SUBMODULE_DIR))

from mpremote import commands  # noqa: E402
from mpremote import mpdebug_config  # noqa: E402
from mpremote import repl_dap  # noqa: E402
from mpremote.main import State  # noqa: E402
from mpremote.mpdebug_config import Target  # noqa: E402
from mpremote.transport_serial import SerialTransport  # noqa: E402

from helpers import PerfServer, debug_args, set_breakpoints, take_msg  # noqa: E402
from pty_device import PtyDevice  # noqa: E402

_MICROPYTHON = Path(
    os.environ.get(
        "MPY_DEBUG_FIRMWARE",
        _TOP_DIR / "micropython/ports/unix/build-standard/micropython",
    )
)

_MICROPYPATH = "{}:{}:{}".format(
    _TOP_DIR / "src",
    _TOP_DIR / "micropython-lib/python-ecosys/debugpy",
    _TOP_DIR / "micropython-lib",
)
_TARGET_PY = str(_TOP_DIR / "src" / "target.py")
_BREAKPOINT_LINE = 80  # src/target.py, main(): the `for` header, before the local is bound
# The first line of the span a deliberately oversized `setBreakpoints` names.
# Every line from here to `_BREAKPOINT_LINE` is asked for, which is enough
# entries to put the encoded request well past one frame and one credit window.
_LARGE_REQUEST_FIRST_LINE = 10

requires_unix_firmware = pytest.mark.skipif(
    not _MICROPYTHON.exists(),
    reason=f"unix firmware not built at {_MICROPYTHON} (see `make firmware-unix`)",
)


def _demux(*chunks):
    """A host `Demux` fed `chunks` in order, as separate reads."""
    d = repl_dap.Demux()
    for chunk in chunks:
        d.feed(chunk)
    return d


class TestFraming:
    """The host reader against the wire a shared stream really delivers."""

    def test_plain_bytes_pass_through_untouched(self):
        d = _demux(b"hello world\n")
        assert bytes(d.plain) == b"hello world\n"
        assert bytes(d.dap) == b""

    def test_marker_split_across_reads_resumes(self):
        """A read boundary anywhere inside a frame must not lose it.

        This is the normal case, not an edge one: a serial read returns
        whatever a USB packet happened to hold, which has no relationship to
        where frames begin and end.
        """
        wire = b"before" + repl_dap.frame(repl_dap.CMD_DAP, b"payload") + b"after"
        for split in range(1, len(wire)):
            d = _demux(wire[:split], wire[split:])
            assert bytes(d.plain) == b"beforeafter", f"split at {split}"
            assert bytes(d.dap) == b"payload", f"split at {split}"

    def test_program_output_containing_the_marker_stays_plain(self):
        """Criterion 2's unit half: escaped output is never read as framing.

        The bytes chosen are a frame header the reader would otherwise act on,
        so a missing escape shows up as a swallowed line rather than as a
        difference only a byte comparison would catch.
        """
        printed = b"\x18" + bytes((repl_dap.CMD_DAP, 5, 0)) + b"hello\x18\x18done\n"
        d = _demux(repl_dap.escape(printed))
        assert bytes(d.plain) == printed
        assert bytes(d.dap) == b""
        assert d.unknown_code is None

    def test_doubled_marker_is_one_literal(self):
        d = _demux(bytes((repl_dap.MARKER, repl_dap.MARKER)))
        assert bytes(d.plain) == bytes((repl_dap.MARKER,))

    def test_zero_length_frame_delivers(self):
        d = _demux(repl_dap.frame(repl_dap.CMD_DAP_EOF, b""))
        assert d.eof is True
        assert bytes(d.plain) == b""

    def test_payload_longer_than_one_length_byte(self):
        payload = bytes(range(256)) * 2
        d = _demux(repl_dap.frame(repl_dap.CMD_DAP, payload))
        assert bytes(d.dap) == payload

    def test_ack_accumulates_credit(self):
        d = _demux(
            repl_dap.frame(repl_dap.CMD_DAP_ACK, bytes((200, 1))),
            repl_dap.frame(repl_dap.CMD_DAP_ACK, bytes((1, 0))),
        )
        assert d.credited == 456 + 1

    def test_unknown_code_is_recorded_and_its_payload_skipped(self):
        """An unhandled code must not desync the reader on top of being reported.

        The length is explicit precisely so a reader can step over a message it
        cannot interpret; the report is what makes the disagreement visible.
        """
        d = _demux(
            repl_dap.frame(99, b"\x18\x18\x18"),
            repl_dap.frame(repl_dap.CMD_DAP, b"still parsed"),
        )
        assert d.unknown_code == 99
        assert bytes(d.dap) == b"still parsed"

    def test_only_the_first_unknown_code_is_kept(self):
        d = _demux(repl_dap.frame(99, b""), repl_dap.frame(98, b""))
        assert d.unknown_code == 99

    def test_mount_command_codes_are_reported_not_consumed(self):
        """Codes 1..13 belong to `mpremote mount`'s filesystem RPC.

        A mount is refused for the length of a REPL-stream DAP session, so one
        arriving means something else is talking on the wire - reported as a
        disagreement rather than quietly dropped.
        """
        d = _demux(repl_dap.frame(1, b""))
        assert d.unknown_code == 1


_CROSS_STREAMS = {
    "plain": b"just output\n",
    "escaped_marker": repl_dap.escape(b"\x18\x0e\x05\x00hello\n"),
    "dap_frame": b"out" + repl_dap.frame(repl_dap.CMD_DAP, b'{"seq":1}') + b"more",
    "long_payload": repl_dap.frame(repl_dap.CMD_DAP, bytes(range(256)) * 2),
    "ack": repl_dap.frame(repl_dap.CMD_DAP_ACK, bytes((0x40, 0x01))),
    "eof": b"tail" + repl_dap.frame(repl_dap.CMD_DAP_EOF, b""),
    "unknown": repl_dap.frame(77, b"xyz") + repl_dap.frame(repl_dap.CMD_DAP, b"after"),
    "interleaved": (
        b"a"
        + repl_dap.frame(repl_dap.CMD_DAP, b"one")
        + repl_dap.escape(b"b\x18c")
        + repl_dap.frame(repl_dap.CMD_DAP_ACK, bytes((10, 0)))
        + b"d"
    ),
}

# Read in three-byte reads as well as whole, so both readers have to carry
# their state across a boundary in the same places.
_CROSS_CHUNK = 3
# Each queue is emptied in two takes, a short one and then the rest. Reading
# `demux.plain`/`demux.dap` directly would compare the same bytes without ever
# calling the in-place consume behind `take_*`, and that consume is where the
# two implementations are least alike: the device's bytearray supports slice
# assignment but not slice deletion, so a host spelling that works here raises
# there. The short take leaves a tail to keep; the second empties the buffer.
_CROSS_TAKE = 2


def _read_with_host_demux(stream):
    d = repl_dap.Demux()
    for i in range(0, len(stream), _CROSS_CHUNK):
        d.feed(stream[i : i + _CROSS_CHUNK])
    head_plain = d.take_plain(_CROSS_TAKE)
    head_dap = d.take_dap(_CROSS_TAKE)
    return {
        "plain_head": head_plain.hex(),
        "plain_rest": d.take_plain(4096).hex(),
        "plain_left": bytes(d.plain).hex(),
        "dap_head": head_dap.hex(),
        "dap_rest": d.take_dap(4096).hex(),
        "dap_left": bytes(d.dap).hex(),
        "credited": d.credited,
        "eof": d.eof,
        "unknown_code": d.unknown_code,
    }


def _run_device(script):
    """Run `script` under the real firmware and parse its last line as JSON.

    A non-zero exit is a failed assertion carrying the device's own traceback:
    several of the things being checked here - `del` on a bytearray slice, an
    exception escaping a façade - are exceptions on the device and not value
    mismatches, so the traceback is the result.
    """
    env = dict(os.environ, MICROPYPATH=_MICROPYPATH)
    proc = subprocess.run(
        [str(_MICROPYTHON), "-c", script],
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"device script failed: {proc.stdout.decode()!r} {proc.stderr.decode()!r}"
    return json.loads(proc.stdout.decode().strip().splitlines()[-1])


@requires_unix_firmware
class TestCrossImplementation:
    """The device's reader and the host's must agree, byte for byte.

    Duplicated code is the price of `mpremote` not depending on
    `micropython-lib`; this is what makes the duplication safe. Every stream
    goes through the real `debugpy.common.repl_mux` under the real firmware,
    not a re-import of the host copy.
    """

    def test_constants_match(self):
        names = (
            "MARKER",
            "CMD_DAP",
            "CMD_DAP_ACK",
            "CMD_DAP_EOF",
            "RX_CREDIT",
            "MAX_PAYLOAD",
            "ACK_THRESHOLD",
        )
        script = (
            "import json\n"
            "from debugpy.common import repl_mux\n"
            f"print(json.dumps({{n: getattr(repl_mux, n) for n in {names!r}}}))\n"
        )
        device = _run_device(script)
        assert device == {n: getattr(repl_dap, n) for n in names}

    def test_readers_agree_on_every_stream(self):
        """Same bytes in, same bytes out - and out through `take_*`, not the queues."""
        streams = {name: stream.hex() for name, stream in _CROSS_STREAMS.items()}
        script = (
            "import json\n"
            "import binascii\n"
            "from debugpy.common import repl_mux\n"
            f"streams = {streams!r}\n"
            f"chunk = {_CROSS_CHUNK}\n"
            f"take = {_CROSS_TAKE}\n"
            "out = {}\n"
            "for name, hexed in streams.items():\n"
            "    data = binascii.unhexlify(hexed)\n"
            "    d = repl_mux.Demux()\n"
            "    for i in range(0, len(data), chunk):\n"
            "        d.feed(data[i:i + chunk])\n"
            "    head_plain = d.take_plain(take)\n"
            "    head_dap = d.take_dap(take)\n"
            "    out[name] = {\n"
            "        'plain_head': binascii.hexlify(head_plain).decode(),\n"
            "        'plain_rest': binascii.hexlify(d.take_plain(4096)).decode(),\n"
            "        'plain_left': binascii.hexlify(d.plain).decode(),\n"
            "        'dap_head': binascii.hexlify(head_dap).decode(),\n"
            "        'dap_rest': binascii.hexlify(d.take_dap(4096)).decode(),\n"
            "        'dap_left': binascii.hexlify(d.dap).decode(),\n"
            "        'credited': d.credited,\n"
            "        'eof': d.eof,\n"
            "        'unknown_code': d.unknown_code,\n"
            "    }\n"
            "print(json.dumps(out))\n"
        )
        device = _run_device(script)
        host = {name: _read_with_host_demux(stream) for name, stream in _CROSS_STREAMS.items()}
        assert device == host

    def test_writers_agree(self):
        """`frame` and `escape` produce identical bytes at both ends."""
        cases = [
            (repl_dap.CMD_DAP, b""),
            (repl_dap.CMD_DAP, b"hello"),
            (repl_dap.CMD_DAP, bytes(range(256)) * 2),
            (repl_dap.CMD_DAP_ACK, bytes((0x40, 0x01))),
            (repl_dap.CMD_DAP_EOF, b""),
        ]
        plains = [b"nothing special", b"\x18", b"a\x18b\x18\x18c", b""]
        script = (
            "import json\n"
            "import binascii\n"
            "from debugpy.common import repl_mux\n"
            f"cases = {[(code, payload.hex()) for code, payload in cases]!r}\n"
            f"plains = {[p.hex() for p in plains]!r}\n"
            "framed = [binascii.hexlify(repl_mux.frame(c, binascii.unhexlify(p))).decode()\n"
            "          for c, p in cases]\n"
            "escaped = [binascii.hexlify(repl_mux.escape(binascii.unhexlify(p))).decode()\n"
            "           for p in plains]\n"
            "print(json.dumps({'framed': framed, 'escaped': escaped}))\n"
        )
        device = _run_device(script)
        assert device["framed"] == [repl_dap.frame(c, p).hex() for c, p in cases]
        assert device["escaped"] == [repl_dap.escape(p).hex() for p in plains]


# A port that refuses every write until told otherwise, and reports itself
# writable throughout - a USB CDC with a full TX buffer, which on the bench
# board is the normal case rather than the exceptional one once the interface
# is detached from the REPL and its flow control becomes CTS.
#
# `ioctl` answers `MP_STREAM_GET_FILENO` with a negative errno for the same
# reason both façades do: on a port built with
# `MICROPY_PY_SELECT_POSIX_OPTIMISATIONS`, an object that hands `select.poll`
# a descriptor is polled through that descriptor and never asked again.
_REFUSING_PORT = (
    "import errno\n"
    "import io\n"
    "class RefusingPort(io.IOBase):\n"
    "    def __init__(self):\n"
    "        self.accept = False\n"
    "        self.sent = bytearray()\n"
    "    def write(self, buf):\n"
    "        if not self.accept:\n"
    "            raise OSError(errno.EAGAIN)\n"
    "        self.sent += buf\n"
    "        return len(buf)\n"
    "    def readinto(self, buf, nbytes=None):\n"
    "        return 0\n"
    "    def ioctl(self, op, arg):\n"
    "        if op == 3:\n"
    "            return arg & 4  # writable always, readable never\n"
    "        if op == 10:\n"
    "            return -errno.EINVAL\n"
    "        return 0\n"
)


@requires_unix_firmware
class TestDeviceOutboundQueue:
    """What the device does when the shared port will not take bytes.

    The constraint is `extmod/os_dupterm.c:196-209`: any exception out of the
    slot object's `write()` deactivates the slot, and on a board where the
    slot is the whole stdout path that silently removes the console and the
    debug channel together. So this runs the real `ReplMux` against a port
    that raises `EAGAIN` on every write and checks two things - that nothing
    escapes, and that nothing is lost.
    """

    def test_a_port_that_refuses_everything_neither_raises_nor_drops(self):
        script = (
            "import json\n"
            "import binascii\n"
            "from debugpy.common import repl_mux\n" + _REFUSING_PORT + "port = RefusingPort()\n"
            "mux = repl_mux.ReplMux(port)\n"
            "mux.console.write(b'first\\n')\n"
            "mux.dap.write(b'payload')\n"
            "refused = bytes(port.sent)\n"
            "port.accept = True\n"
            "mux.console.write(b'second\\n')\n"
            "print(json.dumps({\n"
            "    'refused': binascii.hexlify(refused).decode(),\n"
            "    'sent': binascii.hexlify(port.sent).decode(),\n"
            "}))\n"
        )
        device = _run_device(script)
        assert device["refused"] == "", "the port was written to while it was refusing"
        # In production order, and whole: one outbound queue means a console
        # write and a DAP frame can never be interleaved with each other, only
        # delayed together.
        assert bytes.fromhex(device["sent"]) == (
            repl_dap.escape(b"first\n")
            + repl_dap.frame(repl_dap.CMD_DAP, b"payload")
            + repl_dap.escape(b"second\n")
        )

    def test_the_console_survives_a_port_that_never_accepts(self):
        """`detach()` must return even when nothing it queued can leave.

        A peer that has stopped reading cannot be allowed to keep the stream
        split forever, so the final flush is bounded - the session is over by
        then and the queue is its tail.
        """
        script = (
            "import json\n"
            "from debugpy.common import repl_mux\n" + _REFUSING_PORT + "port = RefusingPort()\n"
            "mux = repl_mux.ReplMux(port)\n"
            "for i in range(200):\n"
            "    mux.console.write(b'output that goes nowhere\\n')\n"
            "returned = mux.detach() is port\n"
            "print(json.dumps({'returned': returned, 'sent': len(port.sent)}))\n"
        )
        device = _run_device(script)
        assert device == {"returned": True, "sent": 0}


class _LoopbackChannel:
    """A `ReplDapChannel` on one end of a pty, with the other end in the test's hand.

    The test plays the device: it writes framed and plain bytes onto the master
    and reads back whatever the channel sends. Console output is captured
    rather than printed so a test can assert on it.
    """

    def __init__(self):
        self.master_fd, self._slave_fd = pty.openpty()
        # Raw both ways: framing bytes are not terminal input, and canonical
        # mode would rewrite CRs and echo the channel's own writes back at it.
        tty.setraw(self._slave_fd)
        self.serial = serial.Serial(os.ttyname(self._slave_fd), 115200, timeout=0.2)
        self.console = bytearray()
        self.channel = repl_dap.ReplDapChannel(self.serial, console=self.console.extend)
        self.channel.start()

    def device_write(self, data):
        os.write(self.master_fd, data)

    def device_read(self, n, timeout=2):
        """Whatever the channel has written to the port, up to `n` bytes."""
        import select

        deadline = time.monotonic() + timeout
        out = b""
        while len(out) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.master_fd], [], [], remaining)[0]:
                break
            out += os.read(self.master_fd, n - len(out))
        return out

    def wait_for_console(self, expected, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if expected in bytes(self.console):
                return True
            time.sleep(0.01)
        return False

    def close(self):
        self.channel.close()
        self.serial.close()
        os.close(self._slave_fd)
        os.close(self.master_fd)


def _writer(channel, data, done=None):
    """Run `write_dap` in a thread, tolerating the channel closing under it.

    A test that deliberately leaves a writer blocked on credit closes the
    channel while it is still there, and `write_dap` reports that as EPIPE -
    which is the contract, not a failure of the test.
    """

    def _run():
        try:
            channel.write_dap(data)
        except OSError:
            return
        if done is not None:
            done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


class TestReplDapChannel:
    """`ReplDapChannel` over a loopback pty, with the test playing the device."""

    def test_plain_bytes_reach_the_console(self):
        link = _LoopbackChannel()
        try:
            link.device_write(b"program output\n")
            assert link.wait_for_console(b"program output\n")
        finally:
            link.close()

    def test_framed_bytes_do_not_reach_the_console(self):
        link = _LoopbackChannel()
        try:
            link.device_write(repl_dap.frame(repl_dap.CMD_DAP, b'{"seq":1}'))
            assert link.channel.read_dap(64) == b'{"seq":1}'
            assert bytes(link.console) == b""
        finally:
            link.close()

    def test_writes_are_framed_and_chunked_at_the_payload_limit(self):
        link = _LoopbackChannel()
        try:
            payload = bytes(range(64)) * 4  # 256 bytes: two full frames
            _writer(link.channel, payload)
            wire = link.device_read((repl_dap.MAX_PAYLOAD + 4) * 2)
            expected = repl_dap.frame(repl_dap.CMD_DAP, payload[: repl_dap.MAX_PAYLOAD])
            assert wire.startswith(expected)
        finally:
            link.close()

    def test_a_writer_blocks_on_credit_until_the_device_acks(self):
        """The overrun guard: a device that has stopped draining stalls the sender.

        The device's receive ring discards the tail of a packet when it
        overflows rather than pushing back, so a sender that ignored credit
        would corrupt a session instead of slowing it.
        """
        link = _LoopbackChannel()
        try:
            payload = b"x" * (repl_dap.MAX_PAYLOAD * 3)
            done = threading.Event()
            _writer(link.channel, payload, done)

            wire_frame = repl_dap.MAX_PAYLOAD + 4
            ack = repl_dap.frame(repl_dap.CMD_DAP_ACK, bytes((wire_frame & 0xFF, wire_frame >> 8)))
            # Two frames would put more than RX_CREDIT wire bytes in flight, so
            # each one waits for the previous one to be credited.
            for i in range(3):
                chunk = payload[i * repl_dap.MAX_PAYLOAD : (i + 1) * repl_dap.MAX_PAYLOAD]
                assert link.device_read(wire_frame) == repl_dap.frame(repl_dap.CMD_DAP, chunk), (
                    f"frame {i} did not arrive"
                )
                if i == 2:
                    break  # the last frame leaves nothing to wait for
                assert link.device_read(1, timeout=0.5) == b"", (
                    f"frame {i + 1} was sent without credit for it"
                )
                assert not done.is_set()
                link.device_write(ack)
            assert done.wait(3), "the writer never resumed after the device credited it"
        finally:
            link.close()

    def test_end_of_session_frame_ends_a_read_and_reports_finished(self):
        link = _LoopbackChannel()
        try:
            assert link.channel.finished is False
            link.device_write(repl_dap.frame(repl_dap.CMD_DAP_EOF, b""))
            assert link.channel.read_dap(64) == b""
            assert link.channel.finished is True
            with pytest.raises(OSError) as excinfo:
                link.channel.write_dap(b"too late")
            assert excinfo.value.errno == errno.EPIPE
        finally:
            link.close()

    def test_payload_arriving_with_the_end_frame_is_delivered_first(self):
        """A response and the end-of-session frame in one read must not race.

        `read_dap` reporting the end before draining what it already holds
        would lose the last DAP message of every session that ends promptly.
        """
        link = _LoopbackChannel()
        try:
            link.device_write(
                repl_dap.frame(repl_dap.CMD_DAP, b"final response")
                + repl_dap.frame(repl_dap.CMD_DAP_EOF, b"")
            )
            assert link.channel.read_dap(64) == b"final response"
            assert link.channel.read_dap(64) == b""
        finally:
            link.close()

    def test_a_framing_disagreement_is_visible_to_the_caller(self):
        link = _LoopbackChannel()
        try:
            link.device_write(repl_dap.frame(77, b"") + b"still talking\n")
            assert link.wait_for_console(b"still talking\n")
            assert link.channel.unknown_code == 77
        finally:
            link.close()


def _repl_session_boot_script(module="target", method="main"):
    """Boot script for a REAL `debugpy` session sharing this process's own stdout.

    The shipped script takes the stream out of `dupterm` slot 1; the unix port
    has no `dupterm` at all, so this opens fd 1 - the same pty master the
    interpreter's REPL is writing to - and hands the mux that. Everything past
    that point is production code: the mux, the framing, the escaping of the
    program's output, and the end-of-session frame.

    The program's output goes through `mux.console` for the same reason it
    reaches `dupterm` on a board: `print()` here would write to fd 1 through
    the runtime's own C path, unescaped, which is exactly what the `dupterm`
    install exists to prevent.
    """
    return (
        "import json\n"
        "import debugpy\n"
        "from debugpy.common import repl_mux\n"
        "mux = repl_mux.ReplMux(open(1, 'r+b'))\n"
        "debugpy.listen_stream(mux.dap)\n"
        "caps = debugpy.get_capabilities()\n"
        "caps['repl_dap'] = True\n"
        "print('MPDBG-READY ' + json.dumps({'host': 'serial', 'port': 1, 'caps': caps}))\n"
        "try:\n"
        "    if debugpy.wait_for_client():\n"
        "        debugpy.debug_this_thread()\n"
        "        mux.console.write(b'marker \\x18 in program output\\n')\n"
        f"        target = __import__({module!r}, None, None, ('*',))\n"
        f"        getattr(target, {method!r})()\n"
        "        mux.console.write(b'Target completed successfully!\\n')\n"
        "    else:\n"
        "        mux.console.write(b'[DAP] no client finished configuring\\n')\n"
        "        debugpy.disconnect()\n"
        "finally:\n"
        "    mux.detach()\n"
    )


_EARLY_FAILURE = "the debug session failed on the device"


def _repl_session_early_failure_script():
    """Boot script for a device whose session ends before any client attaches.

    The shipped launcher runs everything after the handshake inside
    `try/except Exception` with `_release_repl_stream()` in the `finally`, so a
    device-side failure - the debug server itself raising, a target that cannot
    even be imported - ends the mux with nothing yet connected to the bridge.
    The end-of-session frame is then the only thing that can tell mpremote to
    let go: its proxy is still waiting for a first client, and one is not
    coming.
    """
    return (
        "import json\n"
        "import debugpy\n"
        "from debugpy.common import repl_mux\n"
        "mux = repl_mux.ReplMux(open(1, 'r+b'))\n"
        "debugpy.listen_stream(mux.dap)\n"
        "caps = debugpy.get_capabilities()\n"
        "caps['repl_dap'] = True\n"
        "print('MPDBG-READY ' + json.dumps({'host': 'serial', 'port': 1, 'caps': caps}))\n"
        "try:\n"
        f"    raise RuntimeError({_EARLY_FAILURE!r})\n"
        "except Exception as e:\n"
        "    mux.console.write(b'Error: ' + str(e).encode() + b'\\n')\n"
        "finally:\n"
        "    mux.detach()\n"
    )


class _DaemonFuture:
    """Runs `fn` in a daemon thread; `.result(timeout)` behaves like a `Future`'s.

    Daemon rather than a `ThreadPoolExecutor` worker so that a `do_debug` which
    never returns - the wedge these tests exist to catch - is reported as a
    failed assertion instead of hanging the interpreter's atexit join.
    """

    def __init__(self, fn, *args):
        self._result = None
        self._exc = None
        self._done = threading.Event()

        def _run():
            try:
                self._result = fn(*args)
            except BaseException as e:  # noqa: BLE001 - propagated verbatim by result()
                self._exc = e
            finally:
                self._done.set()

        threading.Thread(target=_run, daemon=True).start()

    def result(self, timeout=None):
        if not self._done.wait(timeout):
            raise TimeoutError("do_debug did not finish within the timeout")
        if self._exc is not None:
            raise self._exc
        return self._result


@requires_unix_firmware
class TestRealSession:
    """`do_debug --dap-repl` carrying a real `debugpy` session on one stream.

    One pty carries the raw REPL protocol, the program's output and the DAP
    traffic together, through the production `ReplDapChannel`/`ReplDapBridge`
    and the production device-side `ReplMux`.
    """

    def _spawn(self, monkeypatch, module="target", method="main", script=None):
        device = PtyDevice(_MICROPYTHON, _MICROPYPATH).start()
        transport = SerialTransport(device.path, baudrate=115200)
        state = State()
        state.transport = transport
        commands.do_resume(state)  # the unix build exits on a raw-REPL soft reset

        resolved = Target(name="bench", kind="serial", device=device.path, dap_repl=True)
        monkeypatch.setattr(commands, "resolve_target", lambda name: resolved)

        def _boot_script(mod, meth, port, dap_stream=None, mount_point=None, loop=False):
            assert dap_stream == "repl", dap_stream
            assert mount_point is None, "a mount is refused for this session"
            return script if script is not None else _repl_session_boot_script(module, method)

        monkeypatch.setattr(commands, "_debug_boot_script", _boot_script)

        reported_holder = {}
        orig_report = commands._report_debug_result

        def _capture_report(handshake, path_mappings=None):
            reported_holder["value"] = handshake
            return orig_report(handshake, path_mappings)

        monkeypatch.setattr(commands, "_report_debug_result", _capture_report)

        console = bytearray()
        channel_holder = {}
        orig_channel = commands.repl_dap.ReplDapChannel

        def _capturing_channel(port, console_sink=None):
            # The channel writes plain bytes to mpremote's stdout; capture
            # them instead so the marker-in-output assertion can read them.
            # Keeping the instance also exposes its wire-byte counter, which
            # is how a test can tell what actually went over the port.
            channel = orig_channel(port, console=console.extend)
            channel_holder["value"] = channel
            return channel

        monkeypatch.setattr(commands.repl_dap, "ReplDapChannel", _capturing_channel)

        # `dap_repl` is left at its default: the resolved target's own
        # `dap_repl` is what asks for the shared stream here.
        args = debug_args(target="bench", program=f"{module}:{method}", timeout=15)

        future = _DaemonFuture(commands.do_debug, state, args)
        return future, reported_holder, console, device, transport, channel_holder

    def _wait_for_report(self, reported_holder, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "value" in reported_holder:
                return reported_holder["value"]
            time.sleep(0.05)
        pytest.fail("do_debug never reported the bridge's endpoint")

    def _cleanup(self, device, transport):
        # The transport closes before the process it talks to, so the device
        # never reads from a pty whose only other opener has gone.
        transport.close()
        device.close()

    def test_reaches_a_breakpoint_while_the_program_prints_the_marker(self, monkeypatch):
        future, reported_holder, console, device, transport, _ = self._spawn(monkeypatch)
        try:
            reported = self._wait_for_report(reported_holder)
            assert reported["caps"]["repl_dap"] is True
            assert reported["host"] == "127.0.0.1"

            server = PerfServer("test-client", reported["host"], reported["port"])
            try:
                server.start()
                # `take_msg` rather than `wait_for_msg`: the server answers
                # `initialize` and then sends `initialized` straight after, so
                # the response is no longer the most recent message by the time
                # anything gets round to looking for it.
                assert take_msg(server, response="initialize", timeout=15) is not None, (
                    "no initialize response over the shared stream"
                )
                set_breakpoints(server, _TARGET_PY, [_BREAKPOINT_LINE])
                assert take_msg(server, response="setBreakpoints", timeout=15) is not None, (
                    "setBreakpoints failed over the shared stream"
                )
                server.client.configuration_done()
                stopped = take_msg(server, event="stopped", timeout=20)
                assert stopped is not None, "configurationDone produced no stopped event"
                assert stopped.body.get("reason") == "breakpoint", (
                    f"stopped reason not 'breakpoint': {stopped.body}"
                )

                # Criterion 2: the program's own output, marker byte and all,
                # arrived intact on the same wire as the DAP session above.
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if b"marker \x18 in program output\n" in bytes(console):
                        break
                    time.sleep(0.05)
                assert b"marker \x18 in program output\n" in bytes(console), (
                    f"program output did not survive the shared stream: {bytes(console)!r}"
                )

                server.client.continue_(stopped.body.get("threadId", 1))
            finally:
                server.stop()

            final = future.result(timeout=30)
            assert final["port"] == reported["port"]
        finally:
            self._cleanup(device, transport)

    def test_a_request_far_larger_than_the_credit_window_arrives_whole(self, monkeypatch):
        """A `setBreakpoints` spanning many frames is answered for every line asked.

        The device's receive ring drops the tail of a packet it has no room
        for rather than exerting back-pressure, which is what the credit window
        exists to prevent; the failure it guards against is silent truncation,
        so what is asserted is the response's contents. `set_breakpoints`
        replaces the whole set for a file, and the adapter answers one entry
        per requested line, so a request that lost its tail comes back short.
        """
        lines = list(range(_LARGE_REQUEST_FIRST_LINE, _BREAKPOINT_LINE + 1))
        future, reported_holder, console, device, transport, channel_holder = self._spawn(monkeypatch)
        try:
            reported = self._wait_for_report(reported_holder)
            server = PerfServer("test-client", reported["host"], reported["port"])
            try:
                server.start()
                assert take_msg(server, response="initialize", timeout=15) is not None, (
                    "no initialize response over the shared stream"
                )
                before = channel_holder["value"]._sent
                set_breakpoints(server, _TARGET_PY, lines)
                response = take_msg(server, response="setBreakpoints", timeout=30)
                assert response is not None, "no setBreakpoints response over the shared stream"
                verified = [bp["line"] for bp in response.body["breakpoints"]]
                assert verified == lines, (
                    f"the device answered for {len(verified)} of {len(lines)} lines; "
                    f"a short or reordered answer means the request did not arrive whole"
                )

                # The request has to have been big enough to be the thing the
                # credit window exists for. Measured on the port rather than
                # estimated from the line count, so a change to how the client
                # encodes a request cannot quietly shrink what this covers.
                written = channel_holder["value"]._sent - before
                assert written > repl_dap.RX_CREDIT, (
                    f"the request was {written} wire bytes, inside the "
                    f"{repl_dap.RX_CREDIT}-byte credit window; it never had to wait "
                    f"for an ack, so nothing about overflow was exercised"
                )

                # The session is still coherent afterwards: a request that lost
                # bytes mid-frame would leave the device's reader waiting for a
                # body that never comes, and nothing later would parse.
                server.client.configuration_done()
                stopped = take_msg(server, event="stopped", timeout=20)
                assert stopped is not None, "configurationDone produced no stopped event"
                assert stopped.body.get("reason") == "breakpoint", (
                    f"stopped reason not 'breakpoint': {stopped.body}"
                )
            finally:
                server.stop()
        finally:
            self._cleanup(device, transport)

    def test_a_session_that_ends_with_no_client_still_releases_mpremote(self, monkeypatch):
        """A device that finishes before any client attaches must release mpremote.

        The proxy's own end-of-session signal needs a client to have connected
        first, so a device failing this early is invisible to it; the device's
        end-of-session frame is what covers the rest. Without that, mpremote
        holds the port until Ctrl-C for a session that is already over.
        """
        future, reported_holder, console, device, transport, _ = self._spawn(
            monkeypatch, script=_repl_session_early_failure_script()
        )
        try:
            self._wait_for_report(reported_holder)
            future.result(timeout=30)
            assert _EARLY_FAILURE.encode() in bytes(console), (
                f"the device's own error did not reach the console: {bytes(console)!r}"
            )
        finally:
            self._cleanup(device, transport)


class TestReplDapRefusals:
    """Combinations `--dap-repl` and `dap_repl` must reject, host-side only."""

    def _args(self, **overrides):
        fields = {"target": "bench", "program": "target:main", "timeout": 15, "dap_repl": True}
        fields.update(overrides)
        return debug_args(**fields)

    def test_unix_target_is_refused(self, monkeypatch):
        monkeypatch.setattr(commands, "resolve_target", lambda name: None)
        with pytest.raises(commands.CommandError, match="not valid for a unix target"):
            commands.do_debug(State(), self._args(target="unix"))

    def test_dap_device_conflict_is_refused(self, monkeypatch):
        resolved = Target(
            name="bench", kind="serial", device="/dev/null", dap_device="/dev/null", dap_repl=False
        )
        monkeypatch.setattr(commands, "resolve_target", lambda name: resolved)
        with pytest.raises(commands.CommandError, match="conflicts with target"):
            commands.do_debug(State(), self._args())

    def test_source_is_refused_because_a_mount_frames_the_same_stream(self, monkeypatch, tmp_path):
        resolved = Target(name="bench", kind="serial", device="/dev/null", dap_repl=True)
        monkeypatch.setattr(commands, "resolve_target", lambda name: resolved)
        with pytest.raises(commands.CommandError, match="cannot be combined with --dap-repl"):
            commands.do_debug(State(), self._args(dap_repl=False, source=str(tmp_path)))


class TestReplDapConfig:
    """`dap_repl` as an mpdebug.toml key."""

    def _load(self, tmp_path, body):
        path = tmp_path / "mpdebug.toml"
        path.write_text(body)
        return mpdebug_config._load_targets(path)

    def test_defaults_to_false(self, tmp_path):
        targets = self._load(tmp_path, '[target.bench]\nkind = "serial"\ndevice = "/dev/ttyACM0"\n')
        assert targets["bench"].dap_repl is False

    def test_true_is_carried_onto_the_target(self, tmp_path):
        targets = self._load(
            tmp_path,
            '[target.bench]\nkind = "serial"\ndevice = "/dev/ttyACM0"\ndap_repl = true\n',
        )
        assert targets["bench"].dap_repl is True

    def test_non_boolean_is_rejected(self, tmp_path):
        with pytest.raises(commands.CommandError, match="dap_repl must be true or false"):
            self._load(
                tmp_path,
                '[target.bench]\nkind = "serial"\ndevice = "/dev/ttyACM0"\ndap_repl = "yes"\n',
            )

    def test_with_dap_device_is_rejected(self, tmp_path):
        with pytest.raises(commands.CommandError, match="both 'dap_repl' and 'dap_device'"):
            self._load(
                tmp_path,
                '[target.bench]\nkind = "serial"\ndevice = "/dev/ttyACM0"\n'
                'dap_device = "/dev/ttyACM1"\ndap_repl = true\n',
            )

    def test_on_a_unix_target_is_rejected(self, tmp_path):
        with pytest.raises(commands.CommandError, match="kind 'unix' and sets 'dap_repl'"):
            self._load(tmp_path, '[target.bench]\nkind = "unix"\ndap_repl = true\n')
