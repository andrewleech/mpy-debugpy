"""Behaviour tests for `tools/check-submodule-pins.sh` and the pre-push hook.

The check exists because four CI runs died at the submodule checkout with
`upload-pack: not our ref`: the top repo pinned a commit that lived only in a
local clone, which leaves the working tree looking identical to a correct one.

These build a throwaway fork/submodule/superproject under `tmp_path` and drive
the real scripts, copied into that tree the way they sit in this repo. The
point is the negative case: a check that cannot fail is the failure mode the
risk register is full of, so each direction is asserted separately.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = _REPO_ROOT / "tools"


def _git_env():
    # GIT_CONFIG_* to /dev/null keeps the developer's own config - hooks paths,
    # protocol allowances, commit signing - out of the fixture's behaviour.
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_AUTHOR_NAME="Pin Check",
        GIT_AUTHOR_EMAIL="pin@example.invalid",
        GIT_COMMITTER_NAME="Pin Check",
        GIT_COMMITTER_EMAIL="pin@example.invalid",
    )
    return env


def _git(cwd, *args, check=True):
    # protocol.file.allow: git refuses file:// submodules by default (CVE-2022-39253).
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=str(cwd),
        env=_git_env(),
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture
def superproject(tmp_path):
    """A superproject whose one submodule is pinned to a commit on its fork."""
    fork = tmp_path / "fork.git"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(fork))

    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--quiet", "--initial-branch=main", str(seed))
    (seed / "f.txt").write_text("one\n")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "--quiet", "-m", "one")
    _git(seed, "remote", "add", "origin", str(fork))
    _git(seed, "push", "--quiet", "origin", "main")

    top = tmp_path / "top"
    _git(tmp_path, "init", "--quiet", "--initial-branch=main", str(top))
    _git(top, "submodule", "--quiet", "add", str(fork), "sub")
    (top / "tools").mkdir()
    for script in ("check-submodule-pins.sh", "pre-push-check.sh"):
        shutil.copy2(_TOOLS / script, top / "tools" / script)
    _git(top, "add", "-A")
    _git(top, "commit", "--quiet", "-m", "pin sub")
    return top


def _check(top, *args):
    return subprocess.run(
        [str(top / "tools" / "check-submodule-pins.sh"), *args],
        cwd=str(top),
        env=_git_env(),
        capture_output=True,
        text=True,
    )


def _strand_the_pin(top):
    """Commit in the submodule without pushing, and pin that commit."""
    _git(top / "sub", "commit", "--quiet", "--allow-empty", "-m", "local only")
    stranded = _git(top / "sub", "rev-parse", "HEAD").stdout.strip()
    _git(top, "add", "sub")
    _git(top, "commit", "--quiet", "-m", "bump sub")
    return stranded


def test_pushed_pin_passes(superproject):
    result = _check(superproject)
    assert result.returncode == 0, result.stderr
    assert "ok   sub:" in result.stdout


def test_unpushed_pin_fails_and_names_the_commit(superproject):
    stranded = _strand_the_pin(superproject)

    result = _check(superproject)

    assert result.returncode == 1
    assert stranded in result.stderr
    assert "on no branch of" in result.stderr


def test_earlier_commit_still_passes(superproject):
    """The check reads the pin out of the commit named, not out of HEAD."""
    good = _git(superproject, "rev-parse", "HEAD").stdout.strip()
    _strand_the_pin(superproject)

    assert _check(superproject).returncode == 1
    assert _check(superproject, good).returncode == 0


def test_object_on_the_remote_but_on_no_branch_fails(superproject):
    """A pushed *object* is not enough; a submodule clone fetches branches.

    The mitigation this replaces said "must name a remote branch", and the
    distinction is the whole point: a commit can be present on the remote,
    under a ref no default refspec brings down, and still never arrive.
    """
    stranded = _strand_the_pin(superproject)
    _git(superproject / "sub", "push", "--quiet", "origin", "HEAD:refs/hidden/probe")

    result = _check(superproject)

    assert result.returncode == 1
    assert stranded in result.stderr


def test_pre_push_hook_refuses_the_push(superproject):
    """End to end through git: the hook is what actually guards the push."""
    remote = superproject.parent / "top-remote.git"
    _git(superproject.parent, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))
    _git(superproject, "remote", "add", "origin", str(remote))
    hook = superproject / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.symlink_to(superproject / "tools" / "pre-push-check.sh")

    clean = _git(superproject, "push", "--dry-run", "origin", "main", check=False)
    assert clean.returncode == 0, clean.stderr

    stranded = _strand_the_pin(superproject)
    refused = _git(superproject, "push", "--dry-run", "origin", "main", check=False)

    assert refused.returncode != 0
    assert stranded in refused.stderr
    assert "--no-verify" in refused.stderr


def test_hook_bypass_is_available(superproject):
    """`--no-verify` still works: this is a guard rail, not a gate."""
    remote = superproject.parent / "top-remote.git"
    _git(superproject.parent, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))
    _git(superproject, "remote", "add", "origin", str(remote))
    hook = superproject / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.symlink_to(superproject / "tools" / "pre-push-check.sh")
    _strand_the_pin(superproject)

    result = _git(superproject, "push", "--dry-run", "--no-verify", "origin", "main", check=False)

    assert result.returncode == 0, result.stderr
