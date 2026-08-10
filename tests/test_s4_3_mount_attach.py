"""Host-side pytest coverage for mount-backed attach with staleness guard and generated pathMappings.

Acceptance criteria from STORY-4.3:
- A device target debugs a file that exists only on the host
- Mount-mode session with absolute pathMappings in multi-root layout
- Round-trip property: _filename_as_debugger(_filename_as_debugee(local)) == identity
- Mount teardown on session end, exception, Ctrl-C
- Guard: target module not under source root is refused before boot script
- Adversarial: symlink source root, subpackage target, multiple targets mounting different roots
- Config parsing: relative source, absent, empty, not a string, missing directory, unix+source

`pdb_adapter.py` and `debug_session.py` import `from micropython import const`,
so they are only importable under MicroPython, not the CPython process
pytest runs in - path-translation coverage below runs the real code as a
subprocess script under the built unix firmware
(`test_pdb_adapter_and_attach_path_translation_under_real_interpreter`)
rather than reimplementing the translation logic in the test. Everything
else here imports `mpremote.commands`/`mpremote.mpdebug_config` directly,
which run under CPython, and exercises the real functions (`do_debug`,
`_teardown_mount`, `_stay_attached_mount`, `mpdebug_config.resolve_target`),
never a hand-rolled stand-in for their logic.

The fake transports below stand in for the whole mount RPC layer, so they
can assert `do_debug`'s sequencing (guard before device touch, mount before
boot script, teardown in `finally`, stale `in_raw_repl` never gating the
teardown interrupt) but cannot fail the way a real device does: a short
read stalling `PyboardCommand`, a `SerialIntercept` losing the underlying
port's timeout, or a mount surviving past the point recovery requires a
power cycle. `test_s5_1_mpremote_debug.py::test_read_until_works_through_a_mount_intercept`
and `::test_read_until_enforces_timeout_on_pty` run over a real pty but
construct `SerialIntercept(port, cmd=None)` with no device on the other
end, so they certify `read_until`'s timeout handling through the intercept,
not the `\x18`-prefixed RPC protocol itself. That gap - a live firmware
process actually answering filesystem RPC over a mount, staying answerable
across a stale `in_raw_repl` flag, and remaining usable after teardown - is
what the two `_MountedPtySession` tests at the end of this file exercise
instead.
"""

import json
import os
import signal
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

from mpremote import commands, mpdebug_config  # noqa: E402
from mpremote.commands import CommandError  # noqa: E402
from mpremote.transport_serial import SerialTransport  # noqa: E402

from helpers import (  # noqa: E402
    PerfServer,
    debug_args,
    drain_lines,
    set_breakpoints,
    wait_for_msg,
    wait_for_prefixed_line,
)
from pty_device import PtyDevice  # noqa: E402

_MICROPYTHON = Path(
    os.environ.get(
        "MPY_DEBUG_FIRMWARE",
        _TOP_DIR / "micropython/ports/unix/build-standard/micropython",
    )
)
_MICROPYPATH = "{}:{}".format(
    _TOP_DIR / "micropython-lib/python-ecosys/debugpy", _TOP_DIR / "micropython-lib"
)


def _firmware_has_settrace():
    """Probe the built firmware for sys.settrace rather than trusting its name."""
    if not _MICROPYTHON.exists():
        return False
    try:
        result = subprocess.run(
            [str(_MICROPYTHON), "-c", "import sys; print(hasattr(sys, 'settrace'))"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "True"


requires_settrace_firmware = pytest.mark.skipif(
    not _firmware_has_settrace(),
    reason=f"{_MICROPYTHON} lacks sys.settrace (needs micropython #8767 composed in)",
)


class _FakeState:
    """Minimal stand-in for main.State: tracks did_action() and holds a transport."""

    def __init__(self, transport):
        self.transport = transport
        self._did_action = False

    def ensure_raw_repl(self, soft_reset=None):
        pass

    def did_action(self):
        self._did_action = True

    def run_repl_on_completion(self):
        return not self._did_action


class _FakeMountTransport:
    """Fake SerialTransport that also implements the mount primitives.

    `read_until`/`exec_raw_no_follow` mirror test_s5_1's `_FakeTransport`
    (the scripted bytes are one continuous stream; each call returns
    everything up to and including the first occurrence of `ending`).
    `mount_local`/`umount_local`/`enter_raw_repl` are recorded rather than
    driving any real RPC, so what a test built on this exercises is
    `do_debug`'s own sequencing of them, not the mount protocol itself.
    """

    # A real transport carries the mount point as a class attribute, and the
    # warnings do_debug/_teardown_mount emit name it rather than a literal.
    fs_hook_mount = SerialTransport.fs_hook_mount

    def __init__(self, lines, device_name="/dev/fake-tty"):
        self._buf = b"".join(lines)
        self.exec_calls = []
        self.device_name = device_name
        self.mounted = False
        self.mount_calls = []
        self.umount_calls = 0
        self.in_raw_repl = True
        self.enter_raw_repl_calls = 0
        # _teardown_mount unwraps this back to `orig_serial` (or itself, if
        # there is none) regardless of outcome; a plain sentinel with
        # neither attribute exercises the "no orig_serial" branch the same
        # way an interruption during mount_local's own serial swap would.
        self.serial = object()

    def exec_raw_no_follow(self, command, timeout_overall=None, timeout_overall_strict=False):
        self.exec_calls.append(command)

    def read_until(
        self,
        min_num_bytes,
        ending,
        timeout=10,
        data_consumer=None,
        timeout_overall=None,
        timeout_overall_strict=False,
    ):
        idx = self._buf.find(ending)
        end = len(self._buf) if idx == -1 else idx + len(ending)
        chunk, self._buf = self._buf[:end], self._buf[end:]
        return chunk

    def enter_raw_repl(self, soft_reset=True, timeout_overall=10, timeout_overall_strict=False):
        self.enter_raw_repl_calls += 1
        self.in_raw_repl = True

    def mount_local(self, source_root):
        self.mount_calls.append(source_root)
        self.mounted = True

    def umount_local(self, timeout_overall=None, timeout_overall_strict=False):
        self.umount_calls += 1
        self.mounted = False


def _args(**overrides):
    defaults = {"target": "u0", "program": "target:main"}
    defaults.update(overrides)
    return debug_args(**defaults)


# ==============================================================================
# Round-trip path translation and attach-time pathMappings normalisation,
# against the real interpreter (see module docstring for why).
# ==============================================================================

_ROUND_TRIP_SCRIPT = """
import json
from debugpy.server.pdb_adapter import PdbAdapter
from debugpy.server.debug_session import DebugSession

results = {}

# Round trip through a single mapping.
pdb = PdbAdapter()
pdb.path_mappings = [("/remote", "/home/dev/project/src")]
local = "/home/dev/project/src/module/submodule/file.py"
debugee = pdb._filename_as_debugee(local)
results["single_mapping_debugee"] = debugee
results["single_mapping_round_trip"] = pdb._filename_as_debugger(debugee)

# A sibling directory that only shares the mapped root's name as a string
# prefix must not be translated - boundary-aware matching, not startswith.
sibling = "/home/dev/project/src-old/file.py"
results["sibling_untranslated"] = pdb._filename_as_debugee(sibling)

# First-match-wins across two mappings whose roots nest, in both directions.
pdb2 = PdbAdapter()
pdb2.path_mappings = [
    ("/remote/pkg", "/home/dev/src/pkg"),
    ("/remote", "/home/dev/src"),
]
nested_local = "/home/dev/src/pkg/subpkg/module.py"
nested_debugee = pdb2._filename_as_debugee(nested_local)
results["nested_first_match_debugee"] = nested_debugee
results["nested_round_trip"] = pdb2._filename_as_debugger(nested_debugee)


# _handle_attach strips trailing slashes off pathMappings before pdb_adapter
# ever sees them, so "/remote/" and "/home/dev/src/" match the same way
# "/remote" and "/home/dev/src" do. Called unbound against a lightweight
# stand-in rather than through DebugSession.__init__, which needs a real
# client socket that has nothing to do with what's under test here.
class FakeChannel:
    def send_response(self, *a, **kw):
        pass


class FakeSession:
    def __init__(self):
        self.pdb = PdbAdapter()
        self.channel = FakeChannel()
        self.debug_logging = False

    def _debug_print(self, message):
        pass

    def _trace_function(self, frame, event, arg):
        pass


session = FakeSession()
DebugSession._handle_attach(
    session, 1, {"pathMappings": [{"localRoot": "/home/dev/src/", "remoteRoot": "/remote/"}]}
)
results["attach_normalised_mappings"] = session.pdb.path_mappings
attached_debugee = session.pdb._filename_as_debugee("/home/dev/src/module.py")
results["attach_debugee"] = attached_debugee
results["attach_round_trip"] = session.pdb._filename_as_debugger(attached_debugee)

print("RESULT " + json.dumps(results))
"""


@requires_settrace_firmware
def test_pdb_adapter_and_attach_path_translation_under_real_interpreter():
    """_filename_as_debugee/_filename_as_debugger and _handle_attach's
    pathMappings normalisation, exercised against the real interpreter.
    """
    env = dict(os.environ)
    env["MICROPYPATH"] = _MICROPYPATH
    result = subprocess.run(
        [str(_MICROPYTHON), "-c", _ROUND_TRIP_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"script failed: {result.stdout}\n{result.stderr}"
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("RESULT "))
    results = json.loads(line[len("RESULT ") :])

    assert results["single_mapping_debugee"] == "/remote/module/submodule/file.py"
    assert (
        results["single_mapping_round_trip"]
        == "/home/dev/project/src/module/submodule/file.py"
    )

    assert results["sibling_untranslated"] == "/home/dev/project/src-old/file.py", (
        "a sibling directory sharing the mapped root's name as a string prefix "
        "must not be translated"
    )

    assert results["nested_first_match_debugee"] == "/remote/pkg/subpkg/module.py"
    assert results["nested_round_trip"] == "/home/dev/src/pkg/subpkg/module.py"

    assert results["attach_normalised_mappings"] == [["/remote", "/home/dev/src"]]
    assert results["attach_debugee"] == "/remote/module.py"
    assert results["attach_round_trip"] == "/home/dev/src/module.py"


_DEAD_CHANNEL_SCRIPT = """
import json
from debugpy.server.pdb_adapter import PdbAdapter
from debugpy.server.debug_session import DebugSession

results = {}


class FakeSock:
    def __init__(self, fail_on_none):
        self.fail_on_none = fail_on_none

    def settimeout(self, value):
        if value is None and self.fail_on_none:
            raise OSError(9, "EBADF")

    def close(self):
        pass


class FakeChannel:
    def __init__(self, closed, fail_on_none):
        self.closed = closed
        self.sock = FakeSock(fail_on_none)

    def recv_message(self):
        return None

    def close(self):
        self.closed = True


class Session:
    process_pending_messages = DebugSession.process_pending_messages
    disconnect = DebugSession.disconnect

    def __init__(self, closed, fail_on_none):
        self.channel = FakeChannel(closed, fail_on_none)
        self.pdb = PdbAdapter()
        self.connected = True
        self._pumping = False

    def _handle_message(self, message):
        raise AssertionError("no message to handle")


# A `disconnect` request handled by the pump runs the whole session teardown,
# so on the way out of that request the channel is already gone.
after_disconnect = Session(closed=True, fail_on_none=True)
try:
    after_disconnect.process_pending_messages()
    results["after_disconnect_raised"] = None
except Exception as er:
    results["after_disconnect_raised"] = repr(er)

# A client that vanishes without sending `disconnect` gets there without the
# flag set: the socket operation itself is what reports the channel is gone.
vanished = Session(closed=False, fail_on_none=True)
try:
    vanished.process_pending_messages()
    results["vanished_raised"] = None
except Exception as er:
    results["vanished_raised"] = repr(er)
results["vanished_connected"] = vanished.connected

# A live channel is left in blocking mode with the session untouched.
alive = Session(closed=False, fail_on_none=False)
alive.process_pending_messages()
results["alive_connected"] = alive.connected

print("RESULT " + json.dumps(results))
"""


@requires_settrace_firmware
def test_process_pending_messages_never_raises_into_the_debugged_program():
    """A dead DAP channel ends the session; it does not kill the target.

    Every caller of `process_pending_messages` is `_trace_function`, so an
    exception escaping it lands in whichever line of the debugged program was
    being traced and kills that program with an errno belonging to the debug
    channel. Restoring the socket's blocking mode is the operation that does
    this: it sits in a `finally`, and the loop it guards is what closes the
    socket when it handles a `disconnect` request. Downstream, mpremote's
    `_teardown_mount` sees that dead program's traceback instead of a device
    at a prompt, so this is what stands between an ordinary session end and a
    user being told to power cycle a board that is fine.
    """
    env = dict(os.environ)
    env["MICROPYPATH"] = _MICROPYPATH
    result = subprocess.run(
        [str(_MICROPYTHON), "-c", _DEAD_CHANNEL_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"script failed: {result.stdout}\n{result.stderr}"
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("RESULT "))
    results = json.loads(line[len("RESULT ") :])

    assert results["after_disconnect_raised"] is None, results["after_disconnect_raised"]
    assert results["vanished_raised"] is None, results["vanished_raised"]
    assert results["vanished_connected"] is False, (
        "a channel that fails a socket operation is gone, so the session must end "
        "rather than leave a trace function installed to pump it"
    )
    assert results["alive_connected"] is True, "a live channel must not end the session"


# ==============================================================================
# Guard: target module not under source root, or source root missing,
# refused from host state alone before the device is touched
# ==============================================================================


def test_guard_module_not_under_source_root_raises_before_device_touched(monkeypatch, tmp_path):
    """A target module that can't resolve under source root is refused before connect.

    do_connect is monkeypatched to fail loudly if called at all: this proves
    the guard runs from host state alone, ahead of any raw-REPL round trip,
    rather than merely failing eventually after connecting.
    """

    def _must_not_connect(state, device=None):
        raise AssertionError("do_connect must not run once the module guard has already failed")

    monkeypatch.setattr(commands, "do_connect", _must_not_connect)

    source_dir = tmp_path / "source"
    source_dir.mkdir()  # no target.py under here

    state = _FakeState(_FakeMountTransport([]))
    args = _args(source=str(source_dir))

    with pytest.raises(CommandError, match="does not resolve under source root"):
        commands.do_debug(state, args)


def test_guard_missing_source_directory_raises_before_device_touched(monkeypatch, tmp_path):
    """A --source naming a directory that doesn't exist is refused before connect."""

    def _must_not_connect(state, device=None):
        raise AssertionError("do_connect must not run once the source guard has already failed")

    monkeypatch.setattr(commands, "do_connect", _must_not_connect)

    missing = tmp_path / "does_not_exist"
    state = _FakeState(_FakeMountTransport([]))
    args = _args(source=str(missing))

    with pytest.raises(CommandError, match="is not a directory"):
        commands.do_debug(state, args)


# ==============================================================================
# Mount lifecycle: mounted before the boot script, absolute sys.path,
# generated pathMappings reach the MPDBG-READY line, unmounted in `finally`
# ==============================================================================


def test_do_debug_mount_lifecycle_mounts_boots_reports_and_tears_down(
    monkeypatch, capsys, tmp_path
):
    """do_debug's mount branch: mount before boot, absolute sys.path, pathMappings, umount.

    `_stay_attached_mount` is monkeypatched to a no-op: a real mounted
    session blocks there for Ctrl-C, which is D-Q13's whole point (see the
    module docstring) but has nothing to wait for in a test that supplies
    no interrupt.
    """
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)
    monkeypatch.setattr(commands, "_stay_attached_mount", lambda message, pump_failed=None: None)

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "target.py").write_text("def main():\n    return 42\n")

    handshake = {"host": "192.0.2.10", "port": 5678, "caps": {"settrace": True}}
    transport = _FakeMountTransport(
        [("MPDBG-READY " + json.dumps(handshake) + "\n").encode()]
    )
    state = _FakeState(transport)
    args = _args(source=str(source_dir))

    result = commands.do_debug(state, args)

    expected_root = os.path.realpath(str(source_dir))
    assert transport.mount_calls == [expected_root], "mount_local must run before the boot script"

    boot_script = transport.exec_calls[0]
    assert "sys.path.remove('')" in boot_script
    assert f"sys.path.insert(0, '{SerialTransport.fs_hook_mount}')" in boot_script, (
        "the boot script must root sys.path at the mount point so co_filename "
        "comes out absolute and matches a pathMapping"
    )

    assert result["host"] == handshake["host"]
    assert result["port"] == handshake["port"]
    assert result["caps"] == handshake["caps"]

    out = capsys.readouterr().out
    mpdbg_line = next(line for line in out.splitlines() if line.startswith("MPDBG-READY "))
    payload = json.loads(mpdbg_line[len("MPDBG-READY ") :])
    assert payload["pathMappings"] == [
        {"localRoot": expected_root, "remoteRoot": SerialTransport.fs_hook_mount}
    ]

    assert transport.umount_calls == 1, "the mount must be torn down once do_debug returns"
    assert transport.mounted is False


def test_do_debug_tears_down_mount_on_handshake_failure(monkeypatch, tmp_path):
    """A device that never produces a handshake is still unmounted (finding: teardown on exception).

    The mount is established before exec_raw_no_follow; a transport that
    then produces nothing readable makes `_read_mpdbg_ready` raise -
    `finally` must still run `umount_local`, not just the two success
    exits `do_debug` has elsewhere.
    """
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "target.py").write_text("def main():\n    return 42\n")

    transport = _FakeMountTransport([])  # nothing to read: the handshake wait times out
    state = _FakeState(transport)
    args = _args(source=str(source_dir), timeout=0.2)

    with pytest.raises(CommandError):
        commands.do_debug(state, args)

    assert transport.mount_calls == [os.path.realpath(str(source_dir))]
    assert transport.umount_calls == 1, "a failed handshake must not leave the mount up"
    assert transport.mounted is False


# ==============================================================================
# _teardown_mount: interrupts unconditionally (in_raw_repl cannot be trusted
# to reflect device state - see the module docstring), and reports (rather
# than swallows) a mount that survives teardown
# ==============================================================================


class _FakeTeardownTransport:
    fs_hook_mount = SerialTransport.fs_hook_mount

    def __init__(self, in_raw_repl):
        # `in_raw_repl` is accepted for every test below to construct with,
        # but _teardown_mount no longer reads it: mpremote's own flag only
        # tracks its last requested mode switch, not the device's live
        # state, and stays stale-True for as long as a debugged program
        # keeps running after exec_raw_no_follow started it - which is
        # exactly when the interrupt below is needed most.
        self.in_raw_repl = in_raw_repl
        self.enter_raw_repl_calls = 0
        self.umount_calls = 0
        self.device_name = "/dev/fake-tty"
        self.mounted = True
        self.serial = object()

    def enter_raw_repl(self, soft_reset=True, timeout_overall=None, timeout_overall_strict=False):
        self.enter_raw_repl_calls += 1
        self.in_raw_repl = True

    def umount_local(self, timeout_overall=None, timeout_overall_strict=False):
        self.umount_calls += 1


def test_teardown_mount_always_interrupts_regardless_of_stale_in_raw_repl_flag():
    """The interrupt runs unconditionally, whatever `in_raw_repl` last recorded.

    `exec_raw_no_follow` starts the debugged program without ever touching
    `in_raw_repl`, so the flag reads stale-True for as long as that program
    keeps running - a device state `_teardown_mount` must not mistake for
    "already sitting at a raw-REPL prompt, no interrupt needed".
    """
    for in_raw_repl in (False, True):
        transport = _FakeTeardownTransport(in_raw_repl=in_raw_repl)
        commands._teardown_mount(transport)
        assert transport.enter_raw_repl_calls == 1
        assert transport.umount_calls == 1


def test_teardown_mount_clears_mounted_state_on_success():
    """A clean teardown leaves `mounted` False so a later do_disconnect has nothing to redo."""
    transport = _FakeTeardownTransport(in_raw_repl=True)
    commands._teardown_mount(transport)
    assert transport.mounted is False


def test_teardown_mount_reports_and_swallows_ordinary_failure(capsys):
    """A mount that survives teardown is reported with the power-cycle hint, not swallowed silently."""

    class _FailingTransport(_FakeTeardownTransport):
        def umount_local(self, timeout_overall=None, timeout_overall_strict=False):
            raise OSError("device not responding")

    transport = _FailingTransport(in_raw_repl=True)
    commands._teardown_mount(transport)  # does not raise: reported on stderr instead
    err = capsys.readouterr().err
    assert "power cycle" in err
    assert "device not responding" in err


def test_teardown_mount_clears_mounted_state_even_on_failure(capsys):
    """A device too wedged to unmount still has `mounted` cleared here.

    Otherwise a caller's own later teardown (do_disconnect) would retry the
    same doomed enter_raw_repl/umount_local round trip against a device
    already known not to be responding.
    """

    class _FailingTransport(_FakeTeardownTransport):
        def umount_local(self, timeout_overall=None, timeout_overall_strict=False):
            raise OSError("device not responding")

    transport = _FailingTransport(in_raw_repl=True)
    commands._teardown_mount(transport)
    capsys.readouterr()  # drain the expected warning; not what this test checks
    assert transport.mounted is False


def test_teardown_mount_does_not_claim_a_power_cycle_when_the_device_answered(capsys):
    """A device that ran the statement and reported an error is not a wedged device.

    `TransportExecError` only exists because the raw REPL took a command and
    handed a result back, so "only a power cycle clears it" would be false -
    and it is the advice a user acts on, so getting it wrong costs them a
    reflash of whatever state the board was holding.
    """

    class _ExecErrorTransport(_FakeTeardownTransport):
        def umount_local(self, timeout_overall=None, timeout_overall_strict=False):
            raise commands.TransportExecError(1, "OSError: [Errno 22] EINVAL\r\n")

    transport = _ExecErrorTransport(in_raw_repl=True)
    commands._teardown_mount(transport)
    err = capsys.readouterr().err
    assert "power cycle" not in err, err
    assert "still answering" in err
    assert "EINVAL" in err
    assert transport.mounted is False


def test_teardown_mount_warning_stays_one_line_when_the_device_sends_a_traceback(capsys):
    """A device-side traceback is reduced to its last line before interpolation.

    The whole traceback in a one-line warning buries the exception under
    frames and leaves the visible first line reading nothing but "Traceback
    (most recent call last):".
    """
    device_traceback = (
        "Traceback (most recent call last):\r\n"
        '  File "/remote/app.py", line 4, in main\r\n'
        "OSError: [Errno 9] EBADF\r\n"
    )

    class _TracebackTransport(_FakeTeardownTransport):
        def umount_local(self, timeout_overall=None, timeout_overall_strict=False):
            raise commands.TransportExecError(1, device_traceback)

    transport = _TracebackTransport(in_raw_repl=True)
    commands._teardown_mount(transport)
    err = capsys.readouterr().err
    assert err.count("\n") == 1, f"the warning must be a single line: {err!r}"
    assert "Traceback" not in err
    assert "OSError: [Errno 9] EBADF" in err


def test_teardown_mount_reports_and_reraises_keyboard_interrupt(capsys):
    """A second Ctrl-C during teardown is reported, then still ends the process."""

    class _InterruptedTransport(_FakeTeardownTransport):
        def umount_local(self, timeout_overall=None, timeout_overall_strict=False):
            raise KeyboardInterrupt()

    transport = _InterruptedTransport(in_raw_repl=True)
    with pytest.raises(KeyboardInterrupt):
        commands._teardown_mount(transport)
    assert "power cycle" in capsys.readouterr().err
    assert transport.mounted is False


# ==============================================================================
# _stay_attached_mount: Ctrl-C is the normal, expected end of a mounted
# plain-network session, not a fault
# ==============================================================================


def test_stay_attached_mount_returns_normally_on_keyboard_interrupt(monkeypatch, capsys):
    """Ctrl-C during a mounted stay-attached wait returns normally, not sys.exit(1).

    Unlike `_stay_attached` (which watches a live client session, so an
    interruption there is a fault), a mounted plain-network session has no
    client-facing object of its own to watch - Ctrl-C is the *only* way this
    ever returns.
    """

    def _raise(*_a, **_kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(commands.time, "sleep", _raise)
    commands._stay_attached_mount("test message")  # must return, not raise or sys.exit
    assert "test message" in capsys.readouterr().out


def test_stay_attached_mount_returns_immediately_once_pump_already_failed(monkeypatch, capsys):
    """A pump that failed before the wait even starts ends it on the first check.

    `_pump_mount` has already printed its own warning by the time it sets
    `failed_event` (asserted separately in
    `test_pump_mount_reports_and_sets_failed_event_on_unexpected_read_error`),
    so this wait must add nothing further of its own.
    """

    def _must_not_sleep(*_a, **_kw):
        raise AssertionError("must return on the first pump_failed check, before ever sleeping")

    monkeypatch.setattr(commands.time, "sleep", _must_not_sleep)
    pump_failed = threading.Event()
    pump_failed.set()
    commands._stay_attached_mount("test message", pump_failed=pump_failed)
    assert capsys.readouterr().out == "test message\n"


class _RaisingTransport:
    """Fails every read_until call, as a device that stopped mid-RPC-exchange would.

    `on_read`, when given, runs just before the raise - the hook the second
    test below uses to simulate `stop_event` being set by another thread
    while this call is in flight, rather than before it was ever made.
    """

    def __init__(self, device_name="/dev/fake-tty", on_read=None):
        self.device_name = device_name
        self.read_calls = 0
        self._on_read = on_read

    def read_until(
        self,
        min_num_bytes,
        ending,
        timeout=10,
        data_consumer=None,
        timeout_overall=None,
        timeout_overall_strict=False,
    ):
        self.read_calls += 1
        if self._on_read is not None:
            self._on_read()
        raise commands.TransportError("device stopped responding mid-command")


def test_pump_mount_reports_and_sets_failed_event_on_unexpected_read_error(capsys):
    """A read failure the caller didn't ask for (stop_event unset) sets failed_event and warns.

    This is the mechanism `_stay_attached`/`_stay_attached_mount` rely on to
    notice a mount's filesystem RPC has died on a connection neither of them
    reads from directly (see `_pump_mount`'s own docstring for why nothing
    else would ever see this failure otherwise).
    """
    transport = _RaisingTransport()
    stop_event = threading.Event()
    failed_event = threading.Event()

    commands._pump_mount(transport, stop_event, failed_event)

    assert transport.read_calls == 1
    assert failed_event.is_set()
    err = capsys.readouterr().err
    assert "power cycle" in err
    assert "device stopped responding mid-command" in err


def test_pump_mount_requested_stop_reports_nothing_and_leaves_failed_event_clear(capsys):
    """A read failure racing a requested stop is not reported as a fault.

    `do_debug`'s teardown sets `stop_event` and only then reassigns/closes
    the transport's serial object; a read already in flight at that moment
    can still raise, but the loop's `while not stop_event.is_set()` never
    even re-enters after this call returns, so `_pump_mount` itself has to
    tell the two cases apart from inside the `except` - `on_read` sets
    `stop_event` from within the call to reproduce the race rather than
    setting it beforehand, which the loop's own guard would instead turn
    into never calling `read_until` at all.
    """
    stop_event = threading.Event()
    failed_event = threading.Event()
    transport = _RaisingTransport(on_read=stop_event.set)

    commands._pump_mount(transport, stop_event, failed_event)

    assert not failed_event.is_set()
    assert capsys.readouterr().err == ""


# ==============================================================================
# Adversarial cases
# ==============================================================================


def test_adversarial_symlink_source_root(tmp_path):
    """Source root can be a symlink; it is resolved to an absolute realpath."""
    real_source = tmp_path / "real_src"
    real_source.mkdir()
    (real_source / "target.py").write_text("def main(): pass\n")

    symlink_source = tmp_path / "linked_src"
    symlink_source.symlink_to(real_source)

    (tmp_path / "mpdebug.toml").write_text(
        f"""
        [target.symlink_target]
        kind = "serial"
        device = "/dev/ttyUSB0"
        source = "{symlink_source}"
        """
    )

    target = mpdebug_config.resolve_target("symlink_target", start_dir=tmp_path)

    assert os.path.isabs(target.source)
    assert target.source == os.path.realpath(str(symlink_source))


def test_adversarial_subpackage_target(tmp_path):
    """Target module in a subpackage resolves correctly."""
    source_dir = tmp_path / "src"
    source_dir.mkdir()

    pkg_dir = source_dir / "mypackage"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "submodule.py").write_text("def main(): pass\n")

    module_path, package_path = commands._module_source_path(
        str(source_dir), "mypackage.submodule"
    )

    assert module_path == str(source_dir / "mypackage" / "submodule.py")
    assert os.path.isfile(module_path)


def test_adversarial_two_targets_different_source_roots(tmp_path):
    """Two targets in one config can mount different source roots."""
    src1 = tmp_path / "src1"
    src2 = tmp_path / "src2"
    src1.mkdir()
    src2.mkdir()

    (src1 / "app1.py").write_text("def main(): pass\n")
    (src2 / "app2.py").write_text("def main(): pass\n")

    (tmp_path / "mpdebug.toml").write_text(
        f"""
        [target.first]
        kind = "serial"
        device = "/dev/ttyUSB0"
        program = "app1:main"
        source = "{src1}"

        [target.second]
        kind = "serial"
        device = "/dev/ttyUSB1"
        program = "app2:main"
        source = "{src2}"
        """
    )

    target1 = mpdebug_config.resolve_target("first", start_dir=tmp_path)
    target2 = mpdebug_config.resolve_target("second", start_dir=tmp_path)

    assert target1.source == str(src1)
    assert target2.source == str(src2)
    assert target1.source != target2.source


# ==============================================================================
# Config parsing
# ==============================================================================


def test_config_source_relative_to_config_file(tmp_path):
    """Relative source path is resolved against the config directory."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    source_dir = config_dir / "src"
    source_dir.mkdir()
    (source_dir / "target.py").write_text("def main(): pass\n")

    (config_dir / "mpdebug.toml").write_text(
        """
        [target.relative_source]
        kind = "serial"
        device = "/dev/ttyUSB0"
        source = "src"
        """
    )

    target = mpdebug_config.resolve_target("relative_source", start_dir=config_dir)

    assert os.path.isabs(target.source)
    assert target.source == str(source_dir)


def test_config_source_absent_means_device_resident(tmp_path):
    """Absent source key means the program is device-resident (no mount)."""
    (tmp_path / "mpdebug.toml").write_text(
        """
        [target.device_resident]
        kind = "serial"
        device = "/dev/ttyUSB0"
        program = "app:main"
        """
    )

    target = mpdebug_config.resolve_target("device_resident", start_dir=tmp_path)
    assert target.source is None, "Absent source should result in source=None"


def test_config_source_empty_is_rejected(tmp_path):
    """Empty source string is rejected at config load."""
    (tmp_path / "mpdebug.toml").write_text(
        """
        [target.empty_source]
        kind = "serial"
        device = "/dev/ttyUSB0"
        source = ""
        """
    )

    with pytest.raises(CommandError, match="empty 'source'"):
        mpdebug_config.resolve_target("empty_source", start_dir=tmp_path)


def test_config_source_not_a_string_is_rejected(tmp_path):
    """Source field must be a string, not a number or list."""
    (tmp_path / "mpdebug.toml").write_text(
        """
        [target.bad_source_type]
        kind = "serial"
        device = "/dev/ttyUSB0"
        source = 123
        """
    )

    with pytest.raises(CommandError, match="source must be a string"):
        mpdebug_config.resolve_target("bad_source_type", start_dir=tmp_path)


def test_config_unix_target_with_source_is_rejected(tmp_path):
    """A unix target naming a source is rejected: it already runs from the host
    filesystem, so there is nothing to mount."""
    (tmp_path / "mpdebug.toml").write_text(
        """
        [target.unix_with_source]
        kind = "unix"
        source = "./src"
        """
    )

    with pytest.raises(CommandError, match="there is nothing to mount"):
        mpdebug_config.resolve_target("unix_with_source", start_dir=tmp_path)


def test_config_source_missing_directory_not_rejected_at_load_time(tmp_path):
    """A source naming a directory that doesn't exist yet is accepted at config load.

    Existence is checked once, at the point of use in `do_debug` (see
    test_guard_missing_source_directory_raises_before_device_touched) - an
    isdir check at load time would fail every target in the file the moment
    any one of them names a source root that doesn't (yet) exist, not just
    the one actually being run.
    """
    nonexistent = tmp_path / "does_not_exist"
    (tmp_path / "mpdebug.toml").write_text(
        f"""
        [target.missing_source]
        kind = "serial"
        device = "/dev/ttyUSB0"
        source = "{nonexistent}"
        """
    )

    target = mpdebug_config.resolve_target("missing_source", start_dir=tmp_path)
    assert target.source == str(nonexistent)


# ==============================================================================
# End-to-end: a real device mounting a real host directory, over a real pty,
# stopped mid-session and torn down by an external signal rather than a
# fake transport's scripted return value
#
# Two endings, because teardown can only be as clean as the device's state
# allows and the two states call for opposite things from the user. A client
# that has gone releases the target - `process_pending_messages` sees the dead
# channel, ends the session, and the untraced program runs to its own end and
# back to a raw-REPL prompt - so teardown finds a device that answers and
# unmounts it silently. A client still holding the target at a breakpoint
# leaves nothing on the device reading the port, and an interrupt cannot reach
# a program parked in `wait_for_continue`, so teardown can only time out and
# say so.
# ==============================================================================


class _MountedPtySession:
    """A live `mpremote debug --source` session over a real pty, paused at a breakpoint.

    A real unix-firmware process is the device, connected through a real pty
    pair exactly as `mpremote debug` would be over serial hardware; `mpremote
    resume debug --source ...` runs as its own subprocess (not in-process), so
    `_exit_on_signal` actually registers on that process's own main thread and
    a real signal exercises the same path Ctrl-C would. The module under debug
    (`app.py`) exists only under the mounted host directory, never copied onto
    the device, so reaching a breakpoint in it at all already proves the mount
    served real filesystem RPC rather than a canned response.

    Being parked at a breakpoint is what makes both endings below meaningful:
    it is the state `in_raw_repl` reads stale-True in (`exec_raw_no_follow`
    started the program and never touched the flag), the state
    `_teardown_mount`'s unconditional interrupt exists for.
    """

    BREAKPOINT_LINE = 4  # `total += i`, hit on the first loop iteration

    def __init__(self, tmp_path, port):
        self.source_dir = tmp_path / "src"
        self.source_dir.mkdir()
        self.app_py = self.source_dir / "app.py"
        self.finished_marker = self.source_dir / "finished"
        # The last line writes back through the mount, which is the only way
        # this process can see the released target reach its own end:
        # mpremote's mount pump discards the device's console output, so the
        # `print` never arrives here. Writing it proves the mount still serves
        # RPC after the debug client has gone, and its content proves the loop
        # ran to completion rather than the file merely being created.
        self.app_py.write_text(
            "def main():\n"
            "    total = 0\n"
            "    for i in range(3):\n"
            "        total += i\n"
            "    print('total', total)\n"
            f"    with open('{SerialTransport.fs_hook_mount}/finished', 'w') as f:\n"
            "        f.write(str(total))\n"
        )
        self.port = port
        self.stdout_lines = []
        self.stderr_lines = []
        self.dap_server = None
        self.device = PtyDevice(_MICROPYTHON, _MICROPYPATH)
        self.mpremote_proc = None
        self._threads = []

    @property
    def stderr_text(self):
        return "".join(self.stderr_lines)

    def _mpremote_output(self):
        return f"mpremote stdout {''.join(self.stdout_lines)!r}; stderr {self.stderr_text!r}"

    def start(self):
        """Bring the session up to a breakpoint hit in the mounted-only module."""
        self.device.start()

        self.mpremote_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mpremote",
                "resume",
                "debug",
                "--port",
                str(self.port),
                "--timeout",
                "15",
                "--source",
                str(self.source_dir),
                self.device.path,
                "app:main",
            ],
            cwd=str(_SUBMODULE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for stream, sink in (
            (self.mpremote_proc.stdout, self.stdout_lines),
            (self.mpremote_proc.stderr, self.stderr_lines),
        ):
            thread = threading.Thread(target=drain_lines, args=(stream, sink), daemon=True)
            thread.start()
            self._threads.append(thread)

        handshake_line = wait_for_prefixed_line(self.stdout_lines, "MPDBG-READY ", timeout=20)
        assert handshake_line is not None, (
            f"mpremote never reported its handshake; stdout: {self.stdout_lines!r} "
            f"stderr: {self.stderr_lines!r}"
        )
        handshake = json.loads(handshake_line[len("MPDBG-READY ") :])
        assert handshake["pathMappings"] == [
            {"localRoot": str(self.source_dir), "remoteRoot": SerialTransport.fs_hook_mount}
        ], (
            "generated pathMappings must be the absolute source root, not a "
            "placeholder a client fills in"
        )

        self.dap_server = PerfServer("test-client", handshake["host"], handshake["port"])
        self.dap_server.start()
        # As in fixtures/attach_server.py: a real client sends `attach` with
        # exactly the pathMappings the tool generated, never ones it wrote
        # itself - there is nothing else for it to have written, since the
        # device's own filesystem has no notion of where `source_dir` lives.
        self.dap_server.client.send_request(
            "attach",
            {
                "name": "Attach to MicroPython",
                "type": "debugpy",
                "request": "attach",
                "connect": {"host": handshake["host"], "port": handshake["port"]},
                "pathMappings": handshake["pathMappings"],
                "justMyCode": True,
            },
        )
        assert wait_for_msg(self.dap_server, response="attach", timeout=10), "no attach response"

        assert set_breakpoints(
            self.dap_server, str(self.app_py), [self.BREAKPOINT_LINE], wait=True
        ), "setBreakpoints failed"

        self.dap_server.client.configuration_done()
        stopped = wait_for_msg(self.dap_server, event="stopped", timeout=15)
        assert stopped is not None, "configurationDone produced no stopped event"
        assert stopped.body.get("reason") == "breakpoint", stopped.body

        # Confirms debug_session.py's consumption of the mount-generated
        # mapping on a live session: the reported frame source is the real
        # host path under source_dir, not the device's own /remote view of it.
        self.dap_server.clear_messages()
        self.dap_server.client.send_request("stackTrace", {"threadId": 1})
        stack_response = wait_for_msg(self.dap_server, response="stackTrace", timeout=10)
        assert stack_response is not None, "no stackTrace response"
        frames = stack_response.body["stackFrames"]
        assert frames, "expected at least one stack frame"
        assert frames[0]["source"]["path"] == str(self.app_py), frames[0]["source"]

    def detach_client(self):
        """Drop the DAP client, then wait for the released target to reach its own end.

        `app.main()` writes its result back through the mount as its last act,
        so the wait is for that file rather than for a duration: the device
        has demonstrably run the loop out and returned before teardown is
        asked to unmount.
        """
        self.dap_server.stop()
        self.dap_server = None
        deadline = time.monotonic() + 15
        written = None
        while time.monotonic() < deadline:
            # Absent until the device opens it, and empty between the open and
            # the write completing, so the content is the condition.
            written = (
                self.finished_marker.read_text() if self.finished_marker.exists() else None
            )
            if written == "3":
                return
            time.sleep(0.01)
        raise AssertionError(
            f"the released target never finished: {self.finished_marker} "
            + ("was never written" if written is None else f"holds {written!r}")
            + f"; mpremote stdout {''.join(self.stdout_lines)!r}; stderr {self.stderr_text!r}"
        )

    def end_with(self, sig, timeout=15):
        """Signal mpremote, and return (exit code, seconds it took to exit)."""
        started = time.monotonic()
        self.mpremote_proc.send_signal(sig)
        exit_code = self.mpremote_proc.wait(timeout=timeout)
        elapsed = time.monotonic() - started
        for thread in self._threads:
            thread.join(timeout=2)
        return exit_code, elapsed

    def assert_device_still_usable(self):
        """A fresh connection, carrying none of the dead subprocess's state, finds a live device.

        `SerialTransport` opened on the same slave path either finds a raw-REPL
        prompt or it doesn't; there is no third way to make this pass.
        """
        self.device.assert_usable(context=self._mpremote_output)

    def close(self):
        if self.dap_server is not None:
            self.dap_server.stop()
        if self.mpremote_proc is not None and self.mpremote_proc.poll() is None:
            self.mpremote_proc.terminate()
            try:
                self.mpremote_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.mpremote_proc.kill()
                self.mpremote_proc.wait(timeout=5)
        self.device.close()


@requires_settrace_firmware
def test_do_debug_mount_over_real_pty_round_trips_and_tears_down_silently(
    tmp_path, free_tcp_port
):
    """The ticket's own acceptance criterion: a mount session ends leaving the
    device usable, with nothing for the user to do about it.

    The client is dropped first, which is what a debug session ending in a
    front-end looks like. That releases the target - the device's own
    `process_pending_messages` finds the channel gone and ends the session, so
    the program finishes untraced and the device is back at a raw-REPL prompt
    by the time the signal arrives - so teardown has a responsive device to
    unmount and has nothing to warn about. A warning here would mean the
    ordinary path is telling users to power cycle hardware that is fine.
    """
    session = _MountedPtySession(tmp_path, free_tcp_port)
    try:
        session.start()
        session.detach_client()
        exit_code, _ = session.end_with(signal.SIGTERM)

        stderr_text = session.stderr_text
        assert exit_code == 0, (
            f"a mounted plain-network session ending on SIGTERM is a normal exit, not a "
            f"fault; stderr: {stderr_text!r}"
        )
        assert "warning:" not in stderr_text, (
            f"a session whose client left first has a responsive device to unmount, so "
            f"teardown has nothing to report; stderr: {stderr_text!r}"
        )
        session.assert_device_still_usable()
    finally:
        session.close()


@requires_settrace_firmware
def test_do_debug_mount_teardown_reports_a_device_it_cannot_reach(tmp_path, free_tcp_port):
    """Abandoning mpremote while the client still holds the target is reported, in one line.

    Nothing on the device is reading the port here: the program is parked in
    `wait_for_continue` waiting on a DAP client that is still connected and
    will never send `continue`, and an interrupt cannot reach a program that
    is not reading stdin. `_teardown_mount` can only time out, and this is
    the one case where "only a power cycle clears it" is the truth. What it
    must not do is hang (the timeout is what bounds it), exit non-zero (a
    signal ending a mounted session is a normal end), or spill a device
    traceback into the middle of a warning - the reader sees the first line
    of a warning, and a first line reading "Traceback (most recent call
    last):" tells them nothing.
    """
    session = _MountedPtySession(tmp_path, free_tcp_port)
    try:
        session.start()
        exit_code, elapsed = session.end_with(signal.SIGTERM)

        stderr_text = session.stderr_text
        assert exit_code == 0, (
            f"a mounted session ending on a signal is a normal exit even when the "
            f"device cannot be reached; stderr: {stderr_text!r}"
        )
        assert elapsed < 14, (
            f"teardown must be bounded by its own timeout, not by the caller's "
            f"patience; took {elapsed:.1f}s"
        )
        warnings = [line for line in session.stderr_lines if line.startswith("warning:")]
        assert len(warnings) == 1, f"expected exactly one warning; stderr: {stderr_text!r}"
        assert "power cycle" in warnings[0]
        assert "Traceback" not in stderr_text, (
            f"a device traceback must be reduced to its last line, not interpolated "
            f"whole into a one-line warning; stderr: {stderr_text!r}"
        )
    finally:
        session.close()

