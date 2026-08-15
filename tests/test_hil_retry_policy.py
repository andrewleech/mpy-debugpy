"""What the hardware suite retries, and what it still fails on first attempt.

The WiFi stall is parked (`planning/20260814_wifi_stall_investigation_handover.md`):
it lives below MicroPython, it is not this project's to fix, and it fails a
hardware scenario about once in twenty runs. So a scenario gets one retry - but
only against that fault's own failure text, because a blanket retry would hide
every other kind of intermittent the hardware suite exists to catch.

These run on the host: the policy is a pair of regexes and a marker, and both
are checkable without a board.
"""

import re
import sys
from pathlib import Path

import pytest

_HIL_DIR = Path(__file__).resolve().parent / "hil"
if str(_HIL_DIR) not in sys.path:
    sys.path.insert(0, str(_HIL_DIR))

# Imported by path rather than as `conftest`, because there is a conftest at two
# levels of this suite and which one wins depends on sys.path order.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("hil_conftest", _HIL_DIR / "conftest.py")
_hil_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hil_conftest)

SIGNATURES = _hil_conftest._PARKED_STALL_SIGNATURES


def _matches(text):
    return any(re.search(pattern, text) for pattern in SIGNATURES)


# Produced verbatim by `dapdrive.Recorder.request` and `dapdrive.attached` when
# a board answers earlier requests on a connection and then stops. Copied from
# real failures rather than paraphrased: the policy is a regex against a message
# nothing pins otherwise, so a reworded assertion would silently stop retrying.
PARKED = [
    "no response to setBreakpoints (seq 2) within 20s; received since: []",
    "no response to configurationDone (seq 3) within 20s; received since: []",
    "no response to stackTrace (seq 7) within 20s; received since: []",
    "no initialize response from 192.168.0.114:5678",
]

# Failures that mean something else, every one of which has been seen for a real
# reason during this project and must still fail on the first attempt.
NOT_PARKED = [
    "target never stopped at target.py:80",
    "no stop after a step",
    "device never recovered after the bridge was killed while stopped",
    "never saw MPDBG-READY; output:\n",
    "AssertionError: assert 'passed' == 'failed'",
    "OSError: [Errno 2] No such file or directory: 'mpdebug.toml'",
    "the device stopped answering after printing 5124 bytes",
]


@pytest.mark.parametrize("text", PARKED)
def test_the_parked_stall_is_retried(text):
    assert _matches(text), f"{text!r} is the parked fault and would not be retried"


@pytest.mark.parametrize("text", NOT_PARKED)
def test_every_other_failure_is_not_retried(text):
    assert not _matches(text), f"{text!r} is not the parked fault and must not be retried"


def test_hardware_scenarios_carry_one_retry():
    """The marker the hardware conftest attaches, with its bound and its filter.

    A retry with no `only_rerun` would hide everything above; more than one
    retry would turn a fault seen once in twenty runs into one seen once in
    four hundred, which is indistinguishable from fixing it.
    """
    added = []

    class _Item:
        keywords = {}
        fspath = _HIL_DIR / "test_hil_network.py"

        def add_marker(self, marker):
            added.append(marker)

    _hil_conftest.pytest_collection_modifyitems([_Item()])

    assert "hil" in added, added
    flaky = [m for m in added if getattr(m, "name", None) == "flaky"]
    assert len(flaky) == 1, added
    assert flaky[0].kwargs["reruns"] == 1, flaky[0].kwargs
    assert flaky[0].kwargs["only_rerun"] == SIGNATURES, flaky[0].kwargs


def test_a_host_test_carries_no_retry():
    """Nothing outside `tests/hil/` is retried, whatever it fails with."""
    added = []

    class _Item:
        keywords = {}
        fspath = Path(__file__)

        def add_marker(self, marker):
            added.append(marker)

    _hil_conftest.pytest_collection_modifyitems([_Item()])
    assert added == [], added
