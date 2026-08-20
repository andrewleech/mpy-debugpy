"""Host-side tests for the HIL results-record writer.

A results record is provenance for a hardware run, so a session that never
opened the board must not write one. The failure mode is self-perpetuating
rather than merely untidy: the record lands untracked in `planning/`, which
makes the tree dirty, which fails the next run's attribution gate before it
reaches the board, which writes the record again.

Reachable without a board because the writer is a session hook over two
module-level dicts, so a fake session and a throwaway top directory are the
whole rig.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_TESTS_DIR = Path(__file__).resolve().parent
_HIL_DIR = _TESTS_DIR / "hil"
for _path in (str(_TESTS_DIR), str(_HIL_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest  # noqa: E402

_NODEID = "tests/hil/test_hil_network.py::test_hil_breakpoint_stops_the_target_on_the_board"

_FACTS = {
    "device": "/dev/serial/by-id/usb-fake-if01",
    "board": "FAKE_BOARD",
    "machine": "FAKE with FAKECPU",
    "firmware": "v0.0.0-fake",
    "usb_mode": "VCP+MSC",
    "capabilities": {"settrace": True, "second_cdc": False},
    "debuggee": "/flash/target.py",
}

_TREE = {"head": "0" * 40, "pins": {"micropython": "1" * 40}, "dirty": [], "allowed_dirty": False}


@pytest.fixture
def writer(tmp_path, monkeypatch):
    """The record writer, aimed at a throwaway top directory.

    Loaded under a name of its own rather than imported: pytest owns the
    `conftest` module name, and this needs the module object to patch
    `_TOP_DIR` on.
    """
    spec = importlib.util.spec_from_file_location("hil_conftest_under_test", _HIL_DIR / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_TOP_DIR", tmp_path)
    (tmp_path / "planning").mkdir()
    module._RESULTS.clear()
    module._MEASUREMENTS.clear()
    return module


def _records(tmp_path):
    return sorted(p.name for p in (tmp_path / "planning").iterdir())


def test_a_session_that_never_reached_the_board_writes_no_record(writer, tmp_path):
    """The tree gate errors every scenario in setup, and setup errors count.

    So results alone do not mean the board was opened, and a record written
    here would describe a run that never happened - while dirtying the tree
    that made the run refuse in the first place.
    """
    writer._RESULTS[_NODEID] = "error"
    writer.pytest_sessionfinish(SimpleNamespace())
    assert _records(tmp_path) == []


def test_a_session_that_reached_the_board_writes_one(writer, tmp_path):
    """The control: the same call, with the facts a live board produced."""
    writer._RESULTS[_NODEID] = "passed"
    writer.pytest_sessionfinish(SimpleNamespace(_hil_facts=_FACTS, _hil_tree=_TREE))
    written = _records(tmp_path)
    assert len(written) == 1, written
    assert written[0].endswith("_hil_FAKE_BOARD.md"), written[0]
    body = (tmp_path / "planning" / written[0]).read_text()
    assert "test_hil_breakpoint_stops_the_target_on_the_board` | passed" in body
    assert _TREE["head"] in body
