#!/usr/bin/env python3
import os
import subprocess
import sys
import time
from urllib.request import urlopen

import psutil


def fetch(url: str) -> str:
    with urlopen(url, timeout=3) as resp:
        return resp.read().decode("utf-8")


def main() -> int:
    env = os.environ.copy()
    env.setdefault("LIVEKIT_API_KEY", "devkey")
    env.setdefault("LIVEKIT_API_SECRET", "devsecret")

    proc = subprocess.Popen([sys.executable, "server.py", "--host", "127.0.0.1", "--port", "18000"], env=env)
    p = psutil.Process(proc.pid)

    try:
        for _ in range(20):
            try:
                fetch("http://127.0.0.1:18000/healthz")
                break
            except Exception:
                time.sleep(0.2)
        else:
            print("health check failed")
            return 2

        rss_samples = []
        for _ in range(20):
            fetch("http://127.0.0.1:18000/livekit/token?room=perf&identity=tester&role=viewer")
            rss_samples.append(p.memory_info().rss)
            time.sleep(0.2)

        rss_delta_kib = (max(rss_samples) - min(rss_samples)) / 1024.0
        print(f"Server token endpoint rss delta: {rss_delta_kib:.1f} KiB")
        print("Validation smoke test: OK")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
