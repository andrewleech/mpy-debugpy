---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: mpremote_transport_fixes
status: branch exists locally, not pushed; needs /mpy-rules:review clean pass
title: "tools/mpremote: Bound serial reads by wall clock, and fix mount reads."
head: 9ee81df4b7
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

### Summary

While building a debug command on top of mpremote's serial transport I kept hitting reads that never returned. Two separate causes.

On a pty (the unix port, QEMU) `read_until` can block forever in `serial.read()` once the peer goes quiet, because a pty read has no timeout of its own to expire, so the loop never gets to check its deadline. The port's read timeout is now lowered for the duration of the poll to give it that tick.

A mounted transport had a couple of ways to hang or crash too. `SerialIntercept` looped forever when the device was quiet for a whole timeout period, indexed an empty read when a command byte didn't arrive, and a short read of an RPC field reached `struct.unpack` as a `struct.error` with no cause. Those now return a short read or raise `TransportError` saying the device stopped mid-command. A filesystem RPC is a multi-read exchange so it gets its own 5 s floor around the exchange rather than inheriting the short polling timeout.

`read_until` also gains `timeout_overall_strict`. The default is unchanged: `timeout_overall` is only consulted once the peer goes quiet, which is what stops a verbose board's boot printing being cut off mid-line. A caller reading from a peer that may stream for the whole session (a mount's RPC pump, a handshake scan) passes True to get a bound that holds regardless.

### Testing

`tools/mpremote/tests/test_transport_timeouts.sh` is new and device-free: it drives `read_until`'s quiet and strict bounds over a pty pair and checks `SerialIntercept`'s timeout round-trip. `test_mount.sh` passes on a PYBD_SF6 at both commits.

Not tested on Windows or on a UART-bridge (CP210x / CH340) board.

### Trade-offs and Alternatives

Lowering the port timeout is done per poll and restored afterwards, rather than set once at open, so code that reads the port directly keeps the timeout it asked for.

### Generative AI

I used generative AI tools when creating this PR, but a human has checked the code and is responsible for the description above.
