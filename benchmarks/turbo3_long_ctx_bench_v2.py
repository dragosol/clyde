"""Phase 1.6 long-context bench for mlx-vlm + per-layer turbo3 KV.

Loads `mlx-community/Qwen3.6-35B-A3B-4bit` (vision-capable) via mlx-vlm,
builds a per-layer hybrid cache (VOnlyTurboQuantCache(bits=3) for the 10
full-attention layers, native ArraysCache for the 30 GatedDeltaNet layers
returned by `model.language_model.make_cache()`), and benchmarks decode
throughput at multiple context lengths.

Usage:
    .venv/bin/python clyde-benchmarks/turbo3_long_ctx_bench_v2.py \
        --contexts 4096,40960,131072,262144 \
        --decode-tokens 50

Reports per-context: prefill seconds, decode tok/s sustained, peak RSS MB.
Optionally injects a passkey at 25% of context for quality validation.
"""
from __future__ import annotations

import argparse
import resource
import sys
import time
from collections import Counter

import mlx.core as mx
from mlx_vlm import load
from mlx_vlm.generate import stream_generate
from mlx_lm.models.cache import KVCache
from turboquant_mlx.v_only_cache import VOnlyTurboQuantCache


def peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def build_haystack(tok, target_tokens: int, passkey: str | None = None,
                   passkey_position_frac: float = 0.25) -> str:
    filler = (
        "The grass is green. The sky is blue. Birds fly south for the winter. "
        "Trees grow tall in the forest. Rivers flow to the sea. "
        "Mountains stand against the horizon. The sun rises in the east. "
        "Stars twinkle at night. The moon waxes and wanes. Clouds drift by. "
    )
    chunks = []
    cur = 0
    while cur < target_tokens - 200:
        chunks.append(filler)
        cur += len(tok.encode(filler))
    text = "".join(chunks)
    if passkey:
        sentence = f" The secret code is {passkey}. Remember it well. "
        char_pos = int(len(text) * passkey_position_frac)
        while char_pos < len(text) and text[char_pos] != " ":
            char_pos += 1
        text = text[:char_pos] + sentence + text[char_pos:]
    return text


def make_hybrid(model):
    base = model.language_model.make_cache()
    return [VOnlyTurboQuantCache(bits=3) if isinstance(c, KVCache) else c for c in base]


def make_baseline(model):
    return model.language_model.make_cache()


def bench_one(model, processor, tok, prompt: str, max_tokens: int,
              cache_fn, label: str):
    """Time prefill (until first decoded token arrives) and decode (per-token thereafter)
    manually. The last chunk's generation_tps is unreliable for small max_tokens."""
    mx.clear_cache()
    n = 0
    text = ""
    last = None
    chunk_times = []
    t0 = time.perf_counter()
    for r in stream_generate(model, processor, prompt=prompt,
                              max_tokens=max_tokens, temperature=0.0,
                              prompt_cache=cache_fn()):
        n += 1
        last = r
        chunk_times.append(time.perf_counter())
        if hasattr(r, "text") and r.text:
            text += r.text
    wall = time.perf_counter() - t0
    # Chunk 0 = prefill done (token 1). Chunks 1..N-1 = decoded tokens.
    prefill_s = (chunk_times[0] - t0) if chunk_times else 0
    decode_s = (chunk_times[-1] - chunk_times[0]) if len(chunk_times) >= 2 else 0
    n_decode = max(0, len(chunk_times) - 1)
    decode_tps = (n_decode / decode_s) if decode_s > 0 else 0
    prompt_toks = getattr(last, "prompt_tokens", 0) if last else 0
    return {
        "label": label,
        "generated": n,
        "wall_s": wall,
        "prompt_tokens": prompt_toks,
        "prefill_s": prefill_s,
        "prefill_tps": (prompt_toks / prefill_s) if prefill_s > 0 else 0,
        "decode_s": decode_s,
        "decode_tps": decode_tps,
        "peak_metal_gb": getattr(last, "peak_memory", 0) if last else 0,
        "peak_rss_mb": peak_rss_mb(),
        "text": text,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mlx-community/Qwen3.6-35B-A3B-4bit")
    ap.add_argument("--contexts", default="4096,40960,131072,262144")
    ap.add_argument("--decode-tokens", type=int, default=50)
    ap.add_argument("--baseline", action="store_true",
                    help="Also run FP16 KV baseline at each context for comparison.")
    ap.add_argument("--passkey", action="store_true",
                    help="Inject passkey at 25%% and check retrieval (quality test).")
    ap.add_argument("--skip-warmup", action="store_true")
    args = ap.parse_args()

    contexts = [int(c) for c in args.contexts.split(",") if c.strip()]
    print(f"=== Loading {args.model} via mlx-vlm ===")
    t0 = time.perf_counter()
    model, processor = load(args.model, lazy=True)
    tok = processor.tokenizer if hasattr(processor, "tokenizer") else processor
    print(f"load: {time.perf_counter()-t0:.2f}s | RSS={peak_rss_mb():.0f} MB")

    base = model.language_model.make_cache()
    print(f"hybrid cache layout: {dict(Counter(type(c).__name__ for c in base))}")

    if not args.skip_warmup:
        print("\n=== Warmup ===")
        warmup_prompt = "Write a paragraph about cats. " * 5
        res = bench_one(model, processor, tok, warmup_prompt, 30,
                        lambda: make_hybrid(model), "warmup")
        print(f" warmup: {res['generated']} tok in {res['wall_s']:.2f}s")

    PASSKEY = "LANTERN-7743"
    for ctx in contexts:
        print(f"\n=== ctx target ~ {ctx} tokens ===")
        if args.passkey:
            prompt = build_haystack(tok, ctx, passkey=PASSKEY)
            prompt += "\n\nQ: What is the secret code mentioned earlier? A:"
            actual_tokens = len(tok.encode(prompt))
            print(f"  prompt actual tokens: {actual_tokens}")
            res = bench_one(model, processor, tok, prompt, max_tokens=20,
                            cache_fn=lambda: make_hybrid(model),
                            label=f"turbo3-passkey-{ctx}")
            found = PASSKEY in res["text"]
            print(f"  TURBO3: wall {res['wall_s']:.1f}s | prefill {res['prefill_tps']:.0f} tok/s | decode {res['decode_tps']:.1f} tok/s | metal {res['peak_metal_gb']:.1f} GB | RSS {res['peak_rss_mb']:.0f} MB")
            print(f"  PASSKEY {'FOUND' if found else 'MISSING'}: {res['text'][:150]!r}")
            if args.baseline:
                res_b = bench_one(model, processor, tok, prompt, max_tokens=20,
                                  cache_fn=lambda: make_baseline(model),
                                  label=f"baseline-passkey-{ctx}")
                found_b = PASSKEY in res_b["text"]
                print(f"  BASELINE: wall {res_b['wall_s']:.1f}s | prefill {res_b['prefill_tps']:.0f} tok/s | decode {res_b['decode_tps']:.1f} tok/s | metal {res_b['peak_metal_gb']:.1f} GB | RSS {res_b['peak_rss_mb']:.0f} MB")
                print(f"  BASELINE PASSKEY {'FOUND' if found_b else 'MISSING'}: {res_b['text'][:150]!r}")
        else:
            # Add an explicit instruction so the model actually generates rather than EOS-ing
            prompt = build_haystack(tok, ctx) + "\n\nWrite three more sentences continuing the same style:\n"
            actual_tokens = len(tok.encode(prompt))
            print(f"  prompt actual tokens: {actual_tokens}")
            res = bench_one(model, processor, tok, prompt, args.decode_tokens,
                            cache_fn=lambda: make_hybrid(model),
                            label=f"turbo3-{ctx}")
            print(f"  TURBO3: wall {res['wall_s']:.1f}s | prefill {res['prefill_tps']:.0f} tok/s | decode {res['decode_tps']:.1f} tok/s | metal {res['peak_metal_gb']:.1f} GB | RSS {res['peak_rss_mb']:.0f} MB")
            if args.baseline:
                res_b = bench_one(model, processor, tok, prompt, args.decode_tokens,
                                  cache_fn=lambda: make_baseline(model),
                                  label=f"baseline-{ctx}")
                print(f"  BASELINE: wall {res_b['wall_s']:.1f}s | prefill {res_b['prefill_tps']:.0f} tok/s | decode {res_b['decode_tps']:.1f} tok/s | metal {res_b['peak_metal_gb']:.1f} GB | RSS {res_b['peak_rss_mb']:.0f} MB")


if __name__ == "__main__":
    main()
