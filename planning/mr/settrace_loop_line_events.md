---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: settrace_loop_line_events
status: pushed to fork, description written, no PR
title: "py/vm: Fire settrace line events on backward jumps, not FOR_ITER."
pushed_branch: https://github.com/andrewleech/micropython/tree/settrace_loop_line_events
head: 879d3b7c17
relationship: |
  Independent of #8767 (sys.settrace line events are already upstream). It
  does affect expected line-event streams, so local_names_implementation's
  sys_settrace_localnames_comprehensive.py.exp, whose commit message blames a
  "spurious line event on the first iteration" of a range loop, may need
  regenerating once both are on master.
todo:
  - >-
    Rebase onto current upstream/master (140 behind; merge-tree clean).
  - >-
    Check the local_names_implementation .exp files against a tree with this applied.
  - >-
    Code size: the check adds a branch to four jump opcodes, only when MICROPY_PY_SYS_SETTRACE is enabled. Get a number from a settrace-enabled build for the PR.
  - >-
    /mpy-rules:review pass (never reviewed).
---

### Summary

Stepping through loops in a debugger showed the wrong lines. A line event fires when the current instruction's source line differs from the last one reported, and `MP_BC_FOR_ITER` clears the last-reported line on every iteration so a single-line loop body still reports each time. That over-corrects: a multi-line `for` reports its header line twice per iteration, since FOR_ITER has just reported it. A two-deep loop reported the inner header ten times where CPython reports six. It also doesn't help `while` or `range` loops, which don't go through FOR_ITER, so those report their header line once for the whole loop.

The reset now happens on a backward jump instead, which is the rule CPython uses: re-entering code already reported is exactly where a repeat belongs, and leaving a line by a forward jump isn't. General `for` loops now match CPython's line event stream exactly, and `range` / `while` loops whose body is on the header's line iterate visibly.

The offset is cast to `ptrdiff_t` rather than `mp_int_t`, because `DECODE_SLABEL` gives a `size_t` which under `MICROPY_OBJ_REPR_D` is narrower than `mp_int_t` and the sign test would never fire.

### Testing

`tests/misc/sys_settrace_generator.py.exp` changes with the fix, both ways toward CPython: three duplicate events disappear from a `for i in gen:` loop and two appear in a single-line `while True:` loop. I measured the three loop forms against CPython 3.12 with two-statement bodies so the first and last body lines can be told apart, on both a plain-master coverage build and one carrying #8767. `sys_settrace_features.py` has no `.exp` and fails against CPython on master both before and after this change, with byte-identical output.

### Trade-offs and Alternatives

Compiles out entirely without `MICROPY_PY_SYS_SETTRACE`. With it, four jump opcodes gain a sign test.

One difference from CPython remains. The loop test of a `range` / `while` loop is compiled at the bottom of the loop, so it inherits the last body line's number, and the line table only records increasing lines (`py/emitbc.c`), so the test can't be attributed to the header without an .mpy format change. Moving the test to the top would cost every build a second branch per iteration for something only a tracer sees, so I've left it.

### Generative AI

I used generative AI tools when creating this PR, but a human has checked the code and is responsible for the description above.
