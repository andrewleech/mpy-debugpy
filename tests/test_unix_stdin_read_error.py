"""A unix-port REPL whose stdin starts failing ends, rather than spinning on it.

`mp_hal_stdin_rx_chr` handled a zero-byte read as end of input and let a failed
read fall through with `c` uninitialised, returning whatever was on the stack.
The errors that reach it do not clear - a pty whose last other opener has gone
answers EIO for good - so the REPL took the byte and immediately asked for
another. `tests/pty_device.py` documents the cost of the resulting spin, about
85k syscalls across a single test, and works around it by holding a slave fd
open for the whole session.

This drops that reference deliberately, which is what a test that forgets to
hold it does by accident, and asserts the interpreter stops. Without the fix it
never does.
"""

import os
import pty
import subprocess
import time
import tty
from pathlib import Path

import pytest

_TOP_DIR = Path(__file__).resolve().parent.parent
_MICROPYTHON = Path(
    os.environ.get(
        "MPY_DEBUG_FIRMWARE",
        _TOP_DIR / "micropython/ports/unix/build-standard/micropython",
    )
)

pytestmark = pytest.mark.skipif(
    not _MICROPYTHON.exists(), reason=f"no unix firmware at {_MICROPYTHON}"
)

# Long enough that a healthy exit is unambiguous, short enough that the spin
# this guards against is not paid for in full by a failing run.
_EXIT_TIMEOUT_S = 10


def test_a_repl_whose_stdin_fails_exits_instead_of_spinning():
    master_fd, slave_fd = pty.openpty()
    tty.setraw(slave_fd)
    proc = subprocess.Popen(
        [str(_MICROPYTHON)],
        stdin=master_fd,
        stdout=master_fd,
        stderr=master_fd,
        close_fds=True,
    )
    os.close(master_fd)  # the child owns its own reference now
    try:
        # Let it reach the REPL before the line is pulled out from under it.
        deadline = time.monotonic() + 5
        seen = b""
        while not seen.endswith(b">>> ") and time.monotonic() < deadline:
            seen += os.read(slave_fd, 1)
        assert seen.endswith(b">>> "), f"never reached a REPL; it said {seen!r}"

        # The last slave reference goes, so every further read on the master
        # fails EIO. Nothing else changes.
        os.close(slave_fd)
        slave_fd = -1

        try:
            proc.wait(timeout=_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "the interpreter is still running {}s after its stdin started "
                "failing; it is spinning on the error rather than treating it "
                "as end of input".format(_EXIT_TIMEOUT_S)
            )
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
