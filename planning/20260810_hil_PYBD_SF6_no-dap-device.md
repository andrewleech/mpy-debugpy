# Hardware-in-loop run: PYBD_SF6

Written by `tests/hil/` (STORY-6.4); a rerun of the same bench arrangement
overwrites it.

- Date: 2026-08-10
- Tree: `d4182fcedc4e6f84b0db18b0a8160d28acc78c9e`
- Pinned `micropython`: `19410568d68fabe5d3fcf8594a4e30919205a7cd`
- Pinned `micropython-lib`: `b829073c393f9ba1027fe8d526ea58c2c8eb7fe4`
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
| `test_hil_one_cdc_board_refuses_serial_dap` | passed |
| `test_hil_the_board_survives_the_refusal` | passed |
| `test_hil_the_refusal_names_the_board_not_the_missing_node` | passed |

## Measurements

| name | value |
| --- | --- |
| `single_cdc_refusal_seconds` | 0.57 |
