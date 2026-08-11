# Hardware-in-loop run: PYBD_SF6

Written by `tests/hil/` (STORY-6.4); a rerun of the same bench arrangement
overwrites it.

- Date: 2026-08-11
- Tree: `4adc9aa94eb9e0584a520ee7f2980463658d648e`
- Pinned `micropython`: `55103ee80a50eb08130bde79c2004e2c5fcb866b`
- Pinned `micropython-lib`: `2a9bad0971b7700349d233ea82e1669b42e99eb5`
- Working tree: clean
- Device: `/dev/serial/by-id/usb-MicroPython_Pyboard_Virtual_Comm_Port_in_FS_Mode_3254335D3037-if01`
- Dedicated DAP interface: not supplied
- Machine: PYBD-SF6W with STM32F767IIK
- Firmware: v1.29.0-preview.717.g4eaafbc5bc on 2026-08-10
- USB mode: VCP+MSC
- Debuggee on device: `/flash/target.py`
- Probed capabilities: `{'serial_dap': False, 'settrace': True, 'set_local': False, 'f_back': True, 'save_names': True, 'second_cdc': True}`

`serial_dap` is `False` above because these capabilities come from a plain
REPL probe: the key reports which channel a session took, not what the
firmware can do.
No run in this arrangement takes that channel: the scenarios that
would were skipped for want of a second interface, which is what
this arrangement is for.
`second_cdc` being `True` beside a single-VCP USB mode is not a
contradiction: it reports what the firmware can construct, so this
is a board built for two interfaces and booted with one.

| scenario | result |
| --- | --- |
| `test_hil_program_output_carrying_the_marker_arrives_intact` | passed |
| `test_hil_repl_dap_carries_a_large_response` | passed |
| `test_hil_repl_dap_reaches_a_breakpoint` | passed |
| `test_hil_repl_dap_takes_the_stream_it_was_launched_over` | passed |
| `test_hil_the_repl_comes_back_after_the_session` | passed |
| `test_hil_one_cdc_board_refuses_serial_dap` | passed |
| `test_hil_the_board_survives_the_refusal` | passed |
| `test_hil_the_refusal_names_the_board_not_the_missing_node` | passed |

## Measurements

| name | value |
| --- | --- |
| `repl_dap_bytes_per_second` | 81669 |
| `repl_dap_payload_seconds` | 0.201 |
| `single_cdc_refusal_seconds` | 0.6 |
