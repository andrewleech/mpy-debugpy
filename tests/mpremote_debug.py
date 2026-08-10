"""Shared plumbing for driving `mpremote debug` as a subprocess.

Both the unix-flow (s5.3) and full-DAP-flow (s5.5) suites spawn the real
`mpremote debug` CLI and must account for the launcher child it starts, so
the spawn/read/reap logic lives here once instead of drifting between two
copies.
"""

import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOP_DIR = Path(__file__).resolve().parents[1]

# `MPY_DEBUG_MPREMOTE_DIR` points the suite at a different mpremote source
# tree, so the same behavioural tests can validate a composition that carries
# the debug branches alongside other work (the ampremote integration).
SUBMODULE_DIR = Path(
    os.environ.get(
        "MPY_DEBUG_MPREMOTE_DIR",
        TOP_DIR / "micropython" / "tools" / "mpremote",
    )
).resolve()

if str(SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(SUBMODULE_DIR))

MICROPYTHON = Path(
    os.environ.get(
        "MPY_DEBUG_FIRMWARE",
        TOP_DIR / "micropython/ports/unix/build-standard/micropython",
    )
)


def _resume_prefix():
    """`["resume"]`, or `[]` where this mpremote has no such command.

    Serial targets need the connection left running across the `debug`
    command. Resume is the default in micropython PR #17485, which deletes
    the command outright, so a tree composed with it rejects the word.
    """
    main_py = SUBMODULE_DIR / "mpremote" / "main.py"
    try:
        has_resume = '"resume": (' in main_py.read_text()
    except OSError:
        has_resume = True
    return ["resume"] if has_resume else []


RESUME = _resume_prefix()


def _firmware_has_settrace():
    """Probe the built firmware for sys.settrace."""
    if not MICROPYTHON.exists():
        return False
    try:
        result = subprocess.run(
            [str(MICROPYTHON), "-c", "import sys; print(hasattr(sys, 'settrace'))"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "True"


requires_settrace_firmware = pytest.mark.skipif(
    not _firmware_has_settrace(),
    reason=f"{MICROPYTHON} lacks sys.settrace (needs micropython #8767 composed in)",
)


def debug_child_pids():
    """PIDs of launcher subprocesses currently alive, system-wide.

    For flows built on a blocking `subprocess.run` (resolver/handshake
    failures that exit before this returns), there is no live parent pid
    left to scope a `/proc` lookup to, so this is the only way to check for
    an orphan - unlike `child_pids`, it can also match an unrelated
    launcher elsewhere on the machine, so prefer `child_pids`/
    `new_launcher_pids` whenever a live `Popen` handle exists.
    """
    result = subprocess.run(
        ["pgrep", "-f", "mpy_launch_debugpy"], capture_output=True, text=True, timeout=10
    )
    return set(result.stdout.split())


def child_pids(pid):
    """Direct child PIDs of `pid`, read from /proc (Linux-only, matches this
    harness's existing POSIX-only fcntl/pty use elsewhere in the s5.1 suite).

    Scoped to one process, unlike `debug_child_pids`, which would also match
    unrelated launchers on the machine.
    """
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            return set(f.read().split())
    except OSError:
        return set()


def pids_alive(pids):
    """Subset of `pids` still running, probed via signal 0.

    Assert against this rather than reaping strays: a harness that kills
    leftovers would hide the defect it exists to catch.
    """
    alive = set()
    for pid in pids:
        try:
            os.kill(int(pid), 0)
        except OSError:
            continue
        alive.add(pid)
    return alive


def spawn_debug(args, env=None, cwd=None):
    """Start `mpremote debug` and return the live Popen (stdout+stderr merged).

    Binary mode, not `text=True`: `read_until` reads the raw fd directly via
    `os.read`, bypassing Python's own buffered line reader - mixing a
    buffered `readline()` with `select()` on the underlying fd is exactly
    the trap this avoids (see `read_until`'s docstring).
    """
    if env is None:
        env = os.environ.copy()
    return subprocess.Popen(
        [sys.executable, "-m", "mpremote"] + args,
        cwd=str(cwd or SUBMODULE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )


def read_until(proc, marker, timeout=30, at_line_start=False):
    """Read `proc.stdout` lines until one contains `marker`, EOF, or timeout.

    Reads the raw fd via `os.read`, never `proc.stdout.readline()`: a
    buffered reader can slurp several already-flushed lines into its own
    buffer on one underlying read, after which `select()` on the raw fd sees
    no *new* data and blocks until the next real I/O event - stranding
    lines that are already sitting in Python-land unread.

    `at_line_start=True` requires the marker to open the line rather than
    appear anywhere in it. Mandatory for `MPDBG-READY `, because mpremote
    quotes that marker when reporting its absence ("device exited before
    printing a 'MPDBG-READY ' line"): a substring search hands the caller
    the report of the absence as the thing itself. mpremote prints the
    handshake on a line of its own (`commands.py`), and its own reader
    anchors the same way (`mpdebug_handshake.read_handshake`).

    `marker=None` drains whatever arrives for `timeout` seconds without
    looking for anything in particular; `matched_line` is then always None.
    Otherwise returns `(lines_read, matched_line)`, with `matched_line` None
    if the marker never appeared, which the caller must treat as a failure
    (and kill `proc` itself - this never does).
    """
    deadline = time.monotonic() + timeout
    fd = proc.stdout.fileno()
    buf = ""
    lines = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        chunk = os.read(fd, 4096)
        if chunk == b"":
            break
        buf += chunk.decode(errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line += "\n"
            lines.append(line)
            if marker is None:
                continue
            if line.startswith(marker) if at_line_start else marker in line:
                return lines, line
    if buf:
        lines.append(buf)
    return lines, None


def new_launcher_pids(proc):
    """The `mpremote debug` process's launcher child PID(s), read while `proc`
    is still alive.

    On failure, kills `proc` first so a broken assertion doesn't itself leak
    the mpremote process it was checking.
    """
    new_pids = child_pids(proc.pid)
    if not new_pids:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("no launcher child appeared after the handshake")
    return new_pids


def end_session(proc, new_pids, timeout=10):
    """End a supervising `mpremote debug` session and confirm its child is gone.

    If the child already exited on its own (its DAP peer disconnected, its
    own program returned), the command's streaming loop already noticed and
    may have exited too; otherwise SIGINT ends it as Ctrl-C would. Either
    way, `new_pids` (from `new_launcher_pids`) must be gone afterwards.
    """
    import signal

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
    still_alive = pids_alive(new_pids)
    assert not still_alive, f"launcher child(ren) {still_alive} still alive after the session ended"
