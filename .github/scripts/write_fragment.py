#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Write one firmware manifest fragment (STORY-3.2) as JSON.

Invoked once per build job in `.github/workflows/firmware.yml`, after that
job has built and hashed its firmware artifact, to record the fields
`launcher/gen_manifest.py` later assembles into a `firmware.toml`
`[[variant]]` entry. Takes no dependencies beyond the standard library so it
runs identically in the plain-Ubuntu build jobs and inside the esp32
container image, with no `uv`/venv setup of its own.
"""

from __future__ import annotations

import argparse
import json

# The manifest's capability vocabulary. `launcher/gen_manifest.py` validates
# assembled fragments against the same set and `launcher/firmware.py` resolves
# `select --need` from it; `tests/test_gen_manifest.py` holds the three to each
# other, since a key accepted here and rejected there fails the release job
# after the artifacts are already built.
KNOWN_CAPABILITIES = ("settrace", "save_names", "set_local", "f_back")

# The capabilities with no build-time evidence gate, and the value that stands
# in. Write-back (`set_local`) is not built into any v1 debug-firmware variant.
# `settrace` and `save_names` are deliberately absent from this
# table - every caller must supply them from a verify_capabilities.py check
# against the build's resolved macros.
DEFAULT_CAPABILITIES = {"set_local": False, "f_back": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--build-flags", nargs="+", required=True)
    parser.add_argument("--artifact", required=True, help="local cache path, e.g. '<id>/firmware.bin'")
    parser.add_argument("--release-asset", required=True, help="filename uploaded to the GitHub Release")
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--capabilities-note", default=None)
    parser.add_argument("--notes", default=None)
    parser.add_argument(
        "--capability",
        action="append",
        default=[],
        metavar="KEY=true|false",
        required=True,
        help="capability value derived from build evidence (repeatable; settrace and save_names required)",
    )
    parser.add_argument(
        "--toolchain",
        nargs="*",
        default=[],
        metavar="KEY=VALUE",
        help="toolchain identity strings (compiler/SDK/runner-image versions) to record verbatim",
    )
    parser.add_argument("--output", required=True, help="path to write the fragment JSON to")
    return parser


def _parse_toolchain(pairs: list[str]) -> dict[str, str]:
    toolchain = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        toolchain[key] = value
    return toolchain


def _parse_bool(value: str) -> bool:
    if value.lower() in ("true", "1"):
        return True
    if value.lower() in ("false", "0"):
        return False
    raise SystemExit(f"error: capability value {value!r} is not true/false")


def _parse_capabilities(pairs: list[str]) -> dict[str, bool | str]:
    capabilities: dict[str, bool | str] = dict(DEFAULT_CAPABILITIES)
    given = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        if key not in KNOWN_CAPABILITIES:
            raise SystemExit(f"error: unknown --capability {key!r}")
        given[key] = _parse_bool(value)
    missing = {"settrace", "save_names"} - given.keys()
    if missing:
        raise SystemExit(f"error: --capability must include {sorted(missing)} (derived from build evidence)")
    capabilities.update(given)
    return capabilities


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    frag = {
        "id": args.id,
        "description": args.description,
        "repo": args.repo,
        "source_commit": args.source_commit,
        "port": args.port,
        "board": args.board,
        "build_flags": args.build_flags,
        "capabilities": _parse_capabilities(args.capability),
        "artifact": args.artifact,
        "release_asset": args.release_asset,
        "artifact_sha256": args.artifact_sha256,
    }
    if args.capabilities_note:
        frag["capabilities_note"] = args.capabilities_note
    if args.notes:
        frag["notes"] = args.notes
    if args.toolchain:
        frag["toolchain"] = _parse_toolchain(args.toolchain)

    with open(args.output, "w") as f:
        json.dump(frag, f, indent=2)
    print(json.dumps(frag, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
