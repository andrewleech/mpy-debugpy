# Minimal reproduction harness for the DAP TX stall, with no debugpy in it.
#
# Serves one connection at a time. The client's first line is a JSON config
# saying how the pump should behave, so one device run sweeps a whole matrix.
# The exchange mimics the shape the stall was seen at: a first request answered
# by two sends close together, then a second request whose single reply is the
# one that goes missing.
#
# `chatter` is how many lines the handler prints per request, standing in for
# debugpy's per-message logging. The first eight bytes of every reply are the
# milliseconds that printing took, so the client can attribute a slow reply to
# stdout rather than to the socket.
import json
import socket
import time

PORT = 5678
ACCEPT_TIMEOUT_S = 900


def _payload(size, stamp):
    head = "{:08d}".format(min(stamp, 99999999)).encode()
    return head + b"x" * (size - len(head))


def _recv_line(sock, timeout_s=10):
    sock.settimeout(timeout_s)
    buf = bytearray()
    while b"\n" not in buf:
        d = sock.recv(64)
        if not d:
            raise OSError("closed before config arrived")
        buf += d
    return json.loads(bytes(buf).split(b"\n")[0].decode())


def _send_all(sock, data):
    view = memoryview(data)
    sent = 0
    while sent < len(view):
        try:
            sent += sock.send(view[sent:])
        except OSError as e:
            if e.args[0] not in (11, 110, 35):
                raise
            time.sleep_ms(1)
    return sent


def serve(sock):
    cfg = _recv_line(sock)
    if cfg.get("stop"):
        return False
    if cfg.get("nodelay"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if cfg.get("pm") is not None:
        import network

        network.WLAN(network.STA_IF).config(pm=cfg["pm"])
    alternate = cfg.get("alternate", True)
    pump_ms = cfg.get("pump_ms", 12)
    run_ms = cfg.get("run_ms", 25000)
    chatter = cfg.get("chatter", 0)
    replies = cfg.get("replies", [[362, 73], [209]])

    if not alternate:
        sock.settimeout(0.001)

    nreq = 0
    buf = bytearray()
    deadline = time.ticks_add(time.ticks_ms(), run_ms)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if alternate:
            sock.settimeout(0.001)
        try:
            while True:
                d = sock.recv(256)
                if not d:
                    return True
                buf += d
                while len(buf) >= 4:
                    n = int.from_bytes(buf[:4], "big")
                    if len(buf) < 4 + n:
                        break
                    buf = bytearray(buf[4 + n :])
                    sizes = replies[nreq] if nreq < len(replies) else replies[-1]
                    nreq += 1
                    t_chat = time.ticks_ms()
                    for c in range(chatter):
                        print(
                            "[DAP] chatter line {:04d} for request {} padding pad".format(c, nreq)
                        )
                    t_chat = time.ticks_diff(time.ticks_ms(), t_chat)
                    for size in sizes:
                        _send_all(sock, _payload(size, t_chat))
        except OSError:
            pass
        finally:
            if alternate:
                sock.settimeout(None)
        time.sleep_ms(pump_ms)
    return True


def main():
    ls = socket.socket()
    try:
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception:
        pass
    ls.bind(socket.getaddrinfo("0.0.0.0", PORT)[0][-1])
    ls.listen(1)
    ls.settimeout(ACCEPT_TIMEOUT_S)
    print("LISTENING on", PORT)
    while True:
        try:
            cs, addr = ls.accept()
        except OSError:
            break
        keep_going = True
        try:
            keep_going = serve(cs)
        except Exception as e:
            print("ERR", repr(e))
        try:
            cs.close()
        except Exception:
            pass
        if not keep_going:
            break
    ls.close()
    print("BYE")


main()
