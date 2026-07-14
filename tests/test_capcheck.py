"""Unit tests for `launcher/capcheck.py`'s manifest-vs-probe mismatch guard
(STORY-3.3). Pure logic, no firmware process involved - the real-handshake
integration test lives in `test_capability_guard.py`.
"""

import sys
from pathlib import Path

import pytest

_LAUNCHER_DIR = str(Path(__file__).resolve().parent.parent / "launcher")
if _LAUNCHER_DIR not in sys.path:
    sys.path.insert(0, _LAUNCHER_DIR)

import capcheck


def test_matching_claim_does_not_raise():
    claimed = {"settrace": True, "save_names": True, "set_local": False}
    probed = {"settrace": True, "save_names": True, "set_local": False, "f_back": True}
    capcheck.check_capabilities(claimed, probed)  # must not raise


def test_false_positive_claim_raises_and_names_the_key():
    claimed = {"settrace": True, "set_local": True}
    probed = {"settrace": True, "set_local": False, "f_back": True}
    with pytest.raises(capcheck.CapabilityMismatch, match="set_local"):
        capcheck.check_capabilities(claimed, probed)


def test_multiple_mismatches_are_all_named():
    claimed = {"set_local": True, "f_back": True}
    probed = {"set_local": False, "f_back": False}
    with pytest.raises(capcheck.CapabilityMismatch) as exc_info:
        capcheck.check_capabilities(claimed, probed)
    assert "set_local" in str(exc_info.value)
    assert "f_back" in str(exc_info.value)


def test_unknown_string_claim_is_not_a_true_claim():
    # The legacy manifest schema records "unknown" for an unconfirmed build;
    # that must never be treated as a claim capable of mismatching.
    claimed = {"save_names": "unknown"}
    probed = {"save_names": False}
    capcheck.check_capabilities(claimed, probed)  # must not raise


def test_claimed_false_never_mismatches_regardless_of_probe():
    claimed = {"set_local": False}
    probed = {"set_local": True}
    capcheck.check_capabilities(claimed, probed)  # must not raise


def test_key_missing_from_probed_counts_as_false():
    claimed = {"f_back": True}
    probed = {}
    with pytest.raises(capcheck.CapabilityMismatch, match="f_back"):
        capcheck.check_capabilities(claimed, probed)
