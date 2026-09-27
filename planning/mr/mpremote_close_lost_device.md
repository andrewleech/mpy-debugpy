---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: mpremote_close_lost_device
status: fork draft #74; upstream review pending
title: "tools/mpremote: Close the port when RTS/DTR clearing fails"
pushed_branch: https://github.com/andrewleech/micropython/tree/mpremote_close_lost_device
head: 953209e2a9
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

Unplugging a USB CDC board during an mpremote command could produce its intended lost-device error and then a traceback during disconnect. `SerialTransport.close()` tried to clear RTS/DTR first, and an EIO there stopped it from closing the port. Signal clearing is best-effort, so it now closes the port even if that step fails.

On Linux with a USB CDC board, open the port with `SerialTransport('/dev/ttyACM0')`, unplug it / power-cycle its hub port, then call `close()`. Before this fix that raised EIO on the PYBD_SF6; afterwards the command exited with its own error and no traceback. Other USB drivers may not reproduce the EIO. Not tested on Windows or macOS.

### Generative AI

I used generative AI tools when creating this PR. I checked the code and am responsible for the change.
