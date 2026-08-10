"""Q12: the handshake reports the build's CDC maximum, not the boot-time choice.

`caps["second_cdc"]` is what lets the firmware manifest claim a board can run
DAP over an interface of its own (STORY-6.1) without breaking STORY-3.3's rule
that the manifest may never claim what a probe would contradict. The property
it names is a *build* maximum - `MICROPY_HW_USB_CDC_NUM` - and a board can
answer three different ways on one run: built for two interfaces, booted with
one enumerated, with no host holding either.

`pyb.usb_mode()` reports the middle one, so the probe deliberately does not
read it. These drive the shipped boot script under the real unix firmware with
a `pyb` stub on `MICROPYPATH`, in both directions: a stub whose `usb_mode()`
says one interface while a second `USB_VCP` constructs must report True, and a
stub whose `usb_mode()` says two while the second `USB_VCP` refuses must report
False. Either alone would still pass if the probe read the wrong one.

The stubs are what a `pyb` cannot be faked out of: `pyb.USB_VCP(id)` raising
`ValueError` for `id >= MICROPY_HW_USB_CDC_NUM` is the C constructor's own
behaviour (`ports/stm32/usb.c`), measured on a PYBD-SF6W in both boot modes.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

_TOP_DIR = Path(__file__).resolve().parents[1]
_MICROPYTHON = Path(
    os.environ.get(
        "MPY_DEBUG_FIRMWARE",
        _TOP_DIR / "micropython/ports/unix/build-standard/micropython",
    )
)
_BOOT_SCRIPT = _TOP_DIR / "launcher" / "mpy_launch_debugpy.py"
_BASE_PATH = "{}:{}:{}".format(
    _TOP_DIR / "src",
    _TOP_DIR / "micropython-lib/python-ecosys/debugpy",
    _TOP_DIR / "micropython-lib",
)

requires_unix_firmware = pytest.mark.skipif(
    not _MICROPYTHON.exists(),
    reason=f"unix firmware not built at {_MICROPYTHON} (see `make firmware-unix`)",
)

# One `pyb` stub per shape the probe has to tell apart. Each reports a
# `usb_mode` that contradicts its own interface count, so a probe reading
# `usb_mode` gets every case backwards and a probe reading the constructor
# gets every case right.
_STUBS = {
    # Built for two, booted with one enumerated - a real PYBD_SF6 with a
    # single-VCP `boot.py`, which is how this was measured on hardware.
    "two_cdc_one_enumerated": """
class USB_VCP:
    def __init__(self, id=0):
        if id >= 2:
            raise ValueError


def usb_mode():
    return "VCP+MSC"
""",
    # Built for one. `usb_mode` claiming otherwise cannot conjure an
    # interface the firmware has no state for.
    "one_cdc": """
class USB_VCP:
    def __init__(self, id=0):
        if id >= 1:
            raise ValueError


def usb_mode():
    return "2xVCP+MSC"
""",
    # A `pyb` built without USB at all: no `USB_VCP` to construct.
    "no_usb": """
def usb_mode():
    return None
""",
}


def _handshake(stub_dir, port):
    """Launch the shipped boot script and return its MPDBG-READY payload.

    `stub_dir` goes first on `MICROPYPATH` so a `pyb.py` there is what the
    probe imports. The run is killed as soon as the handshake is read: the
    script blocks in `wait_for_client()` next, and nothing here is a client.
    """
    env = os.environ.copy()
    env["MICROPYPATH"] = f"{stub_dir}:{_BASE_PATH}"
    proc = subprocess.Popen(
        [str(_MICROPYTHON), str(_BOOT_SCRIPT), "target", "main", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 30
        lines = []
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if line.startswith("MPDBG-READY "):
                return json.loads(line[len("MPDBG-READY ") :])
        pytest.fail("no MPDBG-READY line; output:\n" + "".join(lines))
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture
def stub_pyb(tmp_path):
    """Write one of the `pyb` stubs into a directory of its own."""

    def _write(name):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "pyb.py").write_text(_STUBS[name])
        return directory

    return _write


@requires_unix_firmware
def test_second_cdc_is_true_when_the_second_interface_is_only_built_in(stub_pyb):
    """The claim survives a boot that enumerates one interface.

    The case Q12 was stuck on: `usb_mode()` says `VCP+MSC`, and the answer is
    still True, because the manifest describes a firmware and the firmware has
    the interface whether or not this boot brought it up.
    """
    payload = _handshake(stub_pyb("two_cdc_one_enumerated"), 5691)

    assert payload["caps"]["second_cdc"] is True, payload["caps"]


@requires_unix_firmware
def test_second_cdc_is_false_when_the_build_has_one_interface(stub_pyb):
    """And a `usb_mode` claiming two does not talk the probe into it."""
    payload = _handshake(stub_pyb("one_cdc"), 5692)

    assert payload["caps"]["second_cdc"] is False, payload["caps"]


@requires_unix_firmware
def test_second_cdc_is_false_on_a_pyb_without_usb(stub_pyb):
    """A `pyb` with no `USB_VCP` reports the absence rather than failing."""
    payload = _handshake(stub_pyb("no_usb"), 5693)

    assert payload["caps"]["second_cdc"] is False, payload["caps"]


@requires_unix_firmware
def test_second_cdc_is_false_on_a_runtime_with_no_pyb(tmp_path):
    """The unix port's own answer, with nothing stubbed in.

    Every port but stm32 lands here today, so this is the value the current
    manifest entries carry - and the reason a port that grows a second CDC has
    to reach this probe before its manifest may say so.
    """
    payload = _handshake(tmp_path, 5694)

    assert payload["caps"]["second_cdc"] is False, payload["caps"]


@requires_unix_firmware
def test_second_cdc_is_reported_separately_from_the_channel_taken(stub_pyb):
    """Having the interface is not the same claim as using it.

    A board with a second CDC that is debugged over TCP has `second_cdc` true
    and `serial_dap` false in the same handshake. Collapsing the two - which
    is what Q12 ruled out - would make the manifest unable to describe the
    board without describing a session.
    """
    caps = _handshake(stub_pyb("two_cdc_one_enumerated"), 5695)["caps"]

    assert (caps["second_cdc"], caps["serial_dap"]) == (True, False), caps
