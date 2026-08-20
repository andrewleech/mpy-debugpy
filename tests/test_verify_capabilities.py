"""Unit tests for `.github/scripts/verify_capabilities.py`.

Round-2 finding (2): rp2/stm32/esp32 fragments must not claim `settrace`/
`save_names` without evidence from the build's own resolved macros. These
tests cover both introspection paths without needing a real cross-compiler:
a `%.pp` file (classic-Make ports) and a stubbed `compile_commands.json`
entry whose "compiler" is a small script that just echoes canned `#define`
output for `-dM` (CMake-based ports).
"""

import json
import stat
import sys
from pathlib import Path


_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / ".github" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import verify_capabilities  # noqa: E402


def _write_fake_compiler(path: Path, macros: dict[str, str]) -> None:
    lines = "\n".join(f'echo "#define {name} {value}"' for name, value in macros.items())
    path.write_text(f"#!/bin/sh\n{lines}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


# --- macros_from_text -------------------------------------------------------


def test_macros_from_text_keeps_last_definition_on_redefinition():
    text = "#define X (0)\nsome code\n#define X (1)\n"
    assert verify_capabilities.macros_from_text(text) == {"X": "(1)"}


# --- macros_from_pp_file ----------------------------------------------------


def test_macros_from_pp_file_reads_defines(tmp_path):
    pp = tmp_path / "modsys.pp"
    pp.write_text("#define MICROPY_PY_SYS_SETTRACE (1)\n#define MICROPY_PY_SYS_SETTRACE_LOCALNAMES (1)\n")
    macros = verify_capabilities.macros_from_pp_file(pp)
    assert macros["MICROPY_PY_SYS_SETTRACE"] == "(1)"
    assert macros["MICROPY_PY_SYS_SETTRACE_LOCALNAMES"] == "(1)"


# --- macros_from_compile_commands -------------------------------------------


def test_macros_from_compile_commands_strips_c_and_o_flags(tmp_path):
    fake_cc = tmp_path / "fake-cc.sh"
    _write_fake_compiler(fake_cc, {"MICROPY_PY_SYS_SETTRACE": "(1)"})
    cc_file = tmp_path / "compile_commands.json"
    cc_file.write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "command": f"{fake_cc} -DFOO=1 -c -o py/modsys.o py/modsys.c",
                    "file": "/abs/path/py/modsys.c",
                }
            ]
        )
    )
    macros = verify_capabilities.macros_from_compile_commands(cc_file, "py/modsys.c")
    assert macros["MICROPY_PY_SYS_SETTRACE"] == "(1)"


def test_macros_from_compile_commands_no_matching_source_raises(tmp_path):
    cc_file = tmp_path / "compile_commands.json"
    cc_file.write_text(json.dumps([{"directory": str(tmp_path), "command": "cc -c x.c", "file": "x.c"}]))
    try:
        verify_capabilities.macros_from_compile_commands(cc_file, "py/modsys.c")
        assert False, "expected SystemExit"
    except SystemExit:
        pass


# --- main: pass/fail --------------------------------------------------------


def test_main_passes_when_macros_match_expectation(tmp_path, capsys):
    pp = tmp_path / "modsys.pp"
    pp.write_text("#define MICROPY_PY_SYS_SETTRACE (1)\n#define MICROPY_PY_SYS_SETTRACE_LOCALNAMES (1)\n")
    rc = verify_capabilities.main(
        [
            "--pp-file",
            str(pp),
            "--expect",
            "MICROPY_PY_SYS_SETTRACE=1",
            "MICROPY_PY_SYS_SETTRACE_LOCALNAMES=1",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "MICROPY_PY_SYS_SETTRACE=true" in out
    assert "MICROPY_PY_SYS_SETTRACE_LOCALNAMES=true" in out


def test_main_fails_when_macro_resolves_to_zero(tmp_path, capsys):
    pp = tmp_path / "modsys.pp"
    pp.write_text("#define MICROPY_PY_SYS_SETTRACE_LOCALNAMES (0)\n")
    rc = verify_capabilities.main(
        ["--pp-file", str(pp), "--expect", "MICROPY_PY_SYS_SETTRACE_LOCALNAMES=1"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "MICROPY_PY_SYS_SETTRACE_LOCALNAMES" in err


def test_main_fails_when_macro_never_defined(tmp_path):
    pp = tmp_path / "modsys.pp"
    pp.write_text("#define SOMETHING_ELSE (1)\n")
    rc = verify_capabilities.main(["--pp-file", str(pp), "--expect", "MICROPY_PY_SYS_SETTRACE=1"])
    assert rc == 1


def test_main_requires_source_with_compile_commands(tmp_path):
    cc_file = tmp_path / "compile_commands.json"
    cc_file.write_text("[]")
    rc = verify_capabilities.main(
        ["--compile-commands", str(cc_file), "--expect", "MICROPY_PY_SYS_SETTRACE=1"]
    )
    assert rc == 2
