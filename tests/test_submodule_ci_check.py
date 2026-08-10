"""Behaviour tests for `tools/check_submodule_ci.py`.

The check answers one question: which gating jobs would a PR from a submodule
branch run that have never run for that branch? A `pull_request` event uses the
workflows from the merge with the base, so upstream's set is what a PR faces; a
push to the fork uses the pushed commit's, so the branch's own set is what has
actually executed.

These build a throwaway upstream/fork pair under `tmp_path` with real workflow
files and drive the real module, because the parts that can be wrong quietly -
the `on:`-parses-as-`True` trigger block, the path filters, the fetch - are all
in what it reads off git rather than in its arithmetic. Each direction is
asserted separately: a check that cannot fail is worth nothing.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "tools"))

import check_submodule_ci as checker  # noqa: E402


def _git_env():
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_AUTHOR_NAME="CI Check",
        GIT_AUTHOR_EMAIL="ci@example.invalid",
        GIT_COMMITTER_NAME="CI Check",
        GIT_COMMITTER_EMAIL="ci@example.invalid",
    )
    return env


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_git_env(),
        capture_output=True,
        text=True,
        check=check,
    )


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())


RUFF_WF = """
    name: ruff
    on: [push, pull_request]
    jobs:
      ruff:
        runs-on: ubuntu-latest
        steps:
          - run: ruff check .
    """

PORTS_WF = """
    name: unix port
    on:
      push:
      pull_request:
        paths:
          - 'py/**'
          - 'ports/unix/**'
    jobs:
      standard:
        runs-on: ubuntu-latest
        steps:
          - run: make
      coverage:
        runs-on: ubuntu-latest
        steps:
          - run: make coverage
    """

CODESPELL_WF = """
    name: codespell
    on: [push, pull_request]
    jobs:
      codespell:
        runs-on: ubuntu-latest
        steps:
          - run: codespell
    """


@pytest.fixture
def workspace(tmp_path):
    """A top repo with one submodule whose `upstream` remote carries master.

    The submodule holds two registered branches: one cut before an extra
    workflow landed upstream, one cut after.
    """
    upstream = tmp_path / "upstream.git"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=master", str(upstream))

    sub = tmp_path / "top" / "sub"
    sub.mkdir(parents=True)
    _git(sub, "init", "--quiet", "--initial-branch=master")
    _write(sub, ".github/workflows/ruff.yml", RUFF_WF)
    _write(sub, ".github/workflows/ports_unix.yml", PORTS_WF)
    _write(sub, "py/vm.c", "int main(void) { return 0; }\n")
    _git(sub, "add", "-A")
    _git(sub, "commit", "--quiet", "-m", "base")
    _git(sub, "remote", "add", "upstream", str(upstream))
    _git(sub, "push", "--quiet", "upstream", "master")

    # Cut before the new workflow lands upstream.
    _git(sub, "checkout", "--quiet", "-b", "old_branch")
    _write(sub, "py/vm.c", "int main(void) { return 1; }\n")
    _git(sub, "commit", "--quiet", "-am", "change the vm")

    _git(sub, "checkout", "--quiet", "master")
    _write(sub, ".github/workflows/codespell.yml", CODESPELL_WF)
    _git(sub, "add", "-A")
    _git(sub, "commit", "--quiet", "-m", "add codespell")
    _git(sub, "push", "--quiet", "upstream", "master")

    # Cut after, so it carries the same set upstream has.
    _git(sub, "checkout", "--quiet", "-b", "new_branch")
    _write(sub, "py/vm.c", "int main(void) { return 2; }\n")
    _git(sub, "commit", "--quiet", "-am", "change the vm again")
    _git(sub, "checkout", "--quiet", "master")
    _git(sub, "fetch", "--quiet", "upstream", "master")

    top = tmp_path / "top"
    (top / "mbm.toml").write_text(
        textwrap.dedent(
            """
            [[submodules]]
            path = "sub"

            [[submodules.branches]]
            name = "old_branch"

            [[submodules.branches]]
            name = "new_branch"
            """
        ).lstrip()
    )
    return top


def _run(top, capsys, *args):
    code = checker.main(["--root", str(top), *args])
    out = capsys.readouterr()
    return code, out.out + out.err


def _register_only(top, *names, pr_number=None):
    """Rewrite the config to register exactly `names`, so one case is asserted alone."""
    pr = f"pr_number = {pr_number}\n" if pr_number else ""
    entries = "".join(f'\n[[submodules.branches]]\nname = "{n}"\n{pr}' for n in names)
    (top / "mbm.toml").write_text('[[submodules]]\npath = "sub"\n' + entries)


def test_a_branch_is_labelled_with_the_pr_it_reaches(workspace, capsys):
    """Which branches reach a reviewer decides which gaps cost anything.

    A registered branch with no `pr_number` is fork-local; the composition names
    branches of both kinds and the report has to tell them apart.
    """
    _register_only(workspace, "old_branch", pr_number=1234)

    code, text = _run(workspace, capsys)

    assert code == 1
    assert "FAIL sub/old_branch (PR #1234)" in text


def test_branch_cut_after_the_job_landed_passes(workspace, capsys):
    _register_only(workspace, "new_branch")

    code, text = _run(workspace, capsys)

    assert code == 0, text
    assert "ok   sub/new_branch (no PR)" in text


def test_branch_cut_before_the_job_landed_fails_and_names_it(workspace, capsys):
    code, text = _run(workspace, capsys)

    assert code == 1
    assert "FAIL sub/old_branch (no PR)" in text
    assert "codespell.yml:codespell" in text
    assert "rebase old_branch" in text
    # The up-to-date branch in the same run must not be tarred with it.
    assert "ok   sub/new_branch (no PR)" in text


def test_a_job_upstream_deleted_is_reported_without_failing(workspace, capsys):
    """A branch carrying a workflow upstream has dropped cannot be blocked by it.

    It is reported anyway: it is the same evidence that the branch's own tree
    does not define what CI runs, pointing the other way.
    """
    sub = workspace / "sub"
    _git(sub, "checkout", "--quiet", "new_branch")
    _write(sub, ".github/workflows/gone.yml", RUFF_WF.replace("ruff:", "retired:"))
    _git(sub, "add", "-A")
    _git(sub, "commit", "--quiet", "-m", "a workflow only this branch has")
    _git(sub, "checkout", "--quiet", "master")
    _register_only(workspace, "new_branch")

    code, text = _run(workspace, capsys)

    assert code == 0, text
    assert "gone.yml:retired no longer exists upstream" in text


def test_a_job_whose_paths_filter_excludes_the_branch_is_not_counted(workspace, capsys):
    """`ports_unix` gates `py/**`; a branch touching only docs does not face it."""
    sub = workspace / "sub"
    _git(sub, "checkout", "--quiet", "-b", "docs_only", "upstream/master")
    _write(sub, "docs/readme.md", "hello\n")
    _git(sub, "add", "-A")
    _git(sub, "commit", "--quiet", "-m", "docs")
    _git(sub, "checkout", "--quiet", "master")
    _register_only(workspace, "docs_only")

    code, text = _run(workspace, capsys)

    assert code == 0, text
    # ruff and codespell have no filter, so two jobs remain; the two ports_unix
    # jobs are filtered out rather than the whole workflow being missed.
    assert "sub/docs_only (no PR): 2 gating job(s), all seen" in text


def test_the_fetch_is_what_makes_the_answer_current(workspace, capsys):
    """A job pushed to upstream after the last fetch must still be reported.

    The remote-tracking ref is stale by construction here, which is the false
    pass the fetch exists to prevent, so `--no-fetch` and the default must
    disagree.
    """
    sub = workspace / "sub"
    scratch = workspace.parent / "scratch"
    _git(workspace.parent, "clone", "--quiet", str(workspace.parent / "upstream.git"), str(scratch))
    _write(scratch, ".github/workflows/late.yml", RUFF_WF.replace("ruff:", "late:"))
    _git(scratch, "add", "-A")
    _git(scratch, "commit", "--quiet", "-m", "a job added after the last fetch")
    _git(scratch, "push", "--quiet", "origin", "master")
    _register_only(workspace, "new_branch")
    assert _git(sub, "rev-parse", "upstream/master").stdout.strip() != (
        _git(scratch, "rev-parse", "HEAD").stdout.strip()
    )

    stale_code, stale_text = _run(workspace, capsys, "--no-fetch")
    assert stale_code == 0, stale_text

    code, text = _run(workspace, capsys)
    assert code == 1
    assert "late.yml:late" in text


def test_a_branch_that_is_not_checked_out_locally_is_an_error(workspace, capsys):
    _register_only(workspace, "never_created")

    code, text = _run(workspace, capsys)

    assert code == 1
    assert "no local branch" in text


def test_a_submodule_without_an_upstream_remote_stops_the_check(workspace, capsys):
    """No verdict is better than a verdict from the wrong remote."""
    _git(workspace / "sub", "remote", "rename", "upstream", "elsewhere")

    code, text = _run(workspace, capsys)

    assert code == 2
    assert "no remote named 'upstream'" in text


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("tools/**", "tools/mpremote/main.py", True),
        ("tools/*", "tools/mpremote/main.py", False),
        ("tools/*", "tools/ci.sh", True),
        (".github/workflows/*.yml", ".github/workflows/ruff.yml", True),
        (".github/workflows/*.yml", ".github/workflows/sub/ruff.yml", False),
        ("**/manifest.py", "python-ecosys/debugpy/manifest.py", True),
        ("**/manifest.py", "manifest.py", True),
        ("py/**", "python-ecosys/x.py", False),
    ],
)
def test_path_filters_use_github_glob_semantics_not_fnmatch(pattern, path, expected):
    """`*` stops at a separator and `**` crosses it; `fnmatch` conflates them."""
    assert checker.paths_admit([pattern], [path]) is expected


def test_a_filter_this_cannot_translate_counts_as_gating():
    """Over-reporting costs a look; under-reporting is the failure being prevented."""
    assert checker.paths_admit(["!docs/**"], ["py/vm.c"]) is True
