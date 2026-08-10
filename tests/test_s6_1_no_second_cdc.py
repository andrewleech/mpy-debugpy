"""STORY-6.1 criterion 2: a board with no dedicated DAP interface refuses.

The criterion asks what happens when a target is configured for serial DAP and
the board cannot provide it: a clear message, no hang, no misleading error. It
was recorded as needing hardware that is not on the bench, on the reading that
"a board with no second CDC" meant a different chip. It does not: a PYBD-SF6W
whose `boot.py` never calls `pyb.usb_mode("2xVCP+MSC")` enumerates one CDC and
takes exactly this path (`20260810_single-uart-dap-framing.md`, D7). The device
side of it is decidable here, under the real interpreter, for every route.

Criterion 2's own wording continues "and falls through to network transport".
That half is superseded by D5: `_detect_dap_stream` raises rather than falling
back, because the caller has a bridge waiting on a stream and nothing listening
on a port, so a silent TCP endpoint would be an endpoint with no client. What
these assert is therefore the opposite - that nothing was bound - alongside the
message and the promptness the criterion does ask for.

Four routes reach "no interface", and the boot script must take the same exit
from all of them:

* no `pyb` at all (rp2, esp32, unix);
* a `pyb` with no `usb_mode` - a build without USB;
* `usb_mode()` reporting no second interface (the PYBD case above);
* `pyb.USB_VCP(1)` refusing to construct, the build genuinely having one CDC.

The stubs contradict their own `usb_mode()` where they can, for the same reason
`test_q12_second_cdc.py` does: a probe reading the wrong instrument would
otherwise pass on the cases it happens to agree with.
"""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_TOP_DIR = Path(__file__).resolve().parents[1]
_SUBMODULE_DIR = _TOP_DIR / "micropython" / "tools" / "mpremote"
_TESTS_DIR = Path(__file__).resolve().parent

for _dir in (_SUBMODULE_DIR, _TESTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from mpremote import commands  # noqa: E402
from mpremote.main import State  # noqa: E402
from mpremote.mpdebug_config import Target  # noqa: E402
from mpremote.transport_serial import SerialTransport  # noqa: E402

from helpers import debug_args  # noqa: E402
from pty_device import PtyDevice  # noqa: E402

_MICROPYTHON = Path(
    os.environ.get(
        "MPY_DEBUG_FIRMWARE",
        _TOP_DIR / "micropython/ports/unix/build-standard/micropython",
    )
)
_BOOT_SCRIPT = _TOP_DIR / "launcher" / "mpy_launch_debugpy.py"
_BASE_PATH = "{}:{}:{}".format(
    _TOP_DIR / "src",
    _TOP_DIR / "micropython-lib/python-ecosys/debugpy",
    _TOP_DIR / "micropython-lib",
)

# What `_detect_dap_stream("board")` says when `_board_dap_stream()` finds
# nothing to hand back. Asserted rather than paraphrased: this string is the
# whole of what a user is told, and it is what the criterion means by "a clear
# message".
_REFUSAL = "no dedicated DAP interface on this board"

requires_unix_firmware = pytest.mark.skipif(
    not _MICROPYTHON.exists(),
    reason=f"unix firmware not built at {_MICROPYTHON} (see `make firmware-unix`)",
)

# One `pyb` stub per route to "no interface". `None` is the fifth case, a
# runtime with no `pyb` module at all, and needs no stub.
_STUBS = {
    # Built for two, booted with one enumerated: a PYBD-SF6W with no
    # `pyb.usb_mode()` call in `boot.py`, which is every PYBD as it ships.
    # The second interface exists in the firmware and is not there to open.
    "two_cdc_one_enumerated": """
class USB_VCP:
    def __init__(self, id=0):
        if id >= 2:
            raise ValueError


def usb_mode():
    return "VCP+MSC"
""",
    # Built for one, `usb_mode` claiming otherwise. The constructor is the
    # instrument that cannot be talked round.
    "one_cdc": """
class USB_VCP:
    def __init__(self, id=0):
        if id >= 1:
            raise ValueError


def usb_mode():
    return "2xVCP+MSC"
""",
    # USB off in this build: `usb_mode()` answers, with nothing.
    "usb_disabled": """
class USB_VCP:
    def __init__(self, id=0):
        raise ValueError


def usb_mode():
    return None
""",
    # A `pyb` for a part with no USB peripheral at all, so no `usb_mode` to
    # call. Reaches the probe's AttributeError branch, which no other case
    # here does.
    "no_usb_peripheral": """
class Pin:
    pass
""",
}


class _BootRun:
    """What one run of the boot script did.

    `listened` is measured, not read out of the output: the rule being checked
    is that a refused serial channel leaves no TCP endpoint behind, and a
    script that bound a port and said nothing about it would satisfy every
    string assertion here.
    """

    def __init__(self, output, exited, listened):
        self.output = output
        self.exited = exited
        self.listened = listened

    def __repr__(self):
        return f"<boot run exited={self.exited} listened={self.listened} output={self.output!r}>"


def _run_boot_script(stub_dir, port, dap_stream, timeout=30):
    """Run the shipped boot script under the real interpreter.

    Returns once the process exits or something accepts a connection on
    `port`, whichever happens first, so a refusal (which exits in
    milliseconds) and a successful TCP bind (which then blocks in
    `wait_for_client()` forever) both finish in the time they really take.

    The connection watch runs from before the process starts until it is
    stopped, so a bind at any point in the run is caught. A `listen()` holds
    its socket for the life of the process, so there is no window in which one
    could open and close between polls.
    """
    env = os.environ.copy()
    env["MICROPYPATH"] = f"{stub_dir}:{_BASE_PATH}" if stub_dir else _BASE_PATH
    argv = [str(_MICROPYTHON), str(_BOOT_SCRIPT), "target", "main", str(port)]
    if dap_stream is not None:
        argv.append(dap_stream)

    listened = threading.Event()
    stop = threading.Event()
    lines = []

    def watch():
        while not stop.is_set():
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            except OSError:
                stop.wait(0.005)
            else:
                listened.set()
                return

    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)

    def drain():
        for line in proc.stdout:
            lines.append(line)

    watcher = threading.Thread(target=watch, daemon=True)
    reader = threading.Thread(target=drain, daemon=True)
    watcher.start()
    reader.start()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if listened.is_set() or proc.poll() is not None:
                break
            time.sleep(0.01)
        exited = proc.poll() is not None
    finally:
        stop.set()
        proc.kill()
        proc.wait(timeout=10)
        reader.join(timeout=10)
        watcher.join(timeout=10)
        proc.stdout.close()
    return _BootRun("".join(lines), exited, listened.is_set())


@pytest.fixture
def stub_pyb(tmp_path):
    """Write one of the `pyb` stubs into a directory of its own."""

    def _write(name):
        if name is None:
            return None
        directory = tmp_path / name
        directory.mkdir()
        (directory / "pyb.py").write_text(_STUBS[name])
        return directory

    return _write


@requires_unix_firmware
@pytest.mark.parametrize("stub", [None, *_STUBS])
def test_the_boot_script_refuses_a_board_channel_it_cannot_provide(stub, stub_pyb, free_tcp_port):
    """Every route to "no interface" ends the run, saying which thing is missing.

    The parametrisation is the point: one route working proves nothing about
    the others, and the probe reaches its `return None` from four different
    places.
    """
    run = _run_boot_script(stub_pyb(stub), free_tcp_port, "board")

    assert run.exited, f"the script should have ended rather than waiting: {run}"
    assert _REFUSAL in run.output, f"the refusal should name the missing interface: {run}"
    assert "MPDBG-READY" not in run.output, f"a refused run has no endpoint to report: {run}"


@requires_unix_firmware
@pytest.mark.parametrize("stub", [None, *_STUBS])
def test_a_refused_board_channel_binds_no_port(stub, stub_pyb, free_tcp_port):
    """D5: the refusal never falls back to TCP behind the caller's back.

    Separate from the message assertions above because it fails for a
    different reason and is the one with real consequences: a caller that
    asked for `dap_stream="board"` has a bridge waiting on a stream, so a
    port bound here would be an endpoint nothing would ever connect to, and
    the session would hang instead of failing.
    """
    run = _run_boot_script(stub_pyb(stub), free_tcp_port, "board")

    assert not run.listened, f"a refused serial channel must not leave a TCP endpoint: {run}"


@requires_unix_firmware
def test_the_same_board_binds_a_port_when_asked_for_the_network_channel(stub_pyb, free_tcp_port):
    """Control for the two above, and for the watch that measures them.

    Without this, `listened is False` would also be what a broken connection
    watch reported, and a missing `MPDBG-READY` would also be what a boot
    script that could not start at all produced. The same stub and the same
    port, asked for TCP instead, reaches the endpoint.

    The run ends here too, but for the opposite reason: the watch's connection
    is a DAP client as far as the script is concerned, and dropping it
    immediately is a client that went away before `configurationDone`, which
    the script declines to run a target under. So `exited` says nothing on
    this path and is not asserted.
    """
    run = _run_boot_script(stub_pyb("two_cdc_one_enumerated"), free_tcp_port, None)

    assert run.listened, f"the network path should have bound {free_tcp_port}: {run}"
    assert "MPDBG-READY" in run.output, f"the network path should report an endpoint: {run}"
    assert _REFUSAL not in run.output, run.output


@requires_unix_firmware
def test_do_debug_reports_the_missing_interface_promptly(monkeypatch, tmp_path, free_tcp_port):
    """What a user running `mpremote debug` against such a board is told.

    The device-side tests above establish the boot script's behaviour; this is
    the whole command over a real transport, which is where the message a user
    reads is actually assembled and where a hang would actually happen.

    `dap_device` deliberately names a node that does not exist. On this path it
    is never opened: `serial_dap.check_device` runs inside
    `_maybe_start_serial_dap`, which `do_debug` only reaches after a
    successful handshake, so the board's refusal comes first and the error is
    about the board rather than about the host's configuration. A run that
    reported the missing node instead would be the "misleading error" the
    criterion rules out.
    """
    stub_dir = tmp_path / "pyb"
    stub_dir.mkdir()
    (stub_dir / "pyb.py").write_text(_STUBS["two_cdc_one_enumerated"])

    device = PtyDevice(_MICROPYTHON, f"{stub_dir}:{_BASE_PATH}").start()
    try:
        transport = SerialTransport(device.path, baudrate=115200)
        state = State()
        state.transport = transport
        # The unix build exits on the raw-REPL soft reset used to prime a fresh
        # device rather than rebooting into a new REPL.
        commands.do_resume(state)

        missing_node = str(tmp_path / "no-such-dap-node")
        resolved = Target(
            name="single-cdc",
            kind="serial",
            device=device.path,
            dap_device=missing_node,
        )
        monkeypatch.setattr(commands, "resolve_target", lambda name: resolved)

        timeout = 30
        args = debug_args(target="single-cdc", program="mod:main", port=free_tcp_port, timeout=timeout)

        start = time.monotonic()
        with pytest.raises(commands.CommandError) as excinfo:
            commands.do_debug(state, args)
        elapsed = time.monotonic() - start
        message = str(excinfo.value)

        assert _REFUSAL in message, f"the error should name the missing interface; got: {message}"
        assert missing_node not in message, (
            f"the board refused before the node was ever reached, so the error "
            f"must not point at the node: {message}"
        )
        # Well inside the handshake timeout: the boot script's `\x04` ends the
        # wait as soon as it exits, so nothing here should be waiting out a
        # clock.
        assert elapsed < timeout / 2, f"the refusal took {elapsed:.1f}s of a {timeout}s timeout"

        transport.close()
        # The refusal is a script that ended, not a device that broke.
        device.assert_usable()
    finally:
        device.close()

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", free_tcp_port), timeout=0.5).close()
