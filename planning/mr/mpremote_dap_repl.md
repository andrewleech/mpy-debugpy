---
upstream_repo: micropython/micropython
upstream_base: master
local_branch: mpremote_dap_repl
status: fork draft #71; upstream review pending
title: "tools/mpremote: Carry DAP over the REPL connection"
head: fab3e83088
depends_on: mpremote_debug_command
relationship: |
  Third of three stacked PRs, based on mpremote_debug_command. This is the
  branch registered in mbm.toml; it contains the other two.

  Device side it needs the repl_mux / listen_stream support that is in
  micropython-lib PR #1022 (commit 33af8af "debugpy: Share the REPL's stream
  with the DAP channel."). The wire constants (marker 0x18, CMD_DAP 14..16,
  RX_CREDIT, MAX_PAYLOAD, ACK_THRESHOLD) are duplicated in repl_mux.py there
  and must stay in step; the only drift check is in the mpy-debugpy wrapper
  repo.
todo:
  - >-
    Rebase with the rest of the stack (same docs/reference/mpremote.rst conflict).
  - >-
    Only reaches stm32 on the legacy USB stack (REPL as a Python object in dupterm slot 1). Say so plainly in the PR; a reviewer will ask about TinyUSB ports and esp32.
  - >-
    /mpy-rules:review clean pass.
---

A board without networking has no endpoint for a DAP client to attach to. `--dap-repl` carries DAP frames over the REPL connection mpremote already holds, and opens a local TCP endpoint for the editor. The same stream also carries program output; the marker byte is escaped so a program can print it safely. Writes wait for credit because the CDC receive ring can drop a packet's tail rather than applying back-pressure.

This is stacked on #70 and needs `repl_mux` / `listen_stream` from micropython-lib#1022. It currently works where the REPL is a Python-visible `dupterm` stream: tested on PYBD_SF6 with the legacy stm32 USB stack, not TinyUSB or esp32. `--source` is refused since mount uses the same wire framing.

The device-free `test_debug_units.sh` covers parser / demux input including byte-at-a-time reads. Five hardware scenarios passed on PYBD_SF6, including breakpoints, marker bytes in output, a large response and returning to REPL after the session.

### Generative AI

I used generative AI tools when creating this PR. I checked the code and am responsible for the change.
