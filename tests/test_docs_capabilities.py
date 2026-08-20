"""Hold the published docs to the capability vocabulary the code uses.

The capability key set has four copies in code (`launcher/firmware.py`,
`launcher/gen_manifest.py`, `.github/scripts/write_fragment.py`,
`mpremote/mpdebug_config.py`), held to each other by `test_gen_manifest.py` and
`test_s5_2_mpdebug_config.py`. It has two more in prose - `docs/firmware.md`'s
variant table and glossary, and `docs/debugging.md`'s probe table and
`requires` row - and until this file nothing held those to anything.

That gap was not hypothetical: `second_cdc` was added to all four code copies
and to `firmware.toml` on 2026-08-10 and reached neither document, so the
published `requires` vocabulary was missing a key a user could legally write
and the capability tables described a handshake that had grown a field. A
reader following either page would have been wrong in a way no test noticed.

The assertion is one-directional on purpose: every known capability must be
named. A document is free to discuss things that are not capabilities, and
pinning exact prose would make every wording change a test failure.
"""

import re
import sys
from pathlib import Path

import mpremote_debug

_TOP_DIR = Path(__file__).resolve().parents[1]
_DOCS_DIR = _TOP_DIR / "docs"

_LAUNCHER_DIR = str(_TOP_DIR / "launcher")
if _LAUNCHER_DIR not in sys.path:
    sys.path.insert(0, _LAUNCHER_DIR)
if str(mpremote_debug.SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(mpremote_debug.SUBMODULE_DIR))

import firmware  # noqa: E402
from mpremote import mpdebug_config  # noqa: E402


def _doc(name):
    return (_DOCS_DIR / name).read_text()


def _names(capability, text):
    """True if `text` uses `capability` as a whole token.

    A substring test is not enough: every capability here is a prefix of a
    plausible neighbour (`set_local` of `set_local_async`, `second_cdc` of
    `second_cdc_count`), so `in` would accept a document that had renamed the
    key and never mentioned the real one.
    """
    return re.search(rf"\b{re.escape(capability)}\b", text) is not None


def test_the_debugging_guide_names_every_manifest_capability():
    text = _doc("debugging.md")
    missing = [c for c in firmware.KNOWN_CAPABILITIES if not _names(c, text)]
    assert missing == [], f"docs/debugging.md documents no such capability: {missing}"


def test_the_firmware_guide_names_every_manifest_capability():
    text = _doc("firmware.md")
    missing = [c for c in firmware.KNOWN_CAPABILITIES if not _names(c, text)]
    assert missing == [], f"docs/firmware.md documents no such capability: {missing}"


def test_the_requires_row_lists_the_vocabulary_a_target_may_actually_name():
    """The one place a doc enumerates the set rather than mentioning members.

    `requires` is validated against `mpdebug_config.KNOWN_CAPABILITIES` before
    any device is touched, so a name missing from this row is a key that works
    and is undocumented, and a name present that the resolver rejects is a
    documented key that fails the moment someone writes it.
    """
    rows = [ln for ln in _doc("debugging.md").splitlines() if ln.startswith("| `requires` |")]
    assert len(rows) == 1, f"expected one `requires` config row, found {len(rows)}"
    named = set(re.findall(r"`([a-z_]+)`", rows[0]))

    for capability in mpdebug_config.KNOWN_CAPABILITIES:
        assert capability in named, f"`requires` row omits {capability!r}"

    # `repl_dap` is the one the resolver refuses, and the row
    # has to say so: both are reported by the handshake next to the others, so a
    # reader with the probe output in front of them has no way to tell them
    # apart otherwise.
    for refused in ("repl_dap",):
        assert refused in named, f"`requires` row does not mention {refused!r}"
        assert refused not in mpdebug_config.KNOWN_CAPABILITIES
