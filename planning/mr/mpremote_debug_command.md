---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: mpremote_debug_command
status: fork draft #70; upstream review pending
title: "tools/mpremote: Start a debugpy session with mpremote debug"
head: 4c8673d70f
depends_on: mpremote_transport_fixes
relationship: |
  Second of three stacked PRs, based on mpremote_transport_fixes (uses its
  `timeout_overall_strict`). mpremote_dap_repl stacks on this one.

  Needs the device-side `debugpy` package from micropython-lib PR #1022
  (python-ecosys/debugpy), and firmware with MICROPY_PY_SYS_SETTRACE. The
  frame / f_locals support that makes variables readable is micropython PR
  #8767 plus local_names_implementation. None of those have to merge first for
  this to be reviewed, but it can't be used against stock firmware and a
  reviewer will ask.

  Supersedes fork PR andrewleech/micropython#51 (head mpremote_debug, pre-split).
todo:
  - >-
    Rebase onto current upstream/master. docs/reference/mpremote.rst conflicts (the upstream `run` note from the resume-by-default change sits where the debug section is inserted); code merges clean in merge-tree.
  - >-
    Decide how `debug` should behave now #17485 made resume the default (merged 2026-09-02). do_debug calls `state.ensure_raw_repl()` and so no longer soft-resets before the boot script; a second session in the same boot finds the previous target and debugpy still in sys.modules. Either soft-reset explicitly (`ensure_raw_repl(soft_reset=True)`) or document that `soft-reset` should be chained. Not tested either way yet.
  - >-
    The `tomli >= 1.1.0; python_version < "3.11"` line in requirements.txt is a new mpremote dependency; mention it in the PR if a reviewer doesn't raise it.
  - >-
    The handshake-parser half of test_debug_units.sh tests code this branch adds but lives on mpremote_dap_repl. Consider splitting it so this PR carries a test of its own.
  - >-
    /mpy-rules:review clean pass on the current 10 commits (only the pre-split branch was ever reviewed).
  - >-
    Decide whether `mpdebug.toml` belongs upstream at all, or should stay in the wrapper/extension. It's ~1/4 of the diff and is the part a maintainer is most likely to question.
---

I want to start a debug session from mpremote without hand-writing a boot script, finding the board's address and making an editor guess its paths. `mpremote connect /dev/ttyACM0 debug app:main` runs the device-side debugpy server and prints `MPDBG-READY` with the bound endpoint, runtime capabilities and optional source path mappings. `--source` mounts host files, `--loop` re-imports edited code on a DAP restart, and `-t unix` runs against a local unix binary. The command keeps draining the console while attached, otherwise a full CDC output buffer can stall the program.

This is the middle PR in the stack: #69 supplies its strict read timeout. The device needs debugpy from micropython-lib#1022 and settrace-enabled firmware; normal stock boards don't yet have the full setup.

I've driven full DAP sessions through the out-of-tree host harness and a PYBD_SF6 over WiFi, including a mounted edit/restart and stopping mpremote at a breakpoint. `test_mount.sh` passed on that board. There's no in-tree end-to-end DAP client in this PR yet. The upstream resume-by-default change (#17485) still needs a decision on whether `debug` explicitly soft-resets for a second run.

### Generative AI

I used generative AI tools when creating this PR. I checked the code and am responsible for the change.
