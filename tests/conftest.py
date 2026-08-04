import sys
from pathlib import Path

import pytest

pytest_plugins = [
    "fixtures.debuggee",
    "fixtures.fake_vscode",
    "fixtures.attach_server",
]

_LAUNCHER_DIR = str(Path(__file__).resolve().parents[1] / "launcher")
if _LAUNCHER_DIR not in sys.path:
    sys.path.insert(0, _LAUNCHER_DIR)

import firmware  # noqa: E402


@pytest.fixture()
def temp_firmware(tmp_path, monkeypatch):
    """Point `launcher/firmware.py`'s manifest/firmware-dir globals at a scratch directory.

    Shared by `test_firmware_fetch.py` and `test_firmware_select.py`.
    """
    firmware_dir = tmp_path / "firmware"
    firmware_dir.mkdir()
    monkeypatch.setattr(firmware, "FIRMWARE_DIR", firmware_dir)
    monkeypatch.setattr(firmware, "MANIFEST_PATH", firmware_dir / "firmware.toml")
    return firmware_dir
