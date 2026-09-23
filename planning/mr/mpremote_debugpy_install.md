---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: mpremote_debugpy_install
status: pushed to fork; not proposable as-is (see todo)
title: "tools/mpremote: Add a debugpy-install command."
pushed_branch: https://github.com/andrewleech/micropython/tree/mpremote_debugpy_install
head: 291c9f91aa
depends_on: mpremote_file_cp_hash
relationship: |
  Calls `fs_writefile(..., verify_hash=True)`, which only exists with
  micropython PR #18436 (mpremote_file_cp_hash). The branch is not based on
  it, so on its own it raises TypeError on the first write. Either rebase onto
  mpremote_file_cp_hash and say "depends on #18436", or wait for #18436.

  Installs the package from micropython-lib PR #1022, so is only useful once
  that lands. Used by mpremote_debug_command's documentation as the way to get
  debugpy onto a board, but neither branch depends on the other in code.
todo:
  - >-
    Rebase onto mpremote_file_cp_hash (d940a381c0), or drop verify_hash until #18436 merges.
  - >-
    Decide whether this should go upstream at all. Once debugpy is in micropython-lib, `mpremote mip install debugpy` installs it; what this adds is .mpy cross-compilation, a content-hash cache and a stale-file sweep. A maintainer will reasonably ask why that's debugpy-specific rather than a mip option. If the answer is "it shouldn't be", generalise to mip (mpy-cross + cache) instead of raising this.
  - >-
    #17485 (resume by default, merged 2026-09-02) makes 291c9f91aa's commit message wrong: "A separate mpremote invocation soft-resets on its first command" no longer holds, so a separate `mpremote debug` after an install that changed files runs the old debugpy. Update the message and the printed warning to say `soft-reset` is needed in every case.
  - >-
    No docs in docs/reference/mpremote.rst and no test in tools/mpremote/tests.
  - >-
    Author/Signed-off-by is the work address on all three commits; the debug stack uses andrew@alelec.net.
  - >-
    /mpy-rules:review pass (never reviewed).
---

### Summary

Getting `debugpy` onto a board is the first thing anyone trying `mpremote debug` hits. Copying the `.py` sources works but is slow to import and heavy on RAM for a package that size, and re-copying on every change is easy to get wrong: a stale `__init__.py` from an earlier install shadows a new `__init__.mpy`, since the importer prefers source.

`mpremote debugpy-install <package-dir>` cross-compiles the package with `mpy-cross` and writes it under the install root the board's own `sys.path` reports (the way `mip` picks its target), then records a marker with a content hash of the sources, the mpy-cross version and flags, and the board's .mpy version. A later run with nothing changed transfers nothing. A board whose .mpy version doesn't match what mpy-cross emits is rejected before anything is compiled.

The install is safe to interrupt. The marker's key is dropped before the first write and only restored once every file has been written and hash-verified, and afterwards the install directory is swept of anything this run didn't write. The cache-hit path also rejects an extra file the marker doesn't know about, since a later `<name>.py` would shadow the installed `.mpy`.

mip's private `_ensure_path_exists` becomes `Transport.fs_ensure_path_exists`, since both need `mkdir -p` over a transport.

The board isn't reset afterwards, matching `mip install`. A write that changed anything says so, because a session that already imported debugpy keeps the old module until a soft reset.

### Testing

On a PYBD-SF6W (filesystem at /flash): a from-scratch install after deleting `/flash/lib/debugpy` and its marker, an immediate re-run reporting up to date with no transfer, the micropython-lib containing folder refused with the inner directory named, and `debugpy-install <dir> + soft-reset + debug` producing a live session in one invocation. The cache, sweep and interrupted-install logic is covered by host tests that live outside this tree, so a reviewer can't run them; they'd need porting to `tools/mpremote/tests` first.

### Trade-offs and Alternatives

See the question above about doing this in mip instead.

### Generative AI

I used generative AI tools when creating this PR, but a human has checked the code and is responsible for the description above.
