"""Host-side pytest coverage for `mpremote debug unix` end-to-end flow.

Subprocess tests drive the real mpremote CLI against the built unix firmware.
The unix flow is the lowest-friction proof of resolve→ensure→launch→attach,
since it needs no device, no serial sync, and offers subprocess stdout as the
control plane.

The command supervises its child once launched: it stays attached (streaming
the child's stdout) until the child exits or the user sends Ctrl-C, so tests
that reach a successful handshake must read up to the report line via
`Popen`, then end the session with SIGINT - never wait for the command to
exit on its own on a success path.

Coverage:
- Happy path: spawns real firmware, reads handshake, reports endpoint.
- Port comes from the handshake, not from --port (mutation-tested).
- Missing binary produces the build hint, not a traceback.
- Handshake timeout names the expected line and surfaces captured stderr.
- Subprocess reaping on every failure path, including after a live SIGINT.
- Adversarial: duplicate MPDBG-READY, missing required caps.
- A real DAP client completes `initialize` against the reported endpoint.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import mpremote_debug
from helpers import PerfServer, wait_for_msg
from mpremote_debug import read_until as _read_until, spawn_debug as _spawn_debug

_TOP_DIR = Path(__file__).resolve().parents[1]
_SUBMODULE_DIR = mpremote_debug.SUBMODULE_DIR

if str(_SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SUBMODULE_DIR))

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
    """Probe the built firmware for sys.settrace."""
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


def _debug_child_pids():
    """PIDs of launcher subprocesses currently alive.

    The lifecycle tests assert against this rather than reaping strays: a
    harness that kills leftovers would hide the defect it exists to catch.
    """
    result = subprocess.run(
        ["pgrep", "-f", "mpy_launch_debugpy"], capture_output=True, text=True, timeout=10
    )
    return set(result.stdout.split())


def _mpremote_cmd(args, timeout=90, env=None, cwd=None):
    """Run mpremote with the given arguments; return (exit_code, stdout, stderr).

    Only for flows that terminate on their own: resolution failures and
    handshake errors, both of which raise before the command starts
    supervising a child. A success path must use `_spawn_debug` instead, or
    this blocks until `timeout` since the command never exits by itself.
    """
    if env is None:
        env = os.environ.copy()

    result = subprocess.run(
        [sys.executable, "-m", "mpremote"] + args,
        cwd=str(cwd or _SUBMODULE_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def _new_launcher_pids(proc, before_pids):
    """PIDs that appeared since `before_pids` was captured.

    `before_pids` is `_debug_child_pids()` taken before the session was
    spawned, so the diff identifies the child it started. Asserts at least
    one appeared - called right after the handshake, before anything (a
    DAP peer disconnecting, Ctrl-C) has had a chance to end the session. On
    failure, kills `proc` first so a broken assertion doesn't itself leak
    the mpremote process it was checking.
    """
    new_pids = _debug_child_pids() - before_pids
    if not new_pids:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("no launcher child appeared after the handshake")
    return new_pids


def _end_session(proc, new_pids, timeout=10):
    """End a supervising `mpremote debug` session and confirm its child is gone.

    If the child already exited on its own (its DAP peer disconnected, its
    own program returned), the command's streaming loop already noticed and
    may have exited too; otherwise SIGINT ends it as Ctrl-C would. Either
    way, `new_pids` (from `_new_launcher_pids`) must be gone afterwards.
    """
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("mpremote did not exit after SIGINT")
    else:
        proc.wait(timeout=timeout)
    still_alive = _debug_child_pids() & new_pids
    assert not still_alive, f"launcher child(ren) {still_alive} still alive after the session ended"


@requires_settrace_firmware
def test_unix_debug_happy_path_reports_endpoint(free_tcp_port):
    """Unix debug spawns the real firmware, reads handshake, reports endpoint."""
    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(_MICROPYTHON)
    env["MICROPYPATH"] = _MICROPYPATH
    before = _debug_child_pids()
    proc = _spawn_debug(["debug", "--port", str(free_tcp_port), "unix", "target:main"], env=env)
    lines, matched = _read_until(proc, "debug server listening on")
    if matched is None:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail(f"never saw the endpoint report; output: {''.join(lines)}")

    new_pids = _new_launcher_pids(proc, before)
    report = "".join(lines)
    assert f":{free_tcp_port}" in report, f"reported port not found; output: {report}"
    assert "127.0.0.1" in report, f"loopback address not resolved; output: {report}"
    _end_session(proc, new_pids)


def test_unix_debug_port_from_handshake(tmp_path, free_tcp_port):
    """The reported port comes from the handshake, not from --port.

    A stub firmware reports a port deliberately different from the one
    requested via --port; the command must relay the handshake's port
    rather than the CLI argument. (Mutation evidence in the review report:
    hardcoding the report to the requested port makes this fail.)
    """
    stub_port = free_tcp_port
    requested_port = stub_port + 1
    stub_path = tmp_path / "stub_fixed_port"
    stub_path.write_text(
        (
            "#!/bin/sh\n"
            'printf \'MPDBG-READY {"host": "127.0.0.1", "port": %d, "caps": {"settrace": true}}\\n\'\n'
            "sleep 5\n"
        )
        % stub_port
    )
    stub_path.chmod(0o755)

    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(stub_path)
    before = _debug_child_pids()
    proc = _spawn_debug(["debug", "--port", str(requested_port), "unix", "target:main"], env=env)
    lines, matched = _read_until(proc, "MPDBG-READY ", at_line_start=True)
    if matched is None:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail(f"never saw MPDBG-READY; output: {''.join(lines)}")

    new_pids = _new_launcher_pids(proc, before)
    payload = json.loads(matched[matched.index("MPDBG-READY ") + len("MPDBG-READY ") :])
    assert payload["port"] == stub_port, (
        f"reported port {payload['port']} should be the handshake's {stub_port}, "
        f"not the requested {requested_port}"
    )
    _end_session(proc, new_pids)


def test_unix_debug_missing_binary_hint(free_tcp_port):
    """Missing binary produces the build hint, not a traceback.

    MPY_DEBUG_FIRMWARE is unset and no mpdebug.toml exists, so binary
    resolution fails with a helpful message before any child is spawned -
    the command exits on its own, so this is a plain subprocess.run.
    """
    env = os.environ.copy()
    env.pop("MPY_DEBUG_FIRMWARE", None)
    code, stdout, stderr = _mpremote_cmd(
        ["debug", "unix", "--port", str(free_tcp_port), "target:main"],
        env=env
    )
    assert code != 0, "should have exited with error"
    assert "no unix debug binary found" in stderr, f"expected build hint; got: {stderr}"
    assert "ports/unix" in stderr, f"missing build command hint; got: {stderr}"
    assert "Traceback" not in stderr, "should not have uncaught exception traceback"


def test_unix_debug_timeout_names_expected_line(free_tcp_port, tmp_path):
    """Handshake timeout names MPDBG-READY and surfaces captured stderr.

    Uses a stub firmware that never prints the handshake, so the timeout
    fires and reports what was actually printed. The command exits on its
    own (it never reaches supervision), so this is a plain subprocess.run.
    """
    stub_path = tmp_path / "stub_hang"
    stub_path.write_text(
        "#!/bin/sh\necho 'Starting...'; sleep 30\n",
    )
    stub_path.chmod(0o755)

    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(stub_path)
    code, stdout, stderr = _mpremote_cmd(
        ["debug", "--port", str(free_tcp_port), "--timeout", "1", "unix", "target:main"],
        timeout=20,
        env=env
    )
    assert code != 0, "should have timed out"
    assert "MPDBG-READY" in stderr, (
        f"timeout error should name the expected line; got: {stderr}"
    )
    assert "timed out" in stderr, f"timeout error should explain why it failed; got: {stderr}"
    assert "Starting" in stderr, f"timeout error should surface captured output; got: {stderr}"


def test_unix_debug_missing_binary_spawns_nothing(tmp_path):
    """Binary resolution fails before any child exists.

    Asserted by counting children rather than by inspection: resolution runs
    before the spawn, so a failure here must leave no process behind at all.
    """
    # cwd=tmp_path means "python -m mpremote" can't rely on the cwd holding
    # the package (that only works from _SUBMODULE_DIR); PYTHONPATH stands in.
    env = {k: v for k, v in os.environ.items() if k != "MPY_DEBUG_FIRMWARE"}
    env["PYTHONPATH"] = str(_SUBMODULE_DIR)
    before = _debug_child_pids()
    code, stdout, stderr = _mpremote_cmd(
        ["debug", "unix", "target:main"], timeout=30, env=env, cwd=tmp_path
    )
    assert code != 0, stdout
    assert "no unix debug binary found" in stderr, stderr
    assert "Traceback" not in stderr, stderr
    assert _debug_child_pids() == before, "no child should exist when resolution fails"


def test_unix_debug_subprocess_reaped_on_immediate_exit(free_tcp_port, tmp_path):
    """Subprocess is reaped when it exits before printing handshake.

    A stub binary that exits immediately without printing MPDBG-READY
    should be cleaned up (terminate/kill ladder) and not left zombie.
    """
    stub_path = tmp_path / "stub_exit"
    stub_path.write_text("#!/bin/sh\nexit 1\n")
    stub_path.chmod(0o755)

    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(stub_path)

    before = _debug_child_pids()
    code, stdout, stderr = _mpremote_cmd(
        ["debug", "--port", str(free_tcp_port), "--timeout", "2", "unix", "target:main"],
        timeout=30,
        env=env
    )
    assert code != 0, "should have failed"
    assert "exited" in stderr or "MPDBG-READY" in stderr, f"expected process error; got: {stderr}"
    assert _debug_child_pids() == before, "stub should be reaped, not left running"


def test_unix_debug_subprocess_reaped_on_no_output(free_tcp_port, tmp_path):
    """Subprocess is reaped when it prints nothing and closes stdout.

    A stub that opens but never prints should be reaped on timeout,
    not left blocking in the handshake reader.
    """
    stub_path = tmp_path / "stub_silent"
    stub_path.write_text("#!/bin/sh\nsleep 10\n")
    stub_path.chmod(0o755)

    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(stub_path)

    before = _debug_child_pids()
    start = time.monotonic()
    code, stdout, stderr = _mpremote_cmd(
        ["debug", "--port", str(free_tcp_port), "--timeout", "2", "unix", "target:main"],
        timeout=30,
        env=env
    )
    elapsed = time.monotonic() - start
    assert code != 0, "should have timed out"
    # Timeout=2 plus some overhead for process spawn/cleanup; allow up to 15s.
    assert elapsed < 15, f"timeout=2 should fire within ~15s, took {elapsed:.1f}s"
    assert _debug_child_pids() == before, "stub should be reaped, not left running"


def test_unix_debug_rejects_duplicate_mpdbg_ready(tmp_path, free_tcp_port):
    """Adversarial: two MPDBG-READY lines in one batch violate the one-line contract.

    A stub firmware prints both lines in a single write so they land in one
    read; the shared handshake parser must reject the duplicate rather than
    taking the first line and moving on.
    """
    stub_path = tmp_path / "stub_dup"
    stub_path.write_text(
        (
            "#!/bin/sh\n"
            'printf \'MPDBG-READY {"host": "127.0.0.1", "port": %d, "caps": {"settrace": true}}\\n'
            'MPDBG-READY {"host": "127.0.0.1", "port": %d, "caps": {"settrace": true}}\\n\'\n'
            "sleep 5\n"
        )
        % (free_tcp_port, free_tcp_port + 1)
    )
    stub_path.chmod(0o755)

    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(stub_path)
    before = _debug_child_pids()
    code, stdout, stderr = _mpremote_cmd(
        ["debug", "--port", str(free_tcp_port), "unix", "target:main"],
        timeout=20,
        env=env,
    )
    assert code != 0, stdout
    assert "expected exactly one" in stderr, stderr
    assert "got 2" in stderr, stderr
    assert _debug_child_pids() == before, "stub should be reaped after the duplicate-handshake error"


def test_unix_debug_rejects_missing_required_cap(tmp_path, free_tcp_port):
    """Adversarial: caps missing a required capability hard-errors naming it.

    A stub firmware reports caps lacking `set_local`; a target declaring
    `requires = ["set_local"]` must hard-error naming it, after the
    handshake but before reporting success.
    """
    stub_path = tmp_path / "stub_caps"
    stub_path.write_text(
        (
            "#!/bin/sh\n"
            'printf \'MPDBG-READY {"host": "127.0.0.1", "port": %d, "caps": {"settrace": true}}\\n\'\n'
            "sleep 5\n"
        )
        % free_tcp_port
    )
    stub_path.chmod(0o755)

    config_path = tmp_path / "mpdebug.toml"
    config_path.write_text(
        '[target.needs_set_local]\n'
        'kind = "unix"\n'
        'firmware = "{}"\n'
        'requires = ["set_local"]\n'.format(stub_path)
    )

    env = os.environ.copy()
    env.pop("MPY_DEBUG_FIRMWARE", None)
    env["PYTHONPATH"] = str(_SUBMODULE_DIR)  # cwd=tmp_path; see _mpremote_cmd note above
    before = _debug_child_pids()
    code, stdout, stderr = _mpremote_cmd(
        ["debug", "needs_set_local", "--port", str(free_tcp_port)],
        env=env,
        cwd=tmp_path,
        timeout=20,
    )
    assert code != 0, stdout
    assert "requires set_local" in stderr, stderr
    assert _debug_child_pids() == before, "stub should be reaped once the requires check fails"


@requires_settrace_firmware
def test_unix_flow_client_can_connect_to_reported_endpoint(free_tcp_port):
    """End-to-end: a real DAP client completes `initialize` against the reported endpoint.

    Parses the endpoint from the command's own printed report (never an
    assumed address), connects, and drives a real `initialize` request to
    completion, proving the address is both correct and live.
    """
    env = os.environ.copy()
    env["MPY_DEBUG_FIRMWARE"] = str(_MICROPYTHON)
    env["MICROPYPATH"] = _MICROPYPATH
    before = _debug_child_pids()
    proc = _spawn_debug(["debug", "--port", str(free_tcp_port), "unix", "target:main"], env=env)
    lines, matched = _read_until(proc, "debug server listening on")
    if matched is None:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail(f"never saw the endpoint report; output: {''.join(lines)}")

    new_pids = _new_launcher_pids(proc, before)

    # "debug server listening on <host>:<port>" - parse the address the
    # command actually printed, not an assumed one.
    endpoint = matched.split()[-1].strip()
    host, port_str = endpoint.rsplit(":", 1)
    port = int(port_str)

    server = PerfServer("test-client", host, port)
    try:
        server.start()
        # Scan rather than require it to be the most recent: the server sends
        # an `initialized` event straight after the response, so the response
        # is no longer last by the time this runs.
        wait_for_msg(server, response="initialize", timeout=10)
        assert any(
            m.type == "response" and m.command == "initialize" for m in server.rcv_messages
        ), (
            "no initialize response received"
        )
        init_response = [
            m for m in server.rcv_messages if m.type == "response" and m.command == "initialize"
        ]
        assert len(init_response) == 1, f"expected 1 initialize response, got {server.rcv_messages}"
        assert init_response[0].success, f"initialize failed: {init_response[0].raw}"
    finally:
        # Closing the DAP socket may itself end the child's wait_for_client()
        # (a lost connection, not just a graceful disconnect) before this
        # gets to send Ctrl-C - _end_session tolerates either.
        server.stop()
        _end_session(proc, new_pids)


def test_unix_target_recognized_in_help():
    """The 'unix' target kind is documented in the debug command help."""
    code, stdout, stderr = _mpremote_cmd(["debug", "--help"])
    assert code == 0, f"help failed: {stderr}"
    assert "unix-port build" in stdout, f"unix target kind not documented in help; got: {stdout}"
