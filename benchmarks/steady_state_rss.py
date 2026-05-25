"""Measure steady-state RSS at 262K context with turbo3 vs baseline KV.

The bench script captures mlx-vlm's `peak_memory` which is biased high by
prefill activation buffers. This script samples actual process RSS
(via `ps`) at three checkpoints:

1. After model load (weights only).
2. After prefill at 262K (peak — same as bench's peak_memory).
3. After 100 tokens of decode (post-prefill steady-state — what users feel).

The "user-felt memory" is checkpoint 3. Pro tier target: ≤22 GB at this
checkpoint with turbo3 KV.

Usage:
    .venv/bin/python clyde-benchmarks/steady_state_rss.py --kv turbo3
    .venv/bin/python clyde-benchmarks/steady_state_rss.py --kv fp16
"""
from __future__ import annotations

import argparse
import gc
import os
import subprocess
import time
from collections import Counter

import mlx.core as mx
from mlx_lm.models.cache import KVCache


def rss_mb(pid: int = None) -> float:
    pid = pid or os.getpid()
    out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)]).decode().strip()
    return int(out) / 1024  # KB → MB


def build_haystack(tok, target_tokens: int) -> str:
    filler = (
        "The grass is green. The sky is blue. Birds fly south for the winter. "
        "Trees grow tall in the forest. Rivers flow to the sea. "
        "Mountains stand against the horizon. The sun rises in the east. "
        "Stars twinkle at night. Clouds drift across the sky. "
    )
    chunks = []
    cur = 0
    while cur < target_tokens - 200:
        chunks.append(filler)
        cur += len(tok.encode(filler))
    return "".join(chunks) + "\n\nWrite three more sentences continuing the same style:\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mlx-community/Qwen3.6-35B-A3B-4bit")
    ap.add_argument("--ctx", type=int, default=262144)
    ap.add_argument("--kv", choices=["turbo3", "fp16"], default="turbo3")
    ap.add_argument("--decode-tokens", type=int, default=100)
    args = ap.parse_args()

    print(f"=== Steady-state RSS check: {args.kv} KV, ctx={args.ctx} ===")
    print(f"checkpoint 1 (process startup): RSS = {rss_mb():.0f} MB")

    from mlx_vlm import load
    from mlx_vlm.generate import stream_generate
    from turboquant_mlx.v_only_cache import VOnlyTurboQuantCache

    print(f"loading {args.model}...")
    t = time.perf_counter()
    model, processor = load(args.model, lazy=True)
    print(f"  load: {time.perf_counter()-t:.2f}s | RSS = {rss_mb():.0f} MB (post-load, weights only)")

    tok = processor.tokenizer if hasattr(processor, "tokenizer") else processor

    def make_cache():
        base = model.language_model.make_cache()
        if args.kv == "turbo3":
            return [VOnlyTurboQuantCache(bits=3) if isinstance(c, KVCache) else c for c in base]
        return base

    print(f"\nbuilding haystack to ~{args.ctx} tokens...")
    prompt = build_haystack(tok, args.ctx)
    actual_tokens = len(tok.encode(prompt))
    print(f"  actual prompt tokens: {actual_tokens}")

    print(f"\ngenerating with {args.kv} cache (will fill KV during prefill)...")
    cache = make_cache()
    print(f"  cache layout: {dict(Counter(type(c).__name__ for c in cache))}")

    n = 0
    text = ""
    last = None
    t0 = time.perf_counter()
    rss_after_first_token = None
    rss_after_decode = None
    for r in stream_generate(model, processor, prompt=prompt,
                              max_tokens=args.decode_tokens, temperature=0.0,
                              prompt_cache=cache):
        n += 1
        last = r
        if hasattr(r, "text") and r.text:
            text += r.text
        # Sample RSS right after first decode token (= prefill done, KV full)
        if n == 2 and rss_after_first_token is None:
            rss_after_first_token = rss_mb()
            print(f"  checkpoint 2 (after prefill, before decode): RSS = {rss_after_first_token:.0f} MB")
    wall = time.perf_counter() - t0
    rss_after_decode = rss_mb()
    print(f"  checkpoint 3 (after {n-1} decode tokens): RSS = {rss_after_decode:.0f} MB")

    # Force GC and re-sample (in case Python is holding objects)
    gc.collect()
    mx.clear_cache()
    rss_post_gc = rss_mb()
    print(f"  checkpoint 4 (after GC + mx.clear_cache): RSS = {rss_post_gc:.0f} MB")

    print(f"\n=== summary ({args.kv}, ctx={actual_tokens}) ===")
    print(f"  weights only:        {rss_mb() if False else 'see checkpoint 1':>10}")
    print(f"  after prefill:       {rss_after_first_token:.0f} MB" if rss_after_first_token else "  N/A")
    print(f"  steady-state decode: {rss_after_decode:.0f} MB")
    print(f"  post-GC:             {rss_post_gc:.0f} MB")
    print(f"  mlx-vlm peak_memory (last chunk reported): {getattr(last, 'peak_memory', 0):.1f} GB")
    print(f"  generated text preview: {text[:120]!r}")
    print(f"  wall: {wall:.1f}s")


if __name__ == "__main__":
    main()
