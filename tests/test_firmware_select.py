"""Unit tests for `launcher/firmware.py`'s capability-driven `select` (STORY-3.3).

These exercise `parse_need`/`select_variants`/`cmd_select` against fixture
manifests built in-memory or as temp TOML files - no firmware process or DAP
session involved. The real-handshake mismatch-guard test lives in
`test_capability_guard.py`.
"""

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

_LAUNCHER_DIR = str(Path(__file__).resolve().parents[1] / "launcher")
if _LAUNCHER_DIR not in sys.path:
    sys.path.insert(0, _LAUNCHER_DIR)

import firmware  # noqa: E402


def _variant(id, port="unix", board="standard", deprecated=False, sha256="0" * 64, url="", **caps):
    return {
        "id": id,
        "port": port,
        "board": board,
        "deprecated": deprecated,
        "artifact": f"{id}/micropython",
        "artifact_sha256": sha256,
        "download_url": url,
        "capabilities": caps,
    }


# --- parse_need ---------------------------------------------------------


def test_parse_need_accepts_known_keys():
    assert firmware.parse_need("settrace, save_names") == ["settrace", "save_names"]


def test_parse_need_rejects_unknown_key():
    with pytest.raises(firmware.SelectionError, match="set_local_write"):
        firmware.parse_need("settrace,set_local_write")


def test_parse_need_rejects_empty_string():
    # An empty --need has no required capability at all; parsing it to []
    # would make select_variants' `all(... for key in [])` vacuously true,
    # silently matching every non-deprecated variant instead of erroring.
    with pytest.raises(firmware.SelectionError, match="at least one capability"):
        firmware.parse_need("")


def test_parse_need_rejects_separator_only_string():
    with pytest.raises(firmware.SelectionError, match="at least one capability"):
        firmware.parse_need(",, ,")


# --- select_variants -----------------------------------------------------


def test_select_variants_excludes_deprecated():
    variants = [
        _variant("legacy", deprecated=True, settrace=True, save_names=True),
        _variant("current", settrace=True, save_names=True),
    ]
    candidates, matches = firmware.select_variants(variants, ["settrace", "save_names"])
    assert [v["id"] for v in candidates] == ["current"]
    assert [v["id"] for v in matches] == ["current"]


def test_select_variants_filters_by_port_and_board():
    variants = [
        _variant("unix-debug", port="unix", board="standard", settrace=True),
        _variant("esp32-debug", port="esp32", board="ESP32_GENERIC", settrace=True),
    ]
    _, matches = firmware.select_variants(variants, ["settrace"], port="unix")
    assert [v["id"] for v in matches] == ["unix-debug"]

    _, matches = firmware.select_variants(variants, ["settrace"], port="esp32", board="ESP32_GENERIC")
    assert [v["id"] for v in matches] == ["esp32-debug"]


def test_select_variants_ambiguous_when_multiple_match():
    variants = [
        _variant("a", settrace=True, save_names=True),
        _variant("b", settrace=True, save_names=True),
    ]
    _, matches = firmware.select_variants(variants, ["settrace"])
    assert len(matches) == 2


def test_select_variants_empty_when_capability_unmet():
    variants = [_variant("a", settrace=True, save_names=False)]
    _, matches = firmware.select_variants(variants, ["save_names"])
    assert matches == []


def test_select_variants_rejects_empty_need():
    # A direct caller (e.g. the future mpremote debug flow) that passes an
    # empty need must get the same hard error the CLI gets via parse_need,
    # not a vacuous match against every candidate.
    variants = [_variant("a", settrace=False, save_names=False)]
    with pytest.raises(firmware.SelectionError, match="at least one capability"):
        firmware.select_variants(variants, [])


def test_select_variants_treats_unknown_string_as_not_satisfying():
    # The legacy schema records "unknown" for a build that was never
    # confirmed; that must not satisfy a --need for the same key.
    variants = [_variant("a", settrace=True, save_names="unknown")]
    _, matches = firmware.select_variants(variants, ["save_names"])
    assert matches == []


# --- cmd_select (CLI-level, against a temp manifest + firmware dir) ------


@pytest.fixture()
def temp_firmware(tmp_path, monkeypatch):
    """Point the module's manifest/firmware-dir globals at a scratch directory."""
    firmware_dir = tmp_path / "firmware"
    firmware_dir.mkdir()
    monkeypatch.setattr(firmware, "FIRMWARE_DIR", firmware_dir)
    monkeypatch.setattr(firmware, "MANIFEST_PATH", firmware_dir / "firmware.toml")
    return firmware_dir


def _run_select(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = firmware.main(["select", *args])
    return rc, out.getvalue(), err.getvalue()


def test_cmd_select_resolves_single_match_with_local_artifact(temp_firmware, monkeypatch):
    artifact_path = temp_firmware / "unix-standard-debug" / "micropython"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"fake-firmware")
    sha256 = firmware.sha256_of(artifact_path)

    variants = [_variant("unix-standard-debug", sha256=sha256, settrace=True, save_names=True)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    rc, out, err = _run_select(["--need", "settrace,save_names", "--port", "unix"])
    assert rc == 0, err
    assert out.strip() == str(artifact_path)


def test_cmd_select_zero_matches_is_an_error(temp_firmware, monkeypatch):
    variants = [_variant("unix-standard-debug", settrace=True, save_names=False)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    rc, out, err = _run_select(["--need", "save_names"])
    assert rc != 0
    assert "resolved to 0 variant" in err
    assert "unix-standard-debug" in err


def test_cmd_select_multiple_matches_is_an_error(temp_firmware, monkeypatch):
    variants = [
        _variant("a", settrace=True),
        _variant("b", settrace=True),
    ]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    rc, out, err = _run_select(["--need", "settrace"])
    assert rc != 0
    assert "resolved to 2 variant" in err


def test_cmd_select_empty_need_is_a_hard_error(temp_firmware, monkeypatch):
    # Regression: an empty --need must never resolve, even against a variant
    # that claims zero capabilities - a shell variable expanding empty
    # (`--need "$caps"`) must not silently select an arbitrary build.
    variants = [_variant("a", settrace=False, save_names=False, set_local=False)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    rc, out, err = _run_select(["--need", ""])
    assert rc != 0
    assert "at least one capability" in err
    assert out.strip() == ""


def test_cmd_select_unknown_capability_is_a_hard_error(temp_firmware, monkeypatch):
    variants = [_variant("a", settrace=True)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    rc, out, err = _run_select(["--need", "teleport"])
    assert rc != 0
    assert "unknown capability" in err
    assert "teleport" in err


def test_cmd_select_missing_artifact_with_placeholder_url_is_an_error(temp_firmware, monkeypatch):
    variants = [_variant("unix-standard-debug", url="", settrace=True)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    rc, out, err = _run_select(["--need", "settrace"])
    assert rc != 0
    assert "STORY-3.2" in err


def test_cmd_select_fetches_missing_artifact_from_real_url(temp_firmware, monkeypatch):
    content = b"fetched-firmware-bytes"
    import hashlib

    sha256 = hashlib.sha256(content).hexdigest()
    variants = [
        _variant("unix-standard-debug", url="https://example.invalid/fw.bin", sha256=sha256, settrace=True)
    ]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    class _FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with patch.object(firmware.urllib.request, "urlopen", return_value=_FakeResponse(content)):
        rc, out, err = _run_select(["--need", "settrace"])

    assert rc == 0, err
    expected_path = temp_firmware / "unix-standard-debug" / "micropython"
    assert out.strip() == str(expected_path)
    assert expected_path.read_bytes() == content
