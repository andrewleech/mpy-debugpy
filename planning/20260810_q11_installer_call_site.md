# Q11: where `ensure_debugpy_installed` is called from

- Date: 2026-08-10
- Top repo HEAD: `efc307e34aa53dd8b0f428d1de256bd5b1c4e4a9`
- `micropython`: `67149a9c1f7979a16d1ae2736662471c450eccfd` (`mpy-debugpy`)
- `micropython-lib`: `3bd6c44697ecdd46da9944445a998adcee9479c1` (`mpy-debugpy`)

## The question

STORY-4.1 built `ensure_debugpy_installed` in
`micropython/tools/mpremote/mpremote/debugpy_install.py`: cross-compile a host
directory with mpy-cross, write only what differs from the device's copy,
record a content-hash marker, and sweep any file under the device directory
the marker does not account for. It is exercised by 47 host tests and was
proven on a real board during the EPIC-6 phase entry, but nothing in mpremote
calls it. `do_debug` does not, so `mpremote debug <device>` assumes debugpy is
already on the board, and the only production-shaped caller is the HIL
conftest. Three candidates were on the table: run it implicitly on every
device session, add a `--install` flag to `debug`, or give it its own command.

## Decision: `mpremote debugpy-install <package_dir>`

Its own command, on the `mpremote_debugpy_install` branch, with the same
optional `--mpy-cross PATH` the installer already takes.

```
mpremote connect <dev> debugpy-install \
    micropython-lib/python-ecosys/debugpy/debugpy
```

The host directory is a required positional because mpremote has no copy of
the debugpy package and no defensible way to guess where one is: the package
lives in micropython-lib, which is a sibling checkout at best and absent at
worst. Naming it is the only honest interface.

The command validates before it opens the port - a path that is not a
directory, or a directory with no `__init__.py`, is refused without touching
the device. The second check carries a hint, because the mistake it catches is
predictable: `python-ecosys/debugpy` is the micropython-lib *package folder*
(manifest, README, and the package inside it), and the installer wants
`python-ecosys/debugpy/debugpy`. When the named directory contains a `debugpy`
subdirectory that does have `__init__.py`, the error names it.

### Rejected: implicit install on every device session

`do_debug` would have to know a host package directory on every run, which
puts the same unguessable path into `mpdebug.toml` or an environment variable
for the benefit of the one run in fifty that needs it. It also inverts who
decides: the installer *sweeps* its device directory, removing anything the
marker does not record, so an implicit call would silently delete a
hand-placed or `mip`-installed file the first time someone typed `mpremote
debug`. Installing is a deliberate act on the device's filesystem and should
read like one.

### Rejected: a `--install` flag on `debug`

Same objection, plus a branch problem that is not cosmetic. `debug` lives on
`mpremote_debug`, which is the branch sitting in front of the user as fork PR
#51; the installer lives on `mpremote_debugpy_install`. A flag would couple
the two, growing the PR under review with a feature it does not need. A
separate command lands entirely on the installer's own branch and leaves #51
untouched.

The flag also cannot honour the soft-reset requirement without doing something
surprising, which is the next section.

## The soft-reset requirement is already satisfied

Q11 recorded that whatever wires this up "must soft-reset between install and
session - a `debugpy` already in `sys.modules` survives the install and
shadows the new files". Reading mpremote's own state machine shows this needs
no new mechanism:

- `State.__init__` sets `_auto_soft_reset = True`. The first
  `ensure_raw_repl()` of an invocation soft-resets and clears the flag.
- So a *separate* `mpremote ... debugpy-install ...` invocation, followed by a
  separate `mpremote debug`, resets on the debug run's first command.
- Within *one* invocation the reset has already happened before the install,
  so `mpremote connect X debugpy-install DIR + soft-reset + debug ...` is what
  covers a chain.

`debugpy-install` therefore does not reset the device itself, matching `mip
install`, and calls `state.ensure_raw_repl()` with mpremote's default policy
rather than forcing `soft_reset=True`. What it does do is say so: when a write
actually changed something it prints a line telling the user to soft-reset
before debugging a target that has already imported debugpy. When nothing
changed it stays silent about resets, because nothing can be shadowing
anything. A `--install` flag on `debug` could not do this - it would sit
*inside* the invocation that is about to import debugpy, and would have to
force a reset the user did not ask for.

`state.did_action()` is called so mpremote does not drop into a REPL after the
command, matching every other action command.

## This does not duplicate `mip install`

debugpy has a `manifest.py` and is a micropython-lib package, so `mpremote mip
install debugpy` becomes a real alternative once #1022 merges and the package
reaches the index - today it 404s, because the index is built from master. It
is a different tool for a different moment:

| | `mip install` | `debugpy-install` |
| --- | --- | --- |
| source | micropython-lib on the network, or a URL | a local checkout |
| compilation | fetches `.mpy` built for the target's `sys.implementation._mpy` | runs the local mpy-cross over local sources |
| unchanged files | rewritten every time | skipped, via a content-hash marker |
| removed files | left on the device | swept from the device directory |

`mip` is how a user installs a *release*. `debugpy-install` is how anyone
working on the debugpy server itself - which is everyone on this integration
until #1022 merges - gets an edited tree onto a board without a full rewrite
each iteration. The skip-when-unchanged behaviour is the point: it makes
"install then debug" cheap enough to run before every session.

### `--target` was deliberately not exposed

`ensure_debugpy_installed` takes a `device_dir`, defaulting to a `lib`
directory resolved from the target's own `sys.path` (the EPIC-6 fix for boards
that mount at `/flash`). It is not a command-line option, because the
installer *sweeps* `device_dir` of anything its marker does not record.
Pointing that at a directory chosen on the command line turns a typo into
deletion of unrelated files. The resolved default is the only value that is
safe by construction.

## Verification

Seven host tests in `tests/test_s4_1_debugpy_install.py`
(`TestDebugpyInstallCommand`) cover registration under its own name, argument
parsing, the two refusals and their messages, and an end-to-end install
through the real installer with the host cache redirected - asserting a first
run writes and says a reset is needed, and a second run writes nothing and
does not. Each was checked by mutation: removing `did_action`, emptying the
hint, and deleting the reset line each fail a test.

On real hardware (PYBD-SF6W), against `micropython-lib/python-ecosys/debugpy/debugpy`:

- with the board's existing install present - `debugpy already up to date`,
  no transfer.
- naming `python-ecosys/debugpy` - refused, with the inner directory named.
- after deleting `/flash/lib/debugpy` and its marker - `debugpy installed
  from ...` plus the soft-reset line, and the HIL suite then passes against
  the reinstalled copy.

## What this unblocks

STORY-6.4's "flash a wiped board and debug it" scenario now has a documented
CLI route for its middle step. The command is documented in
`docs/debugging.md`.
