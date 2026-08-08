# EPIC-4 phase entry: the mount the tickets assume was never built, and was broken

Date: 2026-08-08
HEAD: 2a465a7 (micropython bbdefbc3e8, micropython-lib 85ed324)

Phase-entry revalidation for the two remaining EPIC-4 stories, STORY-4.3
(attach-time staleness guard + generated `pathMappings`) and STORY-4.5
(hot-reload via `sys.modules` eviction). Both tickets were written on
2026-07-06 at HEAD `59fab8450a`; 94 top-repo commits, 213 micropython commits
and the whole `micropython-lib` debugpy branch have landed since.

## The blocking finding: `mpremote mount` did not work at all

`mount_local` substitutes a `SerialIntercept` for the transport's serial
object, so filesystem RPC is answered from any read. `read_until` saves and
restores the serial-level read timeout around its poll:

```python
saved_timeout = self.serial.timeout
if self.is_pty:
    self.serial.timeout = 0.1
```

`SerialIntercept` carried no `timeout`, so every read after a mount raised
`AttributeError`. Reproduced against the PYBD_SF6 over its REPL CDC, and
against the unix firmware over a pty. The unconditional read was introduced by
this project's own `b8d443b4b5` ("Enforce read_until timeouts on pty
transports") on `mpremote_debug`, a branch destined for upstream, so the
regression was ours and would have shipped in the PR.

Two consequences worth recording separately from the fix:

- **STORY-4.5 was never hardware-gated.** The blocker recorded against it -
  `mpremote mount` against a pty-backed unix firmware failing with "timeout
  waiting for first EOF reception" - was this defect. With `timeout` forwarded
  to the wrapped port, that same pty mount now imports from the host directory
  and `umount_local` returns cleanly. EPIC-4's remaining stories can be
  developed and regression-tested without a board; hardware confirms rather
  than gates them.
- **A mount that fails mid-flight wedges the board past software recovery.**
  The first failed attempt left the device blocked inside a filesystem RPC with
  the host no longer answering. Ctrl-C is consumed as RPC payload, so the REPL
  never comes back and `enter_raw_repl` reports "could not enter raw repl"; the
  PYBD needed a `mpy-dev cycle` to recover. Any flow that mounts routinely
  needs to treat an aborted mount as a state the user must be told how to clear.

Fixed in micropython `bbdefbc3e8`, pinned by top-repo `2a465a7`, with
`tests/test_s5_1_mpremote_debug.py::test_read_until_works_through_a_mount_intercept`
pinning the contract (it raises `AttributeError` on the pre-fix tree).

## Nothing in the debug flow mounts anything

`mount_local` has exactly one caller, `do_mount` (`commands.py:1210`). `do_debug`
neither mounts nor syncs: it execs the boot script and the device imports the
target module from its own filesystem (`/flash/target.py` in the hardware runs).
`Target` has no source-root or mount field.

Both tickets presuppose a mount and both put establishing it out of scope -
s4.5 defers to "mount setup itself (existing `mount_local`)", s4.3 to s4.2's
sync record. s4.2 is unreachable (STORY-2.1 said YES, D2), so its no-mount
branch is dead, and the mount branch has no substrate. The 2026-08-06
sequencing entry already observed the gap ("the debug flow mounts and syncs
nothing today") without assigning an owner.

The roadmap's own collapse text names the missing piece: with 2.1 = YES, EPIC-4
becomes "STORY-4.1 plus a live-mount attach path". That attach path is
STORY-4.3. Establishing the mount moves into its scope; STORY-4.5 then builds
on it. Recorded as a roadmap update, not a silent ticket rewrite.

## What the mount forces on `do_debug`'s shape

`SerialIntercept.read` services fs-RPC transparently, so any read on a mounted
transport keeps the mount alive - including `_read_mpdbg_ready`'s. The
corollary is the constraint: mpremote must keep reading. The plain network path
reports the endpoint and returns (`commands.py:1194`), releasing the port; with
a mount that would strand the target on its next filesystem access. Mount mode
therefore forces the `_stay_attached` shape the serial-DAP (`:1188`) and
`--dap-log` (`:1203`) paths already have.

Mount must also be established before `exec_raw_no_follow` runs the boot script,
since `mount_local` needs the raw REPL for `__mount()`.

## STORY-4.5's mechanism, confirmed on hardware

On the PYBD_SF6, in one session with `/remote` mounted from a host directory:

```
first import: 1
after evict+reimport: 2
```

`del sys.modules[name]` followed by re-import re-reads the mounted file. No
upload, no soft reset. The fs hook's `os.chdir('/remote')` is enough for
`import <module>` to resolve there, so no `sys.path` change is needed.

One incidental hazard, from writing through the mount rather than editing on
the host: a file written device-side and not explicitly closed is re-read
truncated, because the host-side `do_write` leaves the bytes in its own file
buffer until close. The real flow has an editor writing the file, so this only
bites device-side writes - but a test that writes through the mount must close
before re-importing.

## Anchor drift

STORY-4.3, `micropython-lib` `0d42fcd5bc` to `85ed324`:

| ticket anchor | now |
| --- | --- |
| `debug_session.py:349-352` attach reads `pathMappings` | `:410-413` |
| `pdb_adapter.py:108` `self.path_mappings` | `:170` |
| `pdb_adapter.py:142` `_filename_as_debugee` | `:204` |
| `pdb_adapter.py:155` `_filename_as_debugger` | `:217` |
| duplicated unreachable loop `:164`/`:170` | `:225-229` / `:231-236`, still there |

STORY-4.5, `launcher/mpy_launch_debugpy.py` (32 to 222 lines) and micropython
`18c76f7c56` to `bbdefbc3e8`:

| ticket anchor | now |
| --- | --- |
| launcher `:90` single `MPDBG-READY` print | `:173` |
| launcher `:93` `__import__(target_module, ...)` | `:190` |
| launcher `:109` `getattr(target, target_method)` | `:195` |
| `transport_serial.py:319` `mount_local` | `:347` |
| `transport_serial.py:417` `umount_local` | `:445` |
| no `restart` handler, no `supportsRestartRequest` | holds; the capability is present but commented out at `debug_session.py:373` |

## A consumer shipped ahead of STORY-4.3

STORY-7.1's extension landed first (2026-08-06 resequencing). It emits an
identity `pathMappings` for a unix target and omits the field entirely for
serial/network targets, naming this story as what will supply the real one
(`extension/src/extension.ts:199-218`). So the generated mapping has two
consumers now: the DAP attach request the command sends, and the launch config
the extension builds. Whatever STORY-4.3 produces has to be reachable from both.

## Unfolded mpremote commits

Four commits now sit on the `mpy-debugpy` integration branch without being
folded back into the `mpremote_debug` feature branch (`e143f3dbec`,
`68a14d836f`, `11828bf02e`, `bbdefbc3e8`). The last of these fixes a defect
`mpremote_debug` itself introduced, so the prospective PR is broken until the
fold happens. Pre-existing backlog, owned by the STORY-8.x upstreaming work,
not by this phase.
