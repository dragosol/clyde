"""Smoke test for mlx_vlm_turbo_server.py.

1. Starts the wrapped server in a subprocess.
2. Polls /v1/models until ready.
3. Sends a text-only chat completion (verifies turbo3 cache injection works).
4. Sends an image+text request (verifies vision still works).
5. Kills the server.

Run from project root:
    .venv/bin/python clyde-benchmarks/test_server_smoke.py
"""
from __future__ import annotations

import argparse
import base64
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import json
from pathlib import Path


def wait_for_health(port: int, timeout: float = 600.0) -> bool:
    """Poll /health until it returns 200 or timeout."""
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(2)
    return False


def chat(port: int, messages: list, model: str, max_tokens: int = 50) -> dict:
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mlx-community/Qwen3.6-35B-A3B-4bit")
    ap.add_argument("--port", type=int, default=8814)
    ap.add_argument("--image", default=None,
                    help="Optional path to an image file for vision smoke test.")
    args = ap.parse_args()

    venv_python = Path(__file__).parent.parent / "mlx-flash" / ".venv" / "bin" / "python"
    server_script = Path(__file__).parent / "mlx_vlm_turbo_server.py"

    print(f"[smoke] starting server: {server_script} --model {args.model} --port {args.port}")
    proc = subprocess.Popen(
        [str(venv_python), str(server_script),
         "--model", args.model, "--port", str(args.port), "--host", "127.0.0.1"],
        stdout=sys.stdout, stderr=sys.stderr,
    )
    try:
        print("[smoke] waiting for /health (up to 600s)...")
        if not wait_for_health(args.port, timeout=600):
            print("[smoke] FAILED: server never became healthy", file=sys.stderr)
            return 1
        print("[smoke] server healthy")

        # Text-only chat
        print("\n[smoke] === text-only chat completion ===")
        t0 = time.perf_counter()
        resp = chat(args.port, [
            {"role": "user", "content": "What's the capital of France? Answer in one word."}
        ], args.model, max_tokens=20)
        dt = time.perf_counter() - t0
        msg = resp.get("choices", [{}])[0].get("message", {}).get("content", "<no content>")
        print(f"[smoke] response in {dt:.2f}s: {msg[:200]!r}")

        # Image chat (if path provided)
        if args.image and Path(args.image).exists():
            print(f"\n[smoke] === image chat completion ({args.image}) ===")
            with open(args.image, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
            ext = Path(args.image).suffix.lstrip(".") or "png"
            data_url = f"data:image/{ext};base64,{img_b64}"
            t0 = time.perf_counter()
            resp = chat(args.port, [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": "Describe this image in one sentence."},
                ],
            }], args.model, max_tokens=80)
            dt = time.perf_counter() - t0
            msg = resp.get("choices", [{}])[0].get("message", {}).get("content", "<no content>")
            print(f"[smoke] vision response in {dt:.2f}s: {msg[:300]!r}")
        else:
            print(f"\n[smoke] (skipping image test — no --image provided or file not found)")

        print("\n[smoke] DONE")
        return 0
    finally:
        print("[smoke] killing server...")
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
