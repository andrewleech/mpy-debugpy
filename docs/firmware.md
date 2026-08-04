# Firmware

Four debug-enabled MicroPython builds, one per supported port/board. Binaries
are not committed; fetch a published artifact or build locally. All four come
from a single GitHub Release on this repo, tagged `fw-<micropython-sha10>`
(currently `fw-f9d7c96b96`, micropython commit `f9d7c96b96a4`), published by
`.github/workflows/firmware.yml`; each manifest entry's `download_url` points
at that Release's asset. `firmware/firmware.toml` is the provenance record
this page summarizes — consult it directly for full toolchain detail per
artifact.

## Which firmware do I need

Build flags, identical for every row of both tables below:
`MICROPY_PY_SYS_SETTRACE=1` and `MICROPY_PY_SYS_SETTRACE_LOCALNAMES=1`, set
in each variant/board header, not `CFLAGS_EXTRA` (see
[Build it yourself](#build-it-yourself)).

| variant id | port / board | artifact |
| --- | --- | --- |
| `unix-standard-debug` | unix / standard | native binary |
| `rp2-rpi-pico-w-debug` | rp2 / RPI_PICO_W | `firmware.uf2` |
| `stm32-pybd-sf6-debug` | stm32 / PYBD_SF6 | `firmware.dfu` (app-only, mboot required) |
| `esp32-generic-debug` | esp32 / ESP32_GENERIC | `firmware.bin` (merged, flash at offset 0) |

Capabilities, keyed by the vocabulary the runtime probe uses
(`debugpy.get_capabilities()`, echoed in the launcher's `MPDBG-READY`
handshake). All four variants claim the same values (`settrace`/`save_names`/
`f_back` true, `set_local` false); the evidence column says how each row's
claim was checked:

| variant id | settrace | save_names | set_local | f_back | evidence |
| --- | --- | --- | --- | --- | --- |
| `unix-standard-debug` | true | true | false | true | probe-confirmed |
| `rp2-rpi-pico-w-debug` | true | true | false | true | build intent |
| `stm32-pybd-sf6-debug` | true | true | false | true | build intent |
| `esp32-generic-debug` | true | true | false | true | build intent |

`unix-standard-debug` is the only row checked against a live `MPDBG-READY`
`caps` dict (fetch the artifact and attach); the three device rows are build
intent, pending a hardware attach test. "Build intent" for `settrace` and
`save_names` means CI verified the compiled macro before staging the
artifact (`.github/scripts/verify_capabilities.py`, run per build in
`.github/workflows/firmware.yml`); each variant's `capabilities_note` in
`firmware.toml` names the header that sets it. `set_local` and `f_back` are
not build-verified per artifact — they follow from the source tree rather
than a build flag: `f_back` is unconditional whenever `sys.settrace` is
compiled in at all (`py/profile.c`), and `set_local` is `false` because no
branch implements local-variable write-back, so editing a local from the
debugger is unsupported everywhere, not just on some variants.

Treat the `caps` dict in the launcher's `MPDBG-READY` line as the actual
answer for a given attach, not this table; see
[Capability truth](#capability-truth).

## Get it

```bash
uv run launcher/firmware.py list                              # variants + provenance
uv run launcher/firmware.py fetch unix-standard-debug          # download + verify
uv run launcher/firmware.py verify                             # re-check all local artifacts (or pass variant ids)
uv run launcher/firmware.py select --need settrace,save_names --port unix
```

`fetch` downloads the artifact from the manifest's `download_url`, hashes it,
and fails if the sha256 doesn't match `firmware.toml`'s `artifact_sha256` (add
`--force` to re-fetch over an already-verified local copy). `verify` re-runs
that hash check against whatever is already on disk, without downloading.
`select --need CAP[,CAP...]` resolves a capability requirement (optionally
narrowed by `--port`/`--board`) to exactly one artifact path — fetching the
artifact if it is absent, or re-verifying an existing copy's sha256 — and
rejects any capability key the probe doesn't report.

Artifacts land under `firmware/<variant-id>/<artifact-file>` (e.g.
`firmware/unix-standard-debug/micropython`,
`firmware/esp32-generic-debug/firmware.bin`).

- **stm32**: `firmware.dfu` is the raw application image (`TEXT0_ADDR
  0x08008000`). It does not include or replace mboot — mboot must already be
  resident on the board (true for stock PYBD_SF6 hardware).
- **esp32**: `firmware.bin` is a merged flash-from-0 image (bootloader +
  partition table + application) — flash it at offset 0, not as an
  application-only OTA image.

## Build it yourself

`.github/workflows/firmware.yml` is the authoritative build recipe; the
commands below are its per-port build steps run locally. They need a full
submodule checkout first (`make bootstrap`, or `git submodule update --init
--recursive` on a checkout that already has the top-level submodules).

CI's `setup` job pins the three environment variables that determine the
artifact bytes and are embedded in the build:
`SOURCE_DATE_EPOCH` (the source commit's own timestamp, so the embedded
build date doesn't depend on which day the build runs) and
`MICROPY_GIT_TAG`/`MICROPY_GIT_HASH` (derived from the commit sha rather
than `git describe`, since a submodule checkout has no tag history to
describe against; the tag appears in the version banner, the hash as a
build define). Export the same values before running any command below:

```bash
cd micropython
full_sha=$(git rev-parse HEAD)
short_sha=${full_sha:0:10}
major=$(sed -n 's/^#define MICROPY_VERSION_MAJOR *//p' py/mpconfig.h)
minor=$(sed -n 's/^#define MICROPY_VERSION_MINOR *//p' py/mpconfig.h)
micro=$(sed -n 's/^#define MICROPY_VERSION_MICRO *//p' py/mpconfig.h)
prerelease=$(sed -n 's/^#define MICROPY_VERSION_PRERELEASE *//p' py/mpconfig.h)
tag="v${major}.${minor}.${micro}"
[ "${prerelease}" != "0" ] && tag="${tag}-preview"
export MICROPY_GIT_TAG="${tag}.g${short_sha}"
export MICROPY_GIT_HASH="${short_sha}"
export SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD)
cd ..
```

These three variables are what make two independent CI runs at the same
micropython pin byte-identical to each other. They do not make a local build
byte-identical to the published Release: matching `firmware.toml`'s
`artifact_sha256` also needs CI's exact toolchain and runner/container image
(recorded per-variant in `firmware.toml`'s `toolchain` table). A local build
following this section reproduces the recipe, not the release bytes.

**unix** — builds directly on the host, no container:

```bash
make firmware-unix
```

The unix `standard` variant already enables `MICROPY_PY_SYS_SETTRACE` and
`MICROPY_PY_SYS_SETTRACE_LOCALNAMES` in
`ports/unix/variants/standard/mpconfigvariant.h`, so `DEBUG_CFLAGS` is empty
and `firmware-unix` passes an empty `CFLAGS_EXTRA`. The other three ports
enable the same two macros in their board headers instead (each variant's
`capabilities_note` in `firmware.toml` names the exact file); the commands
below pass no build flags either.

Output: `micropython/ports/unix/build-standard/micropython`.

The three device builds run in a plain `ubuntu:24.04` (or, for esp32, the
pinned `espressif/idf`) container over the bind-mounted tree. They run as
root because the container installs packages first (`apt-get`) — hence the
`git config --global --add safe.directory "*"` (git refuses to operate on a
differently-owned tree otherwise) and the whole-tree `chown` at the end,
which covers root-owned `.pyc` caches the build writes under
`micropython/py`/`micropython/tools`, not just the board build directory.

**rp2** (`RPI_PICO_W`) — CI installs its toolchain via `apt-get` on a bare
runner; the container needs the same packages plus `git`/`python3`/
`ca-certificates`, which the base image doesn't ship. CI also sets
`CMAKE_ARGS=-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` at job level so its
capability-verification step can inspect the compiled config; without it the
build produces no `compile_commands.json`:

```bash
docker run --rm -v "$(pwd):$(pwd)" -w "$(pwd)" \
  -e SOURCE_DATE_EPOCH -e MICROPY_GIT_TAG -e MICROPY_GIT_HASH \
  -e CMAKE_ARGS=-DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
  ubuntu:24.04 bash -c '
  apt-get update &&
  apt-get install -y --no-install-recommends ca-certificates git python3 cmake gcc-arm-none-eabi libnewlib-arm-none-eabi libstdc++-arm-none-eabi-newlib libstdc++-arm-none-eabi-dev build-essential ninja-build &&
  git config --global --add safe.directory "*" &&
  make -C micropython/mpy-cross &&
  make -C micropython/ports/rp2 BOARD=RPI_PICO_W submodules &&
  make -C micropython/ports/rp2 BOARD=RPI_PICO_W &&
  chown -R '"$(id -u):$(id -g)"' micropython
'
```

Output: `micropython/ports/rp2/build-RPI_PICO_W/firmware.uf2`.

**stm32** (`PYBD_SF6`) — same pattern, CI's toolchain package list:

```bash
docker run --rm -v "$(pwd):$(pwd)" -w "$(pwd)" \
  -e SOURCE_DATE_EPOCH -e MICROPY_GIT_TAG -e MICROPY_GIT_HASH \
  ubuntu:24.04 bash -c '
  apt-get update &&
  apt-get install -y --no-install-recommends ca-certificates git python3 gcc-arm-none-eabi libnewlib-arm-none-eabi build-essential &&
  git config --global --add safe.directory "*" &&
  make -C micropython/mpy-cross &&
  make -C micropython/ports/stm32 BOARD=PYBD_SF6 submodules &&
  make -C micropython/ports/stm32 BOARD=PYBD_SF6 &&
  chown -R '"$(id -u):$(id -g)"' micropython
'
```

Output: `micropython/ports/stm32/build-PYBD_SF6/firmware.dfu`.

**esp32** (`ESP32_GENERIC`) — CI runs this job in the `espressif/idf` image
(pinned by digest), which already ships `git` and `python3`:

```bash
docker run --rm -v "$(pwd):$(pwd)" -w "$(pwd)" \
  -e SOURCE_DATE_EPOCH -e MICROPY_GIT_TAG -e MICROPY_GIT_HASH \
  espressif/idf:v5.3@sha256:89df2532b2f5df278a8cca6a06358f6e615628aabeb0f7e639e27646a23208d7 \
  bash -c '
    apt-get update &&
    apt-get install -y --no-install-recommends build-essential &&
    git config --global --add safe.directory "*" &&
    . "$IDF_PATH/export.sh" &&
    make -C micropython/mpy-cross &&
    make -C micropython/ports/esp32 BOARD=ESP32_GENERIC submodules &&
    make -C micropython/ports/esp32 BOARD=ESP32_GENERIC &&
    chown -R '"$(id -u):$(id -g)"' micropython
  '
```

Output: `micropython/ports/esp32/build-ESP32_GENERIC/firmware.bin`.

None of the four build outputs above sit under `firmware/<variant-id>/`, so
`firmware.py verify`/`select` won't see them — those two subcommands only
check published artifacts against the manifest hash; use a local build
directly from its build directory.

Per-artifact toolchain versions actually resolved by a given CI run (compiler
version, runner/container image, CI run id) are recorded in `firmware.toml`'s
`toolchain` table, not repeated here.

## Capability truth

The manifest's `capabilities` table and the flags above are build intent.
The only trustworthy answer for a given firmware is the `caps` dict in the
launcher's `MPDBG-READY {"host": ..., "port": ..., "caps": {...}}` handshake
line, produced by `debugpy.get_capabilities()` probing the running
interpreter — never inferred from a variant name or directory (D4).
`launcher/capcheck.py` provides the manifest-vs-probe cross-check
(`CapabilityMismatch` on any claimed-true/probed-false key); the host test
harness applies it to a session only when a claim is supplied — parametrize
the `claimed_capabilities` fixture or set `MPY_DEBUG_CLAIMED_CAPS`
(`tests/fixtures/debuggee.py`, exercised by
`tests/test_capability_guard.py`). `firmware.py
select` filters on the manifest's claimed capabilities only — it does not
probe, so treat its result as a candidate artifact and the `MPDBG-READY`
`caps` line as the answer once attached.

The four probe keys, and what they mean for the debugger UX:

- **`settrace`** — `sys.settrace` is available at all. Without it, no
  breakpoints or stepping.
- **`save_names`** — real local-variable names are available. When `false`,
  locals still show up in the debugger, as positional `local_N` placeholders
  (`local_00`, `local_01`, ...) rather than their source names.
- **`set_local`** — the running interpreter accepts local-variable
  write-back from the debugger. `false` on every current build: locals are
  read-only in every session today, regardless of `save_names`.
- **`f_back`** — a frame's caller is reachable (`frame.f_back`), needed for
  a multi-level call stack in the debugger UI.

The macros that back `settrace`/`save_names`:
`MICROPY_PY_SYS_SETTRACE` and `MICROPY_PY_SYS_SETTRACE_LOCALNAMES`
(`py/mpconfig.h`, off by default; on for all four variants above). Readers
arriving from the upstream PR lineage may know the second macro as
`MICROPY_PY_SYS_SETTRACE_SAVE_NAMES` — that spelling is a no-op on this
branch; the real macro is `MICROPY_PY_SYS_SETTRACE_LOCALNAMES`.

A third macro, `MICROPY_PY_SYS_SETTRACE_LOCALNAMES_PERSIST`, controls writing
local-variable names into compiled `.mpy` bytecode (rather than keeping them
only in RAM for the current run). It is off in every variant here, and stays
off intentionally: the code-info writer for this feature appends data to
every function's code-info section without updating the line-number reader to
skip it, which corrupts `sys.settrace()` line numbers for all compiled code,
not just `.mpy` loading. Do not re-enable it without fixing that reader first.
