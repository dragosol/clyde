"""Phase 1.6 Long-Context Benchmark: mlx-vlm + per-layer turbo3 KV on Qwen3.6.

Loads Qwen3.6-35B-A3B-4bit (vision-capable) via mlx-vlm, builds a per-layer
hybrid cache (VOnlyTurboQuantCache for full-attention layers, native ArraysCache
for GatedDeltaNet layers), and benchmarks decode/prefill throughput at
multiple context lengths.

Usage:
    .venv/bin/python clyde-benchmarks/turbo3_long_ctx_bench.py \
        --model mlx-community/Qwen3.6-35B-A3B-4bit \
        --contexts 4096,40960,131072,262144 \
        --decode-tokens 100

Reports per-context: prefill seconds, decode tok/s sustained, peak RSS MB.
"""
from __future__ import annotations

import argparse
import os
import resource
import sys
import time
from collections import Counter
from pathlib import Path

import mlx.core as mx


def _peak_rss_mb() -> float:
    """Process peak RSS in MB. macOS reports in bytes, Linux in KB."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def build_haystack(tok, target_tokens: int, passkey: str | None = None,
                   passkey_position: int | None = None) -> str:
    """Build a roughly target_tokens-long prompt by repeating filler text.

    If passkey + position are supplied, inject the passkey near that token
    position; otherwise return pure filler.
    """
    filler = (
        "The grass is green. The sky is blue. Birds fly south for the winter. "
        "Trees grow tall in the forest. Rivers flow to the sea. "
        "Mountains stand against the horizon. The sun rises in the east. "
    )
    chunks = []
    cur_tokens = 0
    while cur_tokens < target_tokens - 200:
        chunks.append(filler)
        cur_tokens += len(tok.encode(filler))
    text = "".join(chunks)
    if passkey is not None and passkey_position is not None:
        # Insert a passkey sentence near the requested token position
        sentence = f" The secret code is {passkey}. Remember it. "
        # Find a rough char-offset corresponding to the token position
        ratio = passkey_position / cur_tokens if cur_tokens else 0.5
        char_pos = int(len(text) * ratio)
        # Snap to nearest space
        while char_pos < len(text) and text[char_pos] != " ":
            char_pos += 1
        text = text[:char_pos] + sentence + text[char_pos:]
    return text


def make_hybrid_cache(model):
    """Replace KVCache entries with VOnlyTurboQuantCache(bits=3); keep ArraysCache."""
    from mlx_lm.models.cache import make_prompt_cache, KVCache
    from turboquant_mlx.v_only_cache import VOnlyTurboQuantCache

    base = make_prompt_cache(model)
    out = []
    for c in base:
        out.append(VOnlyTurboQuantCache(bits=3) if isinstance(c, KVCache) else c)
    return out, Counter(type(c).__name__ for c in out)


def bench_one(model, tok, prompt: str, max_tokens: int, label: str, *,
              cache_factory):
    """Run one prefill+decode bench. Returns dict with metrics."""
    from mlx_lm.generate import stream_generate
    from mlx_lm.sample_utils import make_sampler

    sampler = make_sampler(temp=0.0)
    cache = cache_factory()
    prompt_tokens = len(tok.encode(prompt))

    # GC before
    mx.clear_cache()

    t0 = time.perf_counter()
    n = 0
    prefill_done = None
    text = ""
    for r in stream_generate(model, tok, prompt=prompt, max_tokens=max_tokens,
                              sampler=sampler, prompt_cache=cache):
        if n == 1 and prefill_done is None:
            prefill_done = time.perf_counter()
        n += 1
        if hasattr(r, "text"):
            text += r.text
    total = time.perf_counter() - t0
    prefill_t = (prefill_done - t0) if prefill_done else 0
    decode_t = total - prefill_t
    decode_tok_per_s = ((n - 1) / decode_t) if decode_t > 0 else 0
    return {
        "label": label,
        "prompt_tokens": prompt_tokens,
        "generated_tokens": n,
        "prefill_s": prefill_t,
        "decode_s": decode_t,
        "decode_tok_per_s": decode_tok_per_s,
        "peak_rss_mb": _peak_rss_mb(),
        "text_preview": text[:200],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mlx-community/Qwen3.6-35B-A3B-4bit",
                    help="HF repo or local path. Use the vision-capable variant.")
    ap.add_argument("--contexts", default="4096,40960",
                    help="Comma-separated context lengths to test.")
    ap.add_argument("--decode-tokens", type=int, default=100,
                    help="Tokens to generate per bench.")
    ap.add_argument("--via-mlx-vlm", action="store_true",
                    help="Load through mlx-vlm (default: mlx-lm).")
    ap.add_argument("--passkey-test", action="store_true",
                    help="Inject a passkey at 25%% of context, ask to retrieve.")
    args = ap.parse_args()

    contexts = [int(c) for c in args.contexts.split(",") if c.strip()]
    print(f"=== Loading {args.model} via {'mlx-vlm' if args.via_mlx_vlm else 'mlx-lm'} ===")
    t0 = time.perf_counter()
    if args.via_mlx_vlm:
        from mlx_vlm import load
        model, processor = load(args.model, lazy=True)
        tok = processor.tokenizer if hasattr(processor, "tokenizer") else processor
    else:
        from mlx_lm.utils import load
        model, tok = load(args.model, lazy=True)
    load_s = time.perf_counter() - t0
    print(f"load: {load_s:.2f}s, peak RSS: {_peak_rss_mb():.0f} MB")

    # Inspect cache layout
    from mlx_lm.models.cache import make_prompt_cache
    base_cache = make_prompt_cache(model)
    type_counts = Counter(type(c).__name__ for c in base_cache)
    print(f"cache layout: {dict(type_counts)}")

    # Hybrid cache factory
    from mlx_lm.models.cache import KVCache
    from turboquant_mlx.v_only_cache import VOnlyTurboQuantCache

    def hybrid_factory():
        return [
            VOnlyTurboQuantCache(bits=3) if isinstance(c, KVCache) else c
            for c in make_prompt_cache(model)
        ]

    def baseline_factory():
        return make_prompt_cache(model)

    # Warmup
    print("\n=== Warmup (50 tok on small prompt) ===")
    short_prompt = "Write a short paragraph about cats."
    res = bench_one(model, tok, short_prompt, max_tokens=50, label="warmup",
                    cache_factory=hybrid_factory)
    print(f"  {res['generated_tokens']} tok in {res['prefill_s']+res['decode_s']:.2f}s")

    # Benches
    for ctx in contexts:
        print(f"\n=== ctx={ctx} ===")
        if args.passkey_test:
            passkey = "LANTERN-7743"
            pos = ctx // 4
            prompt = build_haystack(tok, ctx, passkey=passkey, passkey_position=pos)
            prompt += f"\n\nQUESTION: What is the secret code mentioned earlier? Answer with just the code."
            print(f"  built haystack ~{len(tok.encode(prompt))} tokens, passkey at ~{pos}")
            res = bench_one(model, tok, prompt, max_tokens=20, label=f"passkey-{ctx}",
                            cache_factory=hybrid_factory)
            found = passkey in res["text_preview"]
            print(f"  PASSKEY: {'FOUND' if found else 'MISSING'} in: {res['text_preview']!r}")
        else:
            prompt = build_haystack(tok, ctx)
            res = bench_one(model, tok, prompt, max_tokens=args.decode_tokens,
                            label=f"ctx-{ctx}", cache_factory=hybrid_factory)
        print(f"  prompt={res['prompt_tokens']} tok | prefill={res['prefill_s']:.2f}s "
              f"| decode={res['decode_tok_per_s']:.1f} tok/s | RSS={res['peak_rss_mb']:.0f} MB")


if __name__ == "__main__":
    main()
