---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: mpremote_debugpy_install
status: fork draft #75; upstream review pending
title: "tools/mpremote: Install cross-compiled debugpy on a board"
pushed_branch: https://github.com/andrewleech/micropython/tree/mpremote_debugpy_install
head: 7eafaaea21
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
    #17485 (resume by default, merged 2026-09-02) means a later `mpremote debug` still has the old module imported. The commit message now calls for a soft reset; update the command's printed warning as well.
  - >-
    No docs in docs/reference/mpremote.rst and no test in tools/mpremote/tests.
  - >-
    Author/Signed-off-by is the work address on all three commits; the debug stack uses andrew@alelec.net.
  - >-
    /mpy-rules:review pass (never reviewed).
---

Getting debugpy onto a board is a bit painful if every edit means copying sources again, and an old `.py` can shadow a new `.mpy`. `mpremote debugpy-install <package-dir>` cross-compiles the package, picks the install root from the board's `sys.path`, and uses a source/toolchain/version hash to skip unchanged copies. It invalidates the marker before writing, verifies files after transfer and removes stale files.

I tested fresh install, cache hit and a live debug session on PYBD_SF6W. The host tests for interrupted installs still live outside the mpremote tree. It does not reset the board, so an already-imported debugpy needs an explicit soft reset.

**Not ready beyond fork review:** this branch calls `fs_writefile(..., verify_hash=True)` from upstream micropython#18436 without including it, so the first write fails if #18436 isn't present. Also, this may belong as a general `mip` mpy-cross/cache option rather than a debugpy-only command. I'd like to settle that before raising it upstream.

### Generative AI

I used generative AI tools when creating this PR. I checked the code and am responsible for the change.
