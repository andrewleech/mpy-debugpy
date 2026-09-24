# mpy-debugpy

Debug MicroPython code from VS Code (or any Debug Adapter Protocol client) —
breakpoints, stepping, and stack/variable inspection — on the unix port and on
connected devices.

This repository is an [mbm](https://github.com/andrewleech/micropython-branch-manager)
integration: it composes the in-flight MicroPython and micropython-lib branches
that provide debug support into a working tree you can build and run directly,
while keeping each change tracked as an upstream-bound branch. It also carries
the developer tooling, a host-side test harness, samples, and the project
roadmap.

## What provides what

- `micropython/` — submodule (upstream), with the `sys.settrace` / frame
  inspection branches layered on by mbm. Debug-enabled firmware is built from
  here.
- `micropython-lib/` — submodule (upstream), with the `debugpy` DAP-server
  branches layered on by mbm. This is the module that runs on the target.
- `mbm.toml` — the set of branches/PRs composed into the integration.
- `launcher/` — host tooling: the debug launcher, firmware manifest fetch/verify,
  and the `.mpy` cross-compile/install helpers.
- `firmware/firmware.toml` — provenance manifest for debug-enabled firmware
  (source commit, build flags, artifact hash). Binaries are fetched/built, not
  stored in-repo.
- `tests/` — host-side pytest harness driving a real DAP session against the
  target (a fake VS Code client).
- `src/` — sample target scripts.
- `planning/` — roadmap, background, and investigation spikes.

## Requirements

- `git`, `make`, a C toolchain (for building firmware)
- [`uv`](https://docs.astral.sh/uv/) (tooling scripts use PEP 723 inline deps)
- `mbm`: `uv tool install micropython-branch-manager`
- VS Code with the Python extension (for attaching)

## Getting started

### 1. Clone and build the integration

```bash
git clone --recurse-submodules <this-repo> mpy-debugpy
cd mpy-debugpy
make bootstrap        # check out both submodules at their pinned integration commits
```

The pins already point at composed integration branches on the forks, so no
rebuild is needed to get started. `make integrate` recomposes them from
`mbm.toml` on current upstream, which only matters when changing the branch set.

### 2. Get debug-enabled firmware

Unix port (fastest way to try it):

```bash
make firmware-unix    # builds micropython/ports/unix with settrace enabled
```

Device: build for your board, or fetch a published binary described by the
manifest:

```bash
uv run launcher/firmware.py list           # show known variants + provenance
uv run launcher/firmware.py verify         # check any local artifacts
```

See [`docs/firmware.md`](docs/firmware.md) for the full variant/capability
table, `fetch`/`select` usage, CI-equivalent per-port build commands, and the
capability-probe doctrine.

### 3. Start a debug session

`mpremote debug` is the entry point for every transport. It resolves a target,
starts the program, and prints where to attach:

```bash
export PYTHONPATH="$PWD/micropython/tools/mpremote"
python3 -m mpremote debug -t unix target:main           # unix port
python3 -m mpremote debug -t /dev/ttyACM0 app:main      # device on a serial port
python3 -m mpremote debug -t pico app:main              # a target named in mpdebug.toml
```

A device session runs DAP over the board's network (WiFi, Ethernet or USB
NCM), so the board needs one that is up; the serial port only starts the
program. A board with no network can share its REPL's stream instead
(`--dap-repl`). The board also needs `debugpy` on it - see "Putting debugpy on
a board" in [`docs/debugging.md`](docs/debugging.md).

The `debug` command lives on the mpremote branches composed into
`micropython/tools/mpremote`, so run that copy rather than a released
`mpremote` until it upstreams.

A target name like `pico` comes from an `mpdebug.toml` beside your code;
anything that is not a target name is treated as a connect string. See
[`docs/debugging.md`](docs/debugging.md) for the target file, the
transports, the `--source`/`--loop` iteration loop, and troubleshooting.

`make demo` runs `src/target.py` through the launcher directly, without
mpremote, if you want to see the bare handshake.

The launcher prints one machine-readable line once it is ready:

```
MPDBG-READY {"host": "...", "port": ..., "caps": {...}}
```

`caps` reports what the firmware actually supports (probed at runtime, not
inferred from a build name): `settrace`, `save_names` (real local names vs
positional placeholders), `set_local` (local-variable editing), `f_back`.

### 4. Attach from VS Code

The [`extension/`](extension/) `MicroPython Debug` extension, with its
dependency `ms-python.debugpy`, turns a session into one keypress: its
`micropython` launch configuration spawns `mpremote debug`, reads the
handshake, and attaches with no host or port typed anywhere. It is not on the
Marketplace yet, so build and install it from this checkout:

```bash
cd extension
npm ci
npm run package                                   # writes mpy-debugpy-<version>.vsix
code --install-extension mpy-debugpy-*.vsix
```

Then press F5 on one of the `micropython` configurations in
`.vscode/launch.json`. They run this checkout's `mpremote` (`python3 -m
mpremote` with `PYTHONPATH` set), since a released `mpremote` has no `debug`
command yet.

Without the extension, attach by hand: run `mpremote debug` yourself, take
the host/port off its `MPDBG-READY` line, and paste them into a `debugpy`
`attach` configuration.

## Status

Working on the unix port and on hardware (ESP32-C3, RP2040, PYBD-SF6W), not yet
upstream. See `planning/ROADMAP.md` for the epics/stories and current progress,
and `planning/BACKGROUND.md` for how the pieces fit together. What the debugger
cannot do yet - exception breakpoints, editing locals where the firmware lacks
`set_local`, a few loop-line quirks - is listed under "Known limitations" in
[`docs/debugging.md`](docs/debugging.md#known-limitations).

## Relationship to other repos

The end goal is upstream: the debugpy module lands in `micropython-lib`, the
firmware support in `micropython`, and orchestration as an `mpremote debug`
command (via the [ampremote](https://github.com/andrewleech/ampremote)
integration). This repo is the development and integration harness that ties
those in-flight branches together and drives them to that state.

## License

MIT, see `LICENSE`.
