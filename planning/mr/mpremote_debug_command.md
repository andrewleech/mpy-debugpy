---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: mpremote_debug_command
status: branch exists locally, not pushed; needs rebase and /mpy-rules:review clean pass
title: "tools/mpremote: Add a debug command to run code under a DAP server."
head: d36c5e6f78
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
    Commit messages describe things the code no longer does and would land upstream as-is. 3ebdd608f0 describes `debug <target> [module[:method]]` as two positionals and the `# fmt: off` wrapper; 64cf0538a5 says "target and program are now optional positionals"; 7ab4bbb10b refers to "the wrapper-repo's unix-subprocess test harness" and test_s5_1_mpremote_debug.py. `--target` has been an option since 3ebdd608f0 and the fmt markers are gone.
  - >-
    The `tomli >= 1.1.0; python_version < "3.11"` line in requirements.txt is a new mpremote dependency; mention it in the PR if a reviewer doesn't raise it.
  - >-
    The handshake-parser half of test_debug_units.sh tests code this branch adds but lives on mpremote_dap_repl. Consider splitting it so this PR carries a test of its own.
  - >-
    /mpy-rules:review clean pass on the current 10 commits (only the pre-split branch was ever reviewed).
  - >-
    Decide whether `mpdebug.toml` belongs upstream at all, or should stay in the wrapper/extension. It's ~1/4 of the diff and is the part a maintainer is most likely to question.
---

### Summary

There's no way today to put a breakpoint in code running on a MicroPython board and step through it from an editor. The pieces exist in separate places - `sys.settrace` in the firmware, a DAP server (`debugpy`) that runs on the device - but getting a session going means copying the server over, writing a boot script, finding the board's IP and hand-writing a `launch.json`. `mpremote debug` does that in one command.

```
mpremote connect /dev/ttyACM0 debug app:main
```

connects, runs a small boot script over the raw REPL that imports `debugpy`, starts listening and imports/calls the target, then prints one `MPDBG-READY {json}` line with the address the device actually bound. That line is the contract for editors and scripts: host, port, the firmware's runtime capabilities and, when a source directory is mounted, the `pathMappings` to use.

```mermaid
flowchart LR
    host[mpremote debug] -- raw REPL: boot script --> dev[device: debugpy.listen + import target]
    dev -- MPDBG-READY json --> host
    editor[VS Code / DAP client] -- TCP --> dev
    host -. --source: mount .-> dev
```

On top of that:

- `--source DIR` mounts the host directory before the target is imported, so the debugger stops in the file you are editing rather than a copy on the board. With `--loop`, a DAP restart evicts what the target imported and re-imports it, so an edit takes effect with no upload and no reset.
- `-t unix` runs the same boot script under a local unix-port binary (POSIX hosts), which is handy for testing without hardware.
- `--dap-log` interposes a logging proxy and writes each DAP message as JSONL.
- A named target can be picked from an `mpdebug.toml` found up the directory tree.

While the command is attached it drains the board's console and prints it. Without that a stm32 USB CDC console fills and `print` stalls the program being debugged, which looks like the DAP link has dropped.

### Testing

I've been driving this from an out-of-tree harness: a fake DAP client runs full sessions against the unix port, plus a hardware run on a PYBD_SF6 over WiFi (17 scenarios, including a `--source --loop` edit/restart cycle and killing mpremote at a breakpoint). That hardware run was of all three stacked PRs together. `test_mount.sh` passes at every commit on this branch on the same board. Not tested on esp32 or rp2 hardware in this form, or with a Windows host.

There's no in-tree test of a full session in this PR; mpremote's own test runner has no DAP client. The next PR in the stack adds device-free tests for the handshake parser.

### Trade-offs and Alternatives

The boot script is sent over the raw REPL on each run, stripped of comments and docstrings first (it's about 18 kB with them, and compiled on the board every time). Freezing it in firmware would avoid that but would tie mpremote to firmware that has it.

The device reports its own address rather than mpremote guessing one, because only the device knows which interface it bound. A board that reports a wildcard with no known address is an error rather than an endpoint nothing could connect to.

### Generative AI

I used generative AI tools when creating this PR, but a human has checked the code and is responsible for the description above.
