# Debugging MicroPython

Set breakpoints, step, and inspect the stack from VS Code (or any Debug Adapter
Protocol client) against code running on the MicroPython unix port or on a
connected board.

One command does the orchestration:

```bash
mpremote debug [options] [target] [module[:method]]
```

It connects to the target, starts the on-device debug server, and prints the
endpoint that server is listening on. You never type a host or a port.

## Quick start (unix, no hardware)

From a checkout of this repository:

```bash
make bootstrap                 # submodules + mbm rebuild of both integrations
make firmware-unix             # builds micropython/ports/unix/build-standard/micropython
```

Then start a session against the sample debuggee in `src/`:

```bash
export MPY_DEBUG_FIRMWARE="$PWD/micropython/ports/unix/build-standard/micropython"
export MICROPYPATH="$PWD/src:$PWD/micropython-lib/python-ecosys/debugpy"
export PYTHONPATH="$PWD/micropython/tools/mpremote"
python3 -m mpremote debug unix target:main
```

After the launcher's own banner, the last three lines are the ones that matter,
and then it waits:

```
debug server listening on 127.0.0.1:5678
capabilities: {'serial_dap': False, 'settrace': True, 'set_local': False, 'f_back': True, 'save_names': True}
MPDBG-READY {"host": "127.0.0.1", "port": 5678, "caps": {...}, "pathMappings": [...]}
```

Attach VS Code's Python debugger to `127.0.0.1:5678` and set a breakpoint in
`src/target.py` on line 79, `x = 78` - a good first one, because it is reached
exactly once. The program stops there, in `main`, before that line has run. It
does not start running at all until a client has sent `configurationDone`, so
nothing is missed while you connect.

`PYTHONPATH` is only needed while `mpremote debug` lives on an integration
branch rather than in a released mpremote; with a released one, `mpremote debug
...` works directly. The other two variables are what this repository has no
`mpdebug.toml` for; the next section replaces them with a file.

## The target file

Naming a connect string on every invocation gets old, and hardcoding an IP
address gets wrong. `mpdebug.toml` maps a short name to a transport and its
defaults. It is found like `.git`: the nearest one from the current directory
upward, stopping at a directory holding `.git` or above `$HOME`.

```toml
[target.unix]
kind = "unix"
firmware = "micropython/ports/unix/build-standard/micropython"
program = "target:main"

[target.pico]
kind = "network"
device = "/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_e6614c311b8e2f37-if00"
program = "app:main"
requires = ["settrace", "save_names"]

[target.pybd]
kind = "serial"
device = "/dev/serial/by-id/usb-MicroPython_Pyboard_Virtual_Comm_Port_in_FS_Mode_3254335D3037-if01"
dap_device = "/dev/serial/by-id/usb-MicroPython_Pyboard_Virtual_Comm_Port_in_FS_Mode_3254335D3037-if03"
source = "app"
program = "app:main"
```

| key | meaning |
| --- | --- |
| `kind` | `unix`, `serial`, or `network`. Required. |
| `device` | connect string as `mpremote connect` accepts. Required for `serial`; for `network` it names the control-plane device used for the pre-IP handshake. Never the debug endpoint - the device reports its own. |
| `firmware` | for `unix`, a path to a built binary (relative paths resolve against this file's directory), or `system` for whatever `micropython` is on `PATH`. |
| `program` | default `module[:method]`. Without it, `target:main`. |
| `requires` | capability names checked against the handshake before the session starts. Vocabulary: `settrace`, `save_names`, `set_local`, `f_back`, `second_cdc`. A typo is caught before any device is touched. `serial_dap` and `repl_dap` are deliberately not accepted here - each reports which channel a run took, so requiring one would fail every target before the run that could satisfy it. |
| `dap_device` | the board's second CDC interface, for DAP over serial instead of over the network. Only used when the handshake also reports `serial_dap: true`. The node has to exist first - see [serial](#serial). |
| `dap_repl` | put DAP on the stream that already carries the REPL, for a board with one UART and no network. Conflicts with `dap_device`, and is refused for a `unix` target. See [one UART](#one-uart). |
| `source` | host directory mounted at the device's `/remote` before the program runs, so it debugs a live view of this directory. Relative paths resolve against this file's directory. Not valid on a `unix` target. |

Unknown keys are ignored, so a front-end can keep its own metadata alongside
these. With one target defined, the name can be omitted entirely:
`mpremote debug` uses it. With several and no name given, it lists them.

A name that is not in the file but looks like a connect string is still handled
as one, so `mpremote debug /dev/serial/by-id/...` keeps working in a project
that has an `mpdebug.toml`.

Reference tty devices by `/dev/serial/by-id/<name>`. `/dev/ttyACM*` numbering
changes when devices are replugged.

## Putting debugpy on a board

`mpremote debug` starts the debug server by importing `debugpy` on the target,
so the package has to be there already. On the unix port `MICROPYPATH` covers
it; a board needs the files on its filesystem:

```bash
mpremote connect <device> debugpy-install micropython-lib/python-ecosys/debugpy/debugpy
```

The argument is a host directory - the debugpy package itself, the one holding
`__init__.py`, not the micropython-lib folder wrapping it. The command
cross-compiles it with mpy-cross, writes only the files whose contents differ
from the device's copy, and removes anything in its install directory that it
did not put there. Re-running it against an unchanged tree transfers nothing,
so it is cheap enough to put in front of every session.

`mpremote mip install debugpy` will be the other route once micropython-lib PR
#1022 merges and the package reaches the index. It is the right one for a
release: it fetches `.mpy` built for the target rather than compiling locally.
It is also the wrong one while the debug server's own sources are what you are
editing, since it rewrites every file each time and knows nothing about a local
checkout.

`--mpy-cross PATH` picks the compiler; the default takes `$MPY_CROSS`, then the
`mpy_cross` package, then `PATH`. The install directory is not selectable: it
is resolved from the target's own `sys.path`, which is what makes this work on
a board that mounts at `/flash` rather than `/`.

Installing does not reset the device, and a `debugpy` already imported into
`sys.modules` shadows the files just written. A separate `mpremote` invocation
soft-resets on its first command anyway; within one invocation, chain it:

```bash
mpremote connect <device> debugpy-install <dir> + soft-reset + debug <device> app:main
```

## How a session is put together

Two channels, and you only ever name the first one:

- **Control plane** - how mpremote talks to the target to get the session
  started. For a device that is the raw REPL over `device`; for `unix` it is a
  subprocess mpremote owns. This is what `mpdebug.toml` configures.
- **Data plane** - how the DAP client talks to the debug server. A TCP port the
  device binds, the board's second CDC interface, or a loopback port on the
  host. The device chooses and *reports* it; nobody configures it.

The sequence:

1. **Resolve** the target name to a transport, a device, and a program.
2. **Connect** over the control plane and enter the raw REPL.
3. **Mount** the source directory at `/remote`, if the target has one.
4. **Launch** the boot script, which imports `debugpy`, probes the firmware's
   real capabilities, binds the data plane, and prints one handshake line.
5. **Handshake**: mpremote reads that line, resolves the bind address to
   something connectable, checks it against the target's `requires`, and
   re-prints it with the resolved endpoint plus its own `pathMappings`.
6. **Attach**: the client connects, sends breakpoints, sends
   `configurationDone`, and only then does the program start.

The handshake line is the contract between mpremote and whatever launched it:

```
MPDBG-READY {"host": "192.168.1.42", "port": 5678,
             "caps": {"settrace": true, "save_names": true, "set_local": false,
                      "f_back": true, "serial_dap": false},
             "pathMappings": [{"localRoot": "/home/me/app", "remoteRoot": "/remote"}]}
```

`caps` comes from probing the running interpreter, never from a build name or a
variant id. `pathMappings` is mpremote's own knowledge and is absent when there
is nothing mounted and no mapping to report.

## Transports

The four differ only in what the control plane is and where the data plane ends
up. Everything after the handshake is identical, and a DAP client cannot tell
them apart.

### unix

```bash
mpremote debug unix mymodule:main
```

mpremote runs a debug-enabled unix binary as its own child process, so it owns
the process and reaps it when the session ends. The binary comes from
`MPY_DEBUG_FIRMWARE`, or from the target's `firmware` key. A manifest variant id
is refused here with the reason: this command cannot fetch firmware, so build
one (`make firmware-unix`) or point at a binary.

`MICROPYPATH` gets the project directory (the one holding `mpdebug.toml`, else
the cwd) put on the front, then whatever you already set, then the port's own
defaults - so `debugpy` is reachable if you have it on `MICROPYPATH`, frozen in,
or `mip`-installed.

`--source` is refused for a unix target: it already runs the program straight
from the host filesystem, so there is nothing to mount.

### network

```bash
mpremote debug pico app:main
```

The default device path. The board binds a TCP port on its own network
interface and reports the address it bound. If it bound a wildcard
(`0.0.0.0`), mpremote resolves that against what it knows about the control
plane rather than handing a client an unconnectable address.

The board needs to be on the network before the debug server starts - put the
connection in `boot.py`, or in the program before it imports `debugpy`.

**Do not debug over WiFi in `network.WLAN.PM_POWERSAVE`.** On a PYBD-SF6W in
that mode a session stops getting answers a few requests in and never recovers,
every time, while the board still answers pings - measured in
`planning/20260813_wifi_powersave_tcp_stall.md`, with a reproduction that has no
debugger in it. The default (`PM_PERFORMANCE`) survived 120 runs of that
reproduction, so it is the better of the two, but it is not clean: the hardware
suite has seen rare stalls of the same shape on it, roughly one run in twenty.

`PM_NONE` is the conservative choice and is what the bench uses when it wants a
quiet link:

```python
wlan.config(pm=network.WLAN.PM_NONE)   # after the connection is up
```

Be aware that this is not known to remove the rare stall. It has only been
measured over a handful of runs, which is nowhere near enough to see a
one-in-twenty fault, and the underlying cause is still open.

### serial

```bash
mpremote debug pybd app:main
```

DAP rides a serial interface of its own instead of the network, and mpremote
bridges it to a loopback port so the client sees an ordinary TCP endpoint. Two
things have to be true, and both are checked rather than assumed:

- the target has a `dap_device`, because only the host can name the interface by
  tty node; and
- the handshake reports `serial_dap: true`, because only the device can map that
  node to a runtime object.

Measured on a PYBD_SF6: 81.7-108.8 kB/s.

This is the narrowest of the three paths, not a shortcut around the network one.
It needs both a build with a second USB CDC interface and a boot that enumerates
it, and the second part is not the default anywhere: stm32 brings up a single
VCP unless `boot.py` says otherwise (`ports/stm32/main.c`), and no board in this
project's set ships a `boot.py` that does. So a board that has the interface
still needs

```python
import pyb
pyb.usb_mode("2xVCP+MSC")
```

in `boot.py` and a reboot - the second tty node does not exist until the board
re-enumerates with it.

Boards with a single UART have no second interface to enumerate at all; they
take the [one UART](#one-uart) path instead.

Three questions get confused here, and they can answer differently on the same
run. `second_cdc` in the handshake is the build's maximum, read from
`MICROPY_HW_USB_CDC_NUM`: it says a second interface is possible, not that this
boot has one. `pyb.usb_mode()` says what boot actually enumerated, which is what
decides whether a session can run. `USB_VCP.isconnected()` says whether a host
is currently holding the interface open. A variant name answers none of them.

### one UART

```bash
mpremote debug --dap-repl pybd app:main
```

For a board with one UART and no network, which is every board as it ships. DAP
rides the stream that already carries the REPL, marked in band with `0x18` the
way `mpremote mount` marks its filesystem RPC, and mpremote bridges it to a
loopback port so the client still sees an ordinary TCP endpoint. Program output
travels on the same wire and reaches your terminal unchanged; a program that
prints `0x18` is escaped, not mistaken for framing.

A target can ask for it instead of the flag:

```toml
[target.pybd]
kind = "serial"
device = "/dev/serial/by-id/usb-MicroPython_Pyboard_Virtual_Comm_Port_in_FS_Mode_3254335D3037-if01"
dap_repl = true
program = "app:main"
```

**The REPL is not usable for anything else while the session runs, and Ctrl-C
does not interrupt the target.** What makes the channel possible is that the
runtime's console is a Python object in a `dupterm` slot, so it can be replaced
with a framing wrapper for the length of the session. On stm32 installing
anything in that slot detaches the interface from the REPL, which stops the
interrupt character being scanned - so Ctrl-C arrives at the target as ordinary
data. Use the client's pause button instead. mpremote puts the slot back on
every exit path, including a failed one.

Measured on a PYBD_SF6: 81.5-81.7 kB/s, against 81.7-108.8 kB/s for the same
payload over a second interface. Five runs land within 0.2% of each other, at
the bottom of the range the dedicated interface spans.

That mechanism is also the port scope: **stm32 boards on the legacy USB stack**,
where the REPL is the object in `dupterm` slot 1. rp2 and esp32 build one slot
and the REPL is not in it, and the unix port has no `dupterm` at all; on those
the session refuses to start rather than handing back a stream that would carry
nothing. So is a REPL stream that cannot report the host letting go of it: this
is the one channel where a session that waits forever costs the console it is
reached by, so a stream with no `isconnected` is refused up front.

`--source` is refused alongside `--dap-repl`: a mount frames the same stream
with the same marker, and the two cannot share it. Put the program on the
device's own filesystem for these sessions.

## The iteration loop

### Debug the directory you are editing

```bash
mpremote debug --source ./app pico app:main
```

The host directory is mounted at the device's `/remote` before the program runs,
so the program imports the files you are editing rather than whatever copy is
on the board. There is no upload step, and no copy on the device to go stale.
mpremote reports the mapping as `pathMappings` in the handshake, so the client
resolves a device path like `/remote/app.py` back to your source file and
breakpoints bind.

Both host-side checks happen before the device is touched: a source root that is
not a directory, or a program module that does not resolve under it, fails
immediately rather than after putting the board into the raw REPL for nothing.

### Re-run edited code without restarting anything

```bash
mpremote debug --source ./app --loop pico app:main
```

`--loop` keeps one process, one DAP session, and one handshake across many runs.
Your client's restart button unwinds the program, evicts everything it imported
from `sys.modules`, and imports it again - so an edit takes effect on the next
run with no upload, no soft reset, and no re-attach. Breakpoints set before the
first run stay bound, because the session never ends.

Each re-run announces itself on the debug console:

```
MPDBG-RESTART {"iteration": 2, "evicted": ["app", "helper"]}
```

The eviction set is everything added to `sys.modules` since just before the
first run of the program. A changed submodule therefore comes back along with
its parent, which is the usual way a hot reload goes wrong, and `debugpy` itself
can never be in the set.

Two limits are inherent, not defects:

- **An already-paused frame runs already-compiled bytecode.** Editing a file
  does not change the frame you are stopped in. Edits apply from the next run.
- **A session in loop mode never sends `terminated`.** That is the point: a
  client that saw it would tear the session down. The session ends when you end
  it.

Without `--loop`, a session does not advertise restart support, and a restart
request is refused with a message saying why, rather than accepted and silently
ignored.

Both flags have launch-configuration equivalents, `source` and `loop`; see
below.

## Attaching from VS Code

Install the extension in [`extension/`](../extension/) along with its
dependency, `ms-python.debugpy`. A `micropython` launch configuration then
spawns `mpremote debug`, reads its handshake, and starts the attach session
with nothing typed:

```json
{
  "type": "micropython",
  "request": "launch",
  "name": "Debug on device",
  "target": "pico",
  "program": "app:main"
}
```

Configuration keys: `target`, `program`, `port`, `timeout`, `dapLog`,
`dapLogFile`, `source`, `loop`, `cwd`, `mpremotePath`, `mpremoteArgs`, `env`.
The status-bar picker selects among `mpdebug.toml` targets and remembers the
choice per workspace.

`source` and `loop` are the two command-line flags above. Each maps to its
flag only when present, so a target that already declares `source` in
`mpdebug.toml` mounts on F5 without the launch configuration repeating it -
set the key only to override that, or to mount a directory no target names:

```json
{
  "type": "micropython",
  "request": "launch",
  "name": "Iterate on device",
  "target": "pico",
  "program": "app:main",
  "source": "${workspaceFolder}/app",
  "loop": true
}
```

Without the extension, run `mpremote debug` yourself and put the host and port
from its output into a `debugpy` `attach` configuration.

## What the debugger can and cannot do

Every capability comes from probing the running interpreter at session start.
The `caps` dict in the handshake is the answer for that session; a variant name
is not evidence, and neither is this page.

| capability | meaning when true |
| --- | --- |
| `settrace` | `sys.settrace` exists, so breakpoints and stepping are possible at all. Without it there is no debugger. |
| `save_names` | local variables appear under their real names. Without it they are `local_00`, `local_01`, ... positional placeholders. |
| `set_local` | a local variable can be written from the debugger. |
| `f_back` | frames chain to their caller, so a call stack deeper than one frame can be walked. |
| `second_cdc` | the build could enumerate a second USB CDC interface for DAP to use. A fact about the build, not about this boot - see [serial](#serial) for what still has to be true before one exists. |
| `serial_dap` | this session's DAP channel is a serial stream rather than a TCP socket. A fact about the session, not about the firmware. |
| `repl_dap` | this session's DAP channel is sharing the stream that carries the REPL. A fact about the session, not about the firmware; `serial_dap` is true as well, since a shared stream is still a stream. |

On every firmware artifact this project publishes, and on the unix build:
`settrace`, `save_names` and `f_back` are true, and `set_local` and
`second_cdc` are false. Two of those builds have been probed rather than
assumed - the unix `build-standard` binary and a PYBD_SF6 - and both report the
same values. Function parameters do appear under their real names, not just
locals.

`second_cdc` is the one that will move: the published PYBD_SF6 artifact is
built from a commit predating `MICROPY_HW_USB_CDC_NUM (2)` on that board, so
false is the honest value for it, while a current build of the same board
probes true. The manifest is corrected by the build job that republishes it,
not by hand.

So **local variables are read-only**. No branch implements local write-back, so
the tooling marks them read-only from the probe rather than accepting an edit
and failing later.

See [`firmware.md`](firmware.md) for the variant list, `fetch`/`verify`/`select`,
per-port build commands, and how each artifact's claims were checked.

### Known limitations

Measured behaviour, not speculation. Each is worth knowing before you conclude
your program is at fault.

- **A pause only lands where Python is running.** The trace hook is the one
  thing that can interrupt the target, so a pause takes effect at the next line
  of Python the program executes. If it is asleep, waiting on a socket, or
  inside a long-running C function, nothing stops until it comes back - the
  request stays pending rather than being lost. Pausing a program whose work is
  one long `time.sleep` will look like the pause did nothing.
- **A breakpoint on the last line of a `for x in range(...)` or `while` body
  stops one time too many, the extra stop before the body has run at all** - so
  a variable the body assigns still reads its pre-loop value there. MicroPython
  compiles the loop test to the bottom of the loop, where it inherits the last
  body line's number. A `while` line itself is only reported once, at loop
  entry, rather than on each pass. Every other body line, the `for` line, and
  loops over anything but `range` are exact. Stepping and values are correct;
  only the number and position of the stops are off.
- **A `line` event fires before its statement executes.** When you are stopped
  on a line, that line has not run yet. This matches CPython, and it is worth
  restating because it decides what a variable reads at a breakpoint.
- **Locals are read-only** (see above).

## Ending a session

Ctrl-C, at mpremote. (In a `--dap-repl` session that is the only thing Ctrl-C
still does - it no longer reaches the target; see [one UART](#one-uart).) Detach
the client first if a mounted session is stopped at a
breakpoint: mpremote has to reach the device over the raw REPL to unmount, and a
target still parked in the debugger will not answer. Detaching lets the program
finish, which puts the device back at a REPL prompt, and teardown is then clean
and silent.

If it does not work out that way, the two outcomes are reported differently
because they need opposite things from you:

- `... reported: <error>; the device is still answering, so reconnect and
  umount by hand if a mount was left behind` - the device answered, so nothing
  is wedged. `mpremote umount` clears it.
- `... the device may no longer respond to anything - only a power cycle clears
  it` - the device did not answer within 10 seconds. That is the case that
  really needs the power cord.

## Troubleshooting

**`--port 0 is rejected: ...`** - a port of 0 asks the system to pick one, which
the device could only report back through `getsockname()`, and no port in this
tree binds it. Name a port, or leave `--port` off and let the device use its own
default.

**`module 'app' does not resolve under source root '/home/me/app' (looked for ...)`**
- checked on the host before the device is touched. The paths it looked for are
in the message; usually the module is one directory deeper than `--source`
points.

**`--source is not valid for a unix target`** - a unix target already runs from
the host filesystem.

**`target 'pico' requires save_names, which this firmware does not provide
(probed caps: {...})`** - the firmware on the board is not the one you think it
is. The probed dict is right there in the message; compare it against
[`firmware.md`](firmware.md).

**`no unix debug binary found: ...`** - set `MPY_DEBUG_FIRMWARE`, or give the
target a `firmware` path. A firmware-manifest variant id is not enough:
`mpremote debug` cannot fetch artifacts.

**`waiting for the device to report its debug-server endpoint...` and then a
timeout** - the boot script did not get as far as binding. Most often the
firmware has no `sys.settrace`, or `debugpy` is not importable on the device. To
see the whole conversation:

```bash
mpremote debug --dap-log --dap-log-file /tmp/dap.jsonl pico app:main
```

`--dap-log` inserts a local proxy between the client and the device and writes
every message to JSONL. The client is given the proxy's endpoint, so it cannot
attach straight past the log. With `--dap-log`, `--port` pins the proxy's port -
the one a `launch.json` might have hardcoded - and the device gets a freshly
reserved port of its own.

A run that stays attached - `--dap-log`, or a target with a `dap_device` -
prints the board's own console output as it arrives. On a `dap_device` target
that is the only place your program's `print` output can appear, since the DAP
channel is a separate interface. Reading it is not only for your benefit: a
console this process holds open and never empties stops the board
(`planning/20260813_console_backpressure.md` measures where).

**Breakpoints never bind under `--source`** - check that the handshake reported
`pathMappings`. Absent means nothing was mounted, so the program is running a
copy on the device rather than your source.

**Options appear to be ignored** - they have to come before the positional
arguments: `mpremote debug --loop pico app:main`, never `mpremote debug pico
app:main --loop`. If a chained mpremote command follows, put `+` in front of it
so it is not read as the program name.
