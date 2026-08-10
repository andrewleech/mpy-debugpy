# Loop line events: what a backward jump can fix, and what the .mpy line table forbids

Date: 2026-08-10
HEAD: b302111 (micropython 4eaafbc5bc, micropython-lib b829073)
Story: risk-register follow-up to STORY-4.5, closing the row opened by
`20260809_settrace_line_event_fidelity.md`

The 2026-08-09 note recorded one distortion in MicroPython's `line` event stream
around loops and prescribed a fix: change which line the VM attributes the
loop-setup instructions to. Measuring the three loop forms separately shows
there are three distortions rather than one, that the largest of them is fixable
in the VM without touching line attribution at all, and that the prescribed fix
for the original one is not reachable in the compiler.

## Measurement

Same method as the 2026-08-09 note: one script traced under the built unix
firmware and under CPython 3, counting `line` events per source line. Bodies of
two statements, so "first body line" and "last body line" can be told apart.
Scripts under
`/tmp/.../scratchpad/{loopforms,twoline,oneliners,whiletrue}.py`; the numbers
below are per-line event counts for a loop of three iterations.

| form | line | before | after | CPython |
| --- | --- | --- | --- | --- |
| `for i in [0,1,2]` | `for` header | 7 | 4 | 4 |
| `for i in [0,1,2]` | body lines | 3, 3 | 3, 3 | 3, 3 |
| `for i in range(3)` | `for` header | 4 | 4 | 4 |
| `for i in range(3)` | first body line | 3 | 3 | 3 |
| `for i in range(3)` | last body line | 4 | 4 | 3 |
| `while i < 3` | `while` test | 1 | 1 | 4 |
| `while i < 3` | first body line | 3 | 3 | 3 |
| `while i < 3` | last body line | 4 | 4 | 3 |
| `for i in [0,1,2]: body` (one line) | that line | 4 | 4 | 4 |
| `for i in range(3): body` (one line) | that line | 1 | 4 | 4 |
| `while i < 3: body` (one line) | that line | 1 | 4 | 3 |

The one remaining over-count the fix introduces is the last row: a one-line
`while` now reports four times against CPython's three, which is distortion 2
arriving in a loop small enough that its bottom test shares the body's line.

## The three distortions

**1. General `for` loops reported the header line twice per iteration.**
`MP_BC_FOR_ITER` cleared `frame->lineno` on every iteration so that a one-line
`for` would re-report itself. The clear fires unconditionally, so a multi-line
`for` - where the header line has just been reported by the `FOR_ITER`
instruction itself - reports it a second time. Nested loops compound it: the
inner header of a two-deep loop was reported 10 times where CPython reports 6.

**2. Loop-control code emitted after the body inherits the last body line.**
Both `compile_for_stmt_optimised_range` and `compile_while_stmt` put the loop
test at the *bottom* of the bytecode and enter it with a forward jump, so one
branch per iteration instead of two. The test therefore carries whatever source
line the body ended on. Reaching it fires an event for the last body line at a
point where the body has not run, which is difference 1 of the 2026-08-09 note.
For a `while` loop the same fact means the `while` line itself is only ever
reported once, at the initial forward jump.

**3. Loops whose whole body is on the header's line iterated silently**, unless
they were general `for` loops rescued by distortion 1's clear. `for i in
range(3): t += i` produced a single `line` event covering all three iterations,
because no instruction in the loop ever changed the reported line. A debugger
stepping such a loop never comes back round.

## What was fixed

`py/vm.c`: the `FOR_ITER` clear is replaced by invalidating `frame->lineno` on a
*backward* jump, in `MP_BC_JUMP`, both `POP_JUMP_IF_*` and `MP_BC_UNWIND_JUMP`.
This is the rule CPython uses - a line event fires at the start of a line or
when the instruction is a backward jump - rather than a special case for one
opcode, and it is what distortions 1 and 3 both wanted: re-entering code that
has already been reported is exactly where a repeat belongs, and leaving a line
by a forward jump is not.

Cost is a sign test on three opcodes, compiled out entirely when
`MICROPY_PY_SYS_SETTRACE` is off. The offset is cast to `ptrdiff_t`, not
`mp_int_t`: `DECODE_SLABEL` yields a `size_t`, and under `MICROPY_OBJ_REPR_D`
`mp_int_t` is `int64_t` while `size_t` may be 32-bit, which would make every
jump look forward.

The general-`for` case is now byte-identical to CPython on every line of the
table, and both one-liner `for` forms are exact. `tests/misc/sys_settrace_generator.py.exp`
moves with the fix: three duplicate events disappear from a `for i in gen:` loop
and two appear in a `while True: r += gen.send(None)` loop, both toward CPython.

## What cannot be fixed there, and why not

Distortion 2 is what the risk row asked for, and the prescribed fix - attribute
the loop-control code to the loop header line - is not expressible. The .mpy
line-number table is monotonically non-decreasing by construction:
`mp_emit_bc_set_source_line` (`py/emitbc.c:459`) records a line only when
`source_line > emit->last_source_line`, and `emit_write_code_info_bytes_lines`
encodes positive byte and line skips only. Code emitted after the body cannot
carry a line number from before it without a change to the .mpy format, which is
far out of proportion to a tracer artifact.

The alternative is to move the test to the top of the loop and jump back
unconditionally at the bottom, which is where the line number would then be
correct. That is a second branch per iteration on every build, for a benefit
only a tracer sees. Rejected.

So distortion 2 stands, with a narrower statement than the 2026-08-09 note gave
it:

- It is confined to the **last source line of the body** of an optimised-`range`
  `for` loop or a `while` loop. Every other body line, and the `for` header
  line, is 1:1 with CPython.
- It fires once per *entry* to that bottom test with a different line last
  reported - once per loop for a simple loop, once per outer iteration when the
  loop wraps another loop (measured 7 against CPython's 4 for the body of a
  two-deep nest).
- A `while` loop additionally under-reports: its test line gets one event per
  frame where CPython gives one per iteration.
- General `for` loops - anything not `for x in range(...)` - are unaffected by
  all of it.

## Consequence for tests

The 2026-08-09 prohibition ("no test may assert a stop count for a breakpoint
inside a loop") is wider than the remaining defect. It holds for a breakpoint on
the last line of a `range`/`while` loop body, and for a breakpoint on a `while`
line. A breakpoint on a `for` header, on a general-`for` body line, or on any
body line but the last is now exact and may be counted.

`tests/hil/test_hil_network.py::test_hil_a_loop_breakpoint_fires_on_every_iteration`
breakpoints on a `for _ in range(3):` header and asserts stops exceed iterations
by one. That count is unchanged by this fix and agrees with CPython, which also
reports the header four times for three iterations - three entries plus the
exhausted test, against MicroPython's one entry plus three re-entries.

## Where the change belongs

`py/vm.c` only, and it is a fix to behaviour that is already on upstream master:
`MICROPY_PY_SYS_SETTRACE` and the `FOR_ITER` clear both predate the #8767
lineage. It is therefore its own upstream micropython PR rather than part of
#8767 or of the local-names work, and it carries the
`tests/misc/sys_settrace_generator.py.exp` update with it.

## Related

- `20260809_settrace_line_event_fidelity.md` - the original measurement; carries
  a dated correction pointing here.
- `20260809_settrace_raise_unwind.md` - the other VM-level settrace finding from
  STORY-4.5, fixed at the time.
