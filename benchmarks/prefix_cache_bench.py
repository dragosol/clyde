"""Phase 2c.A prefix cache perf bench.

Measures save/load time + file size at a few realistic prefix sizes
(system prompt + skills + tool schemas would land around 1K-8K tokens).

Usage:
    .venv/bin/python clyde-benchmarks/prefix_cache_bench.py
"""
from __future__ import annotations

import os
import sys
import time

_MLX_FLASH_DIR = os.path.expanduser("~/Documents/Clyde App Project/mlx-flash")
if _MLX_FLASH_DIR not in sys.path:
    sys.path.insert(0, _MLX_FLASH_DIR)

import mlx.core as mx
from mlx_flash.config import FlashConfig
from mlx_flash.integration.lmstudio import apply_flash_patch, _build_flash_prompt_cache
from mlx_flash.prefix_cache import save_cache, load_cache


def _build_haystack(tok, target_tokens: int) -> str:
    filler = (
        "System: You are a helpful assistant. Tools available: bash, read_file, "
        "web_search, edit_file, grep. Respond concisely. "
    )
    cur = 0
    chunks = []
    while cur < target_tokens - 100:
        chunks.append(filler)
        cur += len(tok.encode(filler))
    return "".join(chunks)


def main():
    cfg = FlashConfig(enabled=True, kv_quant_mode="turbo3", kv_quant_seed=0,
                       ram_budget_gb=8.0, debug=False)
    apply_flash_patch(cfg)

    import mlx_vlm
    from mlx_vlm.prompt_utils import apply_chat_template

    print("loading model...")
    model, processor = mlx_vlm.load("mlx-community/Qwen3.6-35B-A3B-4bit", lazy=True)
    tok = processor.tokenizer if hasattr(processor, "tokenizer") else processor

    sizes = [256, 1024, 4096, 8192]
    results = []

    # Warmup
    warm = apply_chat_template(processor, model.config, "Hello.", num_images=0)
    _ = list(mlx_vlm.stream_generate(model, processor, warm, max_tokens=3, temperature=0.0))

    for target in sizes:
        prompt = apply_chat_template(processor, model.config,
                                      _build_haystack(tok, target) + "\nWrite one word:",
                                      num_images=0)
        actual = len(tok.encode(prompt))
        print(f"\n=== prefix ~{target} tokens (actual: {actual}) ===")

        cache = _build_flash_prompt_cache(model, cfg)
        # Prefill + emit 1 token to make the TQ layers accumulate state
        _ = list(mlx_vlm.stream_generate(model, processor, prompt,
                                          max_tokens=1, temperature=0.0,
                                          prompt_cache=cache,
                                          prefill_step_size=128))

        from mlx_vlm.turboquant import TurboQuantKVCache
        tq_offset = next((c.offset for c in cache if isinstance(c, TurboQuantKVCache)), 0)
        print(f"  TQ layer offset: {tq_offset}")

        path = f"/tmp/prefix-cache-bench-{target}.safetensors"

        t0 = time.perf_counter()
        save_cache(path, cache)
        save_ms = (time.perf_counter() - t0) * 1000
        size_mb = os.path.getsize(path) / (1024 * 1024)

        t0 = time.perf_counter()
        loaded = load_cache(path)
        load_ms = (time.perf_counter() - t0) * 1000

        # Verify
        orig_offsets = [c.offset for c in cache if isinstance(c, TurboQuantKVCache)]
        load_offsets = [c.offset for c in loaded if isinstance(c, TurboQuantKVCache)]
        ok = orig_offsets == load_offsets

        results.append(dict(target=target, actual=actual, tq_offset=tq_offset,
                            size_mb=size_mb, save_ms=save_ms, load_ms=load_ms, ok=ok))
        print(f"  file: {size_mb:.2f} MB | save: {save_ms:.0f} ms | load: {load_ms:.0f} ms | match: {ok}")

        os.unlink(path)

    print("\n=== SUMMARY ===")
    print(f"{'ctx':>7} {'actual':>7} {'tq_off':>7} {'size_MB':>9} {'save_ms':>9} {'load_ms':>9} {'ok':>5}")
    for r in results:
        print(f"{r['target']:>7} {r['actual']:>7} {r['tq_offset']:>7} "
              f"{r['size_mb']:>9.2f} {r['save_ms']:>9.0f} {r['load_ms']:>9.0f} {str(r['ok']):>5}")


if __name__ == "__main__":
    main()
