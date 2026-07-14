import fcntl
import json
import os
import random
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Add launcher/ to sys.path to reuse the manifest-vs-probe mismatch guard
# (STORY-3.3) rather than duplicating it for tests.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_launcher_dir = str(_REPO_ROOT / "launcher")
if _launcher_dir not in sys.path:
    sys.path.insert(0, _launcher_dir)

import capcheck

random.seed()


@pytest.fixture()
def free_tcp_port(request):
    """
    Fixture to find a free TCP port on localhost starting from 5678 and increasing upward.
    Can be parameterized to use a specific port.
    """
    if hasattr(request, "param"):
        # Use parameterized port
        requested_port = request.param
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("localhost", requested_port))
                return requested_port
            except OSError:
                pytest.fail(f"Requested port {requested_port} is not available")

    # Default behavior - find free port
    min_port = 5678
    max_port = 8000
    base_port = min_port + random.randint(0, max_port - min_port)

    for port in range(base_port, max_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("localhost", port))
                return port
            except OSError:
                continue

    pytest.fail("Could not find a free TCP port in the range 5678-5999")


@pytest.fixture()
def tgt_module(request):
    """
    Fixture to provide the module name for the test.
    Can be parameterized to use different modules.
    """
    return request.param if hasattr(request, "param") else "target"


@pytest.fixture()
def tgt_method(request):
    """
    Fixture to provide the method name for the test.
    Can be parameterized to use different methods.
    """
    return request.param if hasattr(request, "param") else "main"


@pytest.fixture()
def in_terminal(request):
    """
    Fixture to provide the method name for the test.
    Can be parameterized to use different methods.
    """
    default = True
    default = False
    return request.param if hasattr(request, "param") else default


@pytest.fixture()
def micropython_debuggee(
    pytestconfig,
    tgt_module: str,
    tgt_method: str,
    free_tcp_port: int,
    in_terminal: bool,
):
    """
    Fixture to start the debugpy executable in a separate process.
    can be parameterized with:
    - tgt_module: The target module to run.
    - tgt_method: The target method to run.
    - free_tcp_port: The port to bind the server to.
    """
    # Get the workspace root path using pytest configuration
    root_path = Path(pytestconfig.rootpath)

    # Construct absolute paths. The debug-enabled unix firmware is built from
    # the micropython submodule (see `make firmware-unix`); allow an override
    # via MPY_DEBUG_FIRMWARE for testing an alternative binary.
    micropython_path = Path(
        os.environ.get(
            "MPY_DEBUG_FIRMWARE",
            root_path / "micropython/ports/unix/build-standard/micropython",
        )
    )
    launcher_path = root_path / "launcher/mpy_launch_debugpy.py"
    micropython_lib_path = root_path / "micropython-lib/python-ecosys/debugpy"
    src_path = root_path / "src"

    # Set up the environment
    env = os.environ.copy()
    env["MICROPYPATH"] = f"{src_path}:{micropython_lib_path}:~/.micropython/lib:/usr/lib/micropython"

    # Command to start the MicroPython process
    command = [str(micropython_path), str(launcher_path), tgt_module, tgt_method, str(free_tcp_port)]

    if in_terminal:
        # Make the subprocess visible in a new terminal window
        terminal_command = [
            "x-terminal-emulator",
            "-e",
            " ".join(command),
        ]

        # Start the process in a new terminal
        process = subprocess.Popen(
            terminal_command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # time.sleep(1)  # Give the terminal some time to open

    else:
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Set stdout and stderr to non-blocking mode
        if process.stdout:
            fcntl.fcntl(
                process.stdout.fileno(),
                fcntl.F_SETFL,
                fcntl.fcntl(process.stdout.fileno(), fcntl.F_GETFL) | os.O_NONBLOCK,
            )
        if process.stderr:
            fcntl.fcntl(
                process.stderr.fileno(),
                fcntl.F_SETFL,
                fcntl.fcntl(process.stderr.fileno(), fcntl.F_GETFL) | os.O_NONBLOCK,
            )

        # ======================================
        stdout_data = ""
        stderr_data = ""
        max_retries = 10

        # Verify all expected output lines are present
        expected_lines = [
            "MicroPython VS Code Debugging",
            "==================================",
            f"Target module: {tgt_module}",
            f"Target method: {tgt_method}",
            "==================================",
            f"Debugpy listening on 0.0.0.0:{free_tcp_port}",
        ]

        for attempt in range(max_retries):
            print(f"Attempt {attempt + 1}/{max_retries} to read process output...")

            # Non-blocking read from stdout
            try:
                if process.stdout:
                    chunk = process.stdout.read(1024)
                    if chunk:
                        stdout_data += chunk
            except (BlockingIOError, OSError):
                pass  # No data available
            except Exception as e:
                print(f"Error reading stdout: {e}")

            # Non-blocking read from stderr
            try:
                if process.stderr:
                    chunk = process.stderr.read(1024)
                    if chunk:
                        stderr_data += chunk
            except (BlockingIOError, OSError):
                pass  # No data available
            except Exception as e:
                print(f"Error reading stderr: {e}")

            # Check if process has terminated
            if process.poll() is not None:
                print(f"Process terminated with exit code: {process.returncode}")
                break

            # Check if we have all expected lines
            if all(line.strip() in stdout_data for line in expected_lines):
                break

            time.sleep(0.1)

        # Check for errors in stderr
        if stderr_data.strip():
            pytest.fail(f"Process stderr contains errors: {stderr_data}")

        print("stdout_data:", stdout_data)

        for line in expected_lines:
            assert line in stdout_data, f"Expected line '{line}' not found in stdout. Got: {stdout_data}"

    # ======================================

    yield process

    # Terminate the process after the test if it's still running
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
    finally:
        # Ensure terminal process is terminated if started
        if in_terminal and process.poll() is None:
            process.kill()
            process.wait(timeout=1)
            process.terminate()


def read_mpdbg_ready(process, timeout=5):
    """Drain `process.stdout` (already non-blocking, per `micropython_debuggee`)
    until the launcher's `MPDBG-READY <json>` handshake line shows up, and
    return its decoded payload.

    `debugpy.listen()` does not print this line until it has accepted a
    connection and handled an `initialize` request, so a caller needs a
    connected DAP client first - request `attach_server` (or an equivalent)
    alongside `micropython_debuggee`, not `micropython_debuggee` alone.
    """
    deadline = time.time() + timeout
    stdout_data = ""
    while time.time() < deadline:
        try:
            chunk = process.stdout.read(4096)
            if chunk:
                stdout_data += chunk
        except (BlockingIOError, OSError):
            pass
        if "MPDBG-READY " in stdout_data:
            break
        time.sleep(0.05)

    ready_lines = [line for line in stdout_data.splitlines() if line.startswith("MPDBG-READY ")]
    assert len(ready_lines) == 1, f"Expected exactly one MPDBG-READY line, got {len(ready_lines)}: {ready_lines!r}"
    return json.loads(ready_lines[0][len("MPDBG-READY ") :])


@pytest.fixture()
def claimed_capabilities(request):
    """Capability claim to cross-check the real probe result against.

    `None` (the default, when unparametrized and MPY_DEBUG_CLAIMED_CAPS is
    unset) leaves `probed_capabilities` a plain lookup with no guard, so
    existing callers that don't ask for a claim are unaffected. Parametrize
    this fixture with a dict (e.g. a firmware.toml [[variant]] entry's
    `capabilities` table) to exercise the mismatch guard, or set
    MPY_DEBUG_CLAIMED_CAPS to a JSON object for the same effect.
    """
    if hasattr(request, "param"):
        return request.param
    raw = os.environ.get("MPY_DEBUG_CLAIMED_CAPS")
    return json.loads(raw) if raw else None


@pytest.fixture()
def probed_capabilities(micropython_debuggee, attach_server, claimed_capabilities):
    """Real capabilities from the running debuggee's MPDBG-READY handshake.

    When `claimed_capabilities` is set, cross-checks it against the probe
    result first and raises `capcheck.CapabilityMismatch` on any
    claimed-true-but-probed-false key (D4: the probe is truth, a manifest
    entry is only intent) - the check runs before this fixture yields, so a
    mismatch fails the test during setup rather than corrupting the session.
    """
    payload = read_mpdbg_ready(micropython_debuggee)
    probed = payload["caps"]
    if claimed_capabilities is not None:
        capcheck.check_capabilities(claimed_capabilities, probed)
    return probed
