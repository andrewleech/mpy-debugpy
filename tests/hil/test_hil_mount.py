"""STORY-4.3: debugging a file that exists only on the host, on real hardware.

The criterion is that a device target can debug source the device has never
been given: `mpremote debug --source <dir>` mounts that directory over the
board's serial port, the boot script imports the target from it, and the
breakpoint lands on a line no upload ever put on the board. The pty suite
(`tests/test_s4_3_mount_attach.py`) proves the mechanism against the unix
firmware; this proves it against a real filesystem-RPC link, which is where
the mount's read path actually lives.

The two halves of the mount contract are both hardware-visible here and
nowhere else: filesystem RPC shares the one serial port with the console, and
an abandoned mount leaves the board blocked in an RPC that Ctrl-C cannot
interrupt, because the interrupt is consumed as RPC payload. So this suite
also ends its session for real and then re-opens the port, which is the only
way to tell a mount that was torn down from one that wedged the board.

Runs under the same opt-in as the rest of `tests/hil/` (see `conftest.py`).
"""

import contextlib
import json
import os
import signal
import time

import pytest
from dapdrive import Recorder, run_to_breakpoint

# `mpremote` is importable here because `conftest.py` puts the submodule on
# sys.path when pytest loads it, ahead of collecting this module.
from mpremote.transport_serial import SerialTransport
from mpremote_debug import (
    SUBMODULE_DIR as _SUBMODULE_DIR,
    read_until as _read_until,
    spawn_debug as _spawn_debug,
)

# What the device mounts the host directory at, and so the remote half of the
# mapping the command generates. Read from the transport rather than written
# out, for the same reason no test here writes a device path.
MOUNT_POINT = SerialTransport.fs_hook_mount

# A module the board has never held a copy of, so reaching a breakpoint in it
# cannot be explained by anything already installed. `hil_facts` puts
# `debuggee.TARGET_MODULE` on the board; this is deliberately not that.
HOST_ONLY_MODULE = "hostonly_mounted"
HOST_ONLY_SRC = """\
def main():
    total = 0
    for i in range(3):
        total += i
    print("host-only total", total)
    return total
"""
BREAKPOINT_LINE = 4  # `total += i`, reached on the first pass


@contextlib.contextmanager
def _mount_session(device, source_dir):
    """`mpremote debug --source` against the board, up to its handshake.

    Unlike the network runs this one never detaches: a mounted transport
    answers filesystem RPC from any read, so the command has to hold the port
    and pump for the whole session. The process is signalled by the caller,
    not reaped here, because when and how the session ends is what the
    teardown scenario is about.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_SUBMODULE_DIR), env.get("PYTHONPATH")]))
    proc = _spawn_debug(
        ["debug", "--source", str(source_dir), device, f"{HOST_ONLY_MODULE}:main"],
        env=env,
        cwd=source_dir,
    )
    try:
        lines, matched = _read_until(proc, "MPDBG-READY ", timeout=90)
        if matched is None:
            pytest.fail(f"never saw MPDBG-READY; output:\n{''.join(lines)}")
        payload = json.loads(matched[matched.index("{") :])
        payload["command_output"] = "".join(lines)
        payload["process"] = proc
        yield payload
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=30)
        except Exception:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture()
def host_only_source(tmp_path, hil_device, hil_serial, hil_facts):
    """A source directory holding the debuggee, with the board proved ignorant.

    The absence check is what makes the breakpoint meaningful: without it a
    passing run could be importing a leftover copy from an earlier session,
    and the mount would be doing nothing.
    """
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / f"{HOST_ONLY_MODULE}.py").write_text(HOST_ONLY_SRC)

    with hil_serial() as transport:
        probe = transport.exec(
            "try:\n"
            f" import {HOST_ONLY_MODULE}\n"
            " print('PRESENT')\n"
            "except ImportError:\n"
            " print('ABSENT')\n"
        )
    assert probe.strip() == b"ABSENT", (
        f"the board can already import {HOST_ONLY_MODULE!r}, so a breakpoint in it "
        f"would prove nothing about the mount: {probe!r}"
    )
    return source_dir


def test_hil_mount_breakpoint_in_a_file_the_board_never_had(
    hil_device, hil_serial, host_only_source, record_property
):
    """The whole criterion in one scenario, ending with the board still usable.

    The client sends exactly the `pathMappings` the command generated - there
    is nothing else for it to send, since the board's filesystem has no notion
    of where the host directory lives - and sets its breakpoint on the host
    path, so the stop proves the translation in both directions.
    """
    source_dir = host_only_source
    local_app = str(source_dir / f"{HOST_ONLY_MODULE}.py")

    with _mount_session(hil_device, source_dir) as session:
        assert session["pathMappings"] == [
            {"localRoot": str(source_dir), "remoteRoot": MOUNT_POINT}
        ], (
            "the generated mapping must be the absolute source root, not a "
            f"placeholder a client fills in: {session}"
        )
        assert os.path.isabs(session["pathMappings"][0]["localRoot"]), session

        server = Recorder("hil-mount", session["host"], session["port"], connect_timeout=20)
        server.start()
        try:
            assert (
                server.wait(
                    lambda m: m.type == "response" and m.command == "initialize", timeout=20
                )
                is not None
            ), f"no initialize response from {session['host']}:{session['port']}"
            attach = server.request(
                "attach",
                {
                    "name": "Attach to MicroPython",
                    "type": "debugpy",
                    "request": "attach",
                    "connect": {"host": session["host"], "port": session["port"]},
                    "pathMappings": session["pathMappings"],
                    "justMyCode": True,
                },
            )
            assert attach.success, attach

            started = time.monotonic()
            stopped = run_to_breakpoint(server, local_app, BREAKPOINT_LINE, timeout=90)
            record_property("mount_breakpoint_seconds", round(time.monotonic() - started, 2))
            assert stopped.body["reason"] == "breakpoint", stopped
            thread_id = stopped.body["threadId"]

            frames = server.request("stackTrace", {"threadId": thread_id}).body["stackFrames"]
            assert (frames[0]["name"], frames[0]["line"]) == ("main", BREAKPOINT_LINE), frames[0]
            assert frames[0]["source"]["path"] == local_app, (
                "the frame must come back as the host path the client asked about, "
                f"not the board's own view of it: {frames[0]}"
            )

            # Releasing the target is what lets teardown find a responsive
            # board below; a client that just vanishes leaves the program
            # parked in `wait_for_continue` with nothing reading the port.
            # Sent without waiting for a response: the server answers and ends
            # the session, so whether the response beats the close is a race
            # that says nothing about the mount.
            server.client.send_request("disconnect", {})
        finally:
            server.stop()
        time.sleep(1.0)

        proc = session["process"]
        proc.send_signal(signal.SIGTERM)
        exit_code = proc.wait(timeout=30)
        trailing, _ = _read_until(proc, None, timeout=2)
        output = session["command_output"] + "".join(trailing)

    assert exit_code == 0, f"a signalled mount session is a normal exit; output:\n{output}"
    assert "warning:" not in output, (
        f"a session whose client left first has a responsive board to unmount, so "
        f"teardown has nothing to report; output:\n{output}"
    )

    # The claim the ticket actually makes about teardown: a following
    # connection works, with no power cycle in between. An unmounted board is
    # the only board that can answer this.
    with hil_serial() as transport:
        assert transport.exec("print(6 * 7)").strip() == b"42"
