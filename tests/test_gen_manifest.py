"""Unit tests for `launcher/gen_manifest.py` fragment validation.

Focused on the `artifact` path check: `validate_fragment`'s failure message
claims "artifact must be a non-empty relative path", so an absolute path or a
`..`-escaping path must actually be rejected, not just an empty one.
"""

import json
import sys
from pathlib import Path

_LAUNCHER_DIR = str(Path(__file__).resolve().parents[1] / "launcher")
if _LAUNCHER_DIR not in sys.path:
    sys.path.insert(0, _LAUNCHER_DIR)

import gen_manifest  # noqa: E402

_VALID_FRAGMENT = {
    "id": "unix-standard-debug",
    "description": "test fragment",
    "repo": "https://example.invalid/micropython.git",
    "source_commit": "0" * 40,
    "port": "unix",
    "board": "standard",
    "build_flags": ["MICROPY_PY_SYS_SETTRACE=1"],
    "capabilities": {"settrace": True},
    "artifact": "unix-standard-debug/micropython",
    "release_asset": "firmware-unix-standard",
    "artifact_sha256": "0" * 64,
}


def test_validate_fragment_accepts_relative_artifact_path():
    problems = gen_manifest.validate_fragment(Path("frag.json"), dict(_VALID_FRAGMENT))
    assert problems == []


def test_validate_fragment_rejects_absolute_artifact_path():
    frag = dict(_VALID_FRAGMENT, artifact="/etc/passwd")
    problems = gen_manifest.validate_fragment(Path("frag.json"), frag)
    assert any("relative path" in p for p in problems), problems


def test_validate_fragment_rejects_parent_traversal_artifact_path():
    frag = dict(_VALID_FRAGMENT, artifact="../../etc/passwd")
    problems = gen_manifest.validate_fragment(Path("frag.json"), frag)
    assert any("relative path" in p for p in problems), problems


def test_validate_fragment_accepts_toolchain_table():
    frag = dict(_VALID_FRAGMENT, toolchain={"gcc": "13.2.0", "runner_image": "ubuntu24/20260101.1"})
    problems = gen_manifest.validate_fragment(Path("frag.json"), frag)
    assert problems == []


def test_validate_fragment_rejects_non_string_toolchain_values():
    frag = dict(_VALID_FRAGMENT, toolchain={"gcc": 13.2})
    problems = gen_manifest.validate_fragment(Path("frag.json"), frag)
    assert any("toolchain" in p for p in problems), problems


def test_validate_fragment_rejects_empty_artifact_sha256():
    """A present-but-empty artifact_sha256 must not pass; `sha256sum -c` treats
    an empty digest field as a malformed line and skips it, not a failure, so
    this field is the only thing standing between that and a silently
    unverified published asset (round-2 finding (1))."""
    frag = dict(_VALID_FRAGMENT, artifact_sha256="")
    problems = gen_manifest.validate_fragment(Path("frag.json"), frag)
    assert any("well-formed 64-character hex digest" in p for p in problems), problems


def test_check_fragments_rejects_missing_expected_id(tmp_path):
    (tmp_path / "unix-standard-debug.json").write_text(json.dumps(_VALID_FRAGMENT))
    problems = gen_manifest.check_fragments(tmp_path, expect_ids=["unix-standard-debug", "rp2-rpi-pico-w-debug"])
    assert any("rp2-rpi-pico-w-debug" in p for p in problems), problems


def test_check_fragments_rejects_unexpected_extra_id(tmp_path):
    (tmp_path / "unix-standard-debug.json").write_text(json.dumps(_VALID_FRAGMENT))
    problems = gen_manifest.check_fragments(tmp_path, expect_ids=[])
    assert any("unix-standard-debug" in p for p in problems), problems


def test_check_fragments_accepts_exact_expected_id_set(tmp_path):
    (tmp_path / "unix-standard-debug.json").write_text(json.dumps(_VALID_FRAGMENT))
    problems = gen_manifest.check_fragments(tmp_path, expect_ids=["unix-standard-debug"])
    assert problems == []
