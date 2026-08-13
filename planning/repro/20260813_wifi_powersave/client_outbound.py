"""Host side for the outbound-connection arm: listen, then drive the exchange."""

import socket
import sys
import time

EXCHANGE = [(205, 362), (206, 73 + 209), (83, 120)]
PORT = 5679


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


def main():
    timeout_s = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", PORT))
    listener.listen(1)
    listener.settimeout(60)
    print("listening on", PORT, flush=True)
    sock, addr = listener.accept()
    print("board dialled in from", addr, flush=True)
    try:
        for index, (req, want) in enumerate(EXCHANGE):
            if index:
                time.sleep(0.05)
            sock.sendall(len(b"q" * req).to_bytes(4, "big") + b"q" * req)
            got = read_exactly(sock, want, timeout_s)
            if len(got) != want:
                print("STALLED at request {} (got {} of {})".format(index + 1, len(got), want))
                return 1
            print("request {} ok".format(index + 1))
        print("all exchanges ok")
        return 0
    finally:
        sock.close()
        listener.close()


sys.exit(main())
