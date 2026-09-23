---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: mpremote_close_lost_device
status: pushed to fork, description written, no PR
title: "tools/mpremote: Close the serial port even if clearing RTS/DTR fails."
pushed_branch: https://github.com/andrewleech/micropython/tree/mpremote_close_lost_device
head: 23fe999a40
relationship: |
  Independent. Touches SerialTransport.close() in transport_serial.py, as does
  mpremote_transport_fixes but in a different function; merge-tree clean with
  both.
todo:
  - >-
    Rebase onto current upstream/master (328 behind; merge-tree clean). Check #17321 (mpremote disconnect handling, merged) didn't already cover this path.
  - >-
    /mpy-rules:review pass (never reviewed).
---

### Summary

If a device goes away while a command is running (unplugged, or reset by its own firmware), clearing RTS/DTR in `close()` fails with EIO. Only ENOTTY was tolerated, so the `OSError` propagated out of `do_disconnect()`, which `main()` calls from a `finally` outside its `CommandError` handler. The command printed its message about losing the device, then exited with a traceback, and the port was never closed.

Clearing the signals is best-effort anyway (it exists to stop a Windows host resetting an ESP target on the way out) so a failure no longer stops the close.

### Testing

Linux with a PYBD_SF6 over USB CDC. A standalone probe (open the port, power-cycle its USB hub port, `close()`) reproduces the EIO, and a board power-cycled mid-command now exits with mpremote's own error and no traceback. Not tested on Windows or macOS.

### Trade-offs and Alternatives

Catching every `OSError` rather than adding EIO to the allowed list, since there's nothing useful to do with any failure here and the port still has to be closed.

### Generative AI

I used generative AI tools when creating this PR, but a human has checked the code and is responsible for the description above.
