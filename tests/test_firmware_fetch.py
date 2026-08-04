"""Unit tests for `launcher/firmware.py` fetch/verify against local HTTP stub.

These tests drive happy-path download + sha256 verify; corrupted payload
detection; and clean error messages for network failures. The stub HTTP server
runs on localhost port 0 (OS-assigned ephemeral port).
"""

import hashlib
import http.server
import io
import os
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Thread

import pytest

_LAUNCHER_DIR = str(Path(__file__).resolve().parents[1] / "launcher")
if _LAUNCHER_DIR not in sys.path:
    sys.path.insert(0, _LAUNCHER_DIR)

from helpers import firmware_variant as _variant

import firmware


@contextmanager
def ephemeral_truncating_http_server(sent_content, declared_length):
    """Serve `sent_content` under a Content-Length larger than what is sent.

    Reproduces a connection that drops mid-download: the client's `.read()`
    hits EOF before the promised byte count, raising `http.client.IncompleteRead`.
    """

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(declared_length))
            self.end_headers()
            self.wfile.write(sent_content)

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("localhost", 0), _Handler)
    addr, port = server.server_address
    url = f"http://{addr}:{port}/firmware.bin"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield url
    finally:
        server.shutdown()


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


def test_select_connection_refused_is_clean_error(temp_firmware, monkeypatch):
    """`select`'s fetch path (via _resolve_artifact) must also avoid a raw traceback."""
    refused_url = "http://localhost:1/firmware.bin"

    variants = [_variant("unix-standard-debug", url=refused_url, sha256="0" * 64, settrace=True)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = firmware.main(["select", "--need", "settrace"])

    assert rc != 0
    stderr_text = err.getvalue()
    assert "error" in stderr_text.lower(), f"expected 'error' in stderr, got: {stderr_text}"
    assert "Traceback" not in stderr_text, f"should not have traceback, got: {stderr_text}"
    assert "File \"" not in stderr_text, f"should not have file/line traceback, got: {stderr_text}"


# --- Truncated download (mid-stream connection drop) -----------------------------


def test_fetch_truncated_download_is_clean_error(temp_firmware, monkeypatch):
    """A response that ends before its declared Content-Length raises
    `http.client.IncompleteRead`; that must produce a clean error, not a traceback."""
    sent = b"partial-firmware-bytes"

    with ephemeral_truncating_http_server(sent, declared_length=len(sent) + 500) as url:
        variants = [_variant("unix-standard-debug", url=url, sha256="0" * 64)]
        monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

        rc, out, err = _run_fetch(["unix-standard-debug"])

    assert rc != 0
    assert "error" in err.lower(), f"expected 'error' in stderr, got: {err}"
    assert "Traceback" not in err, f"should not have traceback, got: {err}"
    assert "File \"" not in err, f"should not have file/line traceback, got: {err}"

    artifact_path = temp_firmware / "unix-standard-debug" / "micropython"
    assert not artifact_path.exists(), "truncated download should not leave a partial artifact behind"


def test_fetch_truncated_download_preserves_existing_valid_artifact(temp_firmware, monkeypatch):
    """A truncated re-fetch (--force) must not destroy the previously-valid artifact at that path."""
    good_content = b"previously-valid-firmware"
    good_sha256 = hashlib.sha256(good_content).hexdigest()

    artifact_path = temp_firmware / "unix-standard-debug" / "micropython"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(good_content)

    sent = b"short"
    with ephemeral_truncating_http_server(sent, declared_length=len(sent) + 500) as url:
        variants = [_variant("unix-standard-debug", url=url, sha256=good_sha256)]
        monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

        rc, out, err = _run_fetch(["unix-standard-debug", "--force"])

    assert rc != 0
    assert artifact_path.exists(), "the previously-valid artifact must survive a failed re-fetch"
    assert artifact_path.read_bytes() == good_content


# --- Fetched executable variants are runnable ------------------------------------


def test_fetch_sets_executable_bit_for_unix_variant(temp_firmware, monkeypatch):
    """GitHub Release assets carry no execute bit; a fetched unix artifact must be runnable."""
    content = b"#!/bin/sh\necho fake-micropython\n"
    sha256 = hashlib.sha256(content).hexdigest()

    with ephemeral_http_server(content) as (url, _):
        variants = [_variant("unix-standard-debug", port="unix", url=url, sha256=sha256, settrace=True)]
        monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

        rc, out, err = _run_fetch(["unix-standard-debug"])

    assert rc == 0, err
    artifact_path = temp_firmware / "unix-standard-debug" / "micropython"
    assert os.access(artifact_path, os.X_OK), "fetched unix artifact must be executable"


def test_fetch_repairs_executable_bit_on_already_present_artifact(temp_firmware, monkeypatch):
    """A hash-valid unix artifact left over from a hand-placed download (or a
    pre-fix cache) may not carry the execute bit; the already-present-and-
    verified branch must repair it, not just report success."""
    content = b"#!/bin/sh\necho fake-micropython\n"
    sha256 = hashlib.sha256(content).hexdigest()

    artifact_path = temp_firmware / "unix-standard-debug" / "micropython"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(content)
    artifact_path.chmod(0o644)

    variants = [_variant("unix-standard-debug", port="unix", sha256=sha256, settrace=True)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    rc, out, err = _run_fetch(["unix-standard-debug"])

    assert rc == 0, err
    assert "already present and verified" in out
    assert os.access(artifact_path, os.X_OK), "already-present unix artifact must be executable"


# --- Artifact path escape guard ---------------------------------------------------


def test_fetch_rejects_absolute_artifact_path(temp_firmware, monkeypatch):
    """A manifest entry whose `artifact` escapes FIRMWARE_DIR must not be honoured,
    even if it slipped past gen_manifest.py --check (hand-edited manifest)."""
    variant = _variant("unix-standard-debug", sha256="0" * 64)
    variant["artifact"] = "/etc/passwd"
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: [variant])

    with pytest.raises(SystemExit, match="unsafe artifact path"):
        firmware.main(["fetch", "unix-standard-debug"])


# --- verify with no args: deprecated/unfetched entries are SKIP, not FAIL --


def _run_verify(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = firmware.main(["verify", *args])
    return rc, out.getvalue(), err.getvalue()


def test_verify_no_args_skips_deprecated_entries(monkeypatch):
    variants = [_variant("legacy", deprecated=True, sha256="0" * 64)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    rc, out, _ = _run_verify([])
    assert rc == 0, out
    assert "SKIP legacy" in out


def test_verify_no_args_skips_unfetched_artifact(temp_firmware, monkeypatch):
    variants = [_variant("unix-standard-debug", sha256="0" * 64)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    rc, out, _ = _run_verify([])
    assert rc == 0, out
    assert "SKIP unix-standard-debug" in out


def test_verify_explicit_id_still_fails_on_missing_artifact(temp_firmware, monkeypatch):
    """Naming a variant explicitly is a request to check exactly that one;
    it not being present is still a real failure, not a SKIP."""
    variants = [_variant("unix-standard-debug", sha256="0" * 64)]
    monkeypatch.setattr(firmware, "load_manifest", lambda *a, **k: variants)

    rc, out, _ = _run_verify(["unix-standard-debug"])
    assert rc != 0
    assert "FAIL unix-standard-debug" in out


# --- load_manifest resolves MANIFEST_PATH at call time ---------------------


def test_load_manifest_with_no_args_honours_monkeypatched_manifest_path(temp_firmware):
    """`load_manifest()`'s every real caller passes no argument, so a stale
    module-load-time default would make the `temp_firmware` fixture's
    MANIFEST_PATH monkeypatch inert; it must resolve at call time instead."""
    firmware.MANIFEST_PATH.write_text(
        '[[variant]]\nid = "x"\nartifact = "x/fw"\nartifact_sha256 = "0"\n'
    )
    variants = firmware.load_manifest()
    assert [v["id"] for v in variants] == ["x"]
