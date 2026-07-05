# mp_debugpy — Background

Starting-point notes on how MicroPython debugpy support fits together, captured from
inspecting the PRs and the checked-out submodule source. This is orientation material,
not a design decision record.

## What this project is

`Josverl/mp_debugpy` is a wrapper repo that pins two MicroPython forks as submodules and
adds tooling, sample code, and docs so that VS Code (or any Debug Adapter Protocol client)
can remotely debug MicroPython — breakpoints, stepping, stack/variable inspection, and
variable modification — over a socket.

## The dependency stack

Three layers, resting on two upstream PRs plus several fork-to-fork PRs.

### Layer 1 — firmware primitive (`micropython/py/`)

Everything rests on `sys.settrace()`, gated behind `MICROPY_PY_SYS_SETTRACE`.

- **[micropython#8767](https://github.com/micropython/micropython/pull/8767)** (andrewleech, *open*)
  — the foundation. Adds `sys.gettrace()`, `sys._getframe()`, and `frame.f_trace` so a
  debugger can install per-frame trace callbacks. Touches `py/modsys.c`, `py/profile.c/.h`,
  `py/objfun.c`. dpgeorge has reviewed (naming nits like `mp_sys__getframe`, null-pointer
  edge cases, questioned whether locals support belongs here).

- **[andrewleech/micropython#3](https://github.com/andrewleech/micropython/pull/3)** and
  **[#4](https://github.com/andrewleech/micropython/pull/4)** (Josverl → into Andrew's fork,
  *closed/merged/superseded by #5*) — local-variable introspection on top of 8767. Add
  `frame.f_locals`, `function.__code__`, and two compile-time flags:
  - `MICROPY_PY_SYS_SETTRACE_SAVE_NAMES` — preserves real local variable names in bytecode
    (new `py/localnames.c/.h`, `py/compile.c` hooks). Without it: placeholder names
    (`local_01`, `local_02`, …).
  - `MICROPY_SAVE_LOCAL_VARIABLE_NAMES` — broader name storage.
  - Also enable `sys.settrace()` by default on the unix standard variant.
  - The `frame._set_local` write-back path used for setVariable comes from this line of work.

### Layer 2 — the debugpy module (`micropython-lib/python-ecosys/debugpy/`)

- **[micropython-lib#1022](https://github.com/micropython/micropython-lib/pull/1022)**
  (andrewleech, *open*) — the origin. A pure-Python DAP server VS Code attaches to.
  ~95% written with Claude Code. Non-blocking to fit MicroPython's single-threaded model.

- **[andrewleech/micropython-lib#2](https://github.com/andrewleech/micropython-lib/pull/2)**
  ("Debugpy/updates 02", Josverl → Andrew's `add-debugpy-support` branch, *open*) — the
  enhancement layer: complex variable handling (objects/lists/dicts with caching), a
  "Special" section for dunders, bare-metal (ESP32) support, path-mapping improvements,
  breakpoint optimizations, DAP-monitor CLI, ruff formatting, type hints.

### Layer 3 — this repo (`Josverl/mp_debugpy`)

Usability wrapper. Pins the two Josverl forks as submodules and adds tooling + samples + docs.

## Two divergent fork lineages (important)

The PRs listed above live under **andrewleech's** forks, but this repo's submodules point at
**Josverl's** forks under different branch names:

| Submodule       | URL                         | Branch            | Pinned SHA |
|-----------------|-----------------------------|-------------------|------------|
| `micropython`   | josverl/micropython.git     | `pdb_support_jos` | `05b7818`  |
| `micropython-lib` | josverl/micropython-lib.git | `debugpy/jos`   | `1b49992`  |

They carry the same work under different branch names, so "the branch in the submodule" is not
1:1 with "the PR branch". Anything upstreamed needs reconciling back to Andrew's
`add-debugpy-support` / `8767` / `1022` branches.

## Repo layout (this wrapper)

- `launcher/` — usability layer:
  - `compile_debugpy.py` — cross-compiles the debugpy `.py` sources listed in `debugpy.json`
    to `.mpy` via `mpy-cross -O2` (saves device RAM / import time).
  - `debugpy.json` (source manifest) / `debugpy_mpy.json` (compiled manifest) — `mpremote mip
    install` targets.
  - `mpy_launch_debugpy_{unix,esp32}.py` — entry points: `debugpy.listen()` →
    `debugpy.breakpoint()` → `debug_this_thread()` → import & call a target module/method so
    VS Code can attach and step through it.
  - `upload_all.py` — device upload helper.
- `src/` — demo/target scripts (`target.py`, `pystone.py`, complex-var demos,
  `check_settrace.py`).
- `tests/` — host-side pytest harness with a `fake_vscode` DAP client + `attach_server` /
  `debuggee` fixtures driving initialize → attach → breakpoints → continue end-to-end.
- `firmware/` — prebuilt binaries for flag combinations (`unix_settrace_save_names`,
  `unix_settrace_set_local`, `esp32_settrace_save_names`, …).
- `docs/` — `DAP_SETVARIABLE_FINAL.md`, `SETVARIABLE_SOLUTION.md`.

## debugpy server architecture (`python-ecosys/debugpy/`)

Modules:

```
public_api.py       → listen()/breakpoint()/debug_this_thread()  (module-level singleton _debug_session)
server/debug_session.py → DebugSession: DAP request router + the trace function
server/pdb_adapter.py   → PdbAdapter: breakpoint matching, stepping state, variable inspection
common/messaging.py     → JsonMessageChannel: DAP wire framing (Content-Length + JSON)
common/constants.py     → const() DAP strings (RAM optimization)
```

### Central design constraint: no server thread

MicroPython is single-threaded with no reliable background sockets, so all DAP message
processing is **piggybacked onto the trace callback**:

1. `listen()` (`public_api.py:12`) binds, blocks on `accept()`, handles *only* the `initialize`
   request synchronously, then sets the socket to a **1 ms timeout** (`settimeout(0.001)`) and
   returns control to user code. Everything after initialize is handled inside the trace fn.
2. `debug_this_thread()` installs `DebugSession._trace_function` via `sys.settrace`.
3. `_trace_function` (`debug_session.py:453`), per `call`/`line`/`return` event:
   - `process_pending_messages()` — drains socket non-blockingly (setBreakpoints, scopes,
     variables arrive here), then
   - `pdb.should_stop()` — on breakpoint/step match, send `stopped` event and
     `wait_for_continue()`.
4. `wait_for_continue()` (`pdb_adapter.py:284`) is a **busy-poll loop**:
   `while not continue_event: process_pending_messages(); sleep(0.01)`. The program spins
   inside the trace callback servicing DAP requests until `continue`/`next`/`stepIn` sets
   `continue_event = True`. This is how a "paused" debugger works without threads.

### Stepping state machine (`should_stop`, `pdb_adapter.py:199`)

- **breakpoint**: `filename in breakpoints and lineno in breakpoints[filename]` (hot dict
  lookup on every line).
- **STEP_INTO**: stop on next `call` or `line`.
- **STEP_OVER**: stop on next `line` in the same frame; on `return` from that frame, re-target
  `f_back`.
- **STEP_OUT**: stop on `return` from the step frame.

Leans on firmware features: `frame.f_back` (8767), `frame.f_locals`/`f_globals` (#3/#4),
`frame._set_local` for write-back.

### Variable inspection — `variablesReference` integer encoding

DAP addresses everything by an integer `variablesReference`, overloaded three ways:
- `frame_id * 1000 + {1,2,3,4}` → scope refs (locals=1, globals=2, + "Special" folders 3/4 for
  dunders). Built in `get_scopes` (:345), decoded at :603 by `//1000` and `%1000`.
- `>= 10000` (`VARREF_COMPLEX_BASE`) → entry in `VariableReferenceCache`, a FIFO-evicting dict
  (max 50) holding expandable objects for lazy drill-in (`_expand_complex_variable`, :522).

### Path mapping (host ↔ target)

`pathMappings` from the attach request (`localRoot`/`remoteRoot`) drive
`_filename_as_debugger` / `_filename_as_debugee` (:139–173) — prefix-swap translation so VS
Code's `${workspaceFolder}/src/x.py` lines up with the target's `x.py`. Main pain point Jos
called out in the 1022 review.

## Known rough edges (observed in source)

1. **`_filename_as_debugger` dead code** (`pdb_adapter.py:152`): two identical `for` loops over
   `path_mappings` with the same guard — the second can never fire (first already returned).
   Its `//`-stripping branch is unreachable.
2. **`should_stop` computes the breakpoint match twice** (:209 vs :219). The first block only
   logs and discards its result; the real decision is the second. Pure overhead on the hot path.
3. **Busy-wait paused loop** (`sleep(0.01)`): target burns CPU while paused; on ESP32 this
   blocks WiFi/other housekeeping.
4. **`breakpoint()` fallback is broken** (`public_api.py:114`):
   `hasattr(__builtins__, "breakpoint")` — `__builtins__` is a dict in module context, so this
   is unreliable. Only matters when no session exists.
5. **`_baremetal` heuristic** (`debug_session.py:60`): `sys.platform not in ("linux")` treats
   everything except unix-linux as bare metal, disabling the `source` request (ESP32 "can't
   read source" workaround at :432). Coarse but pragmatic.
6. **`evaluate` / `set_variable` use real `eval()`** against frame globals/locals — an
   unauthenticated remote-eval endpoint by design (expected for a debugger, worth noting).
7. **Flag double-definition**: `MICROPY_PY_SYS_SETTRACE` gets defined by both 8767 and 1022's
   build glue — flagged in the 1022 review.
8. **README candor**: several "To be reviewed" markers (ESP32 flashing steps, variable-support
   claims, version info).

## Build / run essentials

- Firmware must be built with `MICROPY_PY_SYS_SETTRACE=1` (unix:
  `make CFLAGS_EXTRA="-DMICROPY_PY_SYS_SETTRACE=1"`); optionally
  `MICROPY_PY_SYS_SETTRACE_SAVE_NAMES=1` for real local names.
- Install module: `python launcher/compile_debugpy.py` then
  `mpremote mip install launcher/debugpy_mpy.json` (compiled) or `debugpy.json` (source).
- Source must be **copied** to the device filesystem (mpremote mount does not work).
- Target must be network-reachable; VS Code attaches via `launch.json` with `pathMappings`.

## Open questions / next possible directions

- Trace the firmware side (`py/profile.c`, `sys._getframe`, `_set_local`) to confirm exactly
  what the C layer exposes vs. what the Python server assumes.
- Reconcile the Josverl vs andrewleech fork lineages before any upstreaming.
- Decide which rough edges are worth cleaning up vs. leaving for the upstream PR review.

## Firmware (C) internals

Traced against the `micropython` submodule at `05b7818` (branch `pdb_support_jos`).
All paths below are relative to that submodule root (`micropython/`). Line numbers are
from the checked-out source.

### 1. Frame object (`py/profile.c`, `py/profile.h`)

The frame type `mp_type_frame` is defined at `py/profile.c:252-258`. Its C struct is
`mp_obj_frame_t` (`py/profile.h:37-47`): it holds `code_state` (borrowed pointer into the
live VM `mp_code_state_t`), `back`, `callback`, `code`, `lasti`, `lineno`, `trace_opcodes`,
and `f_trace`. Frames are created by `mp_obj_new_frame` (`py/profile.c:260-288`), which also
builds a `code` object; `lasti`/`lineno` are derived from the current instruction pointer.

Attribute access goes through `frame_attr` (`py/profile.c:93-136`). Readable attributes:

- `f_back` (`:111-116`) — returns `mp_const_none`, or the previous frame if
  `code_state->prev_state->frame` is set. So `f_back` **does exist**; it walks the live VM
  call chain, not a captured copy.
- `f_code` (`:117-119`) — the code object.
- `f_globals` (`:120-122`) — `code_state->fun_bc->context->module.globals`, i.e. the **live**
  module globals dict (same object the running code mutates).
- `f_lasti` (`:123-125`) — bytecode offset (small int).
- `f_lineno` (`:126-128`) — current line (small int).
- `f_trace` (`:129-131`) — the per-frame trace callback.
- `f_locals` (`:132-134`) — delegates to `frame_f_locals`.

Writable attributes: `frame_attr`'s store branch (`:96-104`) handles **only** `f_trace`.
There is no store handler for `f_locals` or any `_set_local`.

`frame.f_locals` (`frame_f_locals`, `py/profile.c:138-250`) builds and returns a **fresh
snapshot dict** on every access (`mp_obj_new_dict`, `:144`). It is not a live view: mutating
the returned dict does not write back into `code_state->state[]`. Returns `MP_OBJ_NULL` if the
GC is locked (`:140-142`).

How the dict is populated depends on `MICROPY_PY_SYS_SETTRACE_SAVE_NAMES`:

- **With SAVE_NAMES** (`:153-226`): real names come from `raw_code->local_names`
  (see area 5). Locals are matched to VM slots by a "reverse slot assignment" heuristic
  (`total_locals - 1 - order_idx`, `:211`) with a fallback to `runtime_slot`
  (`:218-224`). Note a latent bug: the parameter loop at `:162-183` computes `var_name_qstr`
  for function parameters but never calls `mp_obj_dict_store`, and the locals loop skips
  `local_num < param_count` (`:200`). Net effect: **function parameters are omitted from
  `f_locals`** in the SAVE_NAMES path; only non-parameter locals are emitted. Name storage is
  also capped at `MICROPY_PY_SYS_SETTRACE_NAMES_MAX` (32) slots.
- **Without SAVE_NAMES** (`:228-248`): names are synthesized as `local_1`, `local_2`, …
  (`vstr_printf(..., "local_%d", order_idx + 1)`, `:240`) over the reverse-slot walk. These
  are positional placeholders, not source names. (The docs elsewhere say `local_01`; the
  actual format is `local_%d` with no zero-padding.)

### 2. `sys` trace primitives (`py/modsys.c`)

Gated by `MICROPY_PY_SYS_SETTRACE` (`py/modsys.c:235-256`), registered in the module dict at
`:338-342`:

- `sys.settrace(tracefunc)` (`:237-240`) → `mp_prof_settrace` (`py/profile.c:316-323`). Stores
  the callback in thread state (`prof_trace_callback`); a non-callable argument clears it.
  Returns `None` (no previous-callback return value, unlike CPython).
- `sys.gettrace()` (`:242-245`) → `mp_prof_gettrace` (`py/profile.c:325-330`). Returns the
  callback or `None`.
- `sys._getframe([depth])` (`:248-255`, exported as `mp_sys_getframe_obj`) → `mp_prof_get_frame`
  (`py/profile.c:332-350`). `depth` defaults to 0; walks `prev_state` `depth` times and raises
  `ValueError("call stack is not deep enough")` if it runs off the top. Note the underscore
  name `_getframe` (matching CPython's private spelling); there is no public `sys.getframe`.

Limitations: the trace callback receives `call`/`line`/`return`/`exception` events
(`mp_prof_instr_tick`, `py/profile.c:414-468`); `opcode` events are stubbed out
(`if (false)`, `:463`). Line events fire only on line-number change (`:445`). Enabling settrace
forces `MICROPY_PERSISTENT_CODE_SAVE` (`py/mpconfig.h:342`, enforced by
`#error` at `py/profile.c:37-41`), because per-line metadata must be retained.

### 3. Local write-back (`frame._set_local` / `sys._set_local_var`)

**Not present in this checkout.** `grep -rn "_set_local\|set_local_var"` over `py/` and
`ports/unix/` returns nothing. `frame_attr`'s store branch (`py/profile.c:96-104`) handles only
`f_trace`, and there is no `_set_local` method on the frame type. There is no `sys._set_local_var`
in `py/modsys.c`.

The Python server calls it defensively: `pdb_adapter.py:771` does
`if hasattr(frame, "_set_local"): frame._set_local(name, new_value)` and otherwise raises
"This MicroPython build doesn't support local variable modification" (`:777-783`). So against
this firmware, **setVariable on a local silently fails over to the error path** — local
modification is a no-op here. The docstring's reference to `sys._set_local_var`
(`pdb_adapter.py:722`) describes an API this firmware does not implement.

Where write-back *would* be constrained even if present: the Python side already restricts it to
the topmost frame (`frame_id != 0` raises, `pdb_adapter.py:735-736`), consistent with the fact
that locals live in slot-based `code_state->state[]` storage rather than a dict.

Global write-back does work: `frame.f_globals` is the live module dict, so
`globals_dict[name] = new_value` (`pdb_adapter.py:761`) mutates the running program.

### 4. Code object attributes (`py/objfun.c`, `py/objcode.c`)

`function.__code__` is served by `mp_obj_fun_bc_attr` (`py/objfun.c:361-383`), but only under
`#if MICROPY_PY_SYS_SETTRACE` (`:374-382`) — so `__code__` exists **only** in settrace builds.
The same function also exposes `__name__` (`:366-368`) and `__globals__` (`:369-372`)
unconditionally (subject to `MICROPY_PY_FUNCTION_ATTRS`).

The code object type `mp_type_code` (`py/objcode.c:149-155`) exposes via `code_attr`
(`:110-147`): `co_code` (`:119`), `co_consts` (`:125`), `co_filename` (`:128`, qstr index 0 of
the context), `co_firstlineno` (`:131`), `co_name` (`:134`, the block name qstr), `co_names`
(`:137`, returns `dict_locals` — note this is a dict, not CPython's tuple of names), and
`co_lnotab` (`:140`, lazily built). All are read-only (`code_attr` returns early on store,
`:111-114`).

### 5. Compile-time flags (`py/mpconfig.h`, `py/localnames.*`, `py/compile.c`)

- `MICROPY_PY_SYS_SETTRACE` — default `0` (`py/mpconfig.h:1571-1572`). Gates all of area 1/2,
  `__code__` (area 4), and forces `MICROPY_PERSISTENT_CODE_SAVE` (`:342`) and
  `MICROPY_PY_BUILTINS_CODE_FULL` (`:1163`).
- `MICROPY_PY_SYS_SETTRACE_SAVE_NAMES` — default `0` (`py/mpconfig.h:242-243`). When on,
  compiles `py/localnames.c` and switches `frame_f_locals` to the real-name path. The compiler
  hook that records names is in `py/compile.c:3441-3450` (`mp_local_names_add(...)` per local id),
  guarded by the flag at `py/compile.c:41`.
- `MICROPY_PY_SYS_SETTRACE_NAMES_MAX` — `32`, defined in `py/localnames.h:35` (only when
  SAVE_NAMES is on). Hard cap on stored local names per scope.
- `MICROPY_SAVE_LOCAL_VARIABLE_NAMES` — **does not exist anywhere in this checkout**
  (`grep -rn` over the whole submodule returns nothing). The name storage flag in this tree is
  solely `MICROPY_PY_SYS_SETTRACE_SAVE_NAMES`.

`py/localnames.c` (`mp_local_names_add`, `:68-102`) stores `local_num → qstr` plus an
order/`runtime_slots` mapping; `frame_f_locals` consumes these via
`mp_local_names_get_name` / `_get_local_num` / `_get_runtime_slot`.

Unix `standard` variant: both flags are **commented out** in
`ports/unix/variants/standard/mpconfigvariant.h:30-31`. So the default unix `standard` build
does **not** enable settrace; it must be turned on via `CFLAGS_EXTRA` (as the build notes
elsewhere in this doc describe) or a variant/board that sets them. This contradicts the
"enable sys.settrace by default on the unix standard variant" note in the Layer-1 summary above
— it is not enabled by default in this submodule state.

### 6. Gaps / mismatches (C provides vs. Python server assumes)

1. **Local modification is unimplemented here.** `pdb_adapter.set_variable` expects
   `frame._set_local` / `sys._set_local_var`; neither exists in this firmware. The `hasattr`
   guard (`pdb_adapter.py:771`) makes this degrade to an error rather than a crash, but DAP
   `setVariable` on locals does not work against `05b7818`. Only global setVariable works.
2. **`f_locals` is a snapshot, not live.** Each read allocates a new dict
   (`py/profile.c:144`). Any code path that writes `frame.f_locals[name] = x` expecting it to
   affect execution would be silently ineffective. The server correctly avoids this and routes
   through `_set_local` instead (which is then absent — see gap 1).
3. **`f_back` exists** (`py/profile.c:111-116`), so the server's defensive
   `hasattr(frame, "f_back")` (`pdb_adapter.py:245,336`) and the comment "MicroPython doesn't
   have f_back" (`:335`) are outdated for this firmware; `f_back` returns `None` at the top of
   the chain rather than being missing.
4. **Function parameters missing from `f_locals` in SAVE_NAMES mode** (`py/profile.c:162-183`
   never stores them; `:200` skips them). A debugger inspecting a function's arguments by name
   will not see them in the locals scope in the real-name build. The placeholder path
   (no SAVE_NAMES) emits every occupied slot as `local_N`, so arguments appear there but only
   as positional placeholders.
5. **Placeholder name format** is `local_%d` (1-based, `py/profile.c:240`), e.g. `local_1`,
   not the `local_01` spelling used in prose elsewhere.
6. **`co_names` is a dict**, not the CPython tuple-of-names (`py/objcode.c:137-139` returns
   `dict_locals`). Consumers assuming CPython semantics for `co_names` would be wrong.
7. **`__code__` only under settrace** (`py/objfun.c:374`). Any non-settrace introspection of
   `function.__code__` fails; acceptable because the whole debugger requires settrace anyway.
8. **`sys.settrace` returns `None`, not the previous tracer** (`py/profile.c:322`), and there is
   no `opcode` trace event (`:463`). Steppers relying on opcode granularity or on save/restore of
   a prior tracer will not get CPython behaviour.
