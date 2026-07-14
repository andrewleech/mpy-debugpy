"""Unit tests for `launcher/firmware.py` fetch/verify against local HTTP stub.

These tests drive happy-path download + sha256 verify; corrupted payload
detection; and clean error messages for network failures. The stub HTTP server
runs on localhost port 0 (OS-assigned ephemeral port).
"""

import hashlib
import http.server
import io
import socket
import sys
import threading
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Thread
from urllib.error import URLError

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


@contextmanager
def ephemeral_http_server(content):
    """Start a local HTTP server on an OS-assigned port serving fixed content.

    Yields (url, server_thread). The server is running until the context exits.
    """
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format, *args):
            # Suppress server log output during tests
            pass

    server = http.server.HTTPServer(("localhost", 0), _Handler)
    addr, port = server.server_address
    url = f"http://{addr}:{port}/firmware.bin"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield url, server
    finally:
        server.shutdown()


@pytest.fixture()
def temp_firmware(tmp_path, monkeypatch):
    """Point the module's manifest/firmware-dir globals at a scratch directory."""
    firmware_dir = tmp_path / "firmware"
    firmware_dir.mkdir()
    monkeypatch.setattr(firmware, "FIRMWARE_DIR", firmware_dir)
    monkeypatch.setattr(firmware, "MANIFEST_PATH", firmware_dir / "firmware.toml")
    return firmware_dir


def _run_fetch(args):
    """Run cmd_fetch and return (return_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = firmware.main(["fetch", *args])
    return rc, out.getvalue(), err.getvalue()


# --- Happy-path: download + verify -----------------------------------------------


def test_fetch_happy_path_downloads_and_verifies(temp_firmware, monkeypatch):
    """Fetch a real artifact over HTTP, verify sha256 matches manifest."""
    content = b"this is the firmware binary payload"
    sha256 = hashlib.sha256(content).hexdigest()

    with ephemeral_http_server(content) as (url, _):
        variants = [_variant("unix-standard-debug", url=url, sha256=sha256, settrace=True)]
        monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

        rc, out, err = _run_fetch(["unix-standard-debug"])

    assert rc == 0, err
    assert "OK" in out or "fetched and verified" in out
    artifact_path = temp_firmware / "unix-standard-debug" / "micropython"
    assert artifact_path.read_bytes() == content


# --- Corrupted payload detection -------------------------------------------------


def test_fetch_detects_corrupted_download(temp_firmware, monkeypatch):
    """Download a file whose sha256 doesn't match manifest; expect both hashes printed."""
    content = b"original firmware"
    sha256_orig = hashlib.sha256(content).hexdigest()

    # Flip the first byte to corrupt
    corrupted = bytearray(content)
    corrupted[0] ^= 0xFF
    corrupted = bytes(corrupted)
    sha256_corrupted = hashlib.sha256(corrupted).hexdigest()

    with ephemeral_http_server(corrupted) as (url, _):
        variants = [
            _variant("unix-standard-debug", url=url, sha256=sha256_orig, settrace=True)
        ]
        monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

        rc, out, err = _run_fetch(["unix-standard-debug"])

    assert rc != 0
    assert "FAIL" in err or "error" in err or "sha256" in err.lower()
    assert sha256_orig in err, f"expected hash not printed; got: {err}"
    assert sha256_corrupted in err, f"actual hash not printed; got: {err}"

    # File should be removed after failed verify
    artifact_path = temp_firmware / "unix-standard-debug" / "micropython"
    assert not artifact_path.exists(), "corrupted artifact should be removed"


# --- Network error handling ------------------------------------------------------


def test_fetch_connection_refused_is_clean_error(temp_firmware, monkeypatch):
    """Connection refused should print a clean error line, not a traceback."""
    # Use a port that will definitely be refused
    refused_url = "http://localhost:1/firmware.bin"
    sha256 = "0" * 64

    variants = [_variant("unix-standard-debug", url=refused_url, sha256=sha256)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    rc, out, err = _run_fetch(["unix-standard-debug"])

    assert rc != 0
    # Should have a clean error message, not a full traceback
    assert "error" in err.lower(), f"expected 'error' in stderr, got: {err}"
    # Should NOT contain Python traceback artifacts
    assert "Traceback" not in err, f"should not have traceback, got: {err}"
    assert "File \"" not in err, f"should not have file/line traceback, got: {err}"


# --- Re-verification of existing artifacts (select path) -------------------------


def test_select_re_verifies_existing_artifact(temp_firmware, monkeypatch):
    """When select finds a local artifact, it should re-verify its sha256."""
    artifact_path = temp_firmware / "unix-standard-debug" / "micropython"
    artifact_path.parent.mkdir(parents=True)

    content = b"valid firmware"
    artifact_path.write_bytes(content)
    sha256 = hashlib.sha256(content).hexdigest()

    variants = [_variant("unix-standard-debug", sha256=sha256, settrace=True)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = firmware.main(["select", "--need", "settrace"])

    assert rc == 0, err.getvalue()
    assert str(artifact_path) in out.getvalue()


def test_select_rejects_corrupted_cached_artifact(temp_firmware, monkeypatch):
    """If a cached artifact's sha256 no longer matches, select should reject it."""
    artifact_path = temp_firmware / "unix-standard-debug" / "micropython"
    artifact_path.parent.mkdir(parents=True)

    # Write one content, but record a different sha256 in the manifest
    original = b"original firmware"
    artifact_path.write_bytes(original)
    original_sha256 = hashlib.sha256(original).hexdigest()

    # Now claim a different sha256 in the manifest (simulating artifact corruption)
    fake_sha256 = "deadbeef" * 8

    variants = [_variant("unix-standard-debug", sha256=fake_sha256, settrace=True)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = firmware.main(["select", "--need", "settrace"])

    assert rc != 0
    stderr_text = err.getvalue()
    assert "error" in stderr_text.lower() or "sha256" in stderr_text.lower(), f"got: {stderr_text}"
    assert original_sha256 in stderr_text, f"expected actual hash in error: {stderr_text}"
    assert fake_sha256 in stderr_text, f"expected expected hash in error: {stderr_text}"
