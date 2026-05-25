"""Pin N GB of incompressible RAM to force OS page cache eviction.

Usage:
    .venv/bin/python memoryhog.py 30   # pin 30 GB
    .venv/bin/python memoryhog.py 38   # pin 38 GB (16 GB Mac sim on 48 GB host)

Writes random bytes in 1 GB chunks and keeps them resident until killed.
Prints RSS every 5s so you can confirm it actually took the RAM.
"""
from __future__ import annotations

import os
import resource
import sys
import time


def rss_gb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024**3) if sys.platform == "darwin" else r / (1024**2)


def main():
    if len(sys.argv) < 2:
        print("usage: memoryhog.py <gb>", file=sys.stderr)
        sys.exit(1)
    gb = int(sys.argv[1])
    print(f"[hog] pinning {gb} GB (pid={os.getpid()})", flush=True)

    buffers = []
    for i in range(gb):
        buf = os.urandom(1024**3)  # 1 GB random bytes, incompressible
        buffers.append(buf)
        if (i + 1) % 5 == 0 or i == gb - 1:
            print(f"[hog] {i+1}/{gb} GB pinned, RSS={rss_gb():.1f} GB", flush=True)

    print(f"[hog] READY — holding {gb} GB. Send SIGTERM / ^C to release.", flush=True)
    try:
        while True:
            time.sleep(5)
            # Touch each buffer to keep pages hot (prevent swap).
            for b in buffers:
                _ = b[0]
    except KeyboardInterrupt:
        print("[hog] exiting", flush=True)


if __name__ == "__main__":
    main()
