"""Runs the VS Code extension's own test suite from `make test`.

The extension is TypeScript with a node test runner, so its tests can't be
expressed as pytest cases; running them from here keeps one entry point for
the repo and stops them rotting unnoticed. Skipped where the toolchain isn't
set up (no node, or `npm install` never run) rather than failing, since the
Python side of this repo must stay usable without a node install.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_EXTENSION_DIR = Path(__file__).resolve().parents[1] / "extension"
_NODE_MODULES = _EXTENSION_DIR / "node_modules"

requires_extension_toolchain = pytest.mark.skipif(
    shutil.which("npm") is None or not _NODE_MODULES.is_dir(),
    reason="extension toolchain not set up (need npm and `npm install` in extension/)",
)


@requires_extension_toolchain
def test_s7_1_extension_suite_passes():
    """`npm test` compiles the extension and runs its node tests.

    That suite includes the integration cases that spawn the real
    `mpremote debug unix`, so this also covers the handshake contract
    between the command and the extension.
    """
    result = subprocess.run(
        ["npm", "test"],
        cwd=_EXTENSION_DIR,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"npm test failed:\n{result.stdout}\n{result.stderr}"
    # A suite that silently ran nothing exits 0 too, so the counts are checked,
    # not just the exit status.
    assert _count(result.stdout, "fail") == 0, result.stdout
    assert _count(result.stdout, "pass") > 0, result.stdout


def _count(stdout: str, label: str) -> int:
    """Read a `node --test` summary count, whichever reporter produced it.

    The counts line is `# pass 80` under the TAP reporter and `ℹ pass 80`
    under the spec reporter, and which one is the default varies by node
    version. Matching the number rather than a formatted line keeps this
    assertion tied to the suite's result instead of to the node release the
    machine happens to have.
    """
    match = re.search(rf"^[#ℹ]\s*{label}\s+(\d+)\s*$", stdout, re.MULTILINE)
    assert match is not None, f"no '{label}' count in node --test output:\n{stdout}"
    return int(match.group(1))
