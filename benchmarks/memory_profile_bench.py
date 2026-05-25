"""Memory profiling bench — SAFE VERSION (no 262K runs).

Runs Qwen3.6 at 4K / 40K / 128K with mx.reset_peak_memory() between phases
to separate prefill-peak from decode-peak. Tests baseline FP16 vs turbo3
V-only (no_v_buffer=False / True).

Purpose: figure out the REAL memory envelope without crashing the Mac.
Extrapolate 262K behavior from 40K → 128K scaling.

Usage:
    .venv/bin/python clyde-benchmarks/memory_profile_bench.py --contexts 4096,40960,131072
"""
from __future__ import annotations

import argparse
import resource
import sys
import time
from collections import Counter

import mlx.core as mx


def rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def build_haystack(tok, target_tokens: int) -> str:
    filler = (
        "The grass is green. The sky is blue. Birds fly south. "
        "Trees grow tall in the forest. Rivers flow to the sea. "
        "Mountains stand against the horizon. The sun rises. "
    )
    chunks = []
    cur = 0
    while cur < target_tokens - 200:
        chunks.append(filler)
        cur += len(tok.encode(filler))
    return "".join(chunks) + "\n\nWrite three more sentences:\n"


def bench_phases(model, processor, tok, prompt: str, max_tokens: int,
                 cache_fn, label: str, prefill_step: int):
    from mlx_vlm.generate import stream_generate

    mx.clear_cache()
    mx.reset_peak_memory()
    print(f"  [{label}] pre-gen peak: {mx.get_peak_memory()/1e9:.2f} GB, RSS: {rss_mb():.0f} MB")

    cache = cache_fn()
    n = 0
    text = ""
    last = None
    first_decode_t = None
    t0 = time.perf_counter()
    prefill_peak_gb = None
    prefill_peak_rss = None

    for r in stream_generate(model, processor, prompt=prompt,
                              max_tokens=max_tokens, temperature=0.0,
                              prompt_cache=cache,
                              prefill_step_size=prefill_step):
        n += 1
        last = r
        if hasattr(r, "text") and r.text:
            text += r.text
        if n == 2 and first_decode_t is None:
            # Just finished prefill — sample peak
            first_decode_t = time.perf_counter()
            prefill_peak_gb = mx.get_peak_memory() / 1e9
            prefill_peak_rss = rss_mb()
            # Now reset peak so we can measure decode-only
            mx.reset_peak_memory()
    wall = time.perf_counter() - t0
    decode_peak_gb = mx.get_peak_memory() / 1e9
    decode_final_rss = rss_mb()
    prefill_s = (first_decode_t - t0) if first_decode_t else 0
    decode_s = wall - prefill_s
    decode_tps = ((n - 1) / decode_s) if decode_s > 0 else 0
    return {
        "label": label,
        "wall_s": wall,
        "prefill_s": prefill_s,
        "decode_tps": decode_tps,
        "n": n,
        "prefill_peak_gb": prefill_peak_gb or 0,
        "prefill_peak_rss": prefill_peak_rss or 0,
        "decode_peak_gb": decode_peak_gb,
        "decode_final_rss": decode_final_rss,
        "text": text[:100],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mlx-community/Qwen3.6-35B-A3B-4bit")
    ap.add_argument("--contexts", default="4096,40960,131072")
    ap.add_argument("--prefill-step", type=int, default=512)
    ap.add_argument("--decode-tokens", type=int, default=30)
    ap.add_argument("--skip-baseline", action="store_true")
    ap.add_argument("--skip-turbo", action="store_true")
    args = ap.parse_args()

    contexts = [int(c) for c in args.contexts.split(",") if c.strip()]
    print(f"Loading {args.model}...")
    t0 = time.perf_counter()
    from mlx_vlm import load
    model, processor = load(args.model, lazy=True)
    tok = processor.tokenizer if hasattr(processor, "tokenizer") else processor
    print(f"load: {time.perf_counter()-t0:.2f}s, post-load peak: {mx.get_peak_memory()/1e9:.2f} GB, RSS: {rss_mb():.0f} MB")

    base = model.language_model.make_cache()
    print(f"cache layout: {dict(Counter(type(c).__name__ for c in base))}")

    from mlx_lm.models.cache import KVCache
    from turboquant_mlx.v_only_cache import VOnlyTurboQuantCache

    def fp16_factory():
        return model.language_model.make_cache()

    def turbo3_factory():
        return [VOnlyTurboQuantCache(bits=3, no_v_buffer=True) if isinstance(c, KVCache) else c
                for c in model.language_model.make_cache()]

    # Warmup (cheap, small ctx, primes Metal kernels)
    print("\n=== warmup ===")
    warmup_prompt = "Write a short paragraph about cats. " * 4
    _ = bench_phases(model, processor, tok, warmup_prompt, 20,
                     turbo3_factory, "warmup", args.prefill_step)

    results = []
    for ctx in contexts:
        print(f"\n=== ctx ~{ctx} ===")
        prompt = build_haystack(tok, ctx)
        actual = len(tok.encode(prompt))
        print(f"  actual prompt tokens: {actual}")
        if not args.skip_baseline:
            print("  running FP16 baseline...")
            r = bench_phases(model, processor, tok, prompt, args.decode_tokens,
                              fp16_factory, f"fp16-{ctx}", args.prefill_step)
            print(f"  FP16: wall {r['wall_s']:.1f}s | decode {r['decode_tps']:.1f} t/s | prefill-peak {r['prefill_peak_gb']:.1f} GB ({r['prefill_peak_rss']:.0f} MB RSS) | decode-only-peak {r['decode_peak_gb']:.2f} GB | final RSS {r['decode_final_rss']:.0f} MB")
            results.append(r)
        if not args.skip_turbo:
            print("  running turbo3 V-only no_v_buffer=True...")
            r = bench_phases(model, processor, tok, prompt, args.decode_tokens,
                              turbo3_factory, f"turbo3-{ctx}", args.prefill_step)
            print(f"  TURBO3: wall {r['wall_s']:.1f}s | decode {r['decode_tps']:.1f} t/s | prefill-peak {r['prefill_peak_gb']:.1f} GB ({r['prefill_peak_rss']:.0f} MB RSS) | decode-only-peak {r['decode_peak_gb']:.2f} GB | final RSS {r['decode_final_rss']:.0f} MB")
            results.append(r)

    print("\n=== SUMMARY ===")
    for r in results:
        print(f"{r['label']:20} | prefill-peak {r['prefill_peak_gb']:.1f} GB | decode-peak {r['decode_peak_gb']:.2f} GB | decode {r['decode_tps']:.1f} t/s")


if __name__ == "__main__":
    main()
