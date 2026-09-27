---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: mpremote_transport_fixes
status: fork draft #69; upstream review pending
title: "tools/mpremote: Stop pty and mount reads hanging"
head: 26905a5ba0
relationship: |
  First of three stacked PRs. mpremote_debug_command is based on this branch
  and calls the `timeout_overall_strict` parameter it adds, so this one lands
  first. It stands on its own though: nothing here is debug-specific, and the
  fixes affect `mount`, `fs`, `mip` and `romfs` today.
todo:
  - >-
    Rebase onto current upstream/master (140 behind at 5f2181f938). merge-tree against 09f5bb4475 is clean.
  - >-
    No /mpy-rules:review has been run on this branch in its current shape; the 2026-08-21 review was of the pre-split mpremote_debug.
  - >-
    test_transport_timeouts.sh is new; confirm the upstream mpremote test runner picks it up after the rebase (the harness gained `-t device` and skip support upstream since the base).
---

I hit two read failures while running mpremote against a pty / mounted board. A quiet pty could block inside `serial.read()` past `read_until`'s deadline, and the mount intercept could keep polling forever or crash on a short RPC field. The serial read now gets a short polling timeout; mount reads return short or raise `TransportError` when the device stops mid-command. `timeout_overall_strict` also bounds a continuously chatty peer without changing the default boot-output behaviour.

To reproduce without hardware, run `bash tools/mpremote/tests/test_transport_timeouts.sh` on POSIX. It covers quiet and streaming pty peers plus the mount intercept's timeout. `test_mount.sh` passed on PYBD_SF6. I haven't tried Windows or UART bridge boards.

### Generative AI

I used generative AI tools when creating this PR. I checked the code and am responsible for the change.
