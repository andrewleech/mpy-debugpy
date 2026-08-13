"""Replay the DAP session's wire shape against the device harness, no debugpy.

The shape taken from a failing capture: a client that sends its first request
about a millisecond after the connection completes, then a second, then a third
whose reply never comes. Sizes and gaps are the measured ones, so the only
thing this leaves out of the failing session is `debugpy` itself.
"""

import argparse
import json
import socket
import sys
import time

# Request sizes and the reply sizes each one triggers, from the capture.
EXCHANGE = [
    (205, [362]),
    (206, [73, 209]),
    (83, [120]),
]


def frame(n):
    body = b"q" * n
    return len(body).to_bytes(4, "big") + body


def read_exactly(sock, count, timeout_s):
    got = bytearray()
    deadline = time.monotonic() + timeout_s
    while len(got) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(count - len(got))
        except (TimeoutError, socket.timeout):
            break
        if not chunk:
            break
        got += chunk
    return bytes(got)


def trial(addr, cfg, timeout_s, gaps, pre_gap=0.0):
    sock = socket.create_connection(addr, timeout=10)
    try:
        # Nothing is sent for `pre_gap` seconds, so the device's accept()
        # has long returned before any byte arrives: the window where lwIP
        # refuses data on a not-yet-accepted pcb is stepped over entirely.
        if pre_gap:
            time.sleep(pre_gap)
        sock.sendall(json.dumps(cfg).encode() + b"\n")
        for index, (req, replies) in enumerate(EXCHANGE):
            if index:
                time.sleep(gaps)
            t0 = time.monotonic()
            sock.sendall(frame(req))
            want = sum(replies)
            got = read_exactly(sock, want, timeout_s)
            dt = time.monotonic() - t0
            if len(got) != want:
                return {"stalled_at": index + 1, "got": len(got), "want": want, "dt": dt}
        return {"stalled_at": None, "dt": dt}
    finally:
        sock.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=5678)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--gap", type=float, default=0.05)
    ap.add_argument("--label", default="")
    ap.add_argument("--no-alternate", action="store_true")
    ap.add_argument("--pre-gap", type=float, default=0.0)
    ap.add_argument("--uniform", type=int, default=0, help="N identical 83/120 exchanges instead")
    ap.add_argument("--pm", type=lambda v: int(v, 0), default=None)
    args = ap.parse_args()
    if args.uniform:
        EXCHANGE[:] = [(83, [120])] * args.uniform
    addr = (args.host, args.port)
    cfg = {
        "alternate": not args.no_alternate,
        "nodelay": False,
        "run_ms": int(args.timeout * 1000) + 10000,
        "replies": [r for _, r in EXCHANGE],
    }
    if args.pm is not None:
        cfg["pm"] = args.pm
    stalls = 0
    for i in range(args.trials):
        r = trial(addr, cfg, args.timeout, args.gap, args.pre_gap)
        if r["stalled_at"]:
            stalls += 1
            print(
                "{}trial {}: STALLED at request {} (got {} of {} bytes)".format(
                    args.label, i + 1, r["stalled_at"], r["got"], r["want"]
                )
            )
        else:
            print("{}trial {}: ok".format(args.label, i + 1))
        time.sleep(0.5)
    print("{}stalls: {}/{}".format(args.label, stalls, args.trials))
    return 1 if stalls else 0


sys.exit(main())
