# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Assert that every gating job a submodule PR would run has also run for the branch.

A submodule's CI is not described by the workflows in the branch's own tree. A
`pull_request` event runs the workflows from the merge of the branch into the
base, so a job added to upstream master after the branch was cut runs against
that branch on every PR while appearing nowhere in what the branch carries. A
push to the fork is the mirror image: it runs the workflows from the pushed
commit, so a green fork build says nothing about the jobs only master has.

The difference between the two sets is the set of jobs that have never executed
for a branch under any trigger, and that is what this reports. A job is counted
when its workflow triggers on `pull_request` and its `paths`/`paths-ignore`
filter admits at least one file the branch changes.

The remedy for a reported job is to rebase that branch onto `upstream/master`,
which makes the two sets equal; the report is empty afterwards.

Usage: uv run tools/check_submodule_ci.py [--no-fetch] [--root <dir>]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

# mbm hardcodes this remote name for its PR fetches and its default rebase
# target, so the composition already requires it to exist and to be upstream.
UPSTREAM_REMOTE = "upstream"
UPSTREAM_BRANCH = "master"


class CheckError(Exception):
    """A setup problem that stops the check from reaching a verdict."""


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise CheckError(f"git {' '.join(args)} in {repo}: {proc.stderr.strip()}")
    return proc.stdout


def glob_to_regex(pattern: str) -> str:
    """Translate one GitHub path filter to a regex.

    GitHub's filter syntax is not `fnmatch`: `*` stops at a separator and `**`
    crosses them, so `tools/*` and `tools/**` mean different things and
    `fnmatch` collapses them.
    """
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out) + r"\Z"


def filter_is_understood(patterns: list[str]) -> bool:
    """True when every pattern uses only the syntax `glob_to_regex` implements.

    A pattern this cannot translate makes the job count as gating rather than
    silently drop out of the report: over-reporting costs a look, and
    under-reporting is the failure this check exists to prevent.
    """
    return not any(p.startswith("!") or set("+@()[]{}") & set(p) for p in patterns)


def paths_admit(patterns: list[str], changed: list[str]) -> bool:
    if not filter_is_understood(patterns):
        return True
    matchers = [re.compile(glob_to_regex(p)) for p in patterns]
    return any(m.match(f) for f in changed for m in matchers)


def gating_jobs(repo: Path, ref: str, changed: list[str]) -> set[str]:
    """The `<workflow>:<job>` ids a `pull_request` at `ref` would run for `changed`."""
    jobs: set[str] = set()
    listing = git(repo, "ls-tree", "--name-only", ref, ".github/workflows/").split()
    for path in listing:
        if not path.endswith((".yml", ".yaml")):
            continue
        try:
            doc = yaml.safe_load(git(repo, "show", f"{ref}:{path}"))
        except yaml.YAMLError as er:
            raise CheckError(f"{ref}:{path} is not parseable YAML: {er}") from er
        if not isinstance(doc, dict):
            continue
        # YAML 1.1 reads a bare `on:` key as the boolean True, which is how
        # every one of these workflows spells its trigger block.
        triggers = doc.get("on", doc.get(True))
        if isinstance(triggers, str):
            triggers = [triggers]
        if isinstance(triggers, list):
            pull_request = {} if "pull_request" in triggers else None
        elif isinstance(triggers, dict) and "pull_request" in triggers:
            # `pull_request:` with no body parses as None and means "no filter".
            pull_request = triggers["pull_request"] or {}
        else:
            pull_request = None
        if pull_request is None:
            continue
        include = pull_request.get("paths")
        exclude = pull_request.get("paths-ignore")
        if include is not None and not paths_admit(include, changed):
            continue
        if exclude is not None and filter_is_understood(exclude):
            matchers = [re.compile(glob_to_regex(p)) for p in exclude]
            if all(any(m.match(f) for m in matchers) for f in changed):
                continue
        name = Path(path).name
        for job in doc.get("jobs") or {}:
            jobs.add(f"{name}:{job}")
    return jobs


def check_submodule(root: Path, entry: dict, fetch: bool) -> tuple[int, list[str]]:
    """Return (failures, report lines) for one submodule's registered branches."""
    sub = root / entry["path"]
    lines: list[str] = []
    if not (sub / ".git").exists():
        raise CheckError(f"{entry['path']} is not a checked-out submodule")
    remotes = git(sub, "remote").split()
    if UPSTREAM_REMOTE not in remotes:
        raise CheckError(
            f"{entry['path']} has no remote named '{UPSTREAM_REMOTE}' (has: {' '.join(remotes)})"
        )
    if fetch:
        # A remote-tracking ref goes stale as soon as anyone pushes, and a stale
        # ref here reports a job set that upstream has already moved past.
        git(sub, "fetch", "--quiet", UPSTREAM_REMOTE, UPSTREAM_BRANCH)
    master = f"{UPSTREAM_REMOTE}/{UPSTREAM_BRANCH}"

    failures = 0
    for branch in entry["branches"]:
        name = branch["name"]
        # A registered branch without a `pr_number` is fork-local and reaches no
        # reviewer, so nothing it faces is urgent - but it is still reported,
        # because the day it becomes a PR is the day the gap costs something.
        pr = f"PR #{branch['pr_number']}" if branch.get("pr_number") else "no PR"
        label = f"{entry['path']}/{name} ({pr})"
        try:
            base = git(sub, "merge-base", master, name).strip()
        except CheckError:
            lines.append(f"FAIL {label}: no local branch of that name")
            failures += 1
            continue
        changed = git(sub, "diff", "--name-only", base, name).split()
        if not changed:
            lines.append(f"ok   {label}: changes nothing against {master}")
            continue
        faced = gating_jobs(sub, master, changed)
        seen = gating_jobs(sub, name, changed)
        unseen = sorted(faced - seen)
        retired = sorted(seen - faced)
        if unseen:
            failures += 1
            lines.append(
                f"FAIL {label}: {len(unseen)} gating job(s) upstream runs "
                f"that this branch has never seen"
            )
            lines.extend(f"       {job}" for job in unseen)
            lines.append(f"       rebase {name} onto {master} to run them")
        else:
            lines.append(f"ok   {label}: {len(faced)} gating job(s), all seen")
        # Reported but not failed: a job upstream deleted cannot block a PR, and
        # it is here because it is the same evidence - the branch's own tree is
        # not the definition of what CI runs, in either direction.
        lines.extend(
            f"     note {label}: {job} no longer exists upstream" for job in retired
        )
    return failures, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="top repo (default: cwd)")
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="trust the existing remote-tracking refs instead of fetching",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    config = root / "mbm.toml"
    if not config.exists():
        print(f"no mbm.toml at {root}", file=sys.stderr)
        return 2

    failures = 0
    for entry in tomllib.loads(config.read_text())["submodules"]:
        try:
            count, lines = check_submodule(root, entry, fetch=not args.no_fetch)
        except CheckError as er:
            print(f"FAIL {entry['path']}: {er}", file=sys.stderr)
            return 2
        failures += count
        # One finding is several lines, so the whole report goes to one stream:
        # splitting it by severity would separate a header from its job list.
        for line in lines:
            print(line)

    if failures:
        sys.stdout.flush()
        print(
            f"{failures} branch(es) face a gating job that has never run for them",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
