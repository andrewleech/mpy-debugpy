#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Cross-check a claimed capability set against a runtime probe result.

Capability is probed at runtime (STORY-1.2: `debugpy.get_capabilities()`,
surfaced in the launcher's `MPDBG-READY` handshake); a firmware manifest
`capabilities` table only records what a build was *intended* to provide.
This module is the single place that compares the two, so a stale or wrong
manifest entry fails loudly instead of silently degrading a debug session
(D4, planning/BACKGROUND.md).
"""

from __future__ import annotations

from typing import Any, Mapping


class CapabilityMismatch(Exception):
    """A manifest claims a capability as true that the runtime probe disproves."""


def check_capabilities(claimed: Mapping[str, Any], probed: Mapping[str, Any]) -> None:
    """Raise `CapabilityMismatch` if any `claimed` key is true but `probed` disagrees.

    Only a claim of exactly `True` is checked - the legacy manifest schema
    also allows `"unknown"` for a build that was never confirmed, and that is
    not a claim capable of mismatching. A key `probed` omits counts as false.
    """
    mismatches = sorted(key for key, value in claimed.items() if value is True and probed.get(key) is not True)
    if mismatches:
        raise CapabilityMismatch(
            "capability mismatch: manifest claims true but the runtime probe disagrees for "
            f"{', '.join(mismatches)} (claimed={dict(claimed)!r}, probed={dict(probed)!r})"
        )
