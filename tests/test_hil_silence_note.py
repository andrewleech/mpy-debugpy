"""Host-side tests for what a HIL DAP timeout says about the board.

A wait that ends in silence reports the same text whether the board is
wedged, has rebooted, or raised inside the request handler, so the timeout
alone never names a cause. `Recorder.silence_note` attaches the two places
the cause can be seen: the board's own stdout, which a network session is
already reading for the length of the test, and the `--dap-log` transcript
when the run recorded one.

Reachable without a board because `Recorder` connects in `start()` rather
than in `__init__`, so a stub client and a stub device are the whole rig.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

_TESTS_DIR = Path(__file__).resolve().parent
_HIL_DIR = _TESTS_DIR / "hil"
for _path in (str(_TESTS_DIR), str(_HIL_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest  # noqa: E402
from dapdrive import Recorder  # noqa: E402


class FakeDevice:
    """Stands in for `DeviceOutput`: everything the board printed, as text."""

    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


def _timeout_message(session):
    """The AssertionError a request that got no response raises.

    `timeout=0` puts the deadline in the past, so the wait scans the (empty)
    message log once and gives up without sleeping.
    """
    server = Recorder("test", "127.0.0.1", 0, session=session)
    server.client = SimpleNamespace(send_request=lambda command, args: 7)
    with pytest.raises(AssertionError) as excinfo:
        server.request("setBreakpoints", {}, timeout=0)
    return str(excinfo.value)


def test_a_timeout_quotes_what_the_board_printed():
    """The one failure this instrument exists for: a raise inside the handler.

    Nothing reaches the client, so the DAP side sees only silence; the
    traceback is on the board's stdout and nowhere else.
    """
    traceback = "Traceback (most recent call last):\n  File 'debugpy/server.py', line 1\nMemoryError:"
    message = _timeout_message({"device": FakeDevice(traceback)})
    assert "board stdout" in message
    assert "MemoryError" in message


def test_a_silent_board_says_so_rather_than_nothing():
    """An empty capture is a finding, not a missing section.

    A board that printed nothing did not raise, which rules out the handler
    and points at the link - the opposite conclusion from a traceback. An
    absent section would read as "not looked at".
    """
    message = _timeout_message({"device": FakeDevice("   \n")})
    assert "(nothing)" in message


def test_the_transcript_path_is_named_when_one_was_recorded():
    message = _timeout_message({"device": FakeDevice(""), "dap_log": Path("/somewhere/dap-0.jsonl")})
    assert "/somewhere/dap-0.jsonl" in message


def test_a_session_with_neither_adds_nothing():
    """The serial-DAP runs: mpremote holds the port, so no capture exists.

    Those sessions carry neither key, and the note has to degrade to silence
    rather than to a section reporting on a device that was never read.
    """
    message = _timeout_message({})
    assert message.endswith("received since: []")
