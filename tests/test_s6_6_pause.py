"""Behavioural tests for STORY-6.6: the DAP `pause` request stops the target.

Every other test in this suite drives a target that stops on its own at a
breakpoint, which is the one thing a pause cannot be measured against. These
drive `src/spinner.py`, which runs until something interrupts it, and assert
what "stopped" has to mean: not that a `stopped` event arrived, but that the
program stopped making progress. A counter the loop advances every iteration
is read twice across a real-time gap for exactly that reason - the defect this
story closes emitted no event at all, but the shape it belongs to (a UI that
believes it is stopped while the program runs on) is only ruled out by the
second read.
"""

import time
from pathlib import Path

import pytest
from helpers import PerfServer, set_breakpoints, take_msg, wait_for_msg

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SPINNER_PY = _REPO_ROOT / "src" / "spinner.py"


def _line_of(fragment: str) -> int:
    """1-based line number of the only line in spinner.py containing `fragment`.

    Derived rather than written down: a breakpoint pinned to a literal line
    number silently moves to the wrong statement the first time the sample is
    edited, and this suite has no formatter keeping it still.
    """
    matches = [
        n for n, line in enumerate(_SPINNER_PY.read_text().splitlines(), 1) if fragment in line
    ]
    assert len(matches) == 1, f"{fragment!r} matches {len(matches)} lines of {_SPINNER_PY}"
    return matches[0]


def _evaluate_int(server: PerfServer, expression: str) -> int:
    """Evaluate `expression` in the paused top frame and return it as an int."""
    server.clear_messages()
    server.client.evaluate(expression, frame_id=0, context="watch")
    msg = wait_for_msg(server, response="evaluate")
    assert msg is not None, f"no evaluate response for {expression!r}"
    assert msg.success is True, f"evaluate({expression!r}) failed: {msg.raw}"
    return int(msg.body["result"])


def _wait_for_exit(process, timeout=15):
    """Wait for the debuggee to exit and return its remaining stdout."""
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process.poll() is not None, "debuggee did not exit after continue"
    try:
        rest = process.stdout.read() or ""
    except (BlockingIOError, OSError):
        rest = ""
    return rest


@pytest.mark.parametrize("tgt_module", ["spinner"], indirect=True)
def test_pause_stops_a_running_target(attach_server, micropython_debuggee, tgt_module: str):
    """A free-running target, sent `pause`, stops - and stays stopped."""
    server: PerfServer = attach_server
    client = server.client

    # No breakpoints at all: nothing in this target stops on its own, so any
    # stop that arrives is the one the pause asked for.
    client.configuration_done()
    # Let it get going, so the pause is interrupting a running program rather
    # than racing the start of one.
    time.sleep(0.3)

    client.pause()
    assert take_msg(server, response="pause", timeout=5) is not None, "pause was never answered"

    stopped = take_msg(server, event="stopped", timeout=10)
    assert stopped is not None, "pause was answered but the target never stopped"
    assert stopped.body["reason"] == "pause", f"stopped for the wrong reason: {stopped.raw}"

    # The frame it stopped in is the loop, not wherever the last trace event
    # happened to leave `current_frame`.
    server.clear_messages()
    client.send_request("stackTrace", {"threadId": 1})
    frames = wait_for_msg(server, response="stackTrace")
    assert frames is not None, "no stackTrace response while stopped"
    assert frames.body["stackFrames"], "stopped with an empty stack"
    assert frames.body["stackFrames"][0]["name"] == "spin"

    # Stopped means stopped: the counter the loop advances every 5 ms reads the
    # same across a gap worth ~80 iterations.
    first = _evaluate_int(server, "ticks")
    assert 0 < first, "paused before the loop had run at all"
    time.sleep(0.4)
    second = _evaluate_int(server, "ticks")
    assert second == first, f"target kept running while reported stopped: {first} -> {second}"

    # And it was stopped mid-run, not caught at the end.
    limit = _evaluate_int(server, "TICK_LIMIT")
    assert first < limit, f"paused only after the loop had finished ({first} of {limit})"

    client.continue_()
    output = _wait_for_exit(micropython_debuggee)
    assert micropython_debuggee.returncode == 0, f"debuggee exited {micropython_debuggee.returncode}"
    assert f"spinner: done {limit}" in output, output


@pytest.mark.parametrize("tgt_module", ["spinner"], indirect=True)
def test_pause_while_already_stopped_is_dropped(
    attach_server, micropython_debuggee, tgt_module: str
):
    """A pause sent at a breakpoint does not stop the target after the continue.

    The client that sends it already believes the target is stopped, so
    honouring it would stop the program one line into its next run for a reason
    nobody asked for.
    """
    server: PerfServer = attach_server
    client = server.client

    # `total = spin()` runs exactly once, so continuing past it cannot hit this
    # breakpoint again and any later stop is the pause.
    set_breakpoints(server, str(_SPINNER_PY), [_line_of("total = spin()")])
    assert wait_for_msg(server, response="setBreakpoints") is not None

    client.configuration_done()
    stopped = take_msg(server, event="stopped", timeout=10)
    assert stopped is not None, "never reached the breakpoint"
    assert stopped.body["reason"] == "breakpoint", stopped.raw

    client.pause()
    assert take_msg(server, response="pause", timeout=5) is not None, "pause was never answered"

    client.continue_()
    spurious = take_msg(server, event="stopped", timeout=2)
    assert spurious is None, f"a pause sent while stopped stopped the target again: {spurious}"

    output = _wait_for_exit(micropython_debuggee)
    assert micropython_debuggee.returncode == 0, f"debuggee exited {micropython_debuggee.returncode}"
    assert "spinner: done" in output, output
