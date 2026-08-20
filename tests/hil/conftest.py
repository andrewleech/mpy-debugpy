"""Hardware-in-loop fixtures: one real board, driven by the real command.

These tests are opt-in. Point `MPY_DEBUG_HIL_DEVICE` at a board's
`/dev/serial/by-id/...` path and they run; leave it unset and they skip, so
`make test` stays a host-only suite. Nothing here hardcodes a device path, a
board name, or an address - the by-id path comes from the environment and the
debug endpoint comes from the device's own handshake, as it does in
production.

Set `MPY_DEBUG_HIL_BOARD` to name the board in the results record; without it
the record uses the board name the firmware reports for itself.

A run must be attributable to a commit, so it refuses to start from a dirty
tree and records the top-repo commit and both submodule pins in its results.
`MPY_DEBUG_HIL_ALLOW_DIRTY=1` overrides the refusal and lists the uncommitted
paths in the record instead (see `tree_state.py`).

The board needs `board_boot.py` from this directory installed as its `boot.py`
before any of this runs, and a reset after. It brings up WiFi, which the
network scenarios need and the single-UART ones ignore; a board with no
network runs the rest. That cannot be reached from here, since it is decided
before the first `mpremote` connection.

Only one process may hold the board's serial port, and `mpremote debug` needs
it, so nothing here keeps a transport open across a debug run: `hil_serial` is
a context manager, not a live connection.
"""

import contextlib
import json
import os
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
BOARD_ENV = "MPY_DEBUG_HIL_BOARD"
RESET_ENV = "MPY_DEBUG_HIL_RESET_CMD"
DAP_LOG_ENV = "MPY_DEBUG_HIL_DAP_LOG"
DIRECT_ENDPOINT_MARK = "hil_direct_endpoint"
BAUDRATE = 115200

DEBUGPY_SRC = _TOP_DIR / "micropython-lib" / "python-ecosys" / "debugpy" / "debugpy"


def pytest_configure(config):
    config.addinivalue_line("markers", "hil: needs a real board (see tests/hil/conftest.py)")
    config.addinivalue_line(
        "markers",
        f"{DIRECT_ENDPOINT_MARK}: asserts about the board's own endpoint, so never proxied",
    )


def pytest_collection_modifyitems(items):
    """Everything under `tests/hil/` is a hardware test, by definition.

    Hardware scenarios also get one retry, and only against the failure text of
    the parked WiFi fault - see `_PARKED_STALL_SIGNATURES`. Applied as a marker
    rather than as `--reruns` on the command line so it cannot leak into the
    host-only suite, and so the restriction travels with the reason for it.
    """
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker("hil")
            item.add_marker(pytest.mark.flaky(reruns=1, only_rerun=_PARKED_STALL_SIGNATURES))


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
        # The USB mode is a boot-time choice, recorded so a run is
        # reproducible from the record alone. Ports without `pyb` have
        # nothing to say about it.
        transport.exec(
            "try:\n import pyb\n _usb_mode = pyb.usb_mode()\nexcept Exception:\n _usb_mode = None\n"
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
            "capabilities": transport.eval("debugpy.get_capabilities()"),
            "debuggee": debuggee,
        }


@pytest.fixture(autouse=True)
def _publish_facts(request):
    """Make the facts available to the end-of-session record writer."""
    if "hil_facts" in request.fixturenames:
        request.session._hil_facts = request.getfixturevalue("hil_facts")


class DeviceOutput:
    """Everything the board printed for the length of a debug session.

    Where that arrives depends on whether the run detaches. A plain network
    run returns once it has reported the endpoint, leaving the board running
    the target with nobody holding its port, so this opens the port itself. A
    run that stays attached - `--dap-log`, or `--dap-repl` - keeps
    the port and drains it, echoing the board's output to its own stdout
    (`_pump_console` in mpremote's commands.py); opening the port as well
    would put two readers on one tty, each taking an arbitrary share of the
    bytes, so this consumes the command's stdout instead.

    Reading that stdout is not optional for such a run: mpremote's console
    pump writes into it, and a pipe nobody empties stalls the pump, which
    stops the board (`planning/20260813_console_backpressure.md`). Its
    bounded buffer makes that lossy rather than fatal now, but the loss is
    still this suite's evidence going missing.

    The target's own completion line is the only signal that it ran to the
    end - the DAP protocol has no event for it here.
    """

    def __init__(self, device=None, stream=None):
        assert (device is None) != (stream is None), "exactly one source"
        self._port = None
        self._stream = stream
        if device is not None:
            import serial

            self._port = serial.Serial(device, BAUDRATE, timeout=0.2)
        self._chunks = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        while not self._stop.is_set():
            try:
                if self._port is not None:
                    data = self._port.read(4096)
                else:
                    # A blocking read of whatever has arrived: the stream is a
                    # pipe from a process that is still running, so read1()
                    # returns as soon as there is anything rather than waiting
                    # for a full buffer, and b"" only at EOF.
                    data = self._stream.read1(4096)
                    if not data:
                        return
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
        if self._port is not None:
            with contextlib.suppress(Exception):
                self._port.close()


@pytest.fixture()
def hil_debug_runner(request, hil_device, hil_facts, tmp_path):
    """Call to start a `mpremote debug` run; returns the handshake payload.

    No `resume` and no `--port`: the command's own soft reset is what clears
    the previous session's listener, and the port is the device's default, so
    the command line carries no address of any kind.

    Calling it again ends the previous run's output capture first. Two
    readers on one tty would each get an arbitrary half of the bytes, so only
    one run at a time may hold the port.

    `MPY_DEBUG_HIL_DAP_LOG=1` adds `--dap-log`, which is the instrument for
    the network intermittent in the risk register: it records the transcript
    of the session the client saw go quiet. Off by default, and not merely to
    save a hop - the log works by interposing a host-side proxy and reporting
    *its* endpoint, so under it a run no longer reports the board's own
    address, which is the thing three of these scenarios assert. Those carry
    `hil_direct_endpoint` and stay unproxied whatever the environment says.
    That is also the caveat on any hunt: the transport under the flag is not
    the transport that produced the intermittent, so its absence there is not
    evidence.
    """
    dap_log = os.environ.get(DAP_LOG_ENV, "") not in ("", "0")
    if request.node.get_closest_marker(DIRECT_ENDPOINT_MARK):
        dap_log = False
    runs = []
    live = []

    def _run():
        if runs:
            runs[-1]["device"].close()
        args = ["debug", hil_device, f"{TARGET_MODULE}:main"]
        log_path = None
        if dap_log:
            log_path = tmp_path / f"dap-{len(runs)}.jsonl"
            args[1:1] = ["--dap-log", "--dap-log-file", str(log_path)]
        proc = _spawn_debug(args)
        lines, matched = _read_until(proc, "MPDBG-READY ", timeout=60, at_line_start=True)
        if matched is None:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"never saw MPDBG-READY; output:\n{''.join(lines)}")
        if log_path is None:
            proc.wait(timeout=30)  # the device path detaches once the endpoint is out
        else:
            # The proxy is this process, so it has to outlive the handshake;
            # it ends itself when the client session does.
            live.append(proc)

        payload = json.loads(matched[matched.index("{") :])
        payload["command_output"] = "".join(lines)
        # Whichever end of the board's console this run leaves readable; see
        # DeviceOutput. Under --dap-log the command is still holding the port,
        # so its stdout is the only place the board's output now appears.
        payload["device"] = (
            DeviceOutput(stream=proc.stdout) if log_path else DeviceOutput(device=hil_device)
        )
        payload["dap_log"] = log_path
        runs.append(payload)
        return payload

    try:
        yield _run
    finally:
        for proc in live:
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
                proc.wait(timeout=5)
        for payload in runs:
            payload["device"].close()


@pytest.fixture()
def hil_debug_session(hil_debug_runner):
    """One `mpremote debug` run, for the tests that only need one."""
    return hil_debug_runner()


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
_RETRIES = {}

# The one failure text a scenario is retried on: a board that answered earlier
# requests on a connection and then stopped answering. That is the parked WiFi
# fault (`20260814_wifi_stall_investigation_handover.md`), which lives below
# MicroPython and is not this project's to fix. Nothing else is retried - a
# scenario that fails any other way still fails the run on its first attempt,
# and a retried scenario is reported as retried, so the rate the risk register
# tracks stays visible rather than being converted into a slower green run.
_PARKED_STALL_SIGNATURES = [
    r"no response to \w+ \(seq \d+\) within",
    r"no initialize response from",
]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if "hil" not in item.keywords:
        return
    if report.outcome == "rerun":
        _RETRIES[item.nodeid] = _RETRIES.get(item.nodeid, 0) + 1
        return
    if report.when == "call":
        _RESULTS[item.nodeid] = report.outcome
        _MEASUREMENTS.update(report.user_properties)
    elif report.failed:
        # A scenario that broke in setup or teardown never reaches "call", and
        # a record that lists only what ran reads as a full pass. Skips are a
        # different thing and stay out: the prose above the table says which
        # scenarios the arrangement excluded, and why.
        _RESULTS[item.nodeid] = "error"


def pytest_sessionfinish(session):
    facts = getattr(session, "_hil_facts", None)
    if not _RESULTS or facts is None:
        # A record is provenance for a hardware run, and setup errors are
        # results, so results alone no longer imply the board was ever opened -
        # the tree gate and the missing-device skip both fire first. Without
        # facts there is nothing to be provenance for, and writing anyway is
        # self-perpetuating: the untracked record dirties the tree, which fails
        # the gate on the next run, which writes the record again.
        return
    # A run that reached the board got past the tree gate, so the state is
    # present; recomputing here would describe the tree at the end of the run,
    # which the run itself has already changed.
    tree = getattr(session, "_hil_tree", None) or _tree_state(_TOP_DIR)
    board = str(facts.get("board", "unknown-board")).replace("/", "_")
    # One bench arrangement now, so a record is named by its date and board
    # alone. It was previously also named by whether a second CDC interface
    # was supplied, because the serial-DAP scenarios and the single-CDC one
    # needed opposite boots of the same board and neither could be allowed to
    # overwrite the other's record.
    path = _TOP_DIR / "planning" / "{}_hil_{}.md".format(time.strftime("%Y%m%d"), board)
    capabilities = facts.get("capabilities")
    lines = [
        f"# Hardware-in-loop run: {board}",
        "",
        "Written by `tests/hil/` (STORY-6.4); a rerun of the same bench arrangement",
        "overwrites it.",
        "",
        f"- Date: {time.strftime('%Y-%m-%d')}",
        *_tree_record_lines(tree),
        f"- Device: `{facts.get('device', 'unknown')}`",
        f"- Machine: {facts.get('machine', 'unknown')}",
        f"- Firmware: {facts.get('firmware', 'unknown')}",
        f"- USB mode: {facts.get('usb_mode') or 'n/a'}",
        f"- Debuggee on device: `{facts.get('debuggee', 'unknown')}`",
        f"- Probed capabilities: `{capabilities if capabilities is not None else 'unknown'}`",
        "",
        "`repl_dap` is `False` above because these capabilities come from a plain",
        "REPL probe: the key reports which channel a session took, not what the",
        "firmware can do. A scenario that takes it asserts it is `True` in the",
        "handshake of the session it starts.",
        "",
        "| scenario | result |",
        "| --- | --- |",
    ]
    for nodeid, outcome in sorted(_RESULTS.items()):
        retries = _RETRIES.get(nodeid, 0)
        note = " (after {} retr{})".format(retries, "y" if retries == 1 else "ies") if retries else ""
        lines.append("| `{}` | {}{} |".format(nodeid.split("::")[-1], outcome, note))
    lines.append("")
    if _RETRIES:
        lines += [
            "A retry means the board answered earlier requests on a connection and",
            "then stopped answering, which is the parked fault in",
            "`20260814_wifi_stall_investigation_handover.md`. It is retried once and",
            "recorded here so the rate stays visible; nothing else is retried.",
            "",
        ]
    if _MEASUREMENTS:
        lines += ["## Measurements", "", "| name | value |", "| --- | --- |"]
        lines += ["| `{}` | {} |".format(k, v) for k, v in sorted(_MEASUREMENTS.items())]
        lines.append("")
    path.write_text("\n".join(lines))
    print(f"\nHIL results written to {path}")
