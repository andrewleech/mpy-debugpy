# The pty-mounted session's intermittent teardown failure is a termios race

- Date: 2026-08-10
- Top repo HEAD: `c38f5a9`
- `micropython`: `67149a9c1f`
- `micropython-lib`: `3bd6c44`

Closes the risk-register row opened 2026-08-09 for
`test_do_debug_mount_over_real_pty_round_trips_and_tears_down_silently`, which
failed intermittently at `assert_device_still_usable()` with `TransportError:
timeout waiting for first EOF reception`. The row's own prescription was
followed - instrument what the device said rather than raise the sleep - and
the instrumentation is what found this. The same defect was then found in
`test_s4_5_hot_reload.py`, which brings its device up the same way.

## What the failing run looked like

The check writes the raw-REPL sequence and reads back:

```
wrote  b'\r\x03'  b'\r\x01'   b'\x05A\x01'   b'print(1)'  b'\x04'
read   b'\n\n>>> \n\n>>> \n\n>>> \n\nraw REPL; CTRL-B to exit\r\n>'
       b'ra' ... b'raw REPL; CTRL-B to exit\r\n>'
       b'OK' b'1' b'\n' b'\x00' b'\x00' b'>'
```

Three anomalies, all in one run:

1. The prompts arrive as `\n\n>>> ` where a healthy device sends `\r\n>>> `.
2. `\x05A\x01` is answered with the raw-REPL banner instead of the raw-paste
   handshake `R\x01`, i.e. the device saw a third byte in its line buffer.
3. `print(1)` returns `OK1\n\x00\x00>`. The two `\x04` markers that end
   raw-REPL output arrive as NUL, so `follow()` never sees its first EOF and
   times out - the error the test reports.

A probe issued after the timeout showed the device alive and answering, but
emitting five `>>> ` prompts for a single `\r`: input nobody wrote.

## Mechanism

The device is a unix-port `micropython` holding the pty **master**; mpremote
and the check open the **slave** by path. Two processes therefore manage one
line discipline, and they disagree about who owns it.

- `pty.openpty()` hands out a tty in the kernel's default **canonical** mode.
- The unix port's `mp_hal_stdio_mode_raw()` (`ports/unix/unix_mphal.c:104`)
  snapshots the *current* termios into a static `orig_termios` and applies a
  partial raw mode: it clears specific `c_iflag` bits and all of `c_lflag`,
  but never touches `c_oflag`.
- `pyexec_raw_repl` (`shared/runtime/pyexec.c:606-613`) drops back to
  `mp_hal_stdio_mode_orig()` **around every command it executes**, so that a
  running program can take SIGINT, and that is exactly the window in which
  `EXEC_FLAG_PRINT_EOF` writes the two `\x04` terminators.
- pyserial, on every open, sets its own fully raw termios on the same pty,
  discarding whatever the device had set.

If the device's snapshot was taken before pyserial first rawified the line -
which it is, because the interpreter reaches its REPL before anything opens
the slave - then every `mode_orig()` puts the pty back into canonical mode for
the duration of an exec. Canonical mode explains all three anomalies at once,
and each is a documented consequence rather than an inference:

- `ICRNL` maps the device's CR to NL: `\r\n>>> ` becomes `\n\n>>> `.
- `ICANON` makes `\x04` the VEOF character, so it terminates a line instead of
  reaching the reader as data.
- `ECHO` echoes what the master writes back to the master, so the device reads
  its own output as input - the third byte in the line buffer that refuses the
  raw-paste handshake, and the feedback loop that prints five prompts for one
  `\r`.

The residue is directly measurable: after a broken run the pty carries
`OPOST ONLCR` and nothing else, which is precisely canonical settings passed
through `mp_hal_stdio_mode_raw()`'s partial cleanup.

Whether a run breaks depends on the interleaving of the device's `mode_raw()`
calls with pyserial's `tcsetattr` - a race between two processes, hence
intermittent.

## Measurements

The reproducer at the end of this note provokes the failure with nothing but a
pty, a `micropython` process and the raw-REPL byte sequence: no mount, no DAP,
no harness. Signature byte-identical to the test's.

| configuration | broken |
| --- | --- |
| as the harness was | 17/250, 32/250 (7-13%) |
| holding a slave fd open across the gap | 32/250 |
| pty set raw before the interpreter is spawned | 0/800 |

The middle row refutes the first hypothesis this investigation pursued, and it
is worth recording because the evidence for it looked strong: with no slave fd
open, the device's `read(2)` on the master fails `EIO`, and
`MP_HAL_RETRY_SYSCALL` (`ports/unix/mphalport.h:93`) breaks out of its retry
loop on any non-`EINTR` error with an **empty raise clause**, leaving
`mp_hal_stdin_rx_chr` (`ports/unix/unix_mphal.c:175-181`) to return the
uninitialised `unsigned char c` to the REPL. A single test run does this
85,128 times, confirmed by `strace`. It is a real defect - uninitialised stack
read into the REPL's input, at ~250 kHz - but it is not this one: closing the
window changed nothing.

## Every harness that spawns this way had it, so the fix is shared

`test_s4_5_hot_reload.py`'s `_LoopSession` brings a device up the same way and
was failing the same check with the same `TransportError: timeout waiting for
first EOF reception` at `transport_serial.py:273`, at a higher rate than the
STORY-4.3 test - 1 in 12 runs and then 1 in 2 across two measured loops. It was
not found by this investigation; it was found by the full suite after the
STORY-4.3 fix landed, which is the argument for the shared module rather than
the same edit twice.

A sweep of the rest of the suite for the same shape then found seven more
sites. A harness is exposed when a firmware process holds a pty master **and**
something opens the slave through pyserial - `SerialTransport`, or an mpremote
subprocess - because pyserial rawifies the line on open, after the device has
already snapshotted it. Nine sites in four files now hold a `PtyDevice`:
`test_s4_3_mount_attach.py` and `test_s4_5_hot_reload.py` (one each),
`test_s5_1_mpremote_debug.py` (five), and `test_s6_1_serial_dap_bridge.py`
(two: the bridge's control plane, and the real-debugpy stream session, which
also passes the data-plane master down to the device).

Harnesses driven only by raw fds are not exposed and were left alone
(`test_s6_3_dap_log.py`, `test_s5_5_command_drive.py`,
`test_s6_2_network_flow.py`, `test_s6_1_stream_transport.py`): with nobody
rawifying the line, the snapshot the device restores is the one already in
force. So are the pty pairs with no firmware process on them, and the
pipe-based spawns.

`tests/pty_device.py` owns bringing the device up and checking it is still
usable:

- `tty.setraw(slave_fd)` before the interpreter is spawned, so every snapshot
  it can ever take is already raw and `mode_orig()` is a no-op.
- The slave fd is kept open for the session instead of being closed, which
  removes the `EIO` spin above along with its 85k syscalls.
- The `time.sleep(0.3)` readiness gate is replaced by reading that fd until
  the friendly-REPL prompt appears, stopping there so nothing mpremote is owed
  is consumed.
- `assert_usable()` records what it wrote and read and reports it on failure
  alongside whatever the caller passes as `context`, and asserts the pty is
  still raw afterwards - a direct guard on this mechanism, since every broken
  run left a cooked flag set.

One harness-specific change, in `_MountedPtySession.detach_client()`: the
unobservable `time.sleep(1.0)` is replaced by polling for a marker
`app.main()` writes back through the mount as its last act, whose content is
the loop's result, so the wait ends on the program having run out rather than
on a duration. `_LoopSession.detach_client()` keeps its sleep, because what it
waits for is the target leaving `wait_for_restart` *after* its own code has
finished: no marker the program could write observes that, and mpremote holds
the pty for the whole window.

## Left undone

The uninitialised-`c` return in `mp_hal_stdin_rx_chr` is an upstream unix-port
defect, reachable by anything that gives the interpreter a stdin that can fail
a read. It is unrelated to the composed branches, so fixing it here would mean
putting an unrelated port change on `pdb_support`. It belongs in its own
micropython PR; the reproducer is the script below with the `tty.setraw` line
removed, run under `strace -e trace=read` and counted for `EIO`.

## Reproducer

Run from the top of this repo with `uv run <this>.py`. Reports the corruption
rate; drop the `tty.setraw` line to see it, keep it to see it gone.

```python
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial"]
# ///
import collections, os, pty, signal, subprocess, sys, time, tty
import serial

MICROPYTHON = "micropython/ports/unix/build-standard/micropython"


def drain(port, quiet=0.05, limit=1.0):
    got, deadline, last = b"", time.monotonic() + limit, time.monotonic()
    while time.monotonic() < deadline:
        n = port.inWaiting()
        if n:
            got += port.read(n)
            last = time.monotonic()
        elif time.monotonic() - last > quiet:
            break
        else:
            time.sleep(0.002)
    return got


def once(gap):
    master_fd, slave_fd = pty.openpty()
    name = os.ttyname(slave_fd)
    tty.setraw(slave_fd)  # the fix; remove to reproduce
    proc = subprocess.Popen(
        [MICROPYTHON], stdin=master_fd, stdout=master_fd,
        stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
    )
    os.close(master_fd)
    os.close(slave_fd)
    try:
        time.sleep(gap)
        port = serial.serial_for_url(name, baudrate=115200, timeout=None, interCharTimeout=1)
        for command in (b"\r\x03\r\x01", b"print(0)\x04", b"\r\x02"):
            port.write(command)
            drain(port)
        port.close()
        time.sleep(gap)

        port = serial.serial_for_url(name, baudrate=115200, timeout=None, interCharTimeout=1)
        port.write(b"\r\x03")
        n = port.inWaiting()
        if n:
            port.read(n)
        port.write(b"\r\x01")
        drain(port)
        port.write(b"\x05A\x01")
        handshake = port.read(2)
        drain(port)
        port.write(b"print(1)\x04")
        answer = drain(port)
        port.close()
        return handshake, answer
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=10)


tally = collections.Counter()
for _ in range(int(sys.argv[1]) if len(sys.argv) > 1 else 250):
    handshake, answer = once(0.05)
    broken = not (handshake == b"R\x01" or answer.endswith(b"\x04\x04>"))
    tally["BROKEN" if broken else "ok"] += 1
    if broken:
        print(f"handshake={handshake!r} answer={answer!r}")
print(dict(tally))
```
