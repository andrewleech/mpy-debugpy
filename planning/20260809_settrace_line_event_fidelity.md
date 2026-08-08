# Which line a trace event claims to be on, measured against CPython

Date: 2026-08-09
HEAD: 878c9bc (micropython 49502103e3, micropython-lib 84aaf65)
Story: EPIC-4 / STORY-4.5 (hot-reload), found while asserting per-run stops

Two differences between MicroPython's `sys.settrace` event stream and CPython's,
measured with one script under both interpreters. Neither is a hot-reload
problem; both change what a debugger stops on, so they are recorded here rather
than absorbed into test expectations.

## Measurement

```python
def body(i):
    events.append(("BODY-RAN", i))
    return i

def loop():          # line 9
    total = 0        # line 10
    for i in range(3):   # line 11
        total += body(i) # line 12
    return total     # line 13
```

The tracer records every event in `loop`'s own frame, interleaved with a marker
appended by `body` itself, so an event that claims a line can be placed against
whether that line has actually run.

| MicroPython | CPython |
| --- | --- |
| `call 9`, `line 10`, `line 11`, `line 12`, `line 11`, `line 12`, BODY-RAN 0, `line 11`, `line 12`, BODY-RAN 1, `line 11`, `line 12`, BODY-RAN 2, `line 13`, `return 13` | `call 9`, `line 10`, `line 11`, `line 12`, BODY-RAN 0, `line 11`, `line 12`, BODY-RAN 1, `line 11`, `line 12`, BODY-RAN 2, `line 11`, `line 13`, `return 13` |

**1. A loop body gets one more `line` event than it has executions.** Four
`line 12` events for three calls to `body`, and the marker ordering says which
one is spurious: the first `line 12` fires before `body` has run at all. The
loop's setup is attributing an event to the body line. CPython's four `line 11`
events are 3 iterations plus the exhausted test, and its three `line 12` events
are 1:1 with executions - MicroPython emits no `line 11` for the exhausted test
and an extra `line 12` at entry instead.

Consequence for a debugger: a breakpoint in a loop body stops N+1 times, and the
*first* stop is before the body has run once, so a variable the body assigns
still reads its pre-loop value there. This is a line-attribution artifact of the
loop setup, not a duplicated execution - the program's own arithmetic is
unaffected, which is why nothing but a tracer sees it.

**2. `return` reports the last line the frame executed.** MicroPython's
`return 13` here agrees with CPython, because this frame ends on an explicit
`return`. They diverge when the frame ends any other way: for a function whose
last statement is inside a loop, or an implicit return, MicroPython reports the
line that ran last and CPython reports the line that ended the frame (the `for`
or `def` line).

## What was fixed, and where

The debugger side, in `PdbAdapter.should_stop`: a breakpoint is a hit only for
`call` and `line` events. Without that gate, difference 2 makes a breakpoint on
a function's final line stop twice per call - the second time on a frame that
has already produced its value, where stepping or evaluating locals is
meaningless. `call` has to stay in the set: it reports the `def` line, so it is
the only event that can ever match a breakpoint placed there.

`tests/test_breakpoint_stop_gate.py` runs the real `PdbAdapter` against a real
event stream under the built unix firmware (the module imports
`from micropython import const`, so it is not importable from CPython) and
asserts both halves: two stops for two calls on the last line, two stops on the
`def` line by `call`. It also asserts a `return` event was *delivered* at the
breakpoint line, so a firmware that never produced one could not make the test
pass vacuously. Verified non-vacuous by replacing the gate with `if True:`,
which fails with `[["line", True], ["return", True], ["line", True],
["return", True]]`.

Difference 1 is **not** fixed and not worked around. Fixing it means changing
which line the VM attributes the loop-setup instructions to, in the same
compiler/VM area as `py/profile.c`'s `mp_prof_frame_enter` line bookkeeping, and
it belongs with the settrace lineage (upstream PR #8767) rather than with a
story about re-importing modules. Until then, tests must not assert exact stop
counts for a breakpoint inside a loop body: `tests/test_s4_5_hot_reload.py`
asserts which lines were reached and what the values were there, never how many
times, precisely so it does not cement this artifact as expected behaviour.

## Related

- `20260809_settrace_raise_unwind.md` - the other VM-level settrace finding from
  the same story, fixed in `py/profile.c` and `py/vm.c`.
