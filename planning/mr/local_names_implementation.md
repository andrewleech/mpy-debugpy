---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: local_names_implementation
status: pushed to fork; needs cleanup before it can be proposed (see todo)
title: "py/profile: Report real local variable names in frame.f_locals."
pushed_branch: https://github.com/andrewleech/micropython/tree/local_names_implementation
head: 42fc0e685e
depends_on: pdb_support
relationship: |
  Stacked on the first commit of micropython PR #8767 (3280c2a5ea, frame
  f_locals / _getframe / f_trace); PR it against master as "depends on
  #8767" with the #8767 commit at the bottom, or wait for #8767 to merge.
  Does not contain #8767's second commit (a764b6cd8c); merge-tree with the
  current #8767 head is clean.

  c59a2ee77a is authored by Jos Verlinde (@Josverl), who started this work on
  the #1022 thread in June 2025. Keep his authorship and check with him
  before raising.

  settrace_loop_line_events changes line-event streams; the .exp files here
  were generated without it.
todo:
  - >-
    Rebase onto current upstream/master (822 behind, base 2dc2e30d98 from 2026-03-21; merge-tree clean, nothing built or run on the new base).
  - >-
    Remove the commented-out MICROPY_DEBUG_VERBOSE / MICROPY_COMP_* knobs c59a2ee77a adds to ports/unix/variants/standard/mpconfigvariant.h.
  - >-
    Decide what to do with MICROPY_PY_SYS_SETTRACE_LOCALNAMES_PERSIST. As committed it's off by default because enabling it corrupts sys.settrace() line numbers for all code (the code-info reader was never taught to skip the appended data), and mp_raw_code_get_local_name() still has a "TODO: implement in Phase 2" stub for it. Upstream won't take a known-broken option. Either fix the reader and finish it, or drop 068b91301e and tests/basics/sys_settrace_localnames_persist.py and do .mpy support as a follow-up.
  - >-
    Tests reference "Phase 1" / "Phase 2" and a placeholder phase; rewrite as plain behaviour tests. The tests are split across tests/basics and tests/misc with overlapping coverage (five files); consolidate.
  - >-
    docs/develop/sys_settrace_localnames.rst is 456 lines of internals; check what a maintainer wants in docs/develop vs the commit message.
  - >-
    Enabling LOCALNAMES on the unix standard variant adds a pointer + uint16 to every mp_raw_code_t there plus the name arrays. Measure heap and binary size on unix standard for the PR.
  - >-
    History: 9 commits including a fix (3dff4d7c70 slot indexing) and a format-only commit (42fc0e685e) that fix earlier commits in the same series. Fold them so each commit builds and passes; micropython doesn't squash on merge.
  - >-
    Author/Signed-off-by is the work address on the non-Jos commits.
  - >-
    /mpy-rules:review pass (never reviewed).
---

### Summary

With #8767, `frame.f_locals` returns the values on a frame but MicroPython doesn't keep local variable names after compilation, so a debugger can only show `local_01`, `local_02` and so on. That's the first thing anyone notices when stopped at a breakpoint in VS Code.

With `MICROPY_PY_SYS_SETTRACE_LOCALNAMES` enabled, the compiler records the source name of each local and cell variable on the raw code object, and `frame.f_locals` reports values under those names. Code without names (the option off, or a function loaded from .mpy) falls back to the `local_NN` placeholders, so nothing gets less visible.

Locals sit at the top of the frame's state array in reverse order (`n_state - 1 - local_num`, as `vm.c` addresses them), so the lookup maps each slot back to its compile-time index before looking up the name. Getting that wrong shows values under the wrong names for any function with more than one local, which is what the regression tests check for.

The option is off by default and needs `MICROPY_PY_SYS_SETTRACE`. This enables it on the unix standard variant, which already enables settrace.

### Testing

Unix standard variant: the new settrace locals tests cover nested functions, loops, exceptions, parameters and edge cases (empty functions, `None`, containers). Used in VS Code debugging sessions on a PYBD_SF6; it also builds for RPI_PICO_W and ESP32_GENERIC, but I haven't run those boards with this version. Not tested with .mpy-loaded code beyond confirming the placeholder fallback.

### Trade-offs and Alternatives

Memory: a pointer and a `uint16_t` per raw code object, plus a qstr per local, only when the option is enabled. Names are interned qstrs so most cost nothing extra.

Storing names in the .mpy so frozen and precompiled code gets them too is the obvious next step, but it changes the bytecode's code-info section and I'd rather do it separately.

### Generative AI

I used generative AI tools when creating this PR, but a human has checked the code and is responsible for the description above.
