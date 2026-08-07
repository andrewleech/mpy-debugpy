"""Device-side boot script for s6.1's stream-transport proof.

Runs the DAP channel over a stream instead of TCP (`debugpy.listen_stream()`),
so a session can be driven with no socket anywhere in the DAP path. Mirrors
`mpy_launch_debugpy.py`'s shape minus everything TCP-specific (host probing,
port argument, the MPDBG-READY handshake - there is no host-side bridge yet
to parse it).

Usage: stream_debuggee.py <stream-device-path> [target_module] [target_method]
"""

import sys


def _run():
    import debugpy

    dev_path = sys.argv[1]
    target_module = sys.argv[2] if len(sys.argv) > 2 else "target"
    target_method = sys.argv[3] if len(sys.argv) > 3 else "main"

    if not hasattr(sys, "settrace"):
        print("sys.settrace is not available; need a debug-enabled firmware.")
        return

    stream = open(dev_path, "r+b")
    debugpy.listen_stream(stream)
    # The one line the proof script scans for: the stream is open and the
    # session can be driven, before any client has attached.
    print("STREAM-READY")

    if not debugpy.wait_for_client():
        print("[DAP] no client finished configuring; not running the target")
        debugpy.disconnect()
        return

    debugpy.debug_this_thread()
    target = __import__(target_module, None, None, ("*",))
    getattr(target, target_method)()
    print("Target completed successfully!")


if __name__ == "__main__":
    _run()
