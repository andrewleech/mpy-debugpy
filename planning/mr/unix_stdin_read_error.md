---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: unix_stdin_read_error
status: fork draft #72; upstream review pending
title: "unix: Stop reading stdin after a pty disconnects"
pushed_branch: https://github.com/andrewleech/micropython/tree/unix_stdin_read_error
head: 9b317d8d72
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

I found this while running the unix port under a pty. Once its peer closes, `read()` can return EIO; `mp_hal_stdin_rx_chr` returned the uninitialised byte `c` and kept reading. The original host run counted about 85k syscalls. A failed read now acts like EOF, after the existing EINTR retry.

A small reproducer, run from this repository with the unix standard binary built:

```sh
python3 - <<'PY'
import os, pty, subprocess
master, slave = pty.openpty()
proc = subprocess.Popen(
    ['ports/unix/build-standard/micropython'], stdin=slave,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
os.close(slave)
os.close(master)
try:
    print('exit:', proc.wait(timeout=2))
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait()
    print('still reading after pty disconnect')
PY
```

That probe exited with status 0 on the current build. I haven't run this exact probe on the unfixed branch or measured the post-fix syscall count yet. Not tested on Windows.

### Generative AI

I used generative AI tools when creating this PR. I checked the code and am responsible for the change.
