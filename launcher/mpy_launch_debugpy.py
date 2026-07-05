"""Single parameterised boot script for MicroPython debugpy sessions.

Usage: mpy_launch_debugpy.py [target_module] [target_method] [port]

Replaces the former mpy_launch_debugpy_unix.py / mpy_launch_debugpy_esp32.py
pair: the bind address is chosen at runtime by probing for the `network`
module rather than by shipping two near-identical scripts, one per
transport. There are no IP or port literals in this file - port 0 means
"pick any free port", and the actual bound endpoint plus the probed firmware
capabilities are reported in a single machine-readable handshake line on
stdout:

    MPDBG-READY {"host": "...", "port": ..., "caps": {...}}

Tooling parses that one line rather than any of the human-readable banner
text around it. `wait_for_client()` (not a fixed sleep) blocks until the DAP
client has finished configuring breakpoints, so breakpoints set before then
are already applied by the time the target starts running.
"""

import json
import sys

import debugpy

_banner = r"""
 _____  _______ ______ _______ _______ ______ ___ ___
|     \|    ___|   __ \   |   |     __|   __ \   |   |
|  --  |    ___|   __ <   |   |    |  |    __/\     /
|_____/|_______|______/_______|_______|___|    |___|
"""


def _detect_host():
    """Return the address debugpy should bind to on this runtime.

    Boards with a `network` module report their own DHCP/WiFi address so
    tooling never has to guess or hardcode a device IP. The unix port has no
    `network` module, and a board with `network` but no configured/connected
    interface has no address to report either - both cases, and any error
    while probing, fall back to binding all interfaces so a network probe
    failure never aborts the launch.
    """
    try:
        import network

        wlan = network.WLAN(network.STA_IF)
        addr = wlan.ipconfig("addr4")[0]
    except Exception:
        return "0.0.0.0"

    if not addr or addr == "0.0.0.0":
        return "0.0.0.0"
    return addr


def _parse_args():
    args = sys.argv[1:]
    target_module = args[0] if len(args) > 0 else "target"
    target_method = args[1] if len(args) > 1 else "main"
    port = int(args[2]) if len(args) > 2 else 0
    if len(args) > 3:
        raise ValueError(
            "Too many arguments. Usage: mpy_launch_debugpy.py [target_module] [target_method] [port]"
        )
    return target_module, target_method, port


def _run():
    print(_banner)
    print("MicroPython VS Code Debugging")
    print("Usage: mpy_launch_debugpy.py [target_module] [target_method] [port]")
    print("==================================")

    target_module, target_method, port = _parse_args()
    print(f"Target module: {target_module}")
    print(f"Target method: {target_method}")
    print("==================================")

    if not hasattr(sys, "settrace"):
        print("sys.settrace is not available. You need a firmware compiled with debugging features.")
        return

    host = _detect_host()
    actual_host, actual_port = debugpy.listen(host=host, port=port)
    print(f"Debug server attached on {actual_host}:{actual_port}")

    caps = debugpy.get_capabilities()
    # Exactly one MPDBG-READY line, valid JSON, nothing else on this line.
    print("MPDBG-READY " + json.dumps({"host": actual_host, "port": actual_port, "caps": caps}))

    try:
        target = __import__(target_module, None, None, ("*",))
    except ImportError as e:
        print(f"Error importing target module '{target_module}': {e}")
        return

    print("Waiting for the client to finish configuring (configurationDone)...")
    if not debugpy.wait_for_client():
        print(
            "[DAP] No client finished configuring (timed out or disconnected) - "
            "not running the target under a dead debug session."
        )
        debugpy.disconnect()
        return

    debugpy.debug_this_thread()

    method = getattr(target, target_method, None)
    if method is None:
        print(f"Method '{target_method}' not found in module '{target_module}'")
        return

    result = method()

    print("Target completed successfully!")
    if result is None:
        print("No result returned from target method")
    else:
        print("Result type:", type(result))
        print("Result:", result)


try:
    _run()
except KeyboardInterrupt:
    print("\nTest interrupted by user")
except Exception as e:
    print(f"Error: {e}")
