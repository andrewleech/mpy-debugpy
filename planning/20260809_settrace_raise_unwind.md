# A trace callback that raises kills tracing for the whole process

Date: 2026-08-09
HEAD: 878c9bc (micropython 49502103e3, micropython-lib 84aaf65)
Story: EPIC-4 / STORY-4.5 (hot-reload), found during phase-entry revalidation

## Why STORY-4.5 needs a trace callback to be able to raise

Restart has to work while the target is *running*, and the common embedded
shape is a `while True:` main loop that never returns. So a restart cannot
wait for the target to finish: the target has to be unwound out of whatever
frame it is in, back to the launcher loop that will evict `sys.modules` and
re-import.

Nothing else on the device can do that unwinding. There is no second thread,
the DAP message pump runs inside `_trace_function`, and that pump must never
raise (`20260809_story4.3_mount_teardown.md`, defect 1: an exception escaping
the pump lands in whichever line of the debugged program was being traced).
The one remaining mechanism is a deliberate raise from the trace callback
itself, which is also how CPython's debuggers unwind a target.

## What the VM actually did

`mp_prof_callback_invoke` in `py/profile.c` set the recursion guard, called the
callback, and cleared the guard afterwards:

```c
mp_prof_is_executing = true;
mp_obj_t top = mp_call_function_n_kw(callback, 3, 0, a);
mp_prof_is_executing = false;
```

A raising callback leaves via an nlr jump, so the clear never runs and
`mp_prof_is_executing` stays set for the rest of the process. Every trace hook
in the VM tests that flag - `FRAME_ENTER`, `FRAME_UPDATE` and `TRACE_TICK` in
`py/vm.c` all begin `if (!mp_prof_is_executing)` - so from that point on no
trace callback is ever invoked again. `sys.settrace()` still stores the callback
and `sys.gettrace()` still returns it; it is simply never called. Measured: a
fresh `sys.settrace(f)` after one raise yields zero callbacks, and a
different function object does not help either.

In a build with assertions enabled the same state trips
`assert(!mp_prof_is_executing)` at the top of `mp_prof_frame_enter` and
`mp_prof_instr_tick`, which is the shape this would have been noticed in had
anything raised from a callback before now.

So the unwind mechanism STORY-4.5 depends on was self-defeating: the raise
reaches the launcher loop, and the re-imported target then runs with no
tracing at all, meaning no breakpoints, silently.

## The fix, and the semantics chosen with it

`mp_prof_callback_invoke` now wraps the call in `nlr_push`/`nlr_pop` and, on the
exception path, clears the guard and re-raises. Two decisions came with it:

**The trace callback is unset when it raises**, matching CPython ("if there is
any error occurred in the trace function, it will be unset, just like
settrace(None) is called"). The argument is not fidelity for its own sake: a
callback that raised once is on an error path and will raise again, and leaving
it installed turns one error into a raise on every subsequent line - on a
device, unrecoverable. A debugger that means to keep tracing re-installs
explicitly, which the launcher loop does per iteration anyway.

**`TRACE_TICK` now also requires the global callback to still be set** before
dispatching to a frame's own callback. Without this, "unset" does not unset:
frames already on the stack hold the callback the tracer returned for them and
keep calling it, so the raise-per-line storm above is reachable through them,
and `sys.settrace(None)` does not stop tracing the frames that are running.
That last point is not hypothetical - `PdbAdapter.wait_for_continue` drops
`sys.settrace(None)` precisely to let a target resume when the client vanishes,
and until now that only stopped frames entered afterwards.

`tests/misc/sys_settrace_raise.py` covers all of it: propagation into the
traced program, the unset, that a following `sys.settrace()` traces exactly as
much as it did before the raise, and that `settrace(None)` inside a traced frame
stops that frame. It has no `.exp` file deliberately - its output is identical
under CPython, so the test runner compares the two.

## What this means for the unwind exception's base class

A target's own `except Exception:` swallows an `Exception` subclass raised from
the callback, and then the restart silently does not happen. Deriving the
restart signal from `BaseException` passes through `except Exception:`, the same
reasoning that puts `KeyboardInterrupt` there. Verified both ways on the unix
port.

Still swallowable by a bare `except:` or `except BaseException:` in the target,
which is a real limit with no mechanism behind it and belongs in the user-facing
text: a target that catches everything cannot be restarted out of.
