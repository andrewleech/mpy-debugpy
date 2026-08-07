"""Host-side pytest coverage for the `mpdebug.toml` named-target resolver.

Pure-host unit tests against `mpremote.mpdebug_config` directly: no device,
no subprocess. `resolve_target(name, start_dir=...)` takes `start_dir`
explicitly so discovery can be pointed at a tmp_path tree instead of the
real cwd.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import mpremote_debug

_TOP_DIR = Path(__file__).resolve().parents[1]
_SUBMODULE_DIR = mpremote_debug.SUBMODULE_DIR

if str(_SUBMODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SUBMODULE_DIR))

_LAUNCHER_DIR = str(_TOP_DIR / "launcher")
if _LAUNCHER_DIR not in sys.path:
    sys.path.insert(0, _LAUNCHER_DIR)

from mpremote import commands, mpdebug_config  # noqa: E402
from mpremote.commands import CommandError  # noqa: E402

import firmware  # noqa: E402
import gen_manifest  # noqa: E402


def _mpremote_cmd(args, cwd, env=None):
    """Run mpremote with the given arguments and cwd; return (code, stdout, stderr)."""
    env = dict(env if env is not None else os.environ, PYTHONPATH=str(_SUBMODULE_DIR))
    result = subprocess.run(
        [sys.executable, "-m", "mpremote"] + args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout, result.stderr


def _write(tmp_path, text):
    (tmp_path / "mpdebug.toml").write_text(text)
    return tmp_path


def test_resolves_named_target(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-MicroPython_Board-if00"
        firmware = "unix-settrace-localnames"
        requires = ["settrace", "save_names"]
        program = "app:run"
        """,
    )
    target = mpdebug_config.resolve_target("pico", start_dir=tmp_path)
    assert target.kind == "serial"
    assert target.device == "/dev/serial/by-id/usb-MicroPython_Board-if00"
    assert target.firmware == "unix-settrace-localnames"
    assert target.requires == ["settrace", "save_names"]
    assert target.program == "app:run"


def test_serial_by_id_device_passes_through_without_warning(tmp_path, capsys):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-MicroPython_Board-if00"
        """,
    )
    target = mpdebug_config.resolve_target("pico", start_dir=tmp_path)
    assert target.device == "/dev/serial/by-id/usb-MicroPython_Board-if00"
    assert capsys.readouterr().err == ""


def test_dev_tty_device_resolves_without_warning(tmp_path, capsys):
    """resolve_target itself is output-free; the tty warning is do_debug's job."""
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/ttyUSB0"
        """,
    )
    target = mpdebug_config.resolve_target("pico", start_dir=tmp_path)
    assert target.device == "/dev/ttyUSB0"
    assert capsys.readouterr().err == ""


def test_no_name_picks_sole_target(tmp_path):
    _write(
        tmp_path,
        """
        [target.only]
        kind = "unix"
        """,
    )
    target = mpdebug_config.resolve_target(None, start_dir=tmp_path)
    assert target.name == "only"


def test_no_name_with_several_targets_is_ambiguous(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-a"

        [target.esp32]
        kind = "network"
        device = "/dev/serial/by-id/usb-b"
        """,
    )
    with pytest.raises(CommandError, match=r"defines several; choose one of: esp32, pico"):
        mpdebug_config.resolve_target(None, start_dir=tmp_path)


def test_absent_config_and_no_name_errors(tmp_path):
    assert mpdebug_config.find_config(tmp_path) is None, (
        "precondition: a real mpdebug.toml above tmp_path would invalidate this test"
    )
    with pytest.raises(CommandError, match="no target given and no mpdebug.toml found"):
        mpdebug_config.resolve_target(None, start_dir=tmp_path)


def test_absent_config_with_name_returns_none_for_literal_fallback(tmp_path):
    # No mpdebug.toml anywhere above tmp_path: a raw connect string must
    # keep working exactly as it did before named targets existed.
    assert mpdebug_config.find_config(tmp_path) is None, (
        "precondition: a real mpdebug.toml above tmp_path would invalidate this test"
    )
    assert mpdebug_config.resolve_target("/dev/does-not-exist", start_dir=tmp_path) is None


def test_unknown_name_errors_listing_available_names(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-a"
        """,
    )
    # A name that is not a configured target is a connect string, so resolution
    # declines it; target_hint supplies the "did you mean" text the caller adds
    # if the transport then rejects it.
    assert mpdebug_config.resolve_target("nope", start_dir=tmp_path) is None
    hint = mpdebug_config.target_hint("nope", start_dir=tmp_path)
    assert "pico" in hint, hint
    assert not mpdebug_config.target_hint("pico", start_dir=tmp_path), (
        "a real target name needs no hint"
    )


def test_unknown_capability_in_requires_errors_at_resolve_time(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-a"
        requires = ["settrace", "typo_cap"]
        """,
    )
    with pytest.raises(CommandError, match="unknown capability 'typo_cap'"):
        mpdebug_config.resolve_target("pico", start_dir=tmp_path)


def test_kind_outside_enum_errors(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "bluetooth"
        device = "/dev/serial/by-id/usb-a"
        """,
    )
    with pytest.raises(CommandError, match="expected one of"):
        mpdebug_config.resolve_target("pico", start_dir=tmp_path)


def test_serial_target_without_device_errors(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        """,
    )
    with pytest.raises(CommandError, match="no 'device'"):
        mpdebug_config.resolve_target("pico", start_dir=tmp_path)


def test_duplicate_target_name_errors(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-a"

        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-b"
        """,
    )
    with pytest.raises(CommandError, match="invalid TOML"):
        mpdebug_config.resolve_target("pico", start_dir=tmp_path)


def test_empty_file_errors(tmp_path):
    _write(tmp_path, "")
    with pytest.raises(CommandError, match="defines no"):
        mpdebug_config.resolve_target(None, start_dir=tmp_path)
    with pytest.raises(CommandError, match="defines no"):
        mpdebug_config.resolve_target("pico", start_dir=tmp_path)


def test_nearest_config_wins_over_parent(tmp_path):
    _write(
        tmp_path,
        """
        [target.parent_only]
        kind = "unix"
        """,
    )
    child = tmp_path / "sub"
    child.mkdir()
    _write(
        child,
        """
        [target.child_only]
        kind = "unix"
        """,
    )
    target = mpdebug_config.resolve_target(None, start_dir=child)
    assert target.name == "child_only"

    # From the parent, only the parent's file is visible.
    target = mpdebug_config.resolve_target(None, start_dir=tmp_path)
    assert target.name == "parent_only"


def test_unknown_keys_in_target_table_are_tolerated(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "unix"
        icon = "raspberry"
        order = 1
        """,
    )
    target = mpdebug_config.resolve_target("pico", start_dir=tmp_path)
    assert target.kind == "unix"


def test_network_target_device_is_optional(tmp_path):
    _write(
        tmp_path,
        """
        [target.esp32]
        kind = "network"
        """,
    )
    target = mpdebug_config.resolve_target("esp32", start_dir=tmp_path)
    assert target.device is None


def test_cli_bare_debug_with_no_config_errors(tmp_path):
    """`mpremote debug` with nothing to resolve names the problem, not a traceback."""
    code, stdout, stderr = _mpremote_cmd(["debug"], cwd=tmp_path)
    assert code != 0
    assert "no target given and no mpdebug.toml found" in stderr
    assert "Traceback" not in stderr


def test_cli_bare_debug_lists_names_when_ambiguous(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "unix"

        [target.esp32]
        kind = "unix"
        """,
    )
    code, stdout, stderr = _mpremote_cmd(["debug"], cwd=tmp_path)
    assert code != 0
    assert "esp32" in stderr and "pico" in stderr
    assert "Traceback" not in stderr


def test_cli_resolves_named_unix_target_as_not_supported_yet(tmp_path):
    """A resolved target still goes through the same kind='unix' guard as the literal."""
    _write(
        tmp_path,
        """
        [target.sim]
        kind = "unix"
        """,
    )
    # The unix flow runs (s5.3), so resolution is proven by how far it gets:
    # past kind dispatch and into binary resolution, which fails here because
    # the environment names no built binary.
    env = {k: v for k, v in os.environ.items() if k != "MPY_DEBUG_FIRMWARE"}
    code, stdout, stderr = _mpremote_cmd(["debug", "sim"], cwd=tmp_path, env=env)
    assert code != 0
    assert "no unix debug binary found" in stderr, stderr
    assert "Traceback" not in stderr


def test_cli_unresolved_raw_connect_string_unaffected_by_absent_config(tmp_path):
    """Without an mpdebug.toml, a raw connect string behaves exactly as in s5.1."""
    env = {k: v for k, v in os.environ.items() if k != "MPY_DEBUG_FIRMWARE"}
    code, stdout, stderr = _mpremote_cmd(["debug", "unix"], cwd=tmp_path, env=env)
    assert code != 0
    assert "no unix debug binary found" in stderr, stderr
    assert "Traceback" not in stderr


def test_toml_import_fallback_when_neither_tomllib_nor_tomli_available(monkeypatch):
    """When both tomllib and tomli are missing, _toml_module reports an actionable error.

    Makes both names actually unimportable (rather than mocking `_toml_module`
    itself) so the real try/except/fallback body in `_toml_module` runs.
    """
    monkeypatch.setitem(sys.modules, "tomllib", None)
    monkeypatch.setitem(sys.modules, "tomli", None)
    with pytest.raises(
        CommandError,
        match=r"reading mpdebug\.toml needs a TOML parser.*Python >= 3\.11.*tomli",
    ):
        mpdebug_config._toml_module()


def test_unreadable_config_raises_command_error_not_traceback(tmp_path):
    """A permission error opening mpdebug.toml is reported cleanly, not as a traceback."""
    if os.geteuid() == 0:
        pytest.skip("root ignores file permission bits")
    _write(tmp_path, '[target.pico]\nkind = "unix"\n')
    path = tmp_path / "mpdebug.toml"
    path.chmod(0o000)
    try:
        with pytest.raises(CommandError, match="mpdebug.toml"):
            mpdebug_config.resolve_target("pico", start_dir=tmp_path)
    finally:
        path.chmod(0o644)


def test_non_string_device_errors(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = 1234
        """,
    )
    with pytest.raises(CommandError, match="device must be a string"):
        mpdebug_config.resolve_target("pico", start_dir=tmp_path)


def test_non_string_program_errors(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "unix"
        program = 7
        """,
    )
    with pytest.raises(CommandError, match="program must be a string"):
        mpdebug_config.resolve_target("pico", start_dir=tmp_path)


def test_non_string_firmware_errors(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "unix"
        firmware = 7
        """,
    )
    with pytest.raises(CommandError, match="firmware must be a string"):
        mpdebug_config.resolve_target("pico", start_dir=tmp_path)


def test_resolves_dap_device(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-MicroPython_Board-if00"
        dap_device = "/dev/serial/by-id/usb-MicroPython_Board-if02"
        """,
    )
    target = mpdebug_config.resolve_target("pico", start_dir=tmp_path)
    assert target.dap_device == "/dev/serial/by-id/usb-MicroPython_Board-if02"


def test_dap_device_defaults_to_none(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-MicroPython_Board-if00"
        """,
    )
    target = mpdebug_config.resolve_target("pico", start_dir=tmp_path)
    assert target.dap_device is None


def test_empty_dap_device_errors(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-MicroPython_Board-if00"
        dap_device = ""
        """,
    )
    with pytest.raises(CommandError, match="empty 'dap_device'"):
        mpdebug_config.resolve_target("pico", start_dir=tmp_path)


def test_non_string_dap_device_errors(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-MicroPython_Board-if00"
        dap_device = 1234
        """,
    )
    with pytest.raises(CommandError, match="dap_device must be a string"):
        mpdebug_config.resolve_target("pico", start_dir=tmp_path)


def test_known_capabilities_matches_wrapper_repo_copies():
    """mpremote's capability tuple must not drift from the wrapper repo's hand-copies.

    `mpdebug_config.KNOWN_CAPABILITIES` is a third hand-copy of the probe's
    key set (an upstreamable mpremote module can't import the wrapper's
    launcher); test_epic1_foundations.py separately checks this tuple against
    a live MPDBG-READY handshake.
    """
    assert mpdebug_config.KNOWN_CAPABILITIES == firmware.KNOWN_CAPABILITIES
    assert mpdebug_config.KNOWN_CAPABILITIES == gen_manifest.KNOWN_CAPABILITIES


def test_literal_connect_string_falls_back_when_config_present(tmp_path):
    """A config existing nearby doesn't stop an unrelated connect string from working."""
    _write(tmp_path, '[target.pico]\nkind = "unix"\n')
    assert mpdebug_config.resolve_target("/dev/ttyACM0", start_dir=tmp_path) is None


def test_reserved_words_fall_back_to_literal_when_not_targets(tmp_path):
    """'auto'/'list'/'unix' and 'a0'-style port shorthand aren't treated as typo'd names."""
    _write(tmp_path, '[target.pico]\nkind = "unix"\n')
    for literal in ("auto", "list", "unix", "a0", "u3", "c1"):
        assert mpdebug_config.resolve_target(literal, start_dir=tmp_path) is None


def test_named_target_still_takes_priority_over_reserved_word(tmp_path):
    """A target literally named e.g. 'unix' resolves as a target, not the literal fallback."""
    _write(
        tmp_path,
        """
        [target.unix]
        kind = "serial"
        device = "/dev/serial/by-id/usb-a"
        """,
    )
    target = mpdebug_config.resolve_target("unix", start_dir=tmp_path)
    assert target.name == "unix"


def test_typod_target_name_is_declined_and_hinted(tmp_path):
    """A typo'd name is handed on as a connect string, with the hint naming targets.

    Shape is not used to tell a target name from a connect string: a connect
    string can be a path, an id:/port: selector, a shortcut, or a bare device
    name like COM4, so any such test would misclassify one platform's devices.
    """
    _write(tmp_path, '[target.pico]\nkind = "unix"\n')
    assert mpdebug_config.resolve_target("picoo", start_dir=tmp_path) is None
    assert "pico" in mpdebug_config.target_hint("picoo", start_dir=tmp_path)


def test_find_config_stops_at_git_boundary(tmp_path):
    """A stray mpdebug.toml above a project's .git root is not discovered from inside it."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "sub"
    sub.mkdir()
    _write(tmp_path, '[target.outside]\nkind = "unix"\n')  # above the repo root

    assert mpdebug_config.find_config(sub) is None
    assert mpdebug_config.find_config(repo) is None


def test_find_config_stops_at_gitdir_file(tmp_path):
    """A worktree/submodule checkout's `.git` is a file (`gitdir: ...`), not a directory."""
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/repo\n")
    _write(tmp_path, '[target.outside]\nkind = "unix"\n')  # above the repo root

    assert mpdebug_config.find_config(sub) is None
    assert mpdebug_config.find_config(repo) is None


def test_nested_table_target_errors_naming_nesting(tmp_path):
    """[target.pico.sub] (a typo'd nested table) names the nesting, not 'kind None'."""
    _write(tmp_path, '[target.pico.sub]\nkind = "unix"\n')
    with pytest.raises(CommandError, match="nested table"):
        mpdebug_config.resolve_target("pico", start_dir=tmp_path)


def test_multiple_unknown_capabilities_all_named_in_error(tmp_path):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "unix"
        requires = ["typo_a", "typo_b"]
        """,
    )
    with pytest.raises(CommandError, match=r"'typo_a'.*'typo_b'"):
        mpdebug_config.resolve_target("pico", start_dir=tmp_path)


class _FakeTransport:
    """Minimal stand-in for SerialTransport, mirroring test_s5_1_mpremote_debug.py."""

    def __init__(self, lines, device_name="/dev/fake-tty"):
        self._lines = list(lines)
        self.exec_calls = []
        self.device_name = device_name

    def exec_raw_no_follow(self, command):
        self.exec_calls.append(command)

    def read_until(self, min_num_bytes, ending, timeout=10, data_consumer=None, timeout_overall=None):
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeState:
    def __init__(self, transport):
        self.transport = transport
        self._did_action = False

    def ensure_raw_repl(self, soft_reset=None):
        pass

    def did_action(self):
        self._did_action = True

    def run_repl_on_completion(self):
        return not self._did_action


def _args(target=None, program=None, port=None, dap_log=False, timeout=60):
    return type(
        "Args",
        (),
        {
            "target": target,
            "program": program,
            "port": port,
            "dap_log": dap_log,
            "dap_log_file": None,
            "timeout": timeout,
        },
    )()


def _handshake_transport(caps, device_name):
    # A real reported address, not the 0.0.0.0 wildcard: these tests are about
    # target resolution/requires-checking, not endpoint resolution - see
    # test_s5_1_mpremote_debug.py for the wildcard-handling coverage.
    handshake = {"host": "192.0.2.10", "port": 5678, "caps": caps}
    return _FakeTransport([("MPDBG-READY " + json.dumps(handshake) + "\n").encode()], device_name=device_name)


def test_do_debug_uses_targets_program_default_when_positional_omitted(tmp_path, monkeypatch):
    """No program positional: the resolved target's own `program` reaches the boot script."""
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-a"
        program = "app:run"
        """,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)
    transport = _handshake_transport({}, "/dev/serial/by-id/usb-a")
    state = _FakeState(transport)

    commands.do_debug(state, _args(target="pico"))

    assert len(transport.exec_calls) == 1
    assert "'app'" in transport.exec_calls[0] and "'run'" in transport.exec_calls[0]


def test_do_debug_cli_program_overrides_targets_default(tmp_path, monkeypatch):
    """A program positional on the CLI outranks the resolved target's `program`."""
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-a"
        program = "app:run"
        """,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)
    transport = _handshake_transport({}, "/dev/serial/by-id/usb-a")
    state = _FakeState(transport)

    commands.do_debug(state, _args(target="pico", program="cli:mod"))

    assert "'cli'" in transport.exec_calls[0] and "'mod'" in transport.exec_calls[0]


def test_do_debug_requires_vs_caps_hard_error(tmp_path, monkeypatch):
    """A target's `requires` failing against the handshake caps is a hard CommandError."""
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-a"
        requires = ["set_local"]
        """,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)
    transport = _handshake_transport({"set_local": False}, "/dev/serial/by-id/usb-a")
    state = _FakeState(transport)

    with pytest.raises(CommandError, match="requires set_local"):
        commands.do_debug(state, _args(target="pico", program="mod:main"))


def test_do_debug_requires_satisfied_by_caps_passes(tmp_path, monkeypatch, capsys):
    """A target's `requires` all present and true in caps lets the command succeed."""
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-a"
        requires = ["set_local"]
        """,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)
    transport = _handshake_transport({"set_local": True}, "/dev/serial/by-id/usb-a")
    state = _FakeState(transport)

    commands.do_debug(state, _args(target="pico", program="mod:main"))

    assert "debug server listening" in capsys.readouterr().out


@pytest.mark.parametrize("bad_caps", [None, [], "settrace", 5])
def test_do_debug_malformed_caps_shape_is_command_error_not_traceback(tmp_path, monkeypatch, bad_caps):
    """A handshake whose `caps` isn't a table is a CommandError, not an AttributeError."""
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-a"
        requires = ["settrace"]
        """,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)
    transport = _handshake_transport(bad_caps, "/dev/serial/by-id/usb-a")
    state = _FakeState(transport)

    with pytest.raises(CommandError, match="'caps' is not a table"):
        commands.do_debug(state, _args(target="pico", program="mod:main"))


def test_do_debug_rejects_non_boolean_cap_value(tmp_path, monkeypatch):
    """A non-boolean caps value (e.g. "unknown") is a schema violation, not a
    truthy pass: the handshake parser rejects it before `requires` ever sees it."""
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/serial/by-id/usb-a"
        requires = ["settrace"]
        """,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)
    transport = _handshake_transport({"settrace": "unknown"}, "/dev/serial/by-id/usb-a")
    state = _FakeState(transport)

    with pytest.raises(CommandError, match="not a table of booleans"):
        commands.do_debug(state, _args(target="pico", program="mod:main"))


def test_do_debug_warns_on_tty_device_from_resolved_target(tmp_path, monkeypatch, capsys):
    _write(
        tmp_path,
        """
        [target.pico]
        kind = "serial"
        device = "/dev/ttyUSB0"
        """,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)
    transport = _handshake_transport({}, "/dev/ttyUSB0")
    state = _FakeState(transport)

    commands.do_debug(state, _args(target="pico", program="mod:main"))

    assert "renumber" in capsys.readouterr().err


def test_do_debug_warns_on_tty_device_given_literally(tmp_path, monkeypatch, capsys):
    """The tty-device warning fires for a raw connect string too, not just a resolved target."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "do_connect", lambda state, device=None: None)
    transport = _handshake_transport({}, "/dev/ttyUSB0")
    state = _FakeState(transport)

    commands.do_debug(state, _args(target="/dev/ttyUSB0", program="mod:main"))

    assert "renumber" in capsys.readouterr().err


def test_bare_com_port_name_is_not_treated_as_a_target(tmp_path):
    """A Windows COM name resolves as a connect string even with a config present.

    `COM4` is a documented mpremote connect device, so classifying names by
    shape would have made every bare COM invocation fail as an unknown target
    on the platform where they are the norm.
    """
    _write(tmp_path, '[target.pico]\nkind = "unix"\n')
    for device in ("COM4", "com4", "COM12"):
        assert mpdebug_config.resolve_target(device, start_dir=tmp_path) is None, device


def test_id_and_port_prefixes_still_fall_back_to_literal(tmp_path):
    _write(tmp_path, '[target.pico]\nkind = "unix"\n')
    assert mpdebug_config.resolve_target("id:1234", start_dir=tmp_path) is None
    assert mpdebug_config.resolve_target("port:/dev/ttyUSB0", start_dir=tmp_path) is None
