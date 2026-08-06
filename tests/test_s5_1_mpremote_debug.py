"""Host-side pytest coverage for `mpremote debug` skeleton.

Subprocess tests drive the real mpremote CLI, exercising argument parsing
and early-error paths. Unit tests import `mpremote.commands` directly to
exercise the boot-script resource load and the handshake reader's own
formatting logic against a fake transport/state. The real-interpreter tests
(`test_debug_boot_script_runs_under_real_interpreter`,
`test_port_before_positionals_takes_effect`,
`test_timeout_before_positionals_takes_effect`,
`test_debug_with_default_port_reads_handshake`,
`test_do_debug_over_real_pty_reads_handshake_before_client_attach`) are the
ones that actually certify the boot script and `do_debug` work against a
device: they exec the boot script under the built unix firmware, most of
them over a real pty via `SerialTransport`/the CLI, exactly as `mpremote
debug` would against serial hardware. These are skipped, not falsely green,
when the built firmware lacks the capability the test needs:
`requires_unix_firmware` checks the binary exists, `requires_settrace_firmware`
also checks `sys.settrace` is present (the boot script bails before printing
anything useful without it).
"""

import fcntl
import json
import os
import pty
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_TOP_DIR = Path(__file__).resolve().parents[1]
_SUBMODULE_DIR = _TOP_DIR / "micropython" / "tools" / "mpremote"

if str(_SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SUBMODULE_DIR))

from mpremote import commands  # noqa: E402
from mpremote.main import State  # noqa: E402
from mpremote.transport_serial import SerialTransport  # noqa: E402

_MICROPYTHON = Path(
    os.environ.get(
        "MPY_DEBUG_FIRMWARE",
        _TOP_DIR / "micropython/ports/unix/build-standard/micropython",
    )
)
_MICROPYPATH = "{}:{}".format(
    _TOP_DIR / "micropython-lib/python-ecosys/debugpy", _TOP_DIR / "micropython-lib"
)

requires_unix_firmware = pytest.mark.skipif(
    not _MICROPYTHON.exists(),
    reason=f"unix firmware not built at {_MICROPYTHON} (see `make firmware-unix`)",
)


def _firmware_has_settrace():
    """Probe the built firmware for sys.settrace rather than trusting its name.

    The boot script checks this itself and bails before doing anything a test
    could observe, so a firmware built without it (e.g. `micropython`
    submodule pointer not composed with #8767) must skip these tests, not
    fail them confusingly or pass against a stale binary left over from a
    different build.
    """
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


def _mpremote_cmd(args, timeout=90):
    """Run mpremote with the given arguments; return (exit_code, stdout, stderr)."""
    # mpremote is at ../micropython/tools/mpremote (submodule), invoke via python -m.
    result = subprocess.run(
        [sys.executable, "-m", "mpremote"] + args,
        cwd=str(_SUBMODULE_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def test_debug_appears_in_help():
    """The 'debug' command is listed in 'mpremote help' with its description."""
    code, stdout, stderr = _mpremote_cmd(["help"])
    assert code == 0, f"help failed: {stderr}"
    assert "debug" in stdout, "debug command not in help output"
    assert "debug a MicroPython script with a DAP client" in stdout, "missing debug description in help"
    # The command table is a two-column layout; a sprawling description breaks
    # it, so keep this row in line with the other commands.
    debug_row = next(line for line in stdout.splitlines() if line.strip().startswith("debug "))
    assert len(debug_row) < 90, f"debug row breaks the help table layout: {debug_row!r}"


def test_debug_help_shows_description():
    """'mpremote debug --help' displays the command description and usage."""
    code, stdout, stderr = _mpremote_cmd(["debug", "--help"])
    assert code == 0, f"debug --help failed: {stderr}"
    assert "debug a MicroPython script with a DAP client" in stdout, "missing description"
    assert "usage:" in stdout, "missing usage info"
    assert "--port" in stdout, "missing --port option"
    assert "--dap-log" in stdout, "missing --dap-log option"


def test_debug_help_documents_option_ordering():
    """--help states that options must precede target/program.

    mpremote's command-chaining REMAINDER positional swallows any option
    given after the positionals as the start of the next command, so users
    need this documented rather than discovering it as a silently dropped
    flag.
    """
    code, stdout, stderr = _mpremote_cmd(["debug", "--help"])
    assert code == 0, f"debug --help failed: {stderr}"
    assert "come before target/program" in stdout, f"missing option-ordering note in --help; got: {stdout}"


def test_malformed_spec_extra_colons():
    """Extra colons in module:method spec are rejected cleanly."""
    code, stdout, stderr = _mpremote_cmd(["debug", "unix", "a:b:c"])
    assert code != 0, "should have exited with error"
    assert "invalid program" in stderr, f"expected 'invalid program' error; got: {stderr}"
    assert "expected 'module[:method]'" in stderr, f"expected helpful hint; got: {stderr}"
    assert "Traceback" not in stderr, "should not have uncaught exception traceback"


def test_malformed_spec_empty_method():
    """Empty method (module:) is rejected cleanly."""
    code, stdout, stderr = _mpremote_cmd(["debug", "unix", "mod:"])
    assert code != 0, "should have exited with error"
    assert "invalid program" in stderr, f"expected 'invalid program' error; got: {stderr}"
    assert "expected 'module[:method]'" in stderr, f"expected helpful hint; got: {stderr}"
    assert "Traceback" not in stderr, "should not have uncaught exception traceback"


def test_malformed_spec_empty_module():
    """Empty module (:method) is rejected cleanly."""
    code, stdout, stderr = _mpremote_cmd(["debug", "unix", ":main"])
    assert code != 0, "should have exited with error"
    assert "invalid program" in stderr, f"expected 'invalid program' error; got: {stderr}"
    assert "expected 'module[:method]'" in stderr, f"expected helpful hint; got: {stderr}"
    assert "Traceback" not in stderr, "should not have uncaught exception traceback"


def test_unix_target_without_a_binary_reports_a_build_hint():
    """Unix target with no resolvable binary names the fix, not a traceback.

    No mpdebug.toml in _SUBMODULE_DIR and MPY_DEBUG_FIRMWARE unset (the test
    environment's own value, if any, is dropped) means binary resolution has
    nothing to try.
    """
    env = dict(os.environ)
    env.pop("MPY_DEBUG_FIRMWARE", None)
    result = subprocess.run(
        [sys.executable, "-m", "mpremote", "debug", "unix"],
        cwd=str(_SUBMODULE_DIR),
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )
    assert result.returncode != 0, "should have exited with error"
    assert "no unix debug binary found" in result.stderr, f"expected clear error; got: {result.stderr}"
    assert "Traceback" not in result.stderr, "should not have uncaught exception traceback"


def test_program_spec_validated_before_connect():
    """Program spec is validated before attempting to connect to a device.

    A malformed spec on a non-existent device produces the spec error,
    not a connection error.
    """
    code, stdout, stderr = _mpremote_cmd(["debug", "/dev/does-not-exist", "invalid:spec:extra"])
    assert code != 0, "should have exited with error"
    assert "invalid program" in stderr, (
        f"program spec should be validated before connect; expected 'invalid program' but got: {stderr}"
    )


def test_port_zero_rejected_before_connect():
    """--port 0 is rejected locally, before any connection attempt.

    No MicroPython port implements socket.getsockname(), so a device can
    never report back which port the OS assigned it; catching this on the
    host means the error names the real reason instead of surfacing as
    whatever `listen(port=0)` raises on the device.
    """
    code, stdout, stderr = _mpremote_cmd(["debug", "--port", "0", "/dev/does-not-exist", "mod:main"])
    assert code != 0, "should have exited with error"
    assert "--port 0" in stderr, f"expected clear error; got: {stderr}"
    assert "Traceback" not in stderr, "should not have uncaught exception traceback"


def test_dap_log_file_requires_dap_log_rejected_before_connect():
    """--dap-log-file without --dap-log is rejected locally, before any connection attempt."""
    code, stdout, stderr = _mpremote_cmd(
        ["debug", "--dap-log-file", "/tmp/x", "/dev/does-not-exist", "mod:main"]
    )
    assert code != 0, "should have exited with error"
    assert "--dap-log-file requires --dap-log" in stderr, f"expected clear error; got: {stderr}"
    assert "Traceback" not in stderr, "should not have uncaught exception traceback"


def test_launcher_copy_matches_mpremote_resource():
    """The wrapper repo's launcher and the mpremote package copy must not drift.

    Byte-identity is what makes the wrapper repo's DAP harness (which runs
    launcher/mpy_launch_debugpy.py) representative of what `mpremote debug`
    actually ships and execs on the device.
    """
    wrapper_copy = _TOP_DIR / "launcher" / "mpy_launch_debugpy.py"
    mpremote_copy = _SUBMODULE_DIR / "mpremote" / "mpy_launch_debugpy.py"
    assert wrapper_copy.read_bytes() == mpremote_copy.read_bytes(), (
        "launcher/mpy_launch_debugpy.py and the mpremote package copy have diverged; "
        "keep them byte-identical or pick one as the sole source of truth"
    )


@requires_settrace_firmware
def test_debug_boot_script_runs_under_real_interpreter(free_tcp_port):
    """`_debug_boot_script`'s argv preamble runs on MicroPython, not just CPython.

    `sys.argv = [...]` raises AttributeError on MicroPython (the built-in
    module dict is read-only); this execs the generated script under the
    built unix firmware and checks it reaches the listening banner and prints
    the handshake without dying on the argv line. listen() returns immediately
    after binding (before any client attaches), so both the listening banner
    and MPDBG-READY arrive promptly.
    """
    script = commands._debug_boot_script("mod", "main", free_tcp_port)
    env = dict(os.environ)
    env["MICROPYPATH"] = _MICROPYPATH
    listening_banner = f"Debugpy listening on 0.0.0.0:{free_tcp_port}"
    handshake_prefix = "MPDBG-READY "

    # listen() returns after binding, before accept(), so both listening
    # banner and handshake arrive without a client; the process then blocks
    # in wait_for_client(), which the `finally` below terminates.
    proc = subprocess.Popen(
        [str(_MICROPYTHON), "-c", script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        fl = fcntl.fcntl(proc.stdout.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(proc.stdout.fileno(), fcntl.F_SETFL, fl | os.O_NONBLOCK)
        deadline = time.monotonic() + 10
        output = ""
        has_listening_banner = False
        has_handshake = False
        while time.monotonic() < deadline and not (has_listening_banner and has_handshake):
            try:
                chunk = proc.stdout.read()
            except (BlockingIOError, TypeError):
                chunk = None
            if chunk:
                output += chunk
                has_listening_banner = listening_banner in output
                has_handshake = handshake_prefix in output
            time.sleep(0.05)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    assert "AttributeError" not in output, output
    assert "Traceback" not in output, output
    assert listening_banner in output, output
    assert handshake_prefix in output, output


class _FakeTransport:
    """Minimal stand-in for SerialTransport's read_until/exec_raw_no_follow.

    `read_until` mirrors the real primitive: the scripted bytes are one
    continuous stream, and each call returns everything up to and including
    the first occurrence of `ending` (or whatever remains, if `ending` never
    shows up) - never the caller-supplied chunk boundaries verbatim. Real
    framing glues bytes together regardless of how a test lists them (e.g.
    the raw-REPL `\x04` marker always arrives stuck to the line after it),
    so tests must not assume `ending` splits the stream where they wrote it.
    """

    def __init__(self, lines, device_name="/dev/fake-tty"):
        self._buf = b"".join(lines)
        self.exec_calls = []
        # SerialTransport always sets this (transport_serial.py), and do_debug
        # reads it to decide whether the open transport already names the
        # requested target.
        self.device_name = device_name

    def exec_raw_no_follow(self, command):
        self.exec_calls.append(command)

    def read_until(self, min_num_bytes, ending, timeout=10, data_consumer=None, timeout_overall=None):
        idx = self._buf.find(ending)
        end = len(self._buf) if idx == -1 else idx + len(ending)
        chunk, self._buf = self._buf[:end], self._buf[end:]
        return chunk  # b"" once the buffer is drained: caller treats it as a timeout


def test_read_mpdbg_ready_times_out_naming_expected_line():
    """A target that never prints MPDBG-READY times out naming the expected line."""
    with pytest.raises(commands.CommandError, match="MPDBG-READY "):
        commands._read_mpdbg_ready(_FakeTransport([]), timeout=0.1)


def test_read_mpdbg_ready_reports_last_line_on_early_exit():
    """A device that exits before the handshake is reported quickly, quoting its
    last output line, rather than stalling for the full timeout."""
    transport = _FakeTransport(
        [
            b"sys.settrace is not available.\r\n",
            b"\x04\x04>",  # raw-REPL EOF with no trailing newline
        ]
    )
    start = time.monotonic()
    with pytest.raises(commands.CommandError, match="sys.settrace is not available"):
        commands._read_mpdbg_ready(transport, timeout=6)
    elapsed = time.monotonic() - start
    assert elapsed < 1, f"should not wait out the full timeout, took {elapsed:.2f}s"


def test_read_mpdbg_ready_quotes_device_exception(monkeypatch):
    """The device's exception text reaches the error message, not stdout.

    The raw REPL frames output as <stdout> \\x04 <exception> \\x04>, and
    `read_until(1, b"\\n", ...)` returns through the first newline - so on
    real hardware the marker always arrives glued to the traceback line that
    follows it, e.g. `b"\\x04Traceback (most recent call last):\\r\\n"`, never
    as a chunk of its own. That framing must not be echoed as ordinary
    program output.
    """
    printed = []
    monkeypatch.setattr(commands, "stdout_write_bytes", printed.append)
    transport = _FakeTransport(
        [
            b"\x04Traceback (most recent call last):\r\n",
            b'  File "<stdin>", line 47, in <module>\r\n',
            b"ImportError: no module named 'debugpy'\r\n",
            b"\x04>",
        ]
    )
    with pytest.raises(commands.CommandError, match="no module named 'debugpy'") as exc_info:
        commands._read_mpdbg_ready(transport, timeout=6)
    assert not any(b"Traceback" in p for p in printed), (
        f"exception text must not be echoed to stdout: {printed}"
    )
    assert "Traceback" in str(exc_info.value)


def test_read_mpdbg_ready_rejects_malformed_json():
    """A malformed MPDBG-READY payload raises a CommandError quoting it."""
    transport = _FakeTransport([b"MPDBG-READY {not json\n"])
    with pytest.raises(commands.CommandError, match="malformed"):
        commands._read_mpdbg_ready(transport, timeout=1)


def test_read_until_enforces_timeout_on_pty():
    """SerialTransport.read_until bounds its wait on a pty transport.

    A pty is opened with `timeout=None`, so without a serial-level timeout
    `read(1)` blocks forever once the peer stops producing bytes and
    `read_until`'s own `timeout`/`timeout_overall` never get a chance to
    fire. Nothing is ever written to the peer here, so a regression would
    hang this test rather than fail it quickly.
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    transport = SerialTransport(slave_path, baudrate=115200)
    try:
        start = time.monotonic()
        data = transport.read_until(1, b"\n", timeout=1, timeout_overall=1)
        elapsed = time.monotonic() - start
    finally:
        transport.close()
        os.close(master_fd)

    assert data == b""
    assert elapsed < 3, f"read_until should not block past its timeout, took {elapsed:.2f}s"


@requires_unix_firmware
def test_debug_reports_missing_debugpy_on_device(free_tcp_port):
    """A device without debugpy installed reports the ImportError, not a bare exit.

    Exercises the two-channel raw-REPL framing against the real interpreter: a
    fake transport cannot prove the stderr chunk is actually read off the wire.
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    env = dict(os.environ)
    env["MICROPYPATH"] = ""  # debugpy deliberately unreachable

    proc = subprocess.Popen(
        [str(_MICROPYTHON)],
        stdin=master_fd,
        stdout=master_fd,
        stderr=master_fd,
        env=env,
        close_fds=True,
    )
    os.close(master_fd)
    os.close(slave_fd)
    try:
        time.sleep(0.3)  # let the interpreter start its REPL before talking to it
        code, stdout, stderr = _mpremote_cmd(
            ["resume", "debug", "--port", str(free_tcp_port), "--timeout", "5", slave_path, "mod:main"]
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    assert code != 0, f"expected failure; stdout: {stdout}"
    assert "debugpy" in stderr, f"the device's ImportError should reach the error; got: {stderr}"


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


def test_do_debug_prints_handshake_and_calls_did_action(monkeypatch, capsys):
    """do_debug formats a given handshake dict and suppresses REPL fall-through.

    Fake-transport unit test for the print/`did_action` plumbing only; it
    feeds a handshake directly rather than reproducing device timing, so it
    does not by itself certify that a real device ever produces one -
    `test_do_debug_over_real_pty_reads_handshake_before_client_attach` does.
    """
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)

    # A real reported address (not the 0.0.0.0 wildcard): this test is about
    # the print/did_action plumbing, not endpoint resolution - see
    # test_do_debug_hard_errors_on_unreachable_device for that.
    handshake = {"host": "192.0.2.10", "port": 5678, "caps": {"can_set_local": True}}
    transport = _FakeTransport(
        [
            b"MicroPython VS Code Debugging\n",
            ("MPDBG-READY " + json.dumps(handshake) + "\n").encode(),
        ]
    )
    state = _FakeState(transport)
    args = type(
        "Args",
        (),
        {
            "target": "u0",
            "program": "mod:main",
            "port": None,
            "dap_log": False,
            "dap_log_file": None,
            "timeout": 60,
        },
    )()

    commands.do_debug(state, args)

    out = capsys.readouterr().out
    assert "debug server listening on 192.0.2.10:5678" in out
    assert "capabilities:" in out
    assert "can_set_local" in out
    assert state.run_repl_on_completion() is False
    assert len(transport.exec_calls) == 1


def test_do_debug_hard_errors_on_unreachable_device(monkeypatch):
    """A serial device reporting the 0.0.0.0 wildcard with no known address
    is a hard CommandError - mpremote has no route to it and must not guess."""
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)

    handshake = {"host": "0.0.0.0", "port": 5678, "caps": {}}
    transport = _FakeTransport([("MPDBG-READY " + json.dumps(handshake) + "\n").encode()])
    state = _FakeState(transport)
    args = type(
        "Args",
        (),
        {
            "target": "u0",
            "program": "mod:main",
            "port": None,
            "dap_log": False,
            "dap_log_file": None,
            "timeout": 60,
        },
    )()

    with pytest.raises(commands.CommandError, match="no network address"):
        commands.do_debug(state, args)


def test_do_debug_missing_caps_key(monkeypatch):
    """A handshake payload missing a required key raises a clear CommandError."""
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)

    handshake = {"host": "0.0.0.0", "port": 5678}  # no "caps"
    transport = _FakeTransport([("MPDBG-READY " + json.dumps(handshake) + "\n").encode()])
    state = _FakeState(transport)
    args = type(
        "Args",
        (),
        {
            "target": "u0",
            "program": "mod:main",
            "port": None,
            "dap_log": False,
            "dap_log_file": None,
            "timeout": 60,
        },
    )()

    with pytest.raises(commands.CommandError, match="missing key"):
        commands.do_debug(state, args)


@requires_settrace_firmware
def test_port_before_positionals_takes_effect(free_tcp_port):
    """CLI `--port` given before target/program is honoured.

    Runs the real `mpremote` CLI (not `do_debug` directly) against a pty so
    the REMAINDER-swallowing failure mode - flags placed after the
    positionals silently becoming the next command - would show up as
    `--port` having no effect. The unix build has no `network` module, so it
    always reports the 0.0.0.0 wildcard and the command hard-errors rather
    than printing an endpoint (see the handshake resolution matrix); the
    port the device actually bound to still reaches that error message,
    which is what proves `--port` took effect here.
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    env = dict(os.environ)
    env["MICROPYPATH"] = _MICROPYPATH

    proc = subprocess.Popen(
        [str(_MICROPYTHON)],
        stdin=master_fd,
        stdout=master_fd,
        stderr=master_fd,
        env=env,
        close_fds=True,
    )
    os.close(master_fd)
    os.close(slave_fd)
    try:
        time.sleep(0.3)  # let the interpreter start its REPL before talking to it
        start = time.monotonic()
        code, stdout, stderr = _mpremote_cmd(
            ["resume", "debug", "--port", str(free_tcp_port), slave_path, "mod:main"]
        )
        elapsed = time.monotonic() - start
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    # listen() returns as soon as the socket is bound, before any client
    # connects, so the handshake is readable without a client. The device
    # binds the wildcard, and a pty peer is a local process, so the reported
    # endpoint resolves to the loopback address a client can actually use.
    assert code == 0, f"expected success; stdout: {stdout}; stderr: {stderr}"
    assert f"127.0.0.1:{free_tcp_port}" in stdout, (
        f"--port was not honoured, or the wildcard was not resolved; stdout: {stdout}"
    )
    assert "0.0.0.0" not in stdout.split("debug server listening on")[-1], (
        f"a wildcard must never be reported as the endpoint; stdout: {stdout}"
    )
    assert elapsed < 5, f"handshake should arrive quickly, took {elapsed:.1f}s; stdout: {stdout}"


@requires_settrace_firmware
def test_debug_with_default_port_reads_handshake():
    """`mpremote debug <dev> <prog>` with `--port` omitted uses the device's default.

    The plain invocation from the acceptance criterion: no `--port` anywhere
    on the command line, so the endpoint reported is entirely the device's
    choice (`debugpy.DEFAULT_PORT`), not something the host contributed. The
    The device binds the wildcard and a pty peer is local, so the reported
    endpoint resolves to loopback on that default port.
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    env = dict(os.environ)
    env["MICROPYPATH"] = _MICROPYPATH

    proc = subprocess.Popen(
        [str(_MICROPYTHON)],
        stdin=master_fd,
        stdout=master_fd,
        stderr=master_fd,
        env=env,
        close_fds=True,
    )
    os.close(master_fd)
    os.close(slave_fd)
    try:
        time.sleep(0.3)  # let the interpreter start its REPL before talking to it
        code, stdout, stderr = _mpremote_cmd(["resume", "debug", slave_path, "mod:main"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    assert code == 0, f"expected success; stdout: {stdout}; stderr: {stderr}"
    # debugpy.DEFAULT_PORT (micropython-lib python-ecosys/debugpy/debugpy/common/constants.py).
    assert "127.0.0.1:5678" in stdout, (
        f"expected the device's default port, resolved to loopback; stdout: {stdout}"
    )


@requires_settrace_firmware
def test_timeout_before_positionals_takes_effect(tmp_path, free_tcp_port):
    """CLI `--timeout` given before target/program bounds the handshake wait.

    The device's `debugpy.listen()` is stubbed to hang forever (a
    fake `debugpy` module shadowing the real one on MICROPYPATH), so nothing
    ever satisfies `_read_mpdbg_ready`; only `--timeout` stops the wait. If
    the flag were swallowed by the REMAINDER positional (the failure mode
    `--help` warns about) or ignored downstream, the command would instead
    run out the 60s default.
    """
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "debugpy.py").write_text("def listen(*args, **kwargs):\n    while True:\n        pass\n")

    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    env = dict(os.environ)
    env["MICROPYPATH"] = str(stub_dir)  # only the hanging stub is importable as "debugpy"

    proc = subprocess.Popen(
        [str(_MICROPYTHON)],
        stdin=master_fd,
        stdout=master_fd,
        stderr=master_fd,
        env=env,
        close_fds=True,
    )
    os.close(master_fd)
    os.close(slave_fd)
    try:
        time.sleep(0.3)  # let the interpreter start its REPL before talking to it
        start = time.monotonic()
        code, stdout, stderr = _mpremote_cmd(
            ["resume", "debug", "--port", str(free_tcp_port), "--timeout", "2", slave_path, "mod:main"]
        )
        elapsed = time.monotonic() - start
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    assert code != 0, f"expected the handshake wait to time out; stdout: {stdout}"
    assert "timed out waiting" in stderr, f"expected a timeout error; got: {stderr}"
    assert elapsed < 10, f"--timeout=2 should bound the wait, took {elapsed:.1f}s; stderr: {stderr}"


@requires_settrace_firmware
def test_do_debug_over_real_pty_reads_handshake_before_client_attach(free_tcp_port):
    """`do_debug` reads the real handshake before any client attaches.

    A real `SerialTransport` is connected to the built unix firmware's
    stdin/stdout via a pty pair (the firmware holds the master side, exactly
    as QEMU or a USB-serial bridge would); `do_debug` execs the real boot
    script through it. No client exists until after `do_debug` has returned,
    so if it needed a client to reach the handshake this test would time out
    inside `do_debug` rather than reach the assertions below. The device binds
    the wildcard, and a pty peer is a local process, so the endpoint resolves
    to loopback; connecting a client to it afterwards proves the resolved
    address is one a DAP client can really use, rather than a value that
    merely parsed. `state.transport.device_name` already matches
    `args.target`, exercising the connection-reuse path.
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    env = dict(os.environ)
    env["MICROPYPATH"] = _MICROPYPATH

    proc = subprocess.Popen(
        [str(_MICROPYTHON)],
        stdin=master_fd,
        stdout=master_fd,
        stderr=master_fd,
        env=env,
        close_fds=True,
    )
    os.close(master_fd)
    os.close(slave_fd)
    try:
        time.sleep(0.3)  # let the interpreter start its REPL before talking to it
        transport = SerialTransport(slave_path, baudrate=115200)

        state = State()
        state.transport = transport
        # Unlike embedded ports, the unix build exits on the raw-REPL soft
        # reset used to prime a fresh device, rather than rebooting into a
        # new REPL; do_resume's flag is the existing way to skip that reset.
        commands.do_resume(state)
        args = type(
            "Args",
            (),
            {
                "target": slave_path,
                "program": "mod:main",
                "port": free_tcp_port,
                "dap_log": False,
                "dap_log_file": None,
                "timeout": 15,
            },
        )()

        handshake = commands.do_debug(state, args)
        assert state.run_repl_on_completion() is False

        # Connect to exactly what do_debug reported, rather than to a hardcoded
        # address: that is what makes this prove the resolved endpoint usable.
        assert handshake["host"] == "127.0.0.1", handshake
        assert handshake["port"] == free_tcp_port, handshake
        assert handshake["raw_host"] == "0.0.0.0", (
            f"expected the device to report a wildcard bind; got {handshake['raw_host']!r}"
        )
        client = socket.create_connection((handshake["host"], handshake["port"]), timeout=5)
        with client:
            body = json.dumps(
                {"seq": 1, "type": "request", "command": "initialize", "arguments": {}}
            ).encode()
            client.sendall(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            client.settimeout(5)
            reply = client.recv(4096)
        assert b'"initialize"' in reply and b'"response"' in reply, (
            f"client did not receive an initialize response; got: {reply!r}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
