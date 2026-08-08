"""Which trace events a breakpoint may be hit by, exercised against the real interpreter.

MicroPython's `return` event reports the last line the frame executed, where
CPython's reports the line that ended the frame (the `for` or `def` line for a
loop or an implicit return). A breakpoint on a function's final line therefore
sees that line twice: once as the `line` event about to run it, once as the
`return` event on the way out, with the frame already finished. `should_stop`
gates on the event for that reason, and this is what would catch the gate being
dropped again.

The `call` event is the other side of the same rule: it reports the `def` line,
so a breakpoint placed there can only ever be hit by `call`, and gating on
`line` alone would silently stop honouring those.

`pdb_adapter.py` imports `from micropython import const`, so it is importable
only under MicroPython - the checks below run the real `PdbAdapter` against a
real trace-event stream as a subprocess script under the built unix firmware,
rather than reasoning about the source from CPython.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

_TOP_DIR = Path(__file__).resolve().parents[1]
_MICROPYTHON = Path(
    os.environ.get(
        "MPY_DEBUG_FIRMWARE",
        _TOP_DIR / "micropython/ports/unix/build-standard/micropython",
    )
)
_MICROPYPATH = "{}:{}".format(
    _TOP_DIR / "micropython-lib/python-ecosys/debugpy", _TOP_DIR / "micropython-lib"
)

# Written to a file rather than run with `-c` so `co_filename` is a real path,
# which is what `set_breakpoints` keys on. The two breakpoint lines are found by
# their marker comments and passed in, so editing this script cannot silently
# move a breakpoint onto the wrong line.
_STOP_GATE_SCRIPT = '''
import json
import sys

from debugpy.server.pdb_adapter import PdbAdapter

SCRIPT, last_line, def_line = sys.argv[0], int(sys.argv[1]), int(sys.argv[2])


def leaf(n):  # MARK-def
    doubled = n * 2
    return doubled  # MARK-last


def caller():
    total = 0
    for i in range(2):
        total += leaf(i)
    return total


def probe(breakpoint_line):
    """Run `caller()` with one breakpoint, reporting every event at that line.

    Both what `should_stop` agreed to stop on and what it saw and refused: a
    firmware that never delivered the refused event at all would otherwise look
    the same as one whose refusal works.
    """
    adapter = PdbAdapter()
    adapter.set_breakpoints(SCRIPT, [{"line": breakpoint_line}])
    seen, stops = [], []

    def tracer(frame, event, arg):
        if frame.f_code.co_filename == SCRIPT and frame.f_lineno == breakpoint_line:
            seen.append(event)
            if adapter.should_stop(frame, event, None):
                stops.append([event, adapter.hit_breakpoint])
        else:
            adapter.should_stop(frame, event, None)
        return tracer

    sys.settrace(tracer)
    result = caller()
    sys.settrace(None)
    return {"result": result, "seen": seen, "stops": stops}


print(
    "RESULT "
    + json.dumps({"last_line": probe(last_line), "def_line": probe(def_line)})
)
'''


def _marker_line(text, marker):
    """1-based line number of the line carrying `marker`."""
    for number, line in enumerate(text.splitlines(), start=1):
        if marker in line:
            return number
    raise AssertionError(f"marker {marker} not found in the script")


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


@pytest.fixture()
def stop_gate_results(tmp_path):
    script = tmp_path / "stop_gate.py"
    script.write_text(_STOP_GATE_SCRIPT)
    last_line = _marker_line(_STOP_GATE_SCRIPT, "MARK-last")
    def_line = _marker_line(_STOP_GATE_SCRIPT, "MARK-def")

    env = dict(os.environ)
    env["MICROPYPATH"] = _MICROPYPATH
    result = subprocess.run(
        [str(_MICROPYTHON), str(script), str(last_line), str(def_line)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"script failed: {result.stdout}\n{result.stderr}"
    line = next(
        (ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")), None
    )
    assert line is not None, f"script printed no RESULT line: {result.stdout}"
    return json.loads(line[len("RESULT ") :])


@requires_settrace_firmware
def test_breakpoint_on_a_functions_last_line_stops_once_per_call(stop_gate_results):
    """`leaf` is called twice, so its final line is a breakpoint hit twice - not four times."""
    last = stop_gate_results["last_line"]
    assert last["result"] == 2, f"the traced program did not run normally: {last}"

    assert "return" in last["seen"], (
        "this firmware never delivered a return event at the breakpoint line, so "
        "the gate under test was never exercised: " + repr(last["seen"])
    )
    assert [event for event, _hit in last["stops"]] == ["line", "line"], (
        "a breakpoint on a function's final line must be hit once per call, by the "
        "line event only: " + repr(last["stops"])
    )
    assert all(hit for _event, hit in last["stops"]), (
        "each stop must report itself as a breakpoint hit, not a step or a pause: "
        + repr(last["stops"])
    )


@requires_settrace_firmware
def test_breakpoint_on_a_def_line_is_hit_by_the_call_event(stop_gate_results):
    """A `def` line is only ever reported by `call`, so gating on `line` alone would lose it."""
    on_def = stop_gate_results["def_line"]
    assert on_def["result"] == 2, f"the traced program did not run normally: {on_def}"

    assert [event for event, _hit in on_def["stops"]] == ["call", "call"], (
        "a breakpoint on a def line must be hit once per call, by the call event: "
        + repr(on_def["stops"])
    )
    assert all(hit for _event, hit in on_def["stops"]), (
        "each stop must report itself as a breakpoint hit: " + repr(on_def["stops"])
    )
