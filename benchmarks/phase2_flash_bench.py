"""Phase 2 bench — forked mlx-flash + turbo3 KV + vlm model.

Measures prefill + decode at various contexts, with Flash weight streaming
active (ram_budget limits wired mem; OS page cache handles overflow).

Designed to run alongside memoryhog.py — hog pins 30/38 GB and this script
validates the stack still works under pressure.

Usage:
    # Terminal 1:
    .venv/bin/python clyde-benchmarks/memoryhog.py 30

    # Terminal 2:
    .venv/bin/python clyde-benchmarks/phase2_flash_bench.py --contexts 4096,40960 \\
        --ram 4 --kv-mode turbo3
"""
from __future__ import annotations

import argparse
import os
import resource
import sys
import time
from collections import Counter

# Ensure forked mlx_flash is importable when this script is run from any cwd.
_MLX_FLASH_DIR = os.path.expanduser("~/Documents/Clyde App Project/mlx-flash")
if _MLX_FLASH_DIR not in sys.path:
    sys.path.insert(0, _MLX_FLASH_DIR)

import mlx.core as mx


def rss_gb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024**3) if sys.platform == "darwin" else r / (1024**2)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mlx-community/Qwen3.6-35B-A3B-4bit")
    ap.add_argument("--contexts", default="4096,40960")
    ap.add_argument("--decode-tokens", type=int, default=40)
    ap.add_argument("--ram", type=float, default=4.0,
                    help="ram_budget_gb for Flash wired-mem limit")
    ap.add_argument("--kv-mode", default="turbo3", choices=["none","turbo3","turbo4"])
    ap.add_argument("--prefill-step", type=int, default=256,
                    help="prefill_step_size to bound activation burst")
    args = ap.parse_args()

    contexts = [int(c) for c in args.contexts.split(",") if c.strip()]

    from mlx_flash.config import FlashConfig
    from mlx_flash.integration.lmstudio import apply_flash_patch

    cfg = FlashConfig(
        enabled=True,
        kv_quant_mode=args.kv_mode,
        kv_quant_seed=0,
        ram_budget_gb=args.ram,
        debug=True,
    )
    apply_flash_patch(cfg)

    import mlx_vlm
    from mlx_vlm.prompt_utils import apply_chat_template

    print(f"[bench] loading {args.model} via mlx_vlm + Flash (ram={args.ram} GB, kv={args.kv_mode})")
    t0 = time.perf_counter()
    model, processor = mlx_vlm.load(args.model, lazy=True)
    print(f"[bench] load: {time.perf_counter()-t0:.1f}s, RSS={rss_gb():.1f} GB, peak={mx.get_peak_memory()/1e9:.2f} GB")

    tok = processor.tokenizer if hasattr(processor, "tokenizer") else processor

    # Warmup pass — primes Metal kernels + streams initial pages.
    print("[bench] warmup (~20 tokens @ 256 ctx)")
    warm_prompt = apply_chat_template(processor, model.config, "Say hello.", num_images=0)
    t0 = time.perf_counter()
    n_warm = 0
    for r in mlx_vlm.stream_generate(model, processor, warm_prompt, max_tokens=20, temperature=0.0):
        n_warm += 1
    print(f"[bench] warmup: {n_warm} segs in {time.perf_counter()-t0:.1f}s, RSS={rss_gb():.1f} GB")

    results = []
    for ctx in contexts:
        print(f"\n=== ctx ~{ctx} ===")
        haystack = build_haystack(tok, ctx)
        prompt = apply_chat_template(processor, model.config,
                                     haystack + "\nWrite three more sentences:",
                                     num_images=0)
        actual_tokens = len(tok.encode(prompt))
        print(f"  actual tokens: {actual_tokens}")

        mx.clear_cache()
        mx.reset_peak_memory()
        pre_rss = rss_gb()

        t0 = time.perf_counter()
        first_token_t = None
        n = 0
        text = ""
        for r in mlx_vlm.stream_generate(model, processor, prompt,
                                         max_tokens=args.decode_tokens,
                                         temperature=0.0,
                                         prefill_step_size=args.prefill_step):
            if first_token_t is None:
                first_token_t = time.perf_counter()
                prefill_peak_gb = mx.get_peak_memory() / 1e9
                mx.reset_peak_memory()
            seg = getattr(r, "text", r) if not isinstance(r, str) else r
            if seg:
                text += seg
                n += 1
        wall = time.perf_counter() - t0
        decode_peak_gb = mx.get_peak_memory() / 1e9
        final_rss = rss_gb()
        prefill_s = (first_token_t - t0) if first_token_t else wall
        decode_s = max(wall - prefill_s, 1e-6)
        decode_tps = (n - 1) / decode_s if n > 1 else 0
        results.append(dict(ctx=actual_tokens, prefill_s=prefill_s, decode_tps=decode_tps,
                            prefill_peak_gb=prefill_peak_gb, decode_peak_gb=decode_peak_gb,
                            pre_rss=pre_rss, final_rss=final_rss, n_segs=n))
        print(f"  prefill: {prefill_s:.2f}s | decode: {decode_tps:.2f} t/s "
              f"({n} segs in {decode_s:.2f}s)")
        print(f"  prefill-peak: {prefill_peak_gb:.2f} GB | decode-peak: {decode_peak_gb:.2f} GB")
        print(f"  RSS: {pre_rss:.1f} → {final_rss:.1f} GB")
        print(f"  OUTPUT: {text[:120]!r}")

    print("\n=== SUMMARY ===")
    print(f"{'ctx':>8} {'prefill_s':>10} {'decode_t/s':>11} {'pre_peak':>9} {'dec_peak':>9} {'RSS_end':>8}")
    for r in results:
        print(f"{r['ctx']:>8} {r['prefill_s']:>10.2f} {r['decode_tps']:>11.2f} "
              f"{r['prefill_peak_gb']:>9.2f} {r['decode_peak_gb']:>9.2f} {r['final_rss']:>8.1f}")


if __name__ == "__main__":
    main()
