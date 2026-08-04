"""Unit tests for `.github/scripts/write_fragment.py`'s capability handling.

Round-2 finding (2): `settrace`/`save_names` must come from a build-evidence
check (`--capability`), not a hardcoded default, so a config regression can't
silently ship a fragment claiming a capability nothing verified.
"""

import json
import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / ".github" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import pytest  # noqa: E402
import write_fragment  # noqa: E402

_BASE_ARGS = [
    "--id",
    "unix-standard-debug",
    "--description",
    "test",
    "--repo",
    "https://example.invalid/micropython.git",
    "--source-commit",
    "0" * 40,
    "--port",
    "unix",
    "--board",
    "standard",
    "--build-flags",
    "MICROPY_PY_SYS_SETTRACE=1",
    "--artifact",
    "unix-standard-debug/micropython",
    "--release-asset",
    "firmware-unix-standard",
    "--artifact-sha256",
    "0" * 64,
]


def test_main_requires_settrace_and_save_names_capabilities(tmp_path):
    with pytest.raises(SystemExit):
        write_fragment.main([*_BASE_ARGS, "--output", str(tmp_path / "frag.json")])


def test_main_rejects_unknown_capability_key(tmp_path):
    with pytest.raises(SystemExit):
        write_fragment.main(
            [
                *_BASE_ARGS,
                "--capability",
                "settrace=true",
                "--capability",
                "save_names=true",
                "--capability",
                "bogus=true",
                "--output",
                str(tmp_path / "frag.json"),
            ]
        )


def test_main_derives_capabilities_from_evidence(tmp_path):
    out = tmp_path / "frag.json"
    rc = write_fragment.main(
        [
            *_BASE_ARGS,
            "--capability",
            "settrace=true",
            "--capability",
            "save_names=false",
            "--output",
            str(out),
        ]
    )
    assert rc == 0
    frag = json.loads(out.read_text())
    assert frag["capabilities"] == {
        "settrace": True,
        "save_names": False,
        "set_local": False,
        "f_back": True,
    }
