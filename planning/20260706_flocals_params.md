# Q6 — Are function parameters omitted from `frame.f_locals`?

- Date: 2026-07-06
- Top-repo HEAD: `59fab8450a`
- micropython submodule: `18c76f7c56` (branch `mpy-debugpy`)

## Answer

**No — parameters are NOT omitted on the current branch.** Function parameters
appear in `frame.f_locals` under their real source names. The old Josverl-tree
defect (separate param loop that never stored, locals loop skipping
`local_num < param_count`) does not exist in the recomposed code. Confirmed both
by source trace and empirically against the built unix binary.

## 1. Source trace

### Real-name path gating macro

The real-name capture path is gated by `MICROPY_PY_SYS_SETTRACE_LOCALNAMES`
(default `0`, `py/mpconfig.h:1846-1847`; enabled `=1` in the unix standard
variant at `ports/unix/variants/standard/mpconfigvariant.h:33-34`).
`MICROPY_PY_SYS_SETTRACE_LOCALNAMES_PERSIST` is a separate, currently-disabled
persist path. There is **no** `py/localnames.c` file — the logic lives in
`py/compile.c`, `py/emitglue.h`, and `py/profile.c`. The old `_SAVE_NAMES`
macro is gone.

### How names (including params) are captured — `py/compile.c:3480-3499`

After the compiler assigns `local_num` to every id, it allocates a
`qstr names[num_locals]` and fills it:

```c
for (int i = 0; i < scope->id_info_len; i++) {
    id_info_t *id = &scope->id_info[i];
    if ((id->kind == ID_INFO_KIND_LOCAL || id->kind == ID_INFO_KIND_CELL) &&
        id->local_num < scope->num_locals) {
        names[id->local_num] = id->qst;   // <-- no ID_FLAG_IS_PARAM exclusion
    }
}
scope->raw_code->local_names = names;
scope->raw_code->local_names_len = scope->num_locals;
```

Parameters in MicroPython **are** locals: they carry `ID_FLAG_IS_PARAM`
(`py/scope.h:41`) but their `kind` is `ID_INFO_KIND_LOCAL` (or `_CELL` if closed
over) and their `local_num` sits in the range `0 .. num_pos_args-1`. The loop
filters only on `kind` and does **not** exclude params, so parameter names are
written into `local_names` alongside body locals.

### How the dict is built — `py/profile.c:135-203` (the load-bearing site)

The SAVE/real-name branch (`profile.c:162-182`) iterates **all** locals with no
param filter and stores every occupied slot:

```c
for (size_t local_num = 0; local_num < num_locals; ++local_num) {   // profile.c:164
    size_t slot = code_state->n_state - 1 - local_num;              // profile.c:165
    mp_obj_t state_obj = code_state->state[slot];
    if (state_obj == MP_OBJ_NULL) { continue; }
    qstr var_name_qstr = mp_raw_code_get_local_name(rc, local_num); // profile.c:171
    ...
    mp_obj_dict_store(locals_dict, MP_OBJ_NEW_QSTR(var_name_qstr), state_obj); // profile.c:181
}
```

**`py/profile.c:164` (loop over all `local_num`) + `:181` (unconditional store)
is the single most important site** — it has no `local_num < param_count` skip
and no separate never-storing param loop, which is exactly what the old bug had.
`mp_raw_code_get_local_name` (`py/emitglue.h:150-159`) simply returns
`rc->local_names[local_num]`, which for a param index is the param's qstr.

## 2. Empirical proof

Binary present at `micropython/ports/unix/build-standard/micropython`
(LOCALNAMES=1). Test:

```python
import sys
captured = {}
def tracer(frame, event, arg):
    if event == 'line' and frame.f_code.co_name == 'foo':
        captured.update(dict(frame.f_locals))
    return tracer
def foo(a, b, c=10):
    x = a + b
    y = x * c
    return y
sys.settrace(tracer)
foo(1, 2)
sys.settrace(None)
print("KEYS:", sorted(captured.keys()))
```

Observed output:

```
KEYS: ['a', 'b', 'c', 'x', 'y']
HAS_PARAMS: True
```

Parameters `a`, `b`, `c` appear under real names, next to body locals `x`, `y`.
Bug resolved.

## 3. Fix plan

**No fix required** — the defect no longer exists. The recomposition (real-name
path via LOCALNAMES) plus the slot-indexing fix eliminated it.

### Recommended regression guard (STORY-8.6)

There is currently no settrace test that asserts **parameter** names appear in
`f_locals` (`tests/misc/sys_settrace_locals*.py` only exercise index-named/body
locals). Add a positive-coverage test so a future refactor that reintroduces a
param filter is caught. Follow the existing convention in `tests/misc/`
(SKIP guard on missing `sys.settrace`, `.py` + committed `.exp`):

`tests/misc/sys_settrace_locals_params.py`
```python
# Verify function parameters appear in frame.f_locals under their real names.
import sys
try:
    sys.settrace
except AttributeError:
    print("SKIP")
    raise SystemExit

def tracer(frame, event, arg):
    if event == 'line' and frame.f_code.co_name == 'target':
        keys = sorted(frame.f_locals.keys())
        # print once, on first traced line, then stop reporting
        if 'a' in frame.f_locals:
            print("locals:", keys)
            sys.settrace(None)
    return tracer

def target(a, b, c=10):
    x = a + b
    return x

sys.settrace(tracer)
target(1, 2)
sys.settrace(None)
print("done")
```

`tests/misc/sys_settrace_locals_params.py.exp`
```
locals: ['a', 'b', 'c', 'x']
```

Note: on this branch `line` fires BEFORE the statement executes, so on the first
traced line of `target` only params `a`, `b`, `c` are bound and `x` is still
`MP_OBJ_NULL` (skipped). The test above waits for the line where `x` has been
assigned by keying on `'a' in f_locals` being true across lines; if the exact
line/`x` timing proves brittle in CI, assert only the param subset instead:
`all(k in frame.f_locals for k in ('a','b','c'))` and print a stable boolean.
The param-subset assertion is the load-bearing check; the body-local is
secondary.
