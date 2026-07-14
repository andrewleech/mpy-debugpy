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
    parser.add_argument("--output", required=True, help="path to write the fragment JSON to")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Capability intent is identical across all four v1 debug-firmware
    # variants (settled in STORY-3.2's scope): settrace + real local-variable
    # names are built in; write-back (`set_local`) is not.
    frag = {
        "id": args.id,
        "description": args.description,
        "repo": args.repo,
        "source_commit": args.source_commit,
        "port": args.port,
        "board": args.board,
        "build_flags": args.build_flags,
        "capabilities": {
            "settrace": True,
            "save_names": True,
            "set_local": False,
            "f_back": True,
        },
        "artifact": args.artifact,
        "release_asset": args.release_asset,
        "artifact_sha256": args.artifact_sha256,
    }
    if args.capabilities_note:
        frag["capabilities_note"] = args.capabilities_note
    if args.notes:
        frag["notes"] = args.notes

    with open(args.output, "w") as f:
        json.dump(frag, f, indent=2)
    print(json.dumps(frag, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
