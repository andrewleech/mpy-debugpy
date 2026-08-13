# Same exchange as the reproduction, over a connection the board dials out.
#
# The accepted-connection version wedges under cyw43 PM1. This one never goes
# through lwIP's accept path, so nothing can have been refused on a
# not-yet-accepted pcb: if it wedges too, that whole family of explanations is
# out.
import socket
import time

HOST = "192.168.0.8"
PORT = 5679
REPLIES = [[362], [73, 209], [120]]
RUN_MS = 20000


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


def run():
    sock = socket.socket()
    sock.connect(socket.getaddrinfo(HOST, PORT)[0][-1])
    print("CONNECTED out to", HOST, PORT)
    nreq = 0
    buf = bytearray()
    deadline = time.ticks_add(time.ticks_ms(), RUN_MS)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        sock.settimeout(0.001)
        try:
            while True:
                d = sock.recv(256)
                if not d:
                    print("PEER CLOSED after", nreq)
                    sock.close()
                    return
                buf += d
                while len(buf) >= 4:
                    n = int.from_bytes(buf[:4], "big")
                    if len(buf) < 4 + n:
                        break
                    buf = bytearray(buf[4 + n :])
                    sizes = REPLIES[nreq] if nreq < len(REPLIES) else REPLIES[-1]
                    nreq += 1
                    print("REQ", nreq, "at", time.ticks_ms())
                    for size in sizes:
                        _send_all(sock, b"x" * size)
        except OSError:
            pass
        finally:
            sock.settimeout(None)
        time.sleep_ms(12)
    print("RUN DONE reqs=", nreq)
    sock.close()


run()
