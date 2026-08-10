"""Hardware-in-loop fixtures: one real board, driven by the real command.

These tests are opt-in. Point `MPY_DEBUG_HIL_DEVICE` at a board's
`/dev/serial/by-id/...` path and they run; leave it unset and they skip, so
`make test` stays a host-only suite. Nothing here hardcodes a device path, a
board name, or an address - the by-id path comes from the environment and the
debug endpoint comes from the device's own handshake, as it does in
production.

`MPY_DEBUG_HIL_DAP_DEVICE` is a second by-id path, the board's dedicated DAP
interface; it gates the serial-DAP scenarios separately, since a board with
one CDC interface can still run everything else here.

Set `MPY_DEBUG_HIL_BOARD` to name the board in the results record; without it
the record uses the board name the firmware reports for itself.

A run must be attributable to a commit, so it refuses to start from a dirty
tree and records the top-repo commit and both submodule pins in its results.
`MPY_DEBUG_HIL_ALLOW_DIRTY=1` overrides the refusal and lists the uncommitted
paths in the record instead (see `tree_state.py`).

The board needs `board_boot.py` in this directory installed as its `boot.py`
(plus a `_secrets.py`) before any of this runs: the WiFi scenarios need an
interface already up, and the serial-DAP ones need the second CDC already
enumerated. Neither can be arranged from here, since both are decided before
the first `mpremote` connection.

Only one process may hold the board's serial port, and `mpremote debug` needs
it, so nothing here keeps a transport open across a debug run: `hil_serial` is
a context manager, not a live connection.
"""

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from debuggee import TARGET_MODULE, TARGET_SRC
from tree_state import (
    dirty_tree_refusal as _dirty_tree_refusal,
    record_lines as _tree_record_lines,
    tree_state as _tree_state,
)
from mpremote_debug import (
    SUBMODULE_DIR as _SUBMODULE_DIR,
    TOP_DIR as _TOP_DIR,
    read_until as _read_until,
    spawn_debug as _spawn_debug,
)

if str(_SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SUBMODULE_DIR))

DEVICE_ENV = "MPY_DEBUG_HIL_DEVICE"
DAP_DEVICE_ENV = "MPY_DEBUG_HIL_DAP_DEVICE"
BOARD_ENV = "MPY_DEBUG_HIL_BOARD"
RESET_ENV = "MPY_DEBUG_HIL_RESET_CMD"
BAUDRATE = 115200

DEBUGPY_SRC = _TOP_DIR / "micropython-lib" / "python-ecosys" / "debugpy" / "debugpy"


def pytest_configure(config):
    config.addinivalue_line("markers", "hil: needs a real board (see tests/hil/conftest.py)")


def pytest_collection_modifyitems(items):
    """Everything under `tests/hil/` is a hardware test, by definition."""
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker("hil")


@pytest.fixture(scope="session")
def hil_tree(request):
    """The checkout this run is attributable to, or a refusal."""
    state = _tree_state(_TOP_DIR)
    refusal = _dirty_tree_refusal(state)
    if refusal:
        pytest.fail(refusal)
    request.session._hil_tree = state
    return state


@pytest.fixture(scope="session")
def hil_device(request):
    """The board's stable by-id path, or a skip."""
    device = os.environ.get(DEVICE_ENV)
    if not device:
        pytest.skip(f"set {DEVICE_ENV} to a /dev/serial/by-id/... path to run the HIL suite")
    if "/by-id/" not in device:
        # Device nodes renumber whenever anything is replugged, so a raw
        # /dev/ttyACMx names whichever board holds that number today. Fail
        # rather than skip: this is a broken invocation, not a missing board.
        pytest.fail(f"{DEVICE_ENV} must be a /dev/serial/by-id/... path, got {device}")
    if not Path(device).exists():
        pytest.skip(f"{DEVICE_ENV}={device} is not present")
    # After the device gate, never before it: a checkout with no board present
    # is a host-only `make test` run, which has no results to attribute.
    request.getfixturevalue("hil_tree")
    return device


@pytest.fixture(scope="session")
def hil_dap_device():
    """The board's dedicated DAP interface, or a skip.

    A separate opt-in from `hil_device`: whether a board exposes a second CDC
    interface is a per-board fact, so the serial-DAP scenarios skip on a board
    that has only one rather than failing the whole run.
    """
    device = os.environ.get(DAP_DEVICE_ENV)
    if not device:
        pytest.skip(f"set {DAP_DEVICE_ENV} to the board's second /dev/serial/by-id/... path")
    if "/by-id/" not in device:
        pytest.fail(f"{DAP_DEVICE_ENV} must be a /dev/serial/by-id/... path, got {device}")
    if not Path(device).exists():
        pytest.skip(f"{DAP_DEVICE_ENV}={device} is not present")
    return device


@pytest.fixture()
def hil_reset_board(hil_device):
    """Call to reset the board out from under whatever is talking to it.

    The command comes from the environment because there is no portable way
    to reset a board: a bench may cut USB power at a hub, drive NRST from a
    probe, or have nothing at all. `MPY_DEBUG_HIL_RESET_CMD` runs through the
    shell, so the bench supplies whatever it has, and the scenario skips where
    it has none.

    Returns once `hil_device` and any `also` path the caller names are back
    *and* the board has finished booting, so the run leaves the bench as it
    found it. Their disappearance is not waited for: a reset command may well
    outlast it, and the scenario's own assertion is what proves the reset was
    seen.
    """
    command = os.environ.get(RESET_ENV)
    if not command:
        pytest.skip(f"set {RESET_ENV} to a command that resets the board")

    def _reset(also=(), timeout=60):
        subprocess.run(command, shell=True, check=True, timeout=timeout)
        deadline = time.monotonic() + timeout
        for path in [hil_device, *also]:
            while not Path(path).exists():
                assert time.monotonic() < deadline, f"{path} never came back after {command!r}"
                time.sleep(0.2)
        _wait_for_repl(hil_device, deadline)

    return _reset


def _wait_for_repl(device, deadline):
    """Block until the board is back at its REPL, so its boot script has run.

    The ports coming back does not mean the board is ready. A boot script
    that reconfigures USB - which is how a second CDC interface is asked for -
    enumerates where it makes that call, not when it returns, so the paths
    reappear while the rest of the script is still running. On this bench the
    rest is a WiFi association, seconds on a good day and half a minute when
    the first attempt fails, and a scenario that reset the board and then went
    looking for it on the network would race that.

    A newline is sent rather than a banner waited for. It sits unread in the
    interface's rx buffer until the REPL starts, and the prompt echoed back is
    the signal; waiting passively would miss a board whose boot script returns
    before this port is open, because a CDC interface discards what it writes
    while nobody holds it.
    """
    import serial

    with serial.Serial(device, BAUDRATE, timeout=0.2) as port:
        port.write(b"\r\n")
        seen = b""
        while b">>>" not in seen:
            assert time.monotonic() < deadline, (
                f"{device} never reached a REPL prompt after the reset; read: {seen!r}"
            )
            seen += port.read(4096)


@pytest.fixture(scope="session")
def hil_serial(hil_device):
    """`with hil_serial() as transport:` - a raw REPL, released on exit."""

    @contextlib.contextmanager
    def _open(soft_reset=True):
        from mpremote.transport_serial import SerialTransport

        transport = SerialTransport(hil_device, baudrate=BAUDRATE)
        transport.enter_raw_repl(soft_reset=soft_reset)
        try:
            yield transport
        finally:
            with contextlib.suppress(Exception):
                transport.exit_raw_repl()
            transport.close()

    return _open


def _device_root(transport):
    """The directory an importable module belongs in on this board.

    Taken from the board's own `sys.path` rather than assumed to be `/`: a
    pyboard-style filesystem mounts at `/flash`, where `/` is not writable and
    nothing under it would be importable anyway.
    """
    transport.exec("import sys")
    lib_dirs = [p for p in transport.eval("sys.path") if p.endswith("/lib") and not p.startswith("/rom")]
    if not lib_dirs:
        pytest.fail("no lib directory on the board's sys.path; cannot place the debuggee")
    return lib_dirs[0][: -len("/lib")] or "/"


@pytest.fixture(scope="session")
def hil_facts(hil_device, hil_serial):
    """Prepare the board and collect everything the results record reports.

    debugpy and the debuggee are installed rather than assumed: a run has to
    prove the thing it debugs is the code in this checkout, and installing is
    itself the flow STORY-4.1 delivered.
    """
    from mpremote.debugpy_install import ensure_debugpy_installed

    with hil_serial() as transport:
        ensure_debugpy_installed(transport, str(DEBUGPY_SRC))
        # A module the previous session imported outlives the install, so only
        # a reset interpreter is guaranteed to see the freshly written files.
        transport.enter_raw_repl(soft_reset=True)
        root = _device_root(transport)
        debuggee = "{}/{}.py".format(root.rstrip("/"), TARGET_MODULE)
        transport.fs_writefile(debuggee, TARGET_SRC.read_bytes())

        transport.exec("import os, sys, debugpy")
        # The USB mode is a boot-time choice; `second_cdc` is the build's
        # maximum, and the two can disagree - a board can be built for two CDC
        # interfaces and booted with one. Recording both makes a run
        # reproducible from the record alone, and the second is also the
        # independent probe `test_hil_handshake_caps_match_a_live_probe`
        # compares the launcher's handshake against, so it is written out here
        # in full rather than borrowed from that handshake. Ports without
        # `pyb` have nothing to say to either.
        transport.exec(
            "try:\n import pyb\n _usb_mode = pyb.usb_mode()\nexcept Exception:\n _usb_mode = None\n"
        )
        transport.exec(
            "try:\n import pyb\n pyb.USB_VCP(1)\n _second_cdc = True\n"
            "except Exception:\n _second_cdc = False\n"
        )
        return {
            "device": hil_device,
            "board": os.environ.get(BOARD_ENV) or transport.eval("sys.implementation._build"),
            "machine": transport.eval("os.uname().machine"),
            # The full version string, not `release`: it carries the source
            # commit, which is the only thing that ties a run to a build. The
            # board need not be running a manifest artifact, so nothing else
            # here identifies what produced these results.
            "firmware": transport.eval("os.uname().version"),
            "usb_mode": transport.eval("_usb_mode"),
            "capabilities": dict(
                transport.eval("debugpy.get_capabilities()"),
                second_cdc=transport.eval("_second_cdc"),
            ),
            "debuggee": debuggee,
        }


@pytest.fixture(autouse=True)
def _publish_facts(request):
    """Make the facts available to the end-of-session record writer."""
    if "hil_facts" in request.fixturenames:
        request.session._hil_facts = request.getfixturevalue("hil_facts")


class DeviceOutput:
    """Everything the board printed since a debug session detached.

    `mpremote debug` returns once it has reported the endpoint, leaving the
    board running the target with nobody reading its stdout. The target's own
    completion line is the only signal that it ran to the end - the DAP
    protocol has no event for it here - so the port is read in the background
    for the length of the session.
    """

    def __init__(self, device):
        import serial

        self._port = serial.Serial(device, BAUDRATE, timeout=0.2)
        self._chunks = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        while not self._stop.is_set():
            try:
                data = self._port.read(4096)
            except Exception:
                return
            if data:
                self._chunks.append(data)

    def text(self):
        return b"".join(self._chunks).decode("utf-8", "replace")

    def wait_for(self, needle, timeout):
        deadline = time.monotonic() + timeout
        while needle not in self.text() and time.monotonic() < deadline:
            time.sleep(0.2)
        return needle in self.text()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        with contextlib.suppress(Exception):
            self._port.close()


@pytest.fixture()
def hil_debug_runner(hil_device, hil_facts):
    """Call to start a `mpremote debug` run; returns the handshake payload.

    No `resume` and no `--port`: the command's own soft reset is what clears
    the previous session's listener, and the port is the device's default, so
    the command line carries no address of any kind.

    Calling it again ends the previous run's output capture first. Two
    readers on one tty would each get an arbitrary half of the bytes, so only
    one run at a time may hold the port.
    """
    runs = []

    def _run():
        if runs:
            runs[-1]["device"].close()
        proc = _spawn_debug(["debug", hil_device, f"{TARGET_MODULE}:main"])
        lines, matched = _read_until(proc, "MPDBG-READY ", timeout=60)
        if matched is None:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"never saw MPDBG-READY; output:\n{''.join(lines)}")
        proc.wait(timeout=30)  # the device path detaches once the endpoint is out

        payload = json.loads(matched[matched.index("{") :])
        payload["command_output"] = "".join(lines)
        payload["device"] = DeviceOutput(hil_device)
        runs.append(payload)
        return payload

    try:
        yield _run
    finally:
        for payload in runs:
            payload["device"].close()


@pytest.fixture()
def hil_debug_session(hil_debug_runner):
    """One `mpremote debug` run, for the tests that only need one."""
    return hil_debug_runner()


@pytest.fixture()
def hil_serial_dap_runner(hil_device, hil_dap_device, hil_facts, tmp_path):
    """Call to start a `mpremote debug <named target>` run, DAP on the 2nd CDC.

    The two device paths reach the command through an `mpdebug.toml` because
    that is the only way to configure a `dap_device`; it is written to a
    fresh directory per test and the command is run from there, so the file
    the run reads is built from the environment and nothing about the bench
    is committed. `PYTHONPATH` carries the mpremote under test, which the
    other runs get from their working directory instead.

    Unlike the network runs, these do not detach: mpremote *is* the DAP
    endpoint here, so the process has to outlive the handshake and is only
    reaped at the end of the test.

    A second call is what a user does after a session ends badly, so nothing
    is reset between runs beyond what the command does for itself. The
    board's two ports must be free before it can start, which for a run this
    fixture is still holding means the test has to have ended it.
    """
    (tmp_path / "mpdebug.toml").write_text(
        "[target.hil]\n"
        'kind = "serial"\n'
        f'device = "{hil_device}"\n'
        f'dap_device = "{hil_dap_device}"\n'
        f'program = "{TARGET_MODULE}:main"\n'
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_SUBMODULE_DIR), env.get("PYTHONPATH")]))
    procs = []

    def _run(timeout=60):
        proc = _spawn_debug(["debug", "hil"], env=env, cwd=tmp_path)
        procs.append(proc)
        lines, matched = _read_until(proc, "MPDBG-READY ", timeout=timeout)
        if matched is None:
            pytest.fail(f"never saw MPDBG-READY; output:\n{''.join(lines)}")
        payload = json.loads(matched[matched.index("{") :])
        payload["command_output"] = "".join(lines)
        payload["process"] = proc
        return payload

    try:
        yield _run
    finally:
        # A finished client session ends the bridge and the command with it,
        # so a live process here means the test left one open; either way the
        # board's ports must be free before the next test spawns a run.
        for proc in procs:
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
                proc.wait(timeout=5)


@pytest.fixture()
def hil_serial_dap_session(hil_serial_dap_runner):
    """One serial-DAP run, for the tests that only need one."""
    return hil_serial_dap_runner()


# --- results record -------------------------------------------------------
#
# STORY-6.4 asks every run to leave a dated record behind. It is written from
# the reports pytest already collects rather than from the tests themselves,
# so a scenario cannot claim green by forgetting to record its own failure.
# Anything a scenario measured comes along the same way, via the
# `record_property` fixture, so a number in the record is always a number some
# assertion in that run also had to be satisfied by.

_RESULTS = {}
_MEASUREMENTS = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and "hil" in item.keywords:
        _RESULTS[item.nodeid] = report.outcome
        _MEASUREMENTS.update(report.user_properties)


def pytest_sessionfinish(session):
    if not _RESULTS:
        return
    facts = getattr(session, "_hil_facts", {})
    # A run that got far enough to produce results got past the tree gate, so
    # the state is present; recomputing here would describe the tree at the end
    # of the run, which the run itself has already changed.
    tree = getattr(session, "_hil_tree", None) or _tree_state(_TOP_DIR)
    board = str(facts.get("board", "unknown-board")).replace("/", "_")
    path = _TOP_DIR / "planning" / "{}_hil_{}.md".format(time.strftime("%Y%m%d"), board)
    lines = [
        f"# Hardware-in-loop run: {board}",
        "",
        "Written by `tests/hil/` (STORY-6.4); a rerun overwrites it.",
        "",
        f"- Date: {time.strftime('%Y-%m-%d')}",
        *_tree_record_lines(tree),
        f"- Device: `{facts.get('device', 'unknown')}`",
        f"- Machine: {facts.get('machine', 'unknown')}",
        f"- Firmware: {facts.get('firmware', 'unknown')}",
        f"- USB mode: {facts.get('usb_mode') or 'n/a'}",
        f"- Debuggee on device: `{facts.get('debuggee', 'unknown')}`",
        f"- Probed capabilities: `{facts.get('capabilities', 'unknown')}`",
        "",
        "`serial_dap` is `False` above because these capabilities come from a plain",
        "REPL probe: the key reports which channel a session took, not what the",
        "firmware can do. The serial-DAP scenarios below assert it is `True` in the",
        "handshake of the stream session they start.",
        "",
        "| scenario | result |",
        "| --- | --- |",
    ]
    for nodeid, outcome in sorted(_RESULTS.items()):
        lines.append("| `{}` | {} |".format(nodeid.split("::")[-1], outcome))
    lines.append("")
    if _MEASUREMENTS:
        lines += ["## Measurements", "", "| name | value |", "| --- | --- |"]
        lines += ["| `{}` | {} |".format(k, v) for k, v in sorted(_MEASUREMENTS.items())]
        lines.append("")
    path.write_text("\n".join(lines))
    print(f"\nHIL results written to {path}")
