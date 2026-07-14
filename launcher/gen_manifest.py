#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Assemble CI-built firmware manifest fragments into `[[variant]]` TOML entries
(STORY-3.2) compatible with `launcher/firmware.py`'s `select`/`verify`/`fetch`.

Each build job in `.github/workflows/firmware.yml` emits one fragment (a JSON
file describing a single variant: id, port, board, build flags, capability
intent, local artifact path, release asset filename, and the artifact's
sha256) as a build artifact. This script reads a directory of those fragments
and renders them as TOML `[[variant]]` blocks, computing `download_url` from
a GitHub Release tag and each fragment's `release_asset` filename so the
output is directly usable by `firmware.py`'s `fetch`/`select` once the
release exists.

Usage:
    uv run launcher/gen_manifest.py --check --fragments-dir DIR
    uv run launcher/gen_manifest.py --fragments-dir DIR --repo-slug OWNER/REPO \\
        --tag TAG [--output OUT.toml]

`--check` validates the fragment set (required fields present, capability keys
known, sha256 well-formed, no duplicate ids) and exits nonzero on the first
problem found, without writing anything. Without `--check`, `--repo-slug` and
`--tag` are required and the assembled TOML is written to `--output` (or
stdout if omitted). The output holds only the variant blocks built from the
given fragments - merging them into a full `firmware/firmware.toml` (which
also carries deprecated legacy entries and the file's header commentary) is a
separate, later step.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# The exact capability vocabulary the runtime probe reports (mirrors
# `firmware.py`'s KNOWN_CAPABILITIES) plus "unknown", the one non-boolean value
# the manifest schema allows for a capability that was never confirmed either
# way (see firmware.toml's header note on `set_local`/`save_names`).
KNOWN_CAPABILITIES = ("settrace", "save_names", "set_local", "f_back")
KNOWN_CAPABILITY_VALUES = (True, False, "unknown")

REQUIRED_FRAGMENT_FIELDS = (
    "id",
    "description",
    "repo",
    "source_commit",
    "port",
    "board",
    "build_flags",
    "capabilities",
    "artifact",
    "release_asset",
    "artifact_sha256",
)


class FragmentError(Exception):
    """A fragment file fails schema validation."""


def load_fragments(fragments_dir: Path) -> list[dict[str, Any]]:
    paths = sorted(fragments_dir.glob("*.json"))
    if not paths:
        raise FragmentError(f"no *.json fragments found under {fragments_dir}")
    fragments = []
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise FragmentError(f"{path}: invalid JSON ({e})") from e
        fragments.append((path, data))
    return fragments


def validate_fragment(path: Path, frag: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems with `frag` (empty if valid)."""
    problems = []
    for field in REQUIRED_FRAGMENT_FIELDS:
        if field not in frag:
            problems.append(f"{path}: missing required field {field!r}")

    if "build_flags" in frag and not isinstance(frag["build_flags"], list):
        problems.append(f"{path}: build_flags must be a list")

    if "capabilities" in frag:
        caps = frag["capabilities"]
        if not isinstance(caps, dict):
            problems.append(f"{path}: capabilities must be a table")
        else:
            unknown_keys = [k for k in caps if k not in KNOWN_CAPABILITIES]
            if unknown_keys:
                problems.append(f"{path}: capabilities has unknown key(s): {', '.join(unknown_keys)}")
            bad_values = [k for k, v in caps.items() if v not in KNOWN_CAPABILITY_VALUES]
            if bad_values:
                problems.append(
                    f"{path}: capabilities key(s) with unrecognised value: {', '.join(bad_values)} "
                    f"(must be true, false, or \"unknown\")"
                )

    sha256 = frag.get("artifact_sha256", "")
    if sha256 and (len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256.lower())):
        problems.append(f"{path}: artifact_sha256 is not a well-formed 64-character hex digest")

    if not frag.get("artifact"):
        problems.append(f"{path}: artifact must be a non-empty relative path")

    if not frag.get("release_asset"):
        problems.append(f"{path}: release_asset must be the non-empty filename uploaded to the Release")

    return problems


def check_fragments(fragments_dir: Path) -> list[str]:
    """Validate every fragment in `fragments_dir`; return all problems found."""
    fragments = load_fragments(fragments_dir)
    problems = []
    seen_ids: dict[str, Path] = {}
    for path, frag in fragments:
        problems.extend(validate_fragment(path, frag))
        frag_id = frag.get("id")
        if frag_id:
            if frag_id in seen_ids:
                problems.append(f"{path}: duplicate id {frag_id!r} (also in {seen_ids[frag_id]})")
            else:
                seen_ids[frag_id] = path
    return problems


def _toml_string(s: str) -> str:
    """Render `s` as a quoted TOML basic string.

    TOML basic strings escape the same control characters as JSON string
    literals, so `json.dumps` produces a valid TOML string body for the plain
    ASCII content these fragments carry (ids, descriptions, commit shas, URLs).
    """
    return json.dumps(s)


def render_variant(frag: dict[str, Any], repo_slug: str, tag: str) -> str:
    lines = ["[[variant]]"]
    lines.append(f"id = {_toml_string(frag['id'])}")
    lines.append("deprecated = false")
    lines.append(f"description = {_toml_string(frag['description'])}")
    lines.append(f"repo = {_toml_string(frag['repo'])}")
    lines.append(f"source_commit = {_toml_string(frag['source_commit'])}")
    if frag.get("source_commit_note"):
        lines.append(f"source_commit_note = {_toml_string(frag['source_commit_note'])}")
    lines.append(f"port = {_toml_string(frag['port'])}")
    lines.append(f"board = {_toml_string(frag['board'])}")
    flags = ",\n    ".join(_toml_string(f) for f in frag["build_flags"])
    lines.append(f"build_flags = [\n    {flags},\n]" if frag["build_flags"] else "build_flags = []")
    caps = frag["capabilities"]
    cap_str = ", ".join(
        f"{k} = {json.dumps(v) if not isinstance(v, str) else _toml_string(v)}" for k, v in caps.items()
    )
    lines.append(f"capabilities = {{ {cap_str} }}")
    if frag.get("capabilities_note"):
        lines.append(f"capabilities_note = {_toml_string(frag['capabilities_note'])}")
    download_url = f"https://github.com/{repo_slug}/releases/download/{tag}/{frag['release_asset']}"
    lines.append(f"artifact = {_toml_string(frag['artifact'])}")
    lines.append(f"artifact_sha256 = {_toml_string(frag['artifact_sha256'])}")
    lines.append(f"download_url = {_toml_string(download_url)}")
    if frag.get("notes"):
        lines.append(f"notes = {_toml_string(frag['notes'])}")
    return "\n".join(lines) + "\n"


def assemble(fragments_dir: Path, repo_slug: str, tag: str) -> str:
    problems = check_fragments(fragments_dir)
    if problems:
        raise FragmentError("\n".join(problems))
    fragments = load_fragments(fragments_dir)
    blocks = [render_variant(frag, repo_slug, tag) for _path, frag in fragments]
    return "\n".join(blocks)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fragments-dir", required=True, type=Path, help="directory of *.json variant fragments")
    parser.add_argument("--check", action="store_true", help="validate the fragment set only; write nothing")
    parser.add_argument("--repo-slug", help="OWNER/REPO the Release assets live in (required unless --check)")
    parser.add_argument("--tag", help="Release tag the assets were published under (required unless --check)")
    parser.add_argument("--output", type=Path, help="write assembled TOML here instead of stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.check:
        try:
            problems = check_fragments(args.fragments_dir)
        except FragmentError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if problems:
            print(f"check FAILED: {len(problems)} problem(s):", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            return 1
        print(f"check OK: fragments in {args.fragments_dir} are valid")
        return 0

    if not args.repo_slug or not args.tag:
        print("error: --repo-slug and --tag are required unless --check is given", file=sys.stderr)
        return 2

    try:
        toml_text = assemble(args.fragments_dir, args.repo_slug, args.tag)
    except FragmentError as e:
        print(f"error:\n{e}", file=sys.stderr)
        return 1

    if args.output:
        args.output.write_text(toml_text)
        print(f"wrote {args.output}")
    else:
        print(toml_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
