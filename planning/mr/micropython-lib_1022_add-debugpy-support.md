---
upstream_repo: micropython/micropython-lib
upstream_base: master
local_branch: add-debugpy-support
status: PR open upstream; description is the 2025 original and is not to be rewritten; follow-up comment drafted below, not posted
title: "python-ecosys/debugpy: Add VS Code debugging support for MicroPython."
upstream_pr: https://github.com/micropython/micropython-lib/pull/1022
head: b12d2f65f0
relationship: |
  Device-side half of everything in this folder. mpremote_debug_command and
  mpremote_dap_repl drive it; mpremote_debugpy_install installs it; micropython
  #8767 + local_names_implementation provide the settrace / f_locals it needs.
  The wire constants in repl_mux.py are duplicated in mpremote's repl_dap.py.
todo:
  - >-
    History is not mergeable as it stands. The PR has 75 commits, 21 of which are other people's micropython-lib commits (requests, uuid, umqtt.simple, aiorepl, usb-device-cdc, colorsys, aioble) that have since landed on master under different SHAs - `git cherry` finds all 21 upstream - plus one merge commit (42d3551). Against current master it conflicts in aiorepl, umqtt.simple and requests purely because of those. A rebase onto master drops the 21 cleanly; the merge commit and the churn (e367c15 adds serial_dap, abb03f6 removes it) need folding. micropython-lib doesn't squash, so the fold decides what lands.
  - >-
    Rebasing rewrites a PR with review history. Say so in the comment when it happens (below is written for after that push).
  - >-
    Mixed author addresses: early commits and 1544fad..e367c15 use the work address, the rest andrew@alelec.net; Jos's commits keep his.
  - >-
    /mpy-rules:review has never been run on the micropython-lib side.
  - >-
    The top description still lists test_vscode.py / dap_monitor.py and the "Efficient O(n) fibonacci demo"; per the append-don't-rewrite rule that is for a comment, or for the user to decide.
---

## Follow-up comment (post after the rebase push)

Rebased onto master and tidied the history, so this is a force-push this time rather than a fast-forward. The branch had picked up 21 commits from other PRs that have since been merged here under different hashes (requests, uuid, umqtt.simple and a few others), which is why it showed conflicts in files this PR doesn't touch. Those are gone, as is the merge commit from folding in Jos's performance branch, and changes that were later reverted within the branch are squashed out. Jos's commits keep his authorship.

What's new since my last update:

The DAP channel can run over any stream, not just a TCP socket. `debugpy.listen_stream()` takes a stream object, and the main use is sharing the REPL's own USB CDC stream through a small multiplexer (`repl_mux`) installed in `dupterm` slot 1, so a board with no network can still be debugged. The host side of that is a separate micropython PR for `mpremote debug`. On a stream with no EOF, like a CDC interface, the session ends when the host drops DTR, so killing the host at a breakpoint no longer leaves the board stopped until a power cycle.

Behaviour fixes found while testing on a PYBD_SF6:

- the message pump can no longer raise into the program being debugged
- a breakpoint only stops on the trace events that precede its line, so it no longer stops twice on some lines
- path translation between client and device is symmetric and only matches on path boundaries
- `setBreakpoints` replaces the breakpoints stored under both of a file's keys
- pause now actually stops the target
- a DAP `restart` is honoured, re-running the target in the same session

The capability probe runs once per interpreter and includes `save_names` measured on code the firmware compiled. README and the development guide are corrected against a measured session.

Tested with the same host-side harness as before (fake DAP client, real sessions against a unix build) and on a PYBD_SF6 over both WiFi and the shared REPL stream.
