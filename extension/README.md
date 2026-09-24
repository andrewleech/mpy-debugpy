# MicroPython Debug

Press F5 to debug MicroPython code, on the unix port or on a board. You get
breakpoints, stepping, the call stack and local variables.

The extension runs `mpremote debug`, which starts your program on the target
under the `debugpy` server there. It reads the endpoint the target reports,
then attaches VS Code's Python debugger (`ms-python.debugpy`) to it. You don't
type a host or a port anywhere.

> **Preview.** The pieces this extension drives are not released yet: the
> `mpremote debug` command, the on-device `debugpy` package, and the
> `sys.settrace` support in the firmware. They live in the
> [mpy-debugpy](https://github.com/andrewleech/mpy-debugpy) integration repo
> until they are merged upstream. Start from that repo's README.

## Requirements

- `ms-python.debugpy`, which is installed automatically as a dependency.
- An `mpremote` that has the `debug` command, from a checkout of
  [mpy-debugpy](https://github.com/andrewleech/mpy-debugpy). A released
  `mpremote` does not have it yet.
- Firmware built with `MICROPY_PY_SYS_SETTRACE`, and `debugpy` installed on
  the board. mpy-debugpy's docs cover both.
- For a board: a network the host can reach (WiFi, Ethernet or USB NCM), or
  `--dap-repl` to share the REPL's serial stream.

## Launch configuration

```json
{
  "type": "micropython",
  "request": "launch",
  "name": "Debug on device",
  "target": "pico",
  "program": "app:main",
  "source": "${workspaceFolder}/app"
}
```

| Key | Meaning |
| --- | --- |
| `target` | A name from `mpdebug.toml`, `unix`, or an mpremote connect string such as `/dev/ttyACM0`. If you leave it out, the target chosen in the status bar is used. |
| `program` | `module[:function]` to run under the debugger. |
| `source` | Host directory to mount on the board, so breakpoints bind in files the board never had. |
| `loop` | Rerun the program on Restart, picking up edited source. |
| `port`, `timeout` | The debug server's port, and how long to wait for the target to report its endpoint. |
| `dapLog`, `dapLogFile` | Log DAP traffic through a local proxy. |
| `mpremotePath`, `mpremoteArgs`, `env`, `cwd` | How to run `mpremote`. For example, `"mpremotePath": "python3"` with `"mpremoteArgs": ["-m", "mpremote"]` runs a source checkout. |

When the workspace has an `mpdebug.toml`, a status-bar item picks among its
targets. The **MicroPython: Select Debug Target** command does the same, and
the choice is remembered per workspace.

## Known limitations

See [Known limitations](https://github.com/andrewleech/mpy-debugpy/blob/main/docs/debugging.md#known-limitations).
In short:

- Pausing only takes effect when Python code runs.
- There are no exception breakpoints.
- Locals are read-only unless the firmware supports `set_local`.
- A breakpoint on the last line of some loops stops once too often.
