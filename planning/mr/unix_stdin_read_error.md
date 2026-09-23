---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: unix_stdin_read_error
status: pushed to fork, description written, no PR
title: "unix/unix_mphal: Report a failed stdin read as end of input."
pushed_branch: https://github.com/andrewleech/micropython/tree/unix_stdin_read_error
head: 6213c10d44
relationship: |
  Independent. Found while running the unix port under a pty for mpremote
  debug testing, but nothing depends on it.
todo:
  - >-
    Rebase onto current upstream/master (140 behind; merge-tree clean).
  - >-
    The fix itself has no recorded verification: the 85k count is from before it (planning/20260810_pty-termios-race.md). Measure after, and run the unix tests.
  - >-
    No in-tree test. A regression test would need a pty whose other side closes, which the unix test runner doesn't set up; say so rather than add one.
  - >-
    /mpy-rules:review pass (never reviewed).
---

### Summary

I found this running the unix port under a pty for host-side testing of a debugger. `mp_hal_stdin_rx_chr` only treats `read() == 0` as end of input; on `-1` it falls through and returns `c`, which was never written, so the REPL gets an uninitialised byte and immediately asks for another. The errors that land here don't clear (EIO once a pty's last other opener has closed, for one), so it loops forever. I counted about 85k syscalls in a single test run.

A failed read is now reported as end of input the same way a zero-byte read is.

### Testing

Unix port under a pty with no other opener, where the read fails with EIO. TODO: re-run the syscall count with the fix and the unix test suite before raising.

### Trade-offs and Alternatives

Treating every error as EOF also covers errors that might in theory be transient. `MP_HAL_RETRY_SYSCALL` already retries EINTR, and I couldn't find another errno a blocking read of stdin returns that would clear on its own.

### Generative AI

I used generative AI tools when creating this PR, but a human has checked the code and is responsible for the description above.
