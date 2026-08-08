"""
Tests for the debugpy installer (STORY-4.1).

Covers cross-compilation caching, hash-verified transfers, cache-hit detection,
and resilience to mid-install interruption.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

import pytest

import mpremote_debug

# Mirrors mpremote.transport.listdir_result's (name, st_mode) fields, which is
# all _sweep_device_dir reads.
_FakeDirEntry = namedtuple("_FakeDirEntry", ["name", "st_mode"])

# Import the module under test from the micropython submodule.
# Add the micropython tools path to sys.path to import debugpy_install.
_MPREMOTE_PATH = mpremote_debug.SUBMODULE_DIR
if str(_MPREMOTE_PATH) not in sys.path:
    sys.path.insert(0, str(_MPREMOTE_PATH))

# Skip the whole file, rather than fail collection, if the submodule pin
# doesn't yet carry debugpy_install.py (STORY-4.1 is parked on a feature
# branch not composed into mpy-debugpy).
pytest.importorskip("mpremote.debugpy_install", reason="mpremote_debugpy_install not composed")

from mpremote.commands import CommandError
from mpremote.debugpy_install import (
    _cache_key,
    _find_mpy_cross,
    _mpy_cross_version,
    _prune_cache,
    _read_marker,
    _source_files,
    ensure_debugpy_installed,
)
from mpremote.transport import TransportError

# The package structure installed by every test: mirrors the real
# micropython-lib/python-ecosys/debugpy/debugpy layout without depending on
# the checked-out submodule content, so tests are stable across debugpy edits.
_PACKAGE_STRUCTURE = {
    "__init__.py": b"# debugpy package\n__version__ = '1.0.0'\n",
    "public_api.py": b"def public_func():\n    pass\n",
    "common/__init__.py": b"# common module\n",
    "common/constants.py": b"CONSTANT_1 = 1\nCONSTANT_2 = 2\n",
    "common/messaging.py": b"def send_message():\n    pass\n",
    "server/__init__.py": b"# server module\n",
    "server/debug_session.py": b"class DebugSession:\n    pass\n",
    "server/pdb_adapter.py": b"def adapt_pdb():\n    pass\n",
}
_EXPECTED_MPYS = len(_PACKAGE_STRUCTURE)

_REPO_ROOT = Path(__file__).parent.parent
_REAL_MPY_CROSS = _REPO_ROOT / "micropython" / "mpy-cross" / "build" / "mpy-cross"
_REAL_UNIX_MICROPYTHON = _REPO_ROOT / "micropython" / "ports" / "unix" / "build-standard" / "micropython"
_REAL_DEBUGPY_PACKAGE = _REPO_ROOT / "micropython-lib" / "python-ecosys" / "debugpy" / "debugpy"


class FakeTransport:
    """A fake transport that records fs_writefile, fs_readfile and fs_rmfile calls.

    Starts with only "/" existing, like a wiped device with no /lib: a write or
    mkdir under a directory that was never created via fs_mkdir raises, the same
    as the real transport's `open(path, 'wb')` with no parent directory.
    """

    def __init__(self, mpy_version=6, sys_path=("", ".frozen", "/lib")):
        self.files = {}  # path -> bytes
        self.directories = {"/"}  # existing directories
        self.write_calls = []  # (path, data, verify_hash)
        self.read_calls = []  # path
        self.rm_calls = []  # path
        self.exec_calls = []  # code strings passed to exec()
        self.simulate_hash_mismatch_paths = set()  # paths where hash verify should fail
        self.mpy_version = mpy_version  # sys.implementation._mpy & 0xFF on this "device"
        self.sys_path = list(sys_path)  # sys.path on this "device"

    def _parent(self, path):
        return path.rsplit("/", 1)[0] or "/"

    def fs_readfile(self, path):
        """Simulate reading a file from the device."""
        self.read_calls.append(path)
        if path not in self.files:
            raise OSError(f"File not found: {path}")
        return self.files[path]

    def fs_writefile(self, path, data, chunk_size=256, progress_callback=None, verify_hash=False):
        """Simulate writing a file to the device with optional hash verification."""
        if not self.fs_exists(self._parent(path)):
            raise FileNotFoundError(2, "No such file or directory", path)
        if verify_hash and path in self.simulate_hash_mismatch_paths:
            # Same exception type and message shape as the real transport
            # (transport.py: TransportError("file transfer verification
            # failed for '%s'" % dest)), so tests exercise the real failure
            # mode rather than a stand-in that production code never raises.
            raise TransportError(f"file transfer verification failed for '{path}'")
        self.write_calls.append((path, data, verify_hash))
        self.files[path] = data

    def fs_mkdir(self, path):
        """Simulate creating a directory (one level, like the real os.mkdir)."""
        if not self.fs_exists(self._parent(path)):
            raise FileNotFoundError(2, "No such file or directory", path)
        self.directories.add(path)

    def fs_ensure_path_exists(self, path):
        """Simulate Transport.fs_ensure_path_exists (mkdir -p of dirname(path))."""
        split = path.split("/")
        if not split[0]:
            split.pop(0)
            split[0] = "/" + split[0]
        prefix = ""
        for i in range(len(split) - 1):
            prefix += split[i]
            if not self.fs_exists(prefix):
                self.fs_mkdir(prefix)
            prefix += "/"

    def fs_exists(self, path):
        """Simulate checking if a file or directory exists."""
        return path == "/" or path in self.files or path in self.directories

    def fs_rmfile(self, path):
        """Simulate removing a file from the device."""
        self.rm_calls.append(path)
        if path not in self.files:
            raise OSError(f"File not found: {path}")
        del self.files[path]

    def fs_listdir(self, path=""):
        """Simulate listing a directory's direct children, with the
        directory bit (0x4000) set the same way the real transport's
        st_mode does - the subset _sweep_device_dir reads."""
        path = path or "/"
        if path != "/" and path not in self.directories:
            raise OSError(f"Directory not found: {path}")
        prefix = path.rstrip("/") + "/"
        is_dir = {}
        for p in self.files:
            if p.startswith(prefix):
                rest = p[len(prefix) :]
                if rest:
                    head, _, tail = rest.partition("/")
                    is_dir[head] = is_dir.get(head, False) or bool(tail)
        for d in self.directories:
            if d != "/" and d.startswith(prefix):
                rest = d[len(prefix) :]
                if rest:
                    is_dir[rest.split("/", 1)[0]] = True
        return [_FakeDirEntry(name, 0x4000 if dir_ else 0) for name, dir_ in is_dir.items()]

    def fs_hashfile(self, path, algo, chunk_size=256):
        """Simulate device-side hashing (mirrors the real transport's
        fs_hashfile, which hashes over the device connection rather than
        pulling the file back to the host)."""
        if path not in self.files:
            raise OSError(f"File not found: {path}")
        return getattr(hashlib, algo)(self.files[path]).digest()

    def exec(self, code, data_consumer=None):
        """Simulate running code on the device; only tracked, never interpreted."""
        self.exec_calls.append(code)

    def eval(self, expr):
        """Simulate evaluating an expression on the device.

        Only the two probes the installer issues are supported: the mpy
        version and sys.path.
        """
        if "_mpy" in expr:
            return self.mpy_version
        if expr == "sys.path":
            return list(self.sys_path)
        raise NotImplementedError(f"FakeTransport.eval does not support: {expr!r}")


@pytest.fixture
def temp_package_dir():
    """Create a temporary package directory with test .py files."""
    tmpdir = tempfile.mkdtemp(prefix="debugpy_pkg_")

    for relpath, content in _PACKAGE_STRUCTURE.items():
        full_path = os.path.join(tmpdir, relpath)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(content)

    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def temp_cache_dir():
    """Create a temporary cache directory."""
    tmpdir = tempfile.mkdtemp(prefix="debugpy_cache_")
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


def _make_mock_mpy_cross(tmp_path, name, mpy_version="6.3"):
    """Create a mock mpy-cross executable that outputs a fake version string.

    The "compiled" output embeds the source path and its content, so a test
    that swaps one module's bytes onto another module's device path is
    detectable instead of every module compiling to identical bytes. The
    --version output reports `mpy_version`, matching the real mpy-cross
    "emitting mpy vN.M" format the installer parses.
    """
    exe = tmp_path / name
    exe.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        f'  echo "MicroPython v1.23.0 (test) on 2026-01-01; mpy-cross emitting mpy v{mpy_version}"\n'
        "else\n"
        '  out=""\n'
        '  src=""\n'
        "  output_next=0\n"
        '  for arg in "$@"; do\n'
        '    if [ "$output_next" = "1" ]; then\n'
        '      out="$arg"\n'
        "      output_next=0\n"
        '    elif [ "$arg" = "-o" ]; then\n'
        "      output_next=1\n"
        "    fi\n"
        '    src="$arg"\n'
        "  done\n"
        '  printf "mpy:%s\\n" "$src" > "$out"\n'
        '  cat "$src" >> "$out"\n'
        "fi\n"
    )
    exe.chmod(0o755)
    return str(exe)


@pytest.fixture
def mock_mpy_cross(tmp_path):
    """A mock mpy-cross reporting .mpy v6, matching FakeTransport's default."""
    return _make_mock_mpy_cross(tmp_path, "mpy-cross")


class TestCriterion1_FirstRun:
    """Criterion 1: First run against empty device installs all modules.

    FakeTransport starts with only "/" existing (see its docstring), so every
    test in this module already exercises installing onto a device with no
    pre-existing /lib; this class additionally checks the write set directly.
    """

    def test_first_install_all_modules(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """Verify all modules are installed on first run against a wiped device."""
        transport = FakeTransport()
        assert transport.directories == {"/"}  # no /lib yet

        result = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            cache_dir=temp_cache_dir,
        )

        # Should return True (something was installed).
        assert result is True

        # Should have written all .mpy files.
        write_calls = [c for c in transport.write_calls if c[0].startswith("/lib/debugpy/")]
        assert len(write_calls) == _EXPECTED_MPYS, (
            f"Expected {_EXPECTED_MPYS} mpy files, got {len(write_calls)}"
        )

        # All should have verify_hash=True.
        for path, data, verify_hash in write_calls:
            assert verify_hash is True

        # The marker is the very last write overall, after every module file.
        marker_writes = [
            c for c in transport.write_calls if c[0] == "/lib/.debugpy-install.json" and c[2] is True
        ]
        assert len(marker_writes) == 1
        assert transport.write_calls.index(marker_writes[0]) == len(transport.write_calls) - 1

        # Verify marker content.
        marker_data = json.loads(marker_writes[0][1])
        assert "key" in marker_data
        assert "device_dir" in marker_data
        assert "files" in marker_data
        assert len(marker_data["files"]) == _EXPECTED_MPYS

    def test_first_install_creates_lib_directory(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """A device with no /lib gets one created, rather than failing the
        marker write with FileNotFoundError (`Transport.fs_ensure_path_exists`
        is required for exactly this reason)."""
        transport = FakeTransport()

        ensure_debugpy_installed(
            transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
        )

        assert "/lib" in transport.directories
        assert "/lib/debugpy" in transport.directories
        assert "/lib/.debugpy-install.json" in transport.files

    def test_marker_manifest_matches_written_files(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """The installed marker's file set matches what got written to the
        device (no interpreter runs here; see TestIntegration_RealUnixFirmware
        below for the actual "import debugpy succeeds on target" check)."""
        transport = FakeTransport()
        ensure_debugpy_installed(
            transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
        )
        marker = _read_marker(transport, "/lib/.debugpy-install.json")
        assert set(marker["files"]) == {
            path for path, _, _ in transport.write_calls if path != "/lib/.debugpy-install.json"
        }
        assert "/lib/debugpy/__init__.mpy" in marker["files"]


class TestCriterion2_CacheHit:
    """Criterion 2: Second run with unchanged sources performs no fs_writefile calls."""

    def test_cache_hit_no_transfers(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """Verify unchanged sources result in zero transfers."""
        transport = FakeTransport()

        result1 = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            cache_dir=temp_cache_dir,
        )
        assert result1 is True

        transport.write_calls = []

        # Second install with same sources.
        result2 = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            cache_dir=temp_cache_dir,
        )

        # Should return False (nothing was installed).
        assert result2 is False

        # Should have zero fs_writefile calls.
        assert len(transport.write_calls) == 0

    def test_cache_hit_rejected_when_installed_files_missing(
        self, temp_package_dir, temp_cache_dir, mock_mpy_cross
    ):
        """A marker whose key matches but whose files were deleted (e.g. the
        device was reflashed and only /lib/debugpy/* was wiped) must not be
        trusted; the package must be reinstalled."""
        transport = FakeTransport()
        assert (
            ensure_debugpy_installed(
                transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
            )
            is True
        )

        for path in list(transport.files):
            if path.startswith("/lib/debugpy/"):
                del transport.files[path]
        transport.write_calls = []

        result = ensure_debugpy_installed(
            transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
        )

        assert result is True, "a marker with no installed files left must not be a cache hit"
        assert any(c[0].startswith("/lib/debugpy/") for c in transport.write_calls)

    def test_cache_hit_rejected_when_installed_file_corrupted(
        self, temp_package_dir, temp_cache_dir, mock_mpy_cross
    ):
        """A device file whose content no longer matches its recorded marker
        hash (bad write, brownout, filesystem repair) must not be trusted
        merely because the path still exists and the key still matches."""
        transport = FakeTransport()
        assert (
            ensure_debugpy_installed(
                transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
            )
            is True
        )

        transport.files["/lib/debugpy/server/pdb_adapter.mpy"] = b"corrupted"
        transport.write_calls = []

        result = ensure_debugpy_installed(
            transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
        )

        assert result is True, "a device file diverging from its recorded hash must not be trusted"
        assert any(c[0].startswith("/lib/debugpy/") for c in transport.write_calls)


class TestCriterion3_SingleFileChange:
    """Criterion 3: Touching one source file reinstalls that module and updates marker."""

    def test_single_file_change_reinstall(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """Verify touching a single file triggers reinstall of that module."""
        transport = FakeTransport()

        result1 = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            cache_dir=temp_cache_dir,
        )
        assert result1 is True
        marker1 = _read_marker(transport, "/lib/.debugpy-install.json")

        # Modify one source file.
        public_api_path = os.path.join(temp_package_dir, "public_api.py")
        with open(public_api_path, "ab") as f:
            f.write(b"\n# Modified\n")

        transport.write_calls = []

        # Second install with modified source.
        result2 = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            cache_dir=temp_cache_dir,
        )

        # Should return True (something was installed).
        assert result2 is True

        # Should have written new .mpy files and a new marker.
        mpy_writes = [c for c in transport.write_calls if c[0].startswith("/lib/debugpy/")]
        marker_writes = [
            c for c in transport.write_calls if c[0] == "/lib/.debugpy-install.json" and c[2] is True
        ]

        # The changed module itself must be among the writes, with new bytes.
        changed = {path: data for path, data, _ in mpy_writes}
        assert "/lib/debugpy/public_api.mpy" in changed
        assert (
            hashlib.sha256(changed["/lib/debugpy/public_api.mpy"]).hexdigest()
            != marker1["files"]["/lib/debugpy/public_api.mpy"]
        )
        assert len(marker_writes) == 1, "Marker should be updated"

        # New marker should have a different key.
        marker2 = json.loads(marker_writes[0][1])
        assert marker2["key"] != marker1["key"]


class TestCriterion4_FlagsChange:
    """Criterion 4: Changing mpy-cross version or flags forces full reinstall."""

    def test_flags_change_full_reinstall(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """Verify changing flags forces a full reinstall."""
        transport = FakeTransport()

        # First install with -O2.
        result1 = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            mpy_cross_flags=("-O2",),
            cache_dir=temp_cache_dir,
        )
        assert result1 is True
        marker1 = _read_marker(transport, "/lib/.debugpy-install.json")

        transport.write_calls = []

        # Second install with different flags (-O3).
        result2 = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            mpy_cross_flags=("-O3",),
            cache_dir=temp_cache_dir,
        )

        # Should return True (something was installed).
        assert result2 is True

        # Should have written all .mpy files again.
        mpy_writes = [c for c in transport.write_calls if c[0].startswith("/lib/debugpy/")]
        assert len(mpy_writes) == _EXPECTED_MPYS, f"Expected full reinstall with {_EXPECTED_MPYS} files"

        # New marker should have a different key.
        marker_writes = [
            c for c in transport.write_calls if c[0] == "/lib/.debugpy-install.json" and c[2] is True
        ]
        assert len(marker_writes) == 1
        marker2 = json.loads(marker_writes[0][1])
        assert marker2["key"] != marker1["key"]

    def test_mpy_cross_version_change_full_reinstall(
        self, temp_package_dir, temp_cache_dir, mock_mpy_cross, tmp_path
    ):
        """Verify a different mpy-cross build forces a full reinstall, even
        with the same flags and the same major .mpy version (so the
        device-version gate still passes)."""
        transport = FakeTransport()

        result1 = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            cache_dir=temp_cache_dir,
        )
        assert result1 is True
        marker1 = _read_marker(transport, "/lib/.debugpy-install.json")

        transport.write_calls = []

        other_mpy_cross = _make_mock_mpy_cross(tmp_path, "mpy-cross-other", mpy_version="6.9")
        result2 = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=other_mpy_cross,
            cache_dir=temp_cache_dir,
        )
        assert result2 is True

        mpy_writes = [c for c in transport.write_calls if c[0].startswith("/lib/debugpy/")]
        assert len(mpy_writes) == _EXPECTED_MPYS, f"Expected full reinstall with {_EXPECTED_MPYS} files"

        marker_writes = [
            c for c in transport.write_calls if c[0] == "/lib/.debugpy-install.json" and c[2] is True
        ]
        assert len(marker_writes) == 1
        marker2 = json.loads(marker_writes[0][1])
        assert marker2["key"] != marker1["key"]


class TestCriterion5_HashMismatch:
    """Criterion 5: Hash mismatch fails loudly and leaves no valid marker."""

    def test_hash_mismatch_fails_and_invalidates_marker(
        self, temp_package_dir, temp_cache_dir, mock_mpy_cross
    ):
        """Verify hash mismatch raises and the on-device marker is left invalid."""
        transport = FakeTransport()

        result1 = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            cache_dir=temp_cache_dir,
        )
        assert result1 is True
        marker1 = _read_marker(transport, "/lib/.debugpy-install.json")

        # Modify one source to trigger a reinstall.
        public_api_path = os.path.join(temp_package_dir, "public_api.py")
        with open(public_api_path, "ab") as f:
            f.write(b"\n# Corruption test\n")

        # Simulate a hash mismatch on one of the .mpy files during the second install.
        transport.write_calls = []
        transport.simulate_hash_mismatch_paths.add("/lib/debugpy/server/pdb_adapter.mpy")

        # Second install should fail due to hash mismatch, raising the same
        # exception type the real transport raises.
        with pytest.raises(TransportError, match="file transfer verification failed"):
            ensure_debugpy_installed(
                transport,
                temp_package_dir,
                mpy_cross=mock_mpy_cross,
                cache_dir=temp_cache_dir,
            )

        # The marker must not read back as valid: either it is untouched (if
        # the failure happened before the pre-install invalidation) or it was
        # overwritten with content that can never match a real key. Either
        # way, a subsequent call must not treat this device as up to date.
        marker_after_failure = _read_marker(transport, "/lib/.debugpy-install.json")
        assert marker_after_failure is None or marker_after_failure.get("key") != marker1["key"]

        transport.simulate_hash_mismatch_paths.clear()
        transport.write_calls = []
        result2 = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            cache_dir=temp_cache_dir,
        )
        assert result2 is True, "a device left by a failed install must be reinstalled"


class TestCriterion6_InterruptedInstall:
    """Criterion 6: an interrupted install must never be mistaken for success."""

    def test_missing_marker_forces_reinstall(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """An absent marker (e.g. a device wiped mid-install) is detected and retried."""
        transport = FakeTransport()

        result1 = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            cache_dir=temp_cache_dir,
        )
        assert result1 is True

        del transport.files["/lib/.debugpy-install.json"]
        transport.write_calls = []

        result2 = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            cache_dir=temp_cache_dir,
        )

        assert result2 is True
        marker_writes = [
            c for c in transport.write_calls if c[0] == "/lib/.debugpy-install.json" and c[2] is True
        ]
        assert len(marker_writes) == 1
        assert "/lib/.debugpy-install.json" in transport.files

    def test_interrupted_reinstall_survives_source_rollback(
        self, temp_package_dir, temp_cache_dir, mock_mpy_cross
    ):
        """A reinstall killed mid-transfer, followed by reverting the edited
        source back to its original content, must still reinstall rather than
        report the device up to date with zero writes.

        Reproduces: install V1 (marker K1); edit a source to V2; a reinstall
        writes some V2 modules then dies before the marker update; revert the
        source to V1 (recomputing K1); the next call must not see "on-device
        key == K1" and skip reinstalling the V2 leftovers still on disk.
        """
        transport = FakeTransport()

        assert (
            ensure_debugpy_installed(
                transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
            )
            is True
        )

        public_api_path = os.path.join(temp_package_dir, "public_api.py")
        original = Path(public_api_path).read_bytes()

        # Edit to V2, then fail partway through the reinstall.
        with open(public_api_path, "ab") as f:
            f.write(b"\n# V2\n")
        transport.simulate_hash_mismatch_paths.add("/lib/debugpy/server/pdb_adapter.mpy")
        with pytest.raises(TransportError):
            ensure_debugpy_installed(
                transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
            )
        transport.simulate_hash_mismatch_paths.clear()

        # Revert the source back to V1: the cache key is now identical to the
        # original install's key.
        Path(public_api_path).write_bytes(original)
        transport.write_calls = []

        result = ensure_debugpy_installed(
            transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
        )

        assert result is True, "a device left mid-install must be reinstalled, not trusted"
        assert len(transport.write_calls) > 0


class TestCriterion7_PruneRemovedModules:
    """Modules removed from the package must be removed from the device."""

    def test_removed_module_is_deleted_from_device(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        transport = FakeTransport()
        assert (
            ensure_debugpy_installed(
                transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
            )
            is True
        )
        assert "/lib/debugpy/public_api.mpy" in transport.files

        os.remove(os.path.join(temp_package_dir, "public_api.py"))
        transport.write_calls = []

        result = ensure_debugpy_installed(
            transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
        )

        assert result is True
        assert "/lib/debugpy/public_api.mpy" not in transport.files
        assert "/lib/debugpy/public_api.mpy" in transport.rm_calls

        marker = _read_marker(transport, "/lib/.debugpy-install.json")
        assert "/lib/debugpy/public_api.mpy" not in marker["files"]


class TestDeviceDirSweep:
    """device_dir is swept against what this run actually wrote, not just
    diffed against the previous marker's file list, so files the marker
    never knew about don't survive an install."""

    def test_stale_py_from_old_installer_is_removed(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """A device set up by the old mip-based installer has a plain
        __init__.py with no marker at all. MicroPython's importer prefers a
        .py file over a same-named .mpy, so leaving that file behind after
        installing __init__.mpy would make the stale source shadow the new
        compiled module forever."""
        transport = FakeTransport()
        transport.directories.update({"/lib", "/lib/debugpy"})
        transport.files["/lib/debugpy/__init__.py"] = b"# stale mip install\n"

        result = ensure_debugpy_installed(
            transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
        )

        assert result is True
        assert "/lib/debugpy/__init__.py" not in transport.files
        assert "/lib/debugpy/__init__.mpy" in transport.files

    def test_orphan_file_unknown_to_marker_is_removed(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """An orphan under device_dir that no marker ever recorded (e.g. left
        by a killed run whose module set was later renamed, or copied there
        by hand) is removed the next time a source change forces a
        reinstall, even though nothing in the marker's file list mentions it."""
        transport = FakeTransport()
        assert (
            ensure_debugpy_installed(
                transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
            )
            is True
        )

        transport.files["/lib/debugpy/orphan.mpy"] = b"leftover"

        # Force a reinstall so the sweep runs.
        with open(os.path.join(temp_package_dir, "public_api.py"), "ab") as f:
            f.write(b"\n# force reinstall\n")

        result = ensure_debugpy_installed(
            transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
        )

        assert result is True
        assert "/lib/debugpy/orphan.mpy" not in transport.files


class TestDeviceDirChange:
    """A different `device_dir` must not be silently treated as up to date."""

    def test_device_dir_change_triggers_install(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        transport = FakeTransport()
        assert (
            ensure_debugpy_installed(
                transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
            )
            is True
        )
        transport.write_calls = []

        result = ensure_debugpy_installed(
            transport,
            temp_package_dir,
            mpy_cross=mock_mpy_cross,
            cache_dir=temp_cache_dir,
            device_dir="/flash/debugpy",
        )

        assert result is True
        assert any(c[0].startswith("/flash/debugpy/") for c in transport.write_calls)


class TestMpyVersionProbe:
    """The target's .mpy version gates the install and is part of the cache key."""

    def test_no_mpy_support_raises(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """A target with no .mpy support (sys.implementation._mpy == 0) must
        be rejected before anything is compiled or written."""
        transport = FakeTransport(mpy_version=0)

        with pytest.raises(CommandError, match="no .mpy support"):
            ensure_debugpy_installed(
                transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
            )

        assert transport.write_calls == []

    def test_mismatched_mpy_version_raises(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """mpy-cross emits v6 (see mock_mpy_cross); a target reporting a
        different version must be rejected rather than sent incompatible
        bytecode (e.g. after flashing a different firmware build that
        preserves the VFS but changes MPY_VERSION)."""
        transport = FakeTransport(mpy_version=5)

        with pytest.raises(CommandError, match="mismatch|v5|v6"):
            ensure_debugpy_installed(
                transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
            )

        assert transport.write_calls == []

    def test_device_mpy_version_is_part_of_cache_key(self, temp_package_dir, mock_mpy_cross):
        """Two targets with identical sources/flags but different .mpy
        versions must not collide on the same cache key."""
        files = _source_files(temp_package_dir)
        version = _mpy_cross_version(mock_mpy_cross)
        flags = ("-O2",)

        key_v6 = _cache_key(temp_package_dir, files, version, flags, 6)
        key_v5 = _cache_key(temp_package_dir, files, version, flags, 5)

        assert key_v6 != key_v5


class RealFirmwareTransport:
    """A transport backed by a real directory tree, for installs that are then
    imported by the real unix-port MicroPython."""

    def __init__(self, device_root):
        self._device_root = device_root
        # Real target probe: whatever the real unix-port micropython reports
        # for its own .mpy version, matching what _REAL_MPY_CROSS (built from
        # the same tree) emits.
        proc = subprocess.run(
            [
                str(_REAL_UNIX_MICROPYTHON),
                "-c",
                "import sys; print(getattr(sys.implementation, '_mpy', 0) & 0xFF)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self._mpy_version = int(proc.stdout.strip())

    def _full_path(self, path):
        return os.path.join(self._device_root, path.lstrip("/"))

    def fs_readfile(self, path):
        full_path = self._full_path(path)
        if not os.path.exists(full_path):
            raise OSError(f"File not found: {path}")
        with open(full_path, "rb") as f:
            return f.read()

    def fs_writefile(self, path, data, chunk_size=256, progress_callback=None, verify_hash=False):
        full_path = self._full_path(path)
        # No auto-mkdir: like the real transport's open(path, 'wb'), a missing
        # parent directory is a hard failure, so callers must
        # fs_ensure_path_exists first.
        with open(full_path, "wb") as f:
            f.write(data)
        if verify_hash:
            with open(full_path, "rb") as f:
                if hashlib.sha256(f.read()).digest() != hashlib.sha256(data).digest():
                    raise TransportError(f"file transfer verification failed for '{path}'")

    def fs_mkdir(self, path):
        os.mkdir(self._full_path(path))

    def fs_ensure_path_exists(self, path):
        split = path.split("/")
        if not split[0]:
            split.pop(0)
            split[0] = "/" + split[0]
        prefix = ""
        for i in range(len(split) - 1):
            prefix += split[i]
            if not self.fs_exists(prefix):
                self.fs_mkdir(prefix)
            prefix += "/"

    def fs_exists(self, path):
        return os.path.exists(self._full_path(path))

    def fs_rmfile(self, path):
        os.remove(self._full_path(path))

    def fs_listdir(self, path=""):
        full_path = self._full_path(path or "/")
        if not os.path.isdir(full_path):
            raise OSError(f"Directory not found: {path}")
        return [
            _FakeDirEntry(name, 0x4000 if os.path.isdir(os.path.join(full_path, name)) else 0)
            for name in os.listdir(full_path)
        ]

    def fs_hashfile(self, path, algo, chunk_size=256):
        full_path = self._full_path(path)
        if not os.path.exists(full_path):
            raise OSError(f"File not found: {path}")
        with open(full_path, "rb") as f:
            return getattr(hashlib, algo)(f.read()).digest()

    def exec(self, code, data_consumer=None):
        pass

    def eval(self, expr):
        if "_mpy" in expr:
            return self._mpy_version
        if expr == "sys.path":
            return ["", ".frozen", "/lib"]
        raise NotImplementedError(f"RealFirmwareTransport.eval unsupported: {expr!r}")


def _run_micropython(lib_dir, script):
    return subprocess.run(
        [str(_REAL_UNIX_MICROPYTHON), "-c", script],
        env={**os.environ, "MICROPYPATH": lib_dir},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.skipif(
    not (_REAL_MPY_CROSS.is_file() and _REAL_UNIX_MICROPYTHON.is_file() and _REAL_DEBUGPY_PACKAGE.is_dir()),
    reason="real mpy-cross / unix-port micropython / debugpy package not built or checked out",
)
class TestIntegration_RealUnixFirmware:
    """End-to-end: cross-compile the real debugpy package with the real
    mpy-cross, install it into a real filesystem root, and `import debugpy`
    under the real unix-port MicroPython."""

    def test_real_firmware_import_debugpy(self, temp_cache_dir):
        with tempfile.TemporaryDirectory(prefix="device_root_") as device_root:
            transport = RealFirmwareTransport(device_root)

            result = ensure_debugpy_installed(
                transport,
                str(_REAL_DEBUGPY_PACKAGE),
                mpy_cross=str(_REAL_MPY_CROSS),
                cache_dir=temp_cache_dir,
            )
            assert result is True

            marker_path = os.path.join(device_root, "lib", ".debugpy-install.json")
            assert os.path.exists(marker_path)

            # A partially compiled tree (e.g. a build that silently dropped a
            # module) must fail here rather than pass the import check below.
            mpy_files = [
                f
                for _, _, files in os.walk(os.path.join(device_root, "lib", "debugpy"))
                for f in files
                if f.endswith(".mpy")
            ]
            assert len(mpy_files) == len(_source_files(str(_REAL_DEBUGPY_PACKAGE)))

            proc = _run_micropython(
                os.path.join(device_root, "lib"),
                # A bare `import debugpy` succeeds even with no __init__.mpy
                # present (MicroPython falls back to importing the directory
                # as a namespace package), so the check has to touch an
                # attribute that only exists once __init__.mpy has actually
                # run.
                "import debugpy; assert debugpy.DEFAULT_PORT and debugpy.listen; print('IMPORT_OK')",
            )
            assert proc.returncode == 0, proc.stderr
            assert "IMPORT_OK" in proc.stdout

    def test_capability_probe_survives_cross_compilation(self, temp_cache_dir):
        """An .mpy install must not turn `save_names` into a false negative.

        Local names live in the code object that declares them, so a probe
        reading its own frame measures whichever compiler produced debugpy.
        mpy-cross does not persist names (LOCALNAMES_PERSIST is off), so a
        self-frame probe reports False on any cross-compiled install however
        the firmware was built - and STORY-3.3's capability rule then rejects
        a manifest that was telling the truth. This unix build has LOCALNAMES
        on, so the probe must say so from an .mpy install too.
        """
        with tempfile.TemporaryDirectory(prefix="device_root_") as device_root:
            ensure_debugpy_installed(
                RealFirmwareTransport(device_root),
                str(_REAL_DEBUGPY_PACKAGE),
                mpy_cross=str(_REAL_MPY_CROSS),
                cache_dir=temp_cache_dir,
            )
            lib_dir = os.path.join(device_root, "lib")

            source = _run_micropython(
                lib_dir,
                "import sys; sys.path.remove('{}')\n".format(lib_dir)
                + "sys.path.insert(0, '{}')\n".format(_REAL_DEBUGPY_PACKAGE.parent)
                + "import debugpy; print(debugpy.get_capabilities())",
            )
            assert source.returncode == 0, source.stderr
            installed = _run_micropython(lib_dir, "import debugpy; print(debugpy.get_capabilities())")
            assert installed.returncode == 0, installed.stderr

            # Reading the source tree is the control: whatever it reports for
            # this firmware, the .mpy install has to report the same.
            assert "'save_names': True" in source.stdout, source.stdout
            assert "'save_names': True" in installed.stdout, installed.stdout


class TestSourceDiscovery:
    """Test source file discovery and sorting."""

    def test_source_files_discovery(self, temp_package_dir):
        """Verify _source_files discovers exactly the fixture's modules, sorted."""
        files = _source_files(temp_package_dir)

        expected = sorted(_PACKAGE_STRUCTURE.keys())
        assert files == expected

    def test_source_files_discovery_picks_up_new_files(self, temp_package_dir):
        """A newly added module is discovered without any code change."""
        before = _source_files(temp_package_dir)

        new_file = os.path.join(temp_package_dir, "server", "new_module.py")
        with open(new_file, "w") as f:
            f.write("# new module\n")

        after = _source_files(temp_package_dir)
        assert len(after) == len(before) + 1
        assert "server/new_module.py" in after

    def test_ninth_module_compiles_and_installs_end_to_end(
        self, temp_package_dir, temp_cache_dir, mock_mpy_cross
    ):
        """Acceptance: a 9th module needs no code change to be compiled,
        written, and recorded in the marker."""
        new_file = os.path.join(temp_package_dir, "server", "extra_feature.py")
        with open(new_file, "w") as f:
            f.write("def extra():\n    pass\n")

        transport = FakeTransport()
        result = ensure_debugpy_installed(
            transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
        )
        assert result is True

        mpy_writes = [c for c in transport.write_calls if c[0].startswith("/lib/debugpy/")]
        assert len(mpy_writes) == _EXPECTED_MPYS + 1
        assert "/lib/debugpy/server/extra_feature.mpy" in {c[0] for c in mpy_writes}

        marker = _read_marker(transport, "/lib/.debugpy-install.json")
        assert len(marker["files"]) == _EXPECTED_MPYS + 1


class TestCacheKeyUniqueness:
    """Test cache key computation and uniqueness."""

    def test_cache_key_changes_with_source(self, temp_package_dir, mock_mpy_cross):
        """Verify cache key changes when source content changes."""
        files1 = _source_files(temp_package_dir)
        version = _mpy_cross_version(mock_mpy_cross)
        flags = ("-O2",)

        key1 = _cache_key(temp_package_dir, files1, version, flags, 6)

        # Modify a source file.
        public_api_path = os.path.join(temp_package_dir, "public_api.py")
        with open(public_api_path, "ab") as f:
            f.write(b"\n# Added comment\n")

        files2 = _source_files(temp_package_dir)
        key2 = _cache_key(temp_package_dir, files2, version, flags, 6)

        # Keys should be different.
        assert key1 != key2

    def test_cache_key_changes_with_flags(self, temp_package_dir, mock_mpy_cross):
        """Verify cache key changes when compilation flags change."""
        files = _source_files(temp_package_dir)
        version = _mpy_cross_version(mock_mpy_cross)

        key1 = _cache_key(temp_package_dir, files, version, ("-O2",), 6)
        key2 = _cache_key(temp_package_dir, files, version, ("-O3",), 6)

        # Keys should be different.
        assert key1 != key2

    def test_cache_key_framing_is_unambiguous(self, tmp_path):
        """Distinct (content, name) sets must not hash identically merely
        because concatenation makes them look alike, e.g. name "ab.py" with
        content b"x" versus name "b.py" with content b"xa"."""
        version = "v1"
        flags = ("-O2",)

        dir1 = tmp_path / "pkg1"
        dir1.mkdir()
        (dir1 / "ab.py").write_bytes(b"x")

        dir2 = tmp_path / "pkg2"
        dir2.mkdir()
        (dir2 / "b.py").write_bytes(b"xa")

        key1 = _cache_key(str(dir1), _source_files(str(dir1)), version, flags, 6)
        key2 = _cache_key(str(dir2), _source_files(str(dir2)), version, flags, 6)

        assert key1 != key2


class TestMarkerReadWrite:
    """Test marker reading and writing."""

    def test_read_marker_absent(self):
        """Verify reading a non-existent marker returns None."""
        transport = FakeTransport()
        marker = _read_marker(transport, "/lib/.debugpy-install.json")
        assert marker is None

    def test_read_marker_invalid_json(self):
        """Verify reading a marker with invalid JSON returns None."""
        transport = FakeTransport()
        transport.files["/lib/.debugpy-install.json"] = b"{ invalid json"
        marker = _read_marker(transport, "/lib/.debugpy-install.json")
        assert marker is None

    def test_read_marker_valid(self):
        """Verify reading a valid marker returns its content."""
        transport = FakeTransport()
        expected = {"key": "abc123", "files": {}}
        transport.files["/lib/.debugpy-install.json"] = json.dumps(expected).encode()
        marker = _read_marker(transport, "/lib/.debugpy-install.json")
        assert marker == expected

    def test_read_marker_non_dict_json_degrades_to_none(self):
        """A marker file that parses as valid JSON but isn't an object (e.g.
        a bare `3`, hand-written or left by an unrelated tool at this path)
        must degrade to "no marker" rather than raising when the caller does
        `.get(...)` on it."""
        transport = FakeTransport()
        transport.files["/lib/.debugpy-install.json"] = b"3"
        marker = _read_marker(transport, "/lib/.debugpy-install.json")
        assert marker is None

        transport.files["/lib/.debugpy-install.json"] = b"[1]"
        assert _read_marker(transport, "/lib/.debugpy-install.json") is None


class TestFindMpyCross:
    """_find_mpy_cross precedence: an explicit MPY_CROSS override wins, then
    the packaged mpy_cross PyPI package, then PATH, then CommandError."""

    def test_env_var_wins_over_packaged_and_path(self, tmp_path, monkeypatch):
        env_exe = tmp_path / "env-mpy-cross"
        env_exe.write_text("")
        monkeypatch.setenv("MPY_CROSS", str(env_exe))
        monkeypatch.setattr("mpremote.debugpy_install._PACKAGED_MPY_CROSS", str(tmp_path / "packaged"))
        monkeypatch.setattr(shutil, "which", lambda name: str(tmp_path / "path-mpy-cross"))

        assert _find_mpy_cross() == str(env_exe)

    def test_env_var_set_but_missing_falls_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MPY_CROSS", str(tmp_path / "does-not-exist"))
        packaged = tmp_path / "packaged-mpy-cross"
        packaged.write_text("")
        monkeypatch.setattr("mpremote.debugpy_install._PACKAGED_MPY_CROSS", str(packaged))

        assert _find_mpy_cross() == str(packaged)

    def test_packaged_wins_over_path(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MPY_CROSS", raising=False)
        packaged = tmp_path / "packaged-mpy-cross"
        packaged.write_text("")
        monkeypatch.setattr("mpremote.debugpy_install._PACKAGED_MPY_CROSS", str(packaged))
        monkeypatch.setattr(shutil, "which", lambda name: str(tmp_path / "path-mpy-cross"))

        assert _find_mpy_cross() == str(packaged)

    def test_path_used_when_no_env_or_packaged(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MPY_CROSS", raising=False)
        monkeypatch.setattr("mpremote.debugpy_install._PACKAGED_MPY_CROSS", None)
        found = tmp_path / "path-mpy-cross"
        monkeypatch.setattr(shutil, "which", lambda name: str(found))

        assert _find_mpy_cross() == str(found)

    def test_raises_when_nothing_found(self, monkeypatch):
        monkeypatch.delenv("MPY_CROSS", raising=False)
        monkeypatch.setattr("mpremote.debugpy_install._PACKAGED_MPY_CROSS", None)
        monkeypatch.setattr(shutil, "which", lambda name: None)

        with pytest.raises(CommandError, match="mpy-cross not found"):
            _find_mpy_cross()


class TestFastPathIntegrity:
    """The fast path must not accept a device someone else has written to."""

    def test_shadowing_py_added_after_install_forces_reinstall(
        self, temp_package_dir, temp_cache_dir, mock_mpy_cross
    ):
        """A .py dropped in after a good install shadows our .mpy (the importer
        prefers .py), so the marker's own hashes cannot see it - the fast path
        has to notice the extra file and reinstall."""
        transport = FakeTransport()
        common = dict(package_dir=temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir)
        assert ensure_debugpy_installed(transport, **common) is True

        transport.files["/lib/debugpy/__init__.py"] = b"# shadow\n"
        transport.write_calls.clear()

        assert ensure_debugpy_installed(transport, **common) is True, (
            "an unrecorded file under device_dir must force a reinstall"
        )
        assert "/lib/debugpy/__init__.py" not in transport.files, "the shadowing file should have been swept"

    def test_unremovable_stale_file_fails_rather_than_marking_clean(
        self, temp_package_dir, temp_cache_dir, mock_mpy_cross
    ):
        """If the sweep cannot delete a shadowing file, recording a clean
        install would make every later run trust it."""
        transport = FakeTransport()
        transport.files["/lib/debugpy/__init__.py"] = b"# shadow\n"

        def refuse(path):
            raise OSError(1, "read-only")

        transport.fs_rmfile = refuse
        with pytest.raises(CommandError, match="could not remove stale file"):
            ensure_debugpy_installed(
                transport,
                package_dir=temp_package_dir,
                mpy_cross=mock_mpy_cross,
                cache_dir=temp_cache_dir,
            )
        assert not _read_marker(transport, "/lib/.debugpy-install.json"), (
            "a sweep that could not do its job must not leave a valid marker"
        )


class TestDeviceLibDirResolution:
    """The install target comes from the device's sys.path, not a fixed "/lib".

    Pyboard-style boards mount their filesystem at /flash, so "/lib" is
    neither creatable (mkdir raises ENODEV) nor importable there.
    """

    def test_flash_mounted_board_installs_under_flash_lib(
        self, temp_package_dir, temp_cache_dir, mock_mpy_cross
    ):
        transport = FakeTransport(sys_path=["", ".frozen", "/flash", "/flash/lib"])
        transport.directories.add("/flash")

        assert (
            ensure_debugpy_installed(
                transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
            )
            is True
        )

        written = [c[0] for c in transport.write_calls]
        assert "/flash/lib/.debugpy-install.json" in written
        assert all(p.startswith("/flash/lib/") for p in written), written
        assert len([p for p in written if p.startswith("/flash/lib/debugpy/")]) == _EXPECTED_MPYS

    def test_root_mounted_board_installs_under_lib(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        transport = FakeTransport(sys_path=["", ".frozen", "/lib"])

        assert (
            ensure_debugpy_installed(
                transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
            )
            is True
        )

        assert "/lib/.debugpy-install.json" in [c[0] for c in transport.write_calls]

    def test_read_only_rom_lib_is_not_an_install_target(
        self, temp_package_dir, temp_cache_dir, mock_mpy_cross
    ):
        """A /rom entry is frozen and unwritable, so it must be passed over
        even when it is the first lib directory on sys.path."""
        transport = FakeTransport(sys_path=["", "/rom/lib", "/flash/lib"])
        transport.directories.add("/flash")

        ensure_debugpy_installed(
            transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
        )

        assert all(c[0].startswith("/flash/lib/") for c in transport.write_calls)

    def test_no_lib_dir_on_sys_path_raises(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """Guessing a path on a target with no lib directory would fail later
        and less clearly, so the install stops here and names the override."""
        transport = FakeTransport(sys_path=["", ".frozen"])

        with pytest.raises(CommandError, match="no lib directory in the target's sys.path"):
            ensure_debugpy_installed(
                transport, temp_package_dir, mpy_cross=mock_mpy_cross, cache_dir=temp_cache_dir
            )

        assert transport.write_calls == []

    def test_explicit_paths_skip_the_probe(self, temp_package_dir, temp_cache_dir, mock_mpy_cross):
        """A caller that names both paths must not be overridden by, or made
        to depend on, whatever the device reports."""
        transport = FakeTransport(sys_path=["", ".frozen"])  # probe would raise
        transport.directories.add("/remote")

        assert (
            ensure_debugpy_installed(
                transport,
                temp_package_dir,
                mpy_cross=mock_mpy_cross,
                cache_dir=temp_cache_dir,
                device_dir="/remote/debugpy",
                marker_path="/remote/.debugpy-install.json",
            )
            is True
        )

        assert "/remote/.debugpy-install.json" in [c[0] for c in transport.write_calls]


class TestDeviceDirGuard:
    """device_dir is swept, so it must not be allowed to reach beyond the package."""

    @pytest.mark.parametrize("device_dir", ["/", "/lib", "/lib/"])
    def test_too_broad_device_dir_rejected(
        self, device_dir, temp_package_dir, temp_cache_dir, mock_mpy_cross
    ):
        transport = FakeTransport()
        with pytest.raises(CommandError, match="too broad to sweep"):
            ensure_debugpy_installed(
                transport,
                package_dir=temp_package_dir,
                mpy_cross=mock_mpy_cross,
                cache_dir=temp_cache_dir,
                device_dir=device_dir,
            )
        assert not transport.write_calls, "nothing should be written before the guard trips"


class TestPruneCache:
    """_prune_cache is the only unconditional filesystem deletion here."""

    def test_keeps_current_key_and_removes_sibling_keys_only(self, tmp_path):
        keep = "a" * 64
        stale = "b" * 64
        for name in (keep, stale, "not-a-key", "c" * 63):
            (tmp_path / name).mkdir()
            (tmp_path / name / "marker").write_bytes(b"x")

        _prune_cache(str(tmp_path), keep)

        assert (tmp_path / keep).exists(), "the key in use must survive"
        assert not (tmp_path / stale).exists(), "a sibling key directory should be pruned"
        assert (tmp_path / "not-a-key").exists(), "unrelated names must be left alone"
        assert (tmp_path / ("c" * 63)).exists(), "only full 64-hex names are ours"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
