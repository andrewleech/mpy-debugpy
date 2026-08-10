"""Host-side pytest coverage for hot reload: `restart` evicts and re-imports the target.

Acceptance criteria from STORY-4.5:
- A host edit takes effect on the next run with no upload and no reset, over a
  mounted session against a real firmware process on a real pty
- Breakpoints set before the restart still bind after it, without being re-sent
- `debugpy` itself is never evicted (the eviction set is asserted, not assumed)
- A session paused at a breakpoint can be edited and restarted
- Adversarial: the target module imports a submodule that changed as well
- Adversarial: a restart requested from inside the module that was itself
  reloaded, and two restarts requested before the first has completed

Everything here runs against a live session: a unix firmware process is the
device, connected through a real pty pair, with `mpremote resume debug --loop
--source ...` as its own subprocess. The modules under debug exist only under
the mounted host directory and are never on the device's MICROPYPATH, so the
new code reaching the device at all already proves the mount served the edited
file - there is no copy on the device that could have been uploaded, and the
device process's pid never changes, so nothing was reset either.

The observable that makes a re-run visible to a client is a DAP `output` event,
not `terminated`: a client that sees `terminated` tears the session down, which
is the opposite of what loop mode is for. It is also the only route available
here - mpremote's mount pump discards everything the device prints, so the
MPDBG-RESTART marker on device stdout never reaches this process. That is why
the launcher mirrors its markers to the debug console (see `_report` there).
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

from helpers import (  # noqa: E402
    PerfServer,
    drain_lines,
    set_breakpoints,
    take_msg,
    wait_for_prefixed_line,
)
from pty_device import PtyDevice  # noqa: E402

_MICROPYTHON = Path(
    os.environ.get(
        "MPY_DEBUG_FIRMWARE",
        _TOP_DIR / "micropython/ports/unix/build-standard/micropython",
    )
)
# Deliberately without the source directory: the only way the device can reach
# `app`/`helper` is through the mount.
_MICROPYPATH = "{}:{}".format(
    _TOP_DIR / "micropython-lib/python-ecosys/debugpy", _TOP_DIR / "micropython-lib"
)

_RUN_ENDED = "Target finished; waiting for a restart request."
_RESTART_MARKER = "MPDBG-RESTART "

# The two versions of each module differ only in what the marked lines do, so a
# breakpoint set once before the first run means the same thing in both. Line
# numbers come from the markers (see `_marker_line`), never from counting here.
APP_V1 = """import helper


def main():
    total = 0
    for i in range(3):
        total += helper.bump(i)  # BP-body
    return total  # BP-tail
"""

APP_V2 = """import helper


def main():
    total = 0
    for i in range(3):
        total += helper.bump(i)  # BP-body
    return helper.stamp()  # BP-tail
"""

HELPER_V1 = """def bump(i):
    return i  # BP-bump
"""

# `stamp` exists only here, so a stop on its line can only happen if the
# submodule was re-read - the target module alone being reloaded would still be
# calling into the old `helper`.
HELPER_V2 = """def bump(i):
    return i * 10  # BP-bump


def stamp():
    return "v2-only"  # BP-v2only
"""


def _marker_line(text, marker):
    """1-based line number of the line carrying `marker`."""
    for number, line in enumerate(text.splitlines(), start=1):
        if marker in line:
            return number
    raise AssertionError(f"marker {marker} not found")


APP_BODY_LINE = _marker_line(APP_V1, "BP-body")
APP_TAIL_LINE = _marker_line(APP_V1, "BP-tail")
HELPER_BUMP_LINE = _marker_line(HELPER_V1, "BP-bump")
HELPER_V2_ONLY_LINE = _marker_line(HELPER_V2, "BP-v2only")

assert APP_BODY_LINE == _marker_line(APP_V2, "BP-body")
assert APP_TAIL_LINE == _marker_line(APP_V2, "BP-tail")
assert HELPER_BUMP_LINE == _marker_line(HELPER_V2, "BP-bump")
assert HELPER_V2_ONLY_LINE > len(HELPER_V1.splitlines()), (
    "the v2-only breakpoint line must not exist in helper v1, or run 1 could "
    "hit it too and prove nothing"
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


class _LoopSession:
    """A live `mpremote debug --loop --source` session over a real pty.

    Breakpoints are set once, before `configurationDone`, and never re-sent:
    that they still bind on later runs is one of the things under test, so
    re-sending them would hide exactly the failure worth catching.
    """

    def __init__(self, tmp_path, port):
        self.source_dir = tmp_path / "src"
        self.source_dir.mkdir()
        self.app_py = self.source_dir / "app.py"
        self.helper_py = self.source_dir / "helper.py"
        self.app_py.write_text(APP_V1)
        self.helper_py.write_text(HELPER_V1)
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
        """Bring the session up to the point where the first run is about to start."""
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
                "20",
                "--source",
                str(self.source_dir),
                "--loop",
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

        handshake_line = wait_for_prefixed_line(self.stdout_lines, "MPDBG-READY ", timeout=25)
        assert handshake_line is not None, (
            f"mpremote never reported its handshake; stdout: {self.stdout_lines!r} "
            f"stderr: {self.stderr_lines!r}"
        )
        handshake = json.loads(handshake_line[len("MPDBG-READY ") :])

        self.dap_server = PerfServer("test-client", handshake["host"], handshake["port"])
        self.dap_server.start()
        initialize = take_msg(self.dap_server, response="initialize", timeout=15)
        assert initialize is not None, "no initialize response"
        assert initialize.body.get("supportsRestartRequest") is True, (
            "a --loop session must advertise the restart request, or a client has "
            f"no way to ask for a re-run: {initialize.body}"
        )

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
        assert take_msg(self.dap_server, response="attach", timeout=15) is not None, (
            "no attach response"
        )

        assert set_breakpoints(
            self.dap_server, str(self.app_py), [APP_BODY_LINE, APP_TAIL_LINE], wait=True
        ), "setBreakpoints failed for app.py"
        assert set_breakpoints(
            self.dap_server,
            str(self.helper_py),
            [HELPER_BUMP_LINE, HELPER_V2_ONLY_LINE],
            wait=True,
        ), "setBreakpoints failed for helper.py"

        self.dap_server.client.configuration_done()

    def rewrite_sources(self):
        """Edit both modules on the host, as an editor would, mid-session."""
        self.app_py.write_text(APP_V2)
        self.helper_py.write_text(HELPER_V2)

    def describe_stop(self):
        """Where the target is stopped, and what `total` reads there.

        `total` is evaluated in the `app.main` frame wherever that is on the
        stack, so a stop inside `helper` reports the caller's running total
        rather than nothing.
        """
        self.dap_server.client.send_request("stackTrace", {"threadId": 1})
        response = take_msg(self.dap_server, response="stackTrace", timeout=15)
        assert response is not None, "no stackTrace response"
        frames = response.body["stackFrames"]
        assert frames, "expected at least one stack frame"

        app_frame = next(
            (f for f in frames if f["source"]["path"] == str(self.app_py)), None
        )
        total = None
        if app_frame is not None:
            self.dap_server.client.send_request(
                "evaluate",
                {"expression": "total", "frameId": app_frame["id"], "context": "watch"},
            )
            evaluated = take_msg(self.dap_server, response="evaluate", timeout=15)
            assert evaluated is not None, "no evaluate response"
            if evaluated.success:
                total = evaluated.body.get("result")
        return {
            "path": frames[0]["source"]["path"],
            "line": frames[0]["line"],
            "total": total,
        }

    @staticmethod
    def _is_boundary(message):
        if message.type != "event":
            return False
        if message.event == "stopped":
            return True
        if message.event != "output":
            return False
        output = message.body.get("output", "")
        return _RUN_ENDED in output or output.startswith(_RESTART_MARKER)

    def run_until(self, label, pause_at=None, timeout=45):
        """Continue through stops until the run ends; return (stops, boundary).

        `boundary` says what ended the run's worth of stops: `"end"` for the
        target returning normally, `"restart"` for a run cut short by a restart
        (the marker for the next run is the only notice of that), or `"paused"`
        when `pause_at` matched and the target is left stopped for the caller.

        `pause_at` is a `(basename, line)` pair; the stop that matches it is
        included in `stops` and is not continued from.
        """
        stops = []
        while True:
            message = take_msg(self.dap_server, match=self._is_boundary, timeout=timeout)
            assert message is not None, (
                f"{label}: no stop and no run boundary within {timeout}s "
                f"(stops so far: {stops})"
            )
            if message.event == "output":
                output = message.body.get("output", "")
                return stops, ("end" if _RUN_ENDED in output else "restart")
            stop = self.describe_stop()
            stops.append(stop)
            if pause_at is not None and (
                os.path.basename(stop["path"]),
                stop["line"],
            ) == pause_at:
                return stops, "paused"
            self.dap_server.client.send_request("continue", {"threadId": 1})

    def request_restart(self):
        """Ask for a re-run; return the eviction list the device reports.

        The marker comes back over the debug console because on a mounted
        session there is nowhere else for it to come from.
        """
        self.dap_server.client.send_request("restart")
        assert take_msg(self.dap_server, response="restart", timeout=15) is not None, (
            "the restart request went unanswered"
        )
        marker = take_msg(
            self.dap_server,
            match=lambda m: m.type == "event"
            and m.event == "output"
            and m.body.get("output", "").startswith(_RESTART_MARKER),
            timeout=30,
        )
        assert marker is not None, "no restart marker reached the debug console"
        payload = json.loads(marker.body["output"][len(_RESTART_MARKER) :])
        assert "MPDBG-DEGRADED" not in "".join(self.stdout_lines), (
            "the firmware stopped calling back after the unwind; every run after "
            "the first restart would have no breakpoints"
        )
        return payload

    def assert_session_alive(self):
        """The session still answers requests, so a restart never took it down."""
        self.dap_server.client.send_request("threads")
        response = take_msg(self.dap_server, response="threads", timeout=15)
        assert response is not None and response.success, (
            "the session stopped answering requests"
        )

    def assert_never_relaunched(self):
        """One handshake for the whole session: a reset or relaunch would print another."""
        handshakes = [ln for ln in self.stdout_lines if ln.startswith("MPDBG-READY ")]
        assert len(handshakes) == 1, (
            f"expected exactly one handshake for the session, got {handshakes}"
        )
        assert self.device.proc.poll() is None, (
            "the device process died during the session, so something did reset it"
        )

    def detach_client(self):
        """Drop the DAP client, then let the released target leave its restart wait.

        The device's `wait_for_restart` finds the channel gone, so the target
        ends and the device is back at a raw-REPL prompt - which is what makes
        the teardown below have a responsive device to unmount. Signalling
        mpremote with the client still attached is a different case entirely
        (test_s4_3_mount_attach covers it): the target is parked in a wait that
        no interrupt can reach, and only a power cycle clears the mount.

        The wait cannot be polled for: mpremote's mount pump discards the
        device's console output, and the client this would have watched is the
        one that just went away.
        """
        self.dap_server.stop()
        self.dap_server = None
        time.sleep(1.0)

    def end(self, timeout=15):
        """Signal mpremote as Ctrl-C would, and return its exit code."""
        self.mpremote_proc.send_signal(signal.SIGINT)
        exit_code = self.mpremote_proc.wait(timeout=timeout)
        for thread in self._threads:
            thread.join(timeout=2)
        return exit_code

    def assert_device_still_usable(self):
        """A fresh connection finds a live device, carrying none of the session's state."""
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


@pytest.fixture()
def loop_session(tmp_path, free_tcp_port):
    session = _LoopSession(tmp_path, free_tcp_port)
    try:
        session.start()
        yield session
    finally:
        session.close()


def _lines_hit(stops, basename):
    return [s["line"] for s in stops if os.path.basename(s["path"]) == basename]


def _assert_ran_v1(stops):
    """The stops of a run of the original sources."""
    assert HELPER_V2_ONLY_LINE not in _lines_hit(stops, "helper.py"), (
        f"run 1 stopped on a line that only exists in the edited helper: {stops}"
    )
    tails = [s for s in stops if s["line"] == APP_TAIL_LINE]
    assert tails, f"run 1 never reached the tail of main(): {stops}"
    assert tails[-1]["total"] == "3", (
        "run 1 must show the original arithmetic (0+1+2) at the tail of main(): "
        f"{tails[-1]}"
    )


def _assert_ran_v2(stops):
    """The stops of a run of the edited sources, with no breakpoint re-sent."""
    assert HELPER_V2_ONLY_LINE in _lines_hit(stops, "helper.py"), (
        "the edited submodule's new line was never reached, so either the "
        f"submodule was not re-read or its breakpoint stopped binding: {stops}"
    )
    tails = [s for s in stops if s["line"] == APP_TAIL_LINE]
    assert tails, f"the run never reached the tail of main(): {stops}"
    assert tails[-1]["total"] == "30", (
        "the edited helper's arithmetic (0+10+20) must be what the reloaded "
        f"target computed: {tails[-1]}"
    )


def _assert_evicted_target_only(evicted):
    assert evicted["evicted"] == ["app", "helper"], (
        "a restart must evict exactly what the target imported, so the changed "
        f"submodule comes back with it: {evicted}"
    )
    assert not [name for name in evicted["evicted"] if name.startswith("debugpy")], (
        f"debugpy must never be evicted out from under the running session: {evicted}"
    )


@requires_settrace_firmware
def test_host_edit_takes_effect_on_restart_without_upload_or_reset(loop_session):
    """STORY-4.5's own criterion, end to end on a live mounted session."""
    run1, boundary = loop_session.run_until("run 1")
    assert boundary == "end", f"run 1 did not finish on its own: {boundary}"
    _assert_ran_v1(run1)

    loop_session.rewrite_sources()
    _assert_evicted_target_only(loop_session.request_restart())

    run2, boundary = loop_session.run_until("run 2")
    assert boundary == "end", f"run 2 did not finish on its own: {boundary}"
    _assert_ran_v2(run2)

    loop_session.assert_never_relaunched()
    loop_session.assert_session_alive()


@requires_settrace_firmware
def test_restart_while_paused_reloads_and_repeats_from_inside_the_reloaded_module(
    loop_session,
):
    """A restart from a stopped target, then another from inside the module that reloaded.

    The second one is the adversarial case: the frame the request is answered in
    belongs to a module that the eviction is about to drop, and the unwind has
    to leave the session able to run it again anyway.
    """
    run1, boundary = loop_session.run_until(
        "run 1", pause_at=("app.py", APP_BODY_LINE)
    )
    assert boundary == "paused", f"run 1 never stopped in app.main: {run1}"

    loop_session.rewrite_sources()
    _assert_evicted_target_only(loop_session.request_restart())

    run2, boundary = loop_session.run_until(
        "run 2", pause_at=("helper.py", HELPER_BUMP_LINE)
    )
    assert boundary == "paused", (
        f"run 2 never stopped inside the reloaded submodule: {run2}"
    )

    _assert_evicted_target_only(loop_session.request_restart())

    run3, boundary = loop_session.run_until("run 3")
    assert boundary == "end", f"run 3 did not finish on its own: {boundary}"
    _assert_ran_v2(run3)

    loop_session.assert_never_relaunched()
    loop_session.assert_session_alive()


@requires_settrace_firmware
def test_two_restart_requests_before_the_first_completes(loop_session):
    """Both requests are answered and the session survives, whatever they coalesce into.

    Two requests arriving together may be drained into one restart, or the
    second may be read during the next run and restart it again - each request
    restarts, which is the contract. The invariant is what is asserted: both are
    answered, the session stays up, and a run of the edited sources happens.
    """
    run1, boundary = loop_session.run_until(
        "run 1", pause_at=("app.py", APP_BODY_LINE)
    )
    assert boundary == "paused", f"run 1 never stopped in app.main: {run1}"

    loop_session.rewrite_sources()
    loop_session.dap_server.client.send_request("restart")
    loop_session.dap_server.client.send_request("restart")
    for attempt in (1, 2):
        assert (
            take_msg(loop_session.dap_server, response="restart", timeout=20) is not None
        ), f"restart request {attempt} went unanswered"

    # However the two requests divide up into runs, one of them has to be a
    # complete run of the edited sources; a run cut short by the second restart
    # is not, so keep going until one ends on its own.
    for _attempt in range(3):
        stops, boundary = loop_session.run_until("run after two restarts")
        if boundary == "end":
            _assert_ran_v2(stops)
            break
    else:
        pytest.fail("no run of the edited sources ever completed")

    loop_session.assert_never_relaunched()
    loop_session.assert_session_alive()


@requires_settrace_firmware
def test_a_client_leaving_between_runs_leaves_the_mount_tearable_down(loop_session):
    """The client goes away while the target waits for a restart, and teardown is still clean.

    That path is the one where the device's own loop gives up (`wait_for_restart`
    returns False with no client left), and it runs while the mount is still up
    and being pumped. What is checkable from here is what matters for the next
    session: the mount comes down without the pump-stuck warning, and the device
    is left at a working REPL rather than desynced.

    mpremote itself has to be signalled either way - a mounted session with no
    proxy to watch has no client-session end it can observe, so `Ctrl-C` is the
    only way `_stay_attached_mount` ever returns.
    """
    run1, boundary = loop_session.run_until("run 1")
    assert boundary == "end", f"run 1 did not finish on its own: {boundary}"

    loop_session.detach_client()

    assert loop_session.end() == 0, (
        f"mpremote did not exit cleanly after the client left: {loop_session.stderr_text}"
    )
    assert "did not stop in time" not in loop_session.stderr_text, (
        f"the mount's filesystem pump was left wedged: {loop_session.stderr_text}"
    )
    loop_session.assert_device_still_usable()
