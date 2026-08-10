"""A unix-port firmware process on a real pty, as a test reaches a board over serial.

Shared rather than written per harness because a pty is not a neutral stand-in
for a serial port. It is a tty the device itself can *configure*, which no USB
CDC device can, and every harness that hands one to the unix port and then
opens the slave through pyserial inherits the same failure: see
`planning/20260810_pty-termios-race.md` for the mechanism and the measurements.
"""

import os
import pty
import select
import subprocess
import termios
import time
import tty

from mpremote.transport import TransportError
from mpremote.transport_serial import SerialTransport

# Cleared by `PtyDevice.start()`, and named by index into `tcgetattr`'s
# (iflag, oflag, cflag, lflag, ...) so `cooked_flags` can report which of them
# the device has brought back.
_MUST_STAY_RAW = (("ICRNL", 0), ("OPOST", 1), ("ICANON", 3), ("ECHO", 3))


class PtyDevice:
    """A `micropython` unix build reached over a pty pair, as mpremote reaches serial hardware.

    This process holds the master and hands out `path`; mpremote, and any check
    wanting its own connection, open the slave by that path.
    """

    def __init__(self, executable, micropypath=None, pass_fds=()):
        self.executable = executable
        # `None` leaves MICROPYPATH to the environment; `""` is a request for
        # an interpreter that can import nothing but its builtins.
        self.micropypath = micropypath
        # Inherited at their own numbers, for a device that has to reach
        # something other than its own REPL - a second pty's master, say.
        self.pass_fds = tuple(pass_fds)
        self.proc = None
        self.path = None
        self._slave_fd = None

    def start(self):
        """Spawn the interpreter, and return once it has reached its REPL."""
        master_fd, slave_fd = pty.openpty()
        self.path = os.ttyname(slave_fd)
        # A fresh pty comes up in canonical mode, and the unix port's REPL
        # snapshots whatever termios it finds the first time it calls
        # `mp_hal_stdio_mode_raw()`, then restores that snapshot around every
        # command it executes so a running program can take SIGINT
        # (`pyexec_raw_repl`). A snapshot taken before pyserial rawifies the
        # line therefore puts the pty back into canonical mode for the whole of
        # each exec - where ICRNL rewrites the device's CRs, ECHO feeds the
        # device its own output back as input, and the \x04 that ends raw-REPL
        # output is VEOF rather than a byte. Handing the interpreter a line that
        # is already raw makes every snapshot it can take a raw one.
        tty.setraw(slave_fd)
        env = dict(os.environ)
        if self.micropypath is not None:
            env["MICROPYPATH"] = str(self.micropypath)

        self.proc = subprocess.Popen(
            [str(self.executable)],
            stdin=master_fd,
            stdout=master_fd,
            stderr=master_fd,
            env=env,
            close_fds=True,
            pass_fds=self.pass_fds,
        )
        os.close(master_fd)
        # Kept open for the session so the pty always has a slave. With none,
        # the device's `read(2)` on the master fails EIO and the unix port's
        # `mp_hal_stdin_rx_chr` returns an uninitialised byte to the REPL, in a
        # spin that costs ~85k syscalls across a single test.
        self._slave_fd = slave_fd
        self._wait_for_repl_prompt()
        return self

    def _wait_for_repl_prompt(self, timeout=10):
        """Block until the interpreter has printed its friendly-REPL prompt.

        Reads no further than the prompt, so nothing mpremote is owed is
        consumed: the banner is everything the device emits before it is
        spoken to.
        """
        seen = b""
        deadline = time.monotonic() + timeout
        while not seen.endswith(b">>> "):
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"the interpreter never reached its REPL; it said {seen!r}"
            if not select.select([self._slave_fd], [], [], remaining)[0]:
                continue
            seen += os.read(self._slave_fd, 1)

    def cooked_flags(self, fd):
        """Which of the flags `start()` cleared are set on the pty again."""
        attr = termios.tcgetattr(fd)
        return [name for name, field in _MUST_STAY_RAW if attr[field] & getattr(termios, name)]

    def assert_usable(self, context=None):
        """A fresh connection finds a live device, carrying none of the session's state.

        `soft_reset=False` is what `mpremote resume` does, and the only thing
        that can be asked of this device: the ctrl-D of a soft reset ends the
        unix port's process outright, which hangs up the pty and so would
        report a device destroyed by the check as a device the session left
        broken.

        Every byte the check exchanges is recorded, because the failure this
        guards against is a timeout: `TransportError` names which read gave up
        but nothing about what the device said instead, and a device that
        answers the wrong thing, a device that says nothing, and a device that
        is no longer there are three different bugs. `context`, if given, is
        called for whatever the caller knows that this object does not.
        """
        transport = SerialTransport(self.path, baudrate=115200)
        sent, received = bytearray(), bytearray()
        pending = transport.serial.inWaiting()
        port_read = transport.serial.read
        port_write = transport.serial.write

        def recording_read(size=1):
            chunk = port_read(size)
            received.extend(chunk)
            return chunk

        def recording_write(data):
            sent.extend(data)
            return port_write(data)

        transport.serial.read = recording_read
        transport.serial.write = recording_write

        def state():
            return self._state(transport, pending, sent, received, context)

        try:
            try:
                transport.enter_raw_repl(soft_reset=False, timeout_overall=10)
                output = transport.exec("print(1)", timeout_overall=10)
            except TransportError as er:
                raise AssertionError(
                    f"a fresh connection did not get a working device after the "
                    f"session: {er}; {state()}"
                ) from er
            assert output.strip() == b"1", (
                f"device did not respond normally after the session: {output!r}; {state()}"
            )
            # The device restores its own termios snapshot around each exec, so
            # a cooked flag here means it snapshotted the pty before `start()`
            # rawified it and has been mangling the protocol bytes - the failure
            # mode this check would otherwise report only as a timeout.
            cooked = self.cooked_flags(transport.serial.fileno())
            assert cooked == [], f"the device left the pty cooked: {cooked}; {state()}"
        finally:
            transport.close()

    def _state(self, transport, pending, sent, received, context):
        """Everything known about the far end, for a failure message."""
        exit_code = self.proc.poll()
        return (
            "device process "
            + ("still running" if exit_code is None else f"exited {exit_code}")
            + f"; {pending} bytes were already pending when the check connected"
            + f"; it wrote {bytes(sent)!r} and read {bytes(received)!r}"
            + f"; pty cooked flags {self.cooked_flags(transport.serial.fileno())}"
            + (f"; {context()}" if context is not None else "")
        )

    def close(self, grace=5):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=grace)
        if self._slave_fd is not None:
            os.close(self._slave_fd)
            self._slave_fd = None
