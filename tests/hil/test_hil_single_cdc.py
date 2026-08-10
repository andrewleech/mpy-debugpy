"""STORY-6.1 criterion 2 on real hardware: one CDC interface, serial DAP asked for.

The mirror image of `test_hil_serial_dap.py`, and the same bench board. That
suite needs the board booted with two interfaces; this one needs it booted with
one, which is how every stm32 board comes up unless `boot.py` says otherwise
(`ports/stm32/main.c` applies `USBD_MODE_CDC_MSC` when boot.py set no mode). So
a PYBD-SF6W running the bench `board_boot.py` without its `pyb.usb_mode` line
is the board this scenario is about, and no second chip is needed to reach it.

Arranged by the operator, never from here: the USB mode is settled before the
first `mpremote` connection, exactly as the second interface is. The gate below
reads what the board actually did rather than trusting an environment variable,
so a bench still in two-interface mode skips instead of asserting the wrong
thing about the right board.

`tests/test_s6_1_no_second_cdc.py` covers every route to the refusal under the
unix build with `pyb` stubs; the one thing it cannot do is prove that a real
`pyb` on a real single-VCP boot takes one of them.
"""

import os
import subprocess
import time

import pytest
from conftest import BAUDRATE, DAP_DEVICE_ENV
from debuggee import TARGET_MODULE
from mpremote_debug import (
    SUBMODULE_DIR as _SUBMODULE_DIR,
    read_until as _read_until,
    spawn_debug as _spawn_debug,
)

# What the boot script says when the board cannot give it the interface the
# caller asked for. Asserted verbatim: it is the whole of what a user is told.
_REFUSAL = "no dedicated DAP interface on this board"

# Long enough that a hang is unmistakable and short enough that this scenario
# does not hold the bench: the refusal reaches the host as soon as the boot
# script exits, so anything near this is the failure the criterion names.
_TIMEOUT = 30


@pytest.fixture(scope="module")
def single_cdc_board(hil_device, hil_facts):
    """The board, having been booted with no second interface, or a skip.

    Two ways a board qualifies, and the boot script's probe treats them the
    same: a port with no `pyb` at all, and a `pyb` whose `usb_mode()` reports
    a single VCP. `hil_facts` reads both over the REPL, independently of any
    debug session.

    `MPY_DEBUG_HIL_DAP_DEVICE` being set is a contradiction rather than a
    second gate - it asserts the bench condition this scenario needs the
    absence of - so it fails rather than skipping.
    """
    if os.environ.get(DAP_DEVICE_ENV):
        pytest.fail(
            f"{DAP_DEVICE_ENV} is set, so the bench is arranged for the two-interface "
            "scenarios; this one needs a board booted with a single CDC interface"
        )
    usb_mode = hil_facts["usb_mode"]
    if usb_mode and "xVCP" in usb_mode:
        pytest.skip(
            f"the board enumerated a second CDC interface (usb_mode {usb_mode!r}); "
            "this scenario needs a boot with one, which is the stm32 default when "
            "boot.py calls no pyb.usb_mode()"
        )
    return hil_device


@pytest.fixture(scope="module")
def single_cdc_debug_run(single_cdc_board, tmp_path_factory):
    """Run `mpremote debug` against a target configured for serial DAP.

    The `dap_device` names a node that does not exist, which is the honest
    configuration here: on a board booted with one interface there is no
    second node to name, and a user who wrote this target for a two-interface
    boot has exactly this file after a plain reboot. The board's refusal has
    to come first regardless - `serial_dap.check_device` runs only after a
    successful handshake - so the error must be about the board and not about
    the path.

    One run for the module: the three assertions below are three claims about
    the same event, and re-running would put the board through a reset for
    each of them without asking it anything new.

    `at_line_start` is what makes "no handshake" observable here rather than
    everywhere else: this is the one scenario whose expected output contains
    mpremote's own quotation of the marker, so an unanchored search would
    report the absence as the thing itself.
    """
    tmp_path = tmp_path_factory.mktemp("single-cdc")
    absent_node = str(tmp_path / "no-such-dap-node")
    (tmp_path / "mpdebug.toml").write_text(
        "[target.hil]\n"
        'kind = "serial"\n'
        f'device = "{single_cdc_board}"\n'
        f'dap_device = "{absent_node}"\n'
        f'program = "{TARGET_MODULE}:main"\n'
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_SUBMODULE_DIR), env.get("PYTHONPATH")]))

    proc = _spawn_debug(["debug", "hil"], env=env, cwd=tmp_path)
    try:
        started = time.monotonic()
        lines, matched = _read_until(proc, "MPDBG-READY ", timeout=_TIMEOUT, at_line_start=True)
        try:
            returncode = proc.wait(timeout=_TIMEOUT)
        except subprocess.TimeoutExpired:
            returncode = None
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    return {
        "output": "".join(lines),
        "handshake": matched,
        "returncode": returncode,
        "elapsed": elapsed,
        "absent_node": absent_node,
    }


def test_hil_one_cdc_board_refuses_serial_dap(single_cdc_debug_run, record_property):
    """The command fails, saying which thing the board does not have.

    The elapsed time is part of the criterion, not decoration: "no hang" is
    the failure mode a user would actually hit, and it is indistinguishable
    from a clear message until the clock is looked at.
    """
    run = single_cdc_debug_run
    record_property("single_cdc_refusal_seconds", round(run["elapsed"], 2))

    assert run["handshake"] is None, f"the board should have reported no endpoint: {run['output']}"
    assert run["returncode"] not in (None, 0), (
        f"expected a failing exit, got {run['returncode']}; output:\n{run['output']}"
    )
    assert _REFUSAL in run["output"], f"the error should name the missing interface:\n{run['output']}"
    assert run["elapsed"] < _TIMEOUT, f"the refusal took {run['elapsed']:.1f}s"


def test_hil_the_refusal_names_the_board_not_the_missing_node(single_cdc_debug_run):
    """D5, and the criterion's "no misleading error".

    The device-side refusal happens before the host ever opens `dap_device`,
    so an error naming the absent node would mean the board had reported an
    endpoint it could not serve - the pre-D5 fall-through, which would leave
    the caller's bridge waiting on a stream that carries nothing.
    """
    run = single_cdc_debug_run

    assert run["absent_node"] not in run["output"], (
        f"the board refused before the node was reached, so nothing should point at it:\n{run['output']}"
    )
    assert "Debug server listening" not in run["output"], (
        f"a refused serial channel must not leave an endpoint behind:\n{run['output']}"
    )


def test_hil_the_board_survives_the_refusal(single_cdc_debug_run, single_cdc_board):
    """A script that ended, not a board that broke.

    Its own connection rather than `hil_serial`: this has to be a fresh one
    taken after the run, which is what proves the board is still there to
    talk to. The port is opened directly because a raw REPL is more than the
    question needs - a prompt is the whole answer.
    """
    import serial

    with serial.Serial(single_cdc_board, BAUDRATE, timeout=0.2) as port:
        port.write(b"\r\n")
        seen = b""
        deadline = time.monotonic() + 15
        while b">>>" not in seen:
            assert time.monotonic() < deadline, f"no REPL prompt after the refusal; read: {seen!r}"
            seen += port.read(4096)
