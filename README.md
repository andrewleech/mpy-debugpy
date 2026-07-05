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
make bootstrap        # submodule init + mbm rebuild of both integration branches
```

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

### 3. Start a debug session

Unix:

```bash
make demo             # runs src/target.py under the debug launcher
# or:
firmware/.../micropython launcher/mpy_launch_debugpy.py <module> [method] [port]
```

Device: install the debugpy module and copy your source, then run the launcher
on the device:

```bash
uv run launcher/compile_debugpy.py         # cross-compile debugpy to .mpy
mpremote mip install launcher/debugpy_mpy.json
mpremote cp -r src/ :/
mpremote run launcher/mpy_launch_debugpy.py
```

The launcher prints one machine-readable line once it is ready:

```
MPDBG-READY {"host": "...", "port": ..., "caps": {...}}
```

`caps` reports what the firmware actually supports (probed at runtime, not
inferred from a build name): `settrace`, `save_names` (real local names vs
positional placeholders), `set_local` (local-variable editing), `f_back`.

### 4. Attach from VS Code

Use the connection from the handshake line (host/port) with a Python
`attach` configuration. A sample `.vscode/launch.json` is included.

## Status

Early. See `planning/ROADMAP.md` for the epics/stories and current progress, and
`planning/BACKGROUND.md` for how the pieces fit together. Known limitation:
local-variable editing depends on firmware `set_local` support — the tooling
reports this per-session and marks locals read-only when unavailable.

## Relationship to other repos

The end goal is upstream: the debugpy module lands in `micropython-lib`, the
firmware support in `micropython`, and orchestration as an `mpremote debug`
command (via the [ampremote](https://github.com/andrewleech/ampremote)
integration). This repo is the development and integration harness that ties
those in-flight branches together and drives them to that state.

## License

MIT, see `LICENSE`.
