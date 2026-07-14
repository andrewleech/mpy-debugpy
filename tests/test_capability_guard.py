"""Adversarial + integration test for the manifest-vs-probe mismatch guard
(STORY-3.3), wired into `tests/fixtures/debuggee.py`'s `probed_capabilities`
fixture. Drives a real MPDBG-READY handshake against the unix debuggee
firmware rather than asserting against a mocked probe result, so this fails
if the guard - or the probe it checks against - regresses.
"""

import json
import sys
from pathlib import Path

import pytest
from fixtures.debuggee import read_mpdbg_ready

_LAUNCHER_DIR = str(Path(__file__).resolve().parent.parent / "launcher")
if _LAUNCHER_DIR not in sys.path:
    sys.path.insert(0, _LAUNCHER_DIR)

import capcheck

# `set_local` (local-variable write-back) is not implemented by any branch of
# the current lineage (D4, planning/BACKGROUND.md) - claiming it true is
# exactly the false-manifest scenario STORY-3.3 exists to catch.
_FALSE_CLAIM = {"settrace": True, "save_names": True, "set_local": True, "f_back": True}

# A claim that matches what the unix-standard debug build actually probes to.
_TRUE_CLAIM = {"settrace": True}


def test_false_set_local_claim_hard_errors_against_real_probe(attach_server, micropython_debuggee):
    """A manifest entry claiming set_local=true against the real unix build
    must hard-error, never resolve silently."""
    probed = read_mpdbg_ready(micropython_debuggee)["caps"]
    assert probed["set_local"] is False, "test assumption: this firmware has no set_local support"

    with pytest.raises(capcheck.CapabilityMismatch, match="set_local"):
        capcheck.check_capabilities(_FALSE_CLAIM, probed)


@pytest.mark.parametrize("claimed_capabilities", [_TRUE_CLAIM], indirect=True)
def test_probed_capabilities_fixture_accepts_a_matching_claim(attach_server, probed_capabilities):
    """A claim consistent with the real probe passes through unchanged -
    existing (unparametrized) callers of `probed_capabilities` stay unaffected
    by the guard, and a correct claim isn't penalised either."""
    assert probed_capabilities["settrace"] is True


def test_probed_capabilities_fixture_hard_errors_via_env_var_claim(
    attach_server, micropython_debuggee, request, monkeypatch
):
    """The env-var activation path (`MPY_DEBUG_CLAIMED_CAPS`) hard-errors just
    like a parametrized claim - a caller that supplies a claim this way gets
    the same guarantee without needing to parametrize a fixture."""
    monkeypatch.setenv("MPY_DEBUG_CLAIMED_CAPS", json.dumps(_FALSE_CLAIM))

    with pytest.raises(capcheck.CapabilityMismatch, match="set_local"):
        request.getfixturevalue("probed_capabilities")
