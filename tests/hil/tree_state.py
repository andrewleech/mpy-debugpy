"""What tree a hardware run ran against.

A HIL run's output outlives the checkout that produced it, and a run whose
tree is not recorded proves nothing: one on 2026-08-09 failed three network
scenarios against a working-tree draft of a test that was never committed, and
was read as a device regression until the expectation could not be traced to
any commit.

So a run records the top-repo commit and both submodule pins alongside its
results, and refuses to start from a dirty tree. The refusal is overridable -
some benches genuinely need to run a change before committing it - but the
override records the dirt in the results file, so the run stays attributable
either way.
"""

import os
import subprocess

ALLOW_DIRTY_ENV = "MPY_DEBUG_HIL_ALLOW_DIRTY"


def _git(top_dir, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=str(top_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def tree_state(top_dir):
    """The commit, the submodule pins, and whatever is uncommitted.

    `git status --porcelain` reports a submodule whose working tree has been
    touched, so one call covers the debugpy sources this suite copies onto the
    board as well as the harness driving it.
    """
    pins = {}
    for line in _git(top_dir, "submodule", "status").splitlines():
        fields = line.strip().split()
        if len(fields) >= 2:
            pins[fields[1]] = fields[0].lstrip("+-U")
    dirty = [line for line in _git(top_dir, "status", "--porcelain").splitlines() if line]
    return {
        "head": _git(top_dir, "rev-parse", "HEAD") or "unknown",
        "pins": pins,
        "dirty": dirty,
        "allowed_dirty": bool(dirty) and _dirty_allowed(),
    }


def _dirty_allowed():
    return os.environ.get(ALLOW_DIRTY_ENV, "") not in ("", "0")


def dirty_tree_refusal(state):
    """The message to fail with, or None if the run may proceed."""
    if not state["dirty"] or state["allowed_dirty"]:
        return None
    listing = "\n".join("  " + line for line in state["dirty"])
    return (
        "the working tree is dirty, so this run's results could not be "
        "attributed to any commit:\n"
        f"{listing}\n"
        f"commit first, or set {ALLOW_DIRTY_ENV}=1 to run anyway - the "
        "results record will then list the uncommitted paths."
    )


def record_lines(state):
    """The provenance block for the results record."""
    lines = [f"- Tree: `{state['head']}`"]
    for path, pin in sorted(state["pins"].items()):
        lines.append(f"- Pinned `{path}`: `{pin}`")
    if state["dirty"]:
        lines.append(
            "- **Uncommitted at run time**, so no commit describes what ran; "
            "the paths below differ from `{}`:".format(state["head"])
        )
        lines += ["  - `{}`".format(line) for line in state["dirty"]]
    else:
        lines.append("- Working tree: clean")
    return lines
