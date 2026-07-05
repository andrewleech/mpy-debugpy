#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Firmware manifest tool (STORY-3.1): list, verify and fetch prebuilt MicroPython
artifacts described in firmware/firmware.toml.

The manifest is the single source of truth for a variant's provenance (source repo,
commit, build flags) and its expected artifact hash. This tool never infers
capability from a variant's id or directory name — it only reports what the
manifest records and whether the local file's hash matches it. Runtime capability
probing (STORY-1.2) is a separate, later concern.

Usage:
    uv run launcher/firmware.py list
    uv run launcher/firmware.py verify [VARIANT_ID ...]
    uv run launcher/firmware.py fetch VARIANT_ID [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - repo targets >=3.11 per script header
    import tomli as tomllib  # type: ignore[no-redef]

FIRMWARE_DIR = Path(__file__).resolve().parent.parent / "firmware"
MANIFEST_PATH = FIRMWARE_DIR / "firmware.toml"

# Recognisable "not a real URL yet" markers a download_url is allowed to carry
# instead of a working link. fetch() refuses to treat these as fetchable.
PLACEHOLDER_URL_PREFIXES = ("TODO://",)


def load_manifest(manifest_path: Path = MANIFEST_PATH) -> list[dict[str, Any]]:
    with open(manifest_path, "rb") as f:
        data = tomllib.load(f)
    return data.get("variant", [])


def find_variant(variants: list[dict[str, Any]], variant_id: str) -> dict[str, Any]:
    for v in variants:
        if v["id"] == variant_id:
            return v
    known = ", ".join(v["id"] for v in variants)
    raise SystemExit(f"error: unknown variant id {variant_id!r}. Known variants: {known}")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_placeholder_url(url: str) -> bool:
    return not url or url.startswith(PLACEHOLDER_URL_PREFIXES)


def cmd_list(args: argparse.Namespace) -> int:
    variants = load_manifest()
    for v in variants:
        caps = v.get("capabilities", {})
        cap_str = ", ".join(f"{k}={caps[k]}" for k in sorted(caps))
        url = v.get("download_url", "")
        url_str = url if url else "(none)"
        print(f"{v['id']}")
        print(f"  artifact:    {v['artifact']}")
        print(f"  port/board:  {v.get('port')}/{v.get('board')}")
        print(f"  repo:        {v.get('repo')}")
        print(f"  commit:      {v.get('source_commit')}")
        print(f"  flags:       {', '.join(v.get('build_flags', []))}")
        print(f"  capabilities:{cap_str}")
        print(f"  sha256:      {v.get('artifact_sha256')}")
        print(f"  download_url:{url_str}")
        print()
    return 0


def _verify_one(v: dict[str, Any]) -> bool:
    artifact_path = FIRMWARE_DIR / v["artifact"]
    expected = v["artifact_sha256"]
    if not artifact_path.exists():
        print(f"FAIL {v['id']}: artifact not found at {artifact_path}")
        return False
    actual = sha256_of(artifact_path)
    if actual != expected:
        print(f"FAIL {v['id']}: sha256 mismatch")
        print(f"     expected {expected}")
        print(f"     actual   {actual}")
        return False
    print(f"OK   {v['id']}: {artifact_path} matches manifest sha256")
    return True


def cmd_verify(args: argparse.Namespace) -> int:
    variants = load_manifest()
    if args.variant_id:
        selected = [find_variant(variants, vid) for vid in args.variant_id]
    else:
        selected = variants
    results = [_verify_one(v) for v in selected]
    ok = all(results)
    if not ok:
        print("\nverify FAILED: one or more artifacts missing or hash mismatch", file=sys.stderr)
    return 0 if ok else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    variants = load_manifest()
    v = find_variant(variants, args.variant_id)
    url = v.get("download_url", "")
    artifact_path = FIRMWARE_DIR / v["artifact"]

    if artifact_path.exists() and not args.force:
        if sha256_of(artifact_path) == v["artifact_sha256"]:
            print(f"{v['id']}: already present and verified at {artifact_path}, skipping fetch")
            return 0
        print(f"{v['id']}: local artifact present but hash mismatch; re-fetching")

    if is_placeholder_url(url):
        print(
            f"error: {v['id']} has no real download_url in the manifest yet "
            f"(got {url!r}). Publish a release asset and update firmware.toml, "
            "or build locally and place the artifact at "
            f"{artifact_path} before running verify.",
            file=sys.stderr,
        )
        return 2

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"fetching {v['id']} from {url} ...")
    with urllib.request.urlopen(url) as resp, open(artifact_path, "wb") as out:
        out.write(resp.read())

    actual = sha256_of(artifact_path)
    if actual != v["artifact_sha256"]:
        artifact_path.unlink(missing_ok=True)
        print(
            f"error: downloaded artifact for {v['id']} failed sha256 verification "
            f"(expected {v['artifact_sha256']}, got {actual}); file removed",
            file=sys.stderr,
        )
        return 1

    print(f"OK   {v['id']}: fetched and verified at {artifact_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list firmware variants recorded in the manifest")

    p_verify = sub.add_parser("verify", help="verify local artifact(s) against the manifest sha256")
    p_verify.add_argument("variant_id", nargs="*", help="variant id(s) to verify; default: all")

    p_fetch = sub.add_parser("fetch", help="download and verify an artifact from its manifest URL")
    p_fetch.add_argument("variant_id", help="variant id to fetch")
    p_fetch.add_argument("--force", action="store_true", help="re-fetch even if a verified local copy exists")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "list":
        return cmd_list(args)
    if args.command == "verify":
        return cmd_verify(args)
    if args.command == "fetch":
        return cmd_fetch(args)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
