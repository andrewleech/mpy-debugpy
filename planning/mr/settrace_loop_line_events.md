---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: settrace_loop_line_events
status: fork draft #73; upstream review pending
title: "py/vm: Report loop lines again after backward jumps"
pushed_branch: https://github.com/andrewleech/micropython/tree/settrace_loop_line_events
head: 9b2f00d88e
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

Stepping through loops showed duplicate `for` headers and missing repeats for `while` / `range` loops. `FOR_ITER` was clearing the last reported line every iteration, while other loops never went through it. Clearing it on a backward jump instead reports a line when control returns to it, including single-line loop bodies.

To see the trace, run this on a unix build with settrace enabled and under CPython:

```python
import sys

def trace(frame, event, arg):
    if frame.f_code.co_name == 'loop' and event == 'line':
        print(frame.f_lineno, end=' ')
    return trace

def loop():
    for i in (0, 1):
        x = i
        x += 1

sys.settrace(trace)
loop()
sys.settrace(None)
print()
```

Saved as written, the current unix build reports `9 10 11 9 10 11 9 11`; CPython 3 reports `9 10 11 9 10 11 9`. The final `11` remains because the bytecode line table can't attribute the bottom-of-loop test to the header. The existing generator test expectation also changes. This compiles out without settrace; a settrace-enabled code-size number is still needed.

### Generative AI

I used generative AI tools when creating this PR. I checked the code and am responsible for the change.
