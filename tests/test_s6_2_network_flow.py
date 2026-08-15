"""STORY-6.2 criterion 2: the endpoint is re-derived from the handshake every run.

The device flow keeps no endpoint state between runs - no cache file, no
config write-back, nothing carried in the environment - so a target that
answers on a different port next time is followed without the user clearing
anything. Nothing asserted that, which is what this file adds; the rest of
the criteria are covered elsewhere (see the ticket's revalidation section).

The device is the unix firmware behind a pty pair, standing in for a board on
its control plane exactly as `test_s5_5_command_drive.py`'s pty leg does. The
transport kind is what differs between a serial and a network target, not the
flow: `do_debug` runs the same connect/exec/handshake path for both.
"""

import json
import os
import pty
import socket
import subprocess
import time

import pytest

import mpremote_debug
from mpremote_debug import (
    MICROPYTHON as _MICROPYTHON,
    RESUME as _RESUME,
    TOP_DIR as _TOP_DIR,
    read_until as _read_until,
    requires_settrace_firmware,
    spawn_debug as _spawn_debug,
)

_MICROPYPATH = "{}:{}".format(
    _TOP_DIR / "src", _TOP_DIR / "micropython-lib" / "python-ecosys" / "debugpy"
)


def _free_port():
    """A second free port, for the run that must not reuse the first one's."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_against_pty(port, env):
    """One `mpremote debug` run against a fresh pty-backed device.

    Returns the MPDBG-READY payload the command re-emitted. The device is
    torn down with the run, so a second call shares nothing with the first
    beyond the filesystem.
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    device = subprocess.Popen(
        [str(_MICROPYTHON)],
        stdin=master_fd,
        stdout=master_fd,
        stderr=master_fd,
        env=env,
        close_fds=True,
    )
    os.close(master_fd)
    # `slave_fd` is held for the whole run, not closed here: a pty with no open
    # slave makes the device's next read on the master fail EIO, and the unix
    # port treats a failed stdin read as end of input, so the interpreter exits
    # before `mpremote` gets a chance to open the path. See `tests/pty_device.py`,
    # which holds one for the same reason.
    proc = None
    try:
        time.sleep(0.3)  # let the interpreter reach its REPL before talking to it
        proc = _spawn_debug(
            _RESUME + ["debug", "--port", str(port), slave_path, "target:main"], env=env
        )
        lines, matched = _read_until(proc, "MPDBG-READY ", timeout=20, at_line_start=True)
        if matched is None:
            pytest.fail(f"never saw MPDBG-READY on port {port}; output: {''.join(lines)}")
        return json.loads(matched[matched.index("{") :])
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if device.poll() is None:
            device.kill()
            device.wait(timeout=5)
        os.close(slave_fd)


@requires_settrace_firmware
def test_s6_2_endpoint_is_rederived_each_run(free_tcp_port):
    """Two runs against devices on different ports each report their own.

    A cached endpoint would make the second run advertise the first run's
    port, which is the failure this rules out.
    """
    env = dict(os.environ, MICROPYPATH=_MICROPYPATH)

    first = _run_against_pty(free_tcp_port, env)
    assert first["port"] == free_tcp_port, first

    other_port = _free_port()
    second = _run_against_pty(other_port, env)
    assert second["port"] == other_port, second
    assert second["port"] != first["port"], (first, second)


@requires_settrace_firmware
def test_s6_2_no_endpoint_state_is_written(free_tcp_port, tmp_path):
    """The run leaves no endpoint state behind for a later one to read.

    Asserted against the filesystem rather than by inspection: a cache
    written next to the target, into the mpremote package, or into the cwd
    would show up here.
    """
    env = dict(os.environ, MICROPYPATH=_MICROPYPATH)
    watched = [_TOP_DIR / "src", mpremote_debug.SUBMODULE_DIR / "mpremote", tmp_path]
    before = {d: set(os.listdir(d)) for d in watched}

    payload = _run_against_pty(free_tcp_port, env)
    assert payload["port"] == free_tcp_port, payload

    for directory, names in before.items():
        assert set(os.listdir(directory)) == names, f"{directory} gained or lost entries"
