#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Assert a build's resolved sys.settrace macros match what the manifest
fragment is about to claim (STORY-3.2 finding (2), round 2).

The three device boards' `mpconfigboard.h` (the `debug_board_flags`
composition branch) are the only thing that turns MICROPY_PY_SYS_SETTRACE and
MICROPY_PY_SYS_SETTRACE_LOCALNAMES on; if an mbm rebase ever drops that
branch, the device ports build cleanly without them and the manifest would
otherwise keep claiming `save_names = true` with no evidence. This checks the
*compiled* configuration, not the source tree, using each build system's
existing macro-introspection support:

* CMake-based ports (rp2, esp32) emit `compile_commands.json`; look up one
  translation unit's exact compiler invocation and rerun it with -E -dM.
* Classic-Make ports (unix, stm32) generate a `<file>.pp` via the `%.pp`
  pattern rule in py/mkrules.mk, which already runs the file through cpp
  with -dD.

Exits nonzero (with a `::error::`-prefixed message per problem) if any
`--expect`ed macro is undefined or resolves to a different boolean. On
success, prints one `NAME=true`/`NAME=false` line per `--expect`ed macro
(suitable for appending to `$GITHUB_OUTPUT`).

`--report NAME=MACRO>=N` covers the capabilities whose evidence is a count
rather than a flag - `second_cdc` from `MICROPY_HW_USB_CDC_NUM` (Q12). It
asserts nothing: a macro no port in the build defines is reported false,
because for these the undefined case is the real answer and not a failure to
measure. A macro that is defined but does not evaluate to an integer is
still an error, since that is a value this cannot read either way.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

_DEFINE_RE = re.compile(r"^#define\s+(\S+)\s+(.*)$")


def _integer(value: str) -> int:
    v = value.strip()
    if v.startswith("(") and v.endswith(")"):
        v = v[1:-1].strip()
    try:
        return int(v, 0)
    except ValueError:
        raise SystemExit(f"error: macro value {value!r} is not a plain integer; can't evaluate capability")


def _truthy(value: str) -> bool:
    return _integer(value) != 0


def macros_from_text(text: str) -> dict[str, str]:
    """Return {macro: value}, keeping the LAST #define of each name (matches
    -dM's final-state semantics, and -dD's behaviour on redefinition)."""
    macros = {}
    for line in text.splitlines():
        m = _DEFINE_RE.match(line)
        if m:
            macros[m.group(1)] = m.group(2)
    return macros


def macros_from_compile_commands(cc_file: Path, source_suffix: str) -> dict[str, str]:
    entries = json.loads(cc_file.read_text())
    entry = next((e for e in entries if e["file"].endswith(source_suffix)), None)
    if entry is None:
        raise SystemExit(f"error: {cc_file}: no entry for a source path ending in {source_suffix!r}")
    args = entry["arguments"] if "arguments" in entry else shlex.split(entry["command"])

    cleaned = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "-c":
            continue
        if arg == "-o":
            skip_next = True
            continue
        cleaned.append(arg)
    cleaned += ["-E", "-dM"]

    result = subprocess.run(cleaned, cwd=entry.get("directory", "."), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"error: preprocessing {source_suffix} failed:\n{result.stderr}")
    return macros_from_text(result.stdout)


def macros_from_pp_file(pp_file: Path) -> dict[str, str]:
    return macros_from_text(pp_file.read_text())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--compile-commands", type=Path, help="compile_commands.json to look up --source in (CMake-based ports)"
    )
    source_group.add_argument(
        "--pp-file", type=Path, help="a %%.pp file already produced by py/mkrules.mk (classic-Make ports)"
    )
    parser.add_argument("--source", help="source path suffix to match in --compile-commands, e.g. py/modsys.c")
    parser.add_argument(
        "--expect",
        nargs="+",
        required=True,
        metavar="MACRO=0|1",
        help="macro name and the boolean value it must resolve to",
    )
    parser.add_argument(
        "--report",
        nargs="+",
        default=[],
        metavar="NAME=MACRO>=N",
        help="report NAME as the truth of a threshold on a counted macro; asserts nothing",
    )
    return parser


def _report_value(spec: str, macros: dict[str, str]) -> tuple[str, bool]:
    name, _, comparison = spec.partition("=")
    macro, sep, threshold = comparison.partition(">=")
    if not name or not sep or not macro.strip() or not threshold.strip():
        raise SystemExit(f"error: --report {spec!r} is not NAME=MACRO>=N (only >= is supported)")
    macro = macro.strip()
    if macro not in macros:
        return name, False
    return name, _integer(macros[macro]) >= _integer(threshold)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.compile_commands and not args.source:
        print("error: --source is required with --compile-commands", file=sys.stderr)
        return 2

    if args.compile_commands:
        macros = macros_from_compile_commands(args.compile_commands, args.source)
    else:
        macros = macros_from_pp_file(args.pp_file)

    problems = []
    results: dict[str, bool] = {}
    for pair in args.expect:
        name, _, want = pair.partition("=")
        want_bool = _truthy(want)
        if name not in macros:
            problems.append(f"{name} was never #defined (expected {want_bool})")
            continue
        got_bool = _truthy(macros[name])
        results[name] = got_bool
        if got_bool != want_bool:
            problems.append(f"{name} resolved to {macros[name]!r} ({got_bool}), expected {want_bool}")

    if problems:
        print("::error::capability macro verification failed - see below", file=sys.stderr)
        for p in problems:
            print(f"::error::  {p}", file=sys.stderr)
        return 1

    # After the assertions, so a build that fails one reports that rather than
    # a threshold it cannot read.
    for spec in args.report:
        name, value = _report_value(spec, macros)
        results[name] = value

    for name, value in results.items():
        print(f"{name}={'true' if value else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
