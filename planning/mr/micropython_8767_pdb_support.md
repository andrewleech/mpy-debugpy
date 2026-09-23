---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: pdb_support
status: PR open upstream; a764b6cd8c pushed 2026-08-10 with no comment explaining it; follow-up comment drafted below, not posted
title: "Improve sys.settrace to help support debugpy / pdb debugging"
upstream_pr: https://github.com/micropython/micropython/pull/8767
head: a764b6cd8c
relationship: |
  Base of local_names_implementation. Companion to micropython-lib #1022.
todo:
  - >-
    Title doesn't follow `component: Description.`; fixing a title is fine per the append-don't-rewrite rule. Suggest "py/profile: Add sys._getframe and frame.f_trace for debuggers."
  - >-
    Code size bot reports unix x64 +39512 (+4.5%, standard). Find out how much of that is this PR vs settrace itself being enabled on unix standard, and explain it; a reviewer will ask.
  - >-
    Rebase onto current master (base 2dc2e30d98, 2026-03-21; merge-tree clean). The octoprobe run on a764 was green across seven ports.
  - >-
    Mixed identities: first commit work address, a764 work address; fine if consistent across the micropython PRs.
---

## Follow-up comment

I've pushed one more commit, `py/profile: Keep tracing usable after a trace callback raises.`

A trace callback that raised left `mp_prof_is_executing` set for the rest of the process, since the exception leaves `mp_prof_callback_invoke` through an nlr jump. Every trace hook checks that flag, so no callback ever ran again while `sys.settrace()` and `sys.gettrace()` kept reporting success. With assertions on it tripped `assert(!mp_prof_is_executing)` instead.

This matters for a debugger in particular because raising from the trace function is the only way it has to unwind a program out of a frame it's stopped in; there's no second thread to do it from. The callback invoke is now wrapped in `nlr_push`/`nlr_pop`, and on the exception path the guard is cleared and the trace function unset before re-raising, which matches CPython. `TRACE_TICK` also checks the global callback is still set, otherwise `sys.settrace(None)` didn't stop frames already on the stack from calling their local trace function.

`tests/misc/sys_settrace_raise.py` covers it, compared against CPython rather than an .exp file since the output is identical.
