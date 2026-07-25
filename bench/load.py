"""Minimal keep-alive HTTP load generator.

Written against raw sockets so the numbers reflect the server, not an HTTP
client library's overhead.
"""

import argparse
import socket
import statistics
import threading
import time

REQ_TEMPLATE = (
    "GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n"
)


def worker(host, port, path, duration, conns, results, idx):
    req = REQ_TEMPLATE.format(path=path).encode()
    socks = []
    for _ in range(conns):
        s = socket.create_connection((host, port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        socks.append(s)

    count = 0
    errors = 0
    lat = []
    end = time.perf_counter() + duration
    while time.perf_counter() < end:
        for s in socks:
            t0 = time.perf_counter()
            try:
                s.sendall(req)
                data = s.recv(65536)
                if not data:
                    errors += 1
                    continue
            except OSError:
                errors += 1
                continue
            lat.append((time.perf_counter() - t0) * 1e6)
            count += 1

    for s in socks:
        s.close()
    results[idx] = (count, errors, lat)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7474)
    ap.add_argument("--path", default="/healthz")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--conns", type=int, default=16)
    ap.add_argument("--duration", type=float, default=5.0)
    args = ap.parse_args()

    results = [None] * args.threads
    threads = [
        threading.Thread(
            target=worker,
            args=(
                args.host,
                args.port,
                args.path,
                args.duration,
                args.conns,
                results,
                i,
            ),
        )
        for i in range(args.threads)
    ]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0

    total = sum(r[0] for r in results if r)
    errors = sum(r[1] for r in results if r)
    lat = [x for r in results if r for x in r[2]]
    lat.sort()

    def pct(p):
        if not lat:
            return 0.0
        return lat[min(len(lat) - 1, int(len(lat) * p))]

    print(f"path       {args.path}")
    print(f"threads    {args.threads} x {args.conns} conns")
    print(f"elapsed    {elapsed:.2f}s")
    print(f"requests   {total}")
    print(f"errors     {errors}")
    print(f"rps        {total / elapsed:,.0f}")
    if lat:
        print(f"mean       {statistics.mean(lat):.0f}us")
        print(f"p50        {pct(0.50):.0f}us")
        print(f"p99        {pct(0.99):.0f}us")


if __name__ == "__main__":
    main()
