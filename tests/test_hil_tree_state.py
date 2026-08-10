"""Host-side tests for `tests/hil/tree_state.py`.

The HIL suite needs a board, so its own gate would otherwise be exercised only
on the bench - and the risk row it closes is about a run whose provenance
nobody checked. These drive the gate against throwaway repositories instead.
"""

import os
import subprocess
import sys
from pathlib import Path

_HIL_DIR = str(Path(__file__).resolve().parents[0] / "hil")
if _HIL_DIR not in sys.path:
    sys.path.insert(0, _HIL_DIR)

import pytest  # noqa: E402
import tree_state  # noqa: E402


def _git_env():
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_AUTHOR_NAME="Tree State",
        GIT_AUTHOR_EMAIL="tree@example.invalid",
        GIT_COMMITTER_NAME="Tree State",
        GIT_COMMITTER_EMAIL="tree@example.invalid",
    )
    return env


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=str(cwd),
        env=_git_env(),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.delenv(tree_state.ALLOW_DIRTY_ENV, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "tracked.txt").write_text("one\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "--quiet", "-m", "one")
    return repo


def test_clean_tree_is_attributable(checkout):
    state = tree_state.tree_state(checkout)

    assert len(state["head"]) == 40
    assert state["dirty"] == []
    assert tree_state.dirty_tree_refusal(state) is None
    assert "- Working tree: clean" in tree_state.record_lines(state)


def test_modified_file_refuses_the_run(checkout):
    (checkout / "tracked.txt").write_text("two\n")

    state = tree_state.tree_state(checkout)
    refusal = tree_state.dirty_tree_refusal(state)

    assert refusal is not None
    assert "tracked.txt" in refusal
    assert tree_state.ALLOW_DIRTY_ENV in refusal


def test_untracked_file_refuses_the_run(checkout):
    """An untracked file is what a working-tree draft looks like before `git add`."""
    (checkout / "draft_test.py").write_text("assert False\n")

    state = tree_state.tree_state(checkout)

    assert tree_state.dirty_tree_refusal(state) is not None


def test_override_runs_but_names_the_dirt(checkout, monkeypatch):
    monkeypatch.setenv(tree_state.ALLOW_DIRTY_ENV, "1")
    (checkout / "tracked.txt").write_text("two\n")

    state = tree_state.tree_state(checkout)

    assert tree_state.dirty_tree_refusal(state) is None
    record = "\n".join(tree_state.record_lines(state))
    assert "Uncommitted at run time" in record
    assert "tracked.txt" in record
    assert "Working tree: clean" not in record


def test_submodule_pins_are_recorded(tmp_path, checkout):
    fork = tmp_path / "fork.git"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(fork))
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--quiet", "--initial-branch=main", str(seed))
    (seed / "s.txt").write_text("s\n")
    _git(seed, "add", "s.txt")
    _git(seed, "commit", "--quiet", "-m", "s")
    _git(seed, "remote", "add", "origin", str(fork))
    _git(seed, "push", "--quiet", "origin", "main")
    _git(checkout, "submodule", "--quiet", "add", str(fork), "dep")
    _git(checkout, "commit", "--quiet", "-m", "add dep")

    state = tree_state.tree_state(checkout)

    assert list(state["pins"]) == ["dep"]
    assert len(state["pins"]["dep"]) == 40
    assert "- Pinned `dep`: `{}`".format(state["pins"]["dep"]) in tree_state.record_lines(state)


def test_dirty_submodule_refuses_the_run(tmp_path, checkout):
    """The debugpy sources this suite copies onto the board live in a submodule."""
    fork = tmp_path / "fork.git"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(fork))
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--quiet", "--initial-branch=main", str(seed))
    (seed / "s.txt").write_text("s\n")
    _git(seed, "add", "s.txt")
    _git(seed, "commit", "--quiet", "-m", "s")
    _git(seed, "remote", "add", "origin", str(fork))
    _git(seed, "push", "--quiet", "origin", "main")
    _git(checkout, "submodule", "--quiet", "add", str(fork), "dep")
    _git(checkout, "commit", "--quiet", "-m", "add dep")

    (checkout / "dep" / "s.txt").write_text("edited on the bench\n")
    state = tree_state.tree_state(checkout)

    assert tree_state.dirty_tree_refusal(state) is not None
    assert "dep" in "".join(state["dirty"])
