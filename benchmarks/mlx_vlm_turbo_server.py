"""Run mlx_vlm.server with per-layer TurboQuantKVCache injection AND
cross-turn prompt caching (KV cache reuse).

Uses mlx-vlm's NATIVE TurboQuant implementation (`mlx_vlm.turboquant.TurboQuantKVCache`).
No monkey-patch — `mlx_vlm.models.base.scaled_dot_product_attention` already detects
`isinstance(cache, TurboQuantKVCache)` and routes to `cache.prefill_attention()` /
`cache.decode_attention()` methods.

On Qwen3.6-35B-A3B and similar hybrid-attention models, we preserve the 30
GatedDeltaNet `ArraysCache` layers (linear attention, constant state) and only
replace the 10 full-attention `KVCache` layers with `TurboQuantKVCache`.

PROMPT CACHING: A `PromptCacheState` tracks the KV cache across requests.
On the first turn, the full prompt is prefilled and saved. On subsequent
turns, we prefix-match against cached tokens, trim the cache to the common
prefix, and only prefill the delta. This is handled OUTSIDE of mlx_vlm's
built-in prompt_cache_state (which can't trim TurboQuantKVCache) — we do
the tokenization, prefix matching, cache trimming, and input_ids slicing
ourselves, then call stream_generate with the prepared cache.

Usage:
    .venv/bin/python clyde-benchmarks/mlx_vlm_turbo_server.py \
        --model mlx-community/Qwen3.6-35B-A3B-4bit \
        --port 8814 --host 127.0.0.1

Optional: --kv-bits (default 3), --kv-seed (default 0).
"""
from __future__ import annotations

import copy
import sys


def _install_patch(kv_bits: float = 3.0, kv_seed: int = 0):
    """Patch mlx_vlm's stream_generate before the server imports it.

    Two concerns:
    1. TurboQuantKVCache injection — replaces full-attention KVCache layers
       with quantized equivalents while leaving linear-attention layers alone.
    2. Prompt caching — prefix-match and trim across turns so only new
       tokens are prefilled. Handles TurboQuantKVCache via .trim()/.offset
       instead of the array-slicing mlx_vlm uses internally (which crashes
       on quantized state objects).
    """
    import mlx_vlm.generate  # ensures module is loaded
    _gen = sys.modules["mlx_vlm.generate"]
    from mlx_lm.models.cache import KVCache
    from mlx_vlm.turboquant import TurboQuantKVCache
    import mlx.core as mx

    _orig = _gen.stream_generate

    # Persistent cache state — survives across requests.
    _cache_state = _gen.PromptCacheState()
    _first_cache_build = True

    def _build_turbo_cache(model):
        """Build a fresh hybrid TurboQuant + ArraysCache per-layer cache."""
        try:
            base = model.language_model.make_cache()
        except AttributeError:
            base = model.make_cache() if hasattr(model, "make_cache") else None
        if base is None:
            return None
        return [
            TurboQuantKVCache(bits=kv_bits, seed=kv_seed) if isinstance(c, KVCache) else c
            for c in base
        ]

    def _trim_cache_to(kv_cache, prefix_len):
        """Trim each layer's cache to exactly prefix_len tokens.

        TurboQuantKVCache: use .trim(n) + .offset setter.
        Regular KVCache: array-slice keys/values.
        ArraysCache (linear attention): no keys/offset — skip.
        """
        for c in kv_cache:
            if isinstance(c, TurboQuantKVCache):
                if c.offset > prefix_len:
                    c.trim(c.offset - prefix_len)
                elif c.offset < prefix_len:
                    # Shouldn't happen, but safety: force offset
                    c.offset = prefix_len
            elif hasattr(c, "keys") and c.keys is not None:
                # Regular KVCache — array slicing
                cached_len = c.keys.shape[2]
                if cached_len > prefix_len:
                    c.keys = c.keys[:, :, :prefix_len, :]
                    c.values = c.values[:, :, :prefix_len, :]
                if hasattr(c, "offset"):
                    c.offset = prefix_len
            # ArraysCache has no keys/offset — skip

    def _patched(model, processor, prompt, *args, **kwargs):
        nonlocal _first_cache_build

        # ── Never pass prompt_cache_state to _orig ──
        # mlx_vlm's built-in handler can't trim TurboQuantKVCache (crashes
        # on c.keys.shape). We handle prefix matching + trimming ourselves.
        kwargs.pop("prompt_cache_state", None)

        tokenizer = processor.tokenizer if hasattr(processor, "tokenizer") else processor
        new_tokens = tokenizer.encode(prompt)
        prefix_len = _cache_state.find_prefix_length(new_tokens)

        if prefix_len > 0 and prefix_len < len(new_tokens) and _cache_state.cache is not None:
            # ── Cache HIT: reuse prefix, prefill only the delta ──
            kv_cache = copy.deepcopy(_cache_state.cache)
            _trim_cache_to(kv_cache, prefix_len)
            kwargs["prompt_cache"] = kv_cache

            # Tell stream_generate to skip tokenization + only process
            # tokens after the cached prefix. input_ids must be [1, seq].
            remaining = new_tokens[prefix_len:]
            kwargs["input_ids"] = mx.array(remaining)[None]
            # No need for pixel_values/images on continuation turns
            kwargs.pop("image", None)
            kwargs.pop("pixel_values", None)
            kwargs.pop("cached_image_features", None)

            n_new = len(remaining)
            print(
                f"[prompt-cache] HIT — reusing {prefix_len} cached tokens, "
                f"prefilling {n_new} new",
                file=sys.stderr, flush=True,
            )
        else:
            # ── Cache MISS: fresh TurboQuantKVCache ──
            if "prompt_cache" not in kwargs or kwargs["prompt_cache"] is None:
                hybrid = _build_turbo_cache(model)
                if hybrid is not None:
                    kwargs["prompt_cache"] = hybrid
                    if _first_cache_build:
                        n_turbo = sum(1 for c in hybrid if isinstance(c, TurboQuantKVCache))
                        n_total = len(hybrid)
                        print(
                            f"[clyde-turbo] per-layer turbo{kv_bits:g} KV cache: "
                            f"{n_turbo} of {n_total} layers quantized "
                            f"(others = linear-attention, untouched)",
                            file=sys.stderr, flush=True,
                        )
                        _first_cache_build = False
            print(
                f"[prompt-cache] MISS — fresh cache, prefilling {len(new_tokens)} tokens",
                file=sys.stderr, flush=True,
            )

        # Keep a reference to the cache so we can save it after generation.
        # stream_generate modifies it in-place as tokens are generated.
        cache_ref = kwargs.get("prompt_cache")

        # Wrap the generator to save cache state after generation completes.
        gen = _orig(model, processor, prompt, *args, **kwargs)
        generated_tokens = []
        for result in gen:
            if result.token is not None:
                t = result.token
                generated_tokens.append(t.item() if hasattr(t, "item") else t)
            yield result

        # Save full token sequence + updated KV cache for next turn
        if cache_ref is not None:
            all_ids = new_tokens + generated_tokens
            _cache_state.update(all_ids, cache_ref)
            print(
                f"[prompt-cache] saved {len(all_ids)} tokens "
                f"({len(new_tokens)} prompt + {len(generated_tokens)} generated)",
                file=sys.stderr, flush=True,
            )

    _gen.stream_generate = _patched
    # Also patch utils re-export (server imports from there)
    try:
        import mlx_vlm.utils
        _utils = sys.modules["mlx_vlm.utils"]
        _utils.stream_generate = _patched
    except Exception:
        pass


def main():
    import argparse
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--kv-bits", type=float, default=3.0,
                    help="TurboQuant KV bits. Default 3. Integer or .5 step (mlx-vlm rule).")
    ap.add_argument("--kv-seed", type=int, default=0,
                    help="TurboQuant seed for the randomized Hadamard rotation.")
    # Parse our flags first, pass the rest to mlx_vlm.server
    known, remaining = ap.parse_known_args()
    sys.argv = [sys.argv[0]] + remaining

    _install_patch(kv_bits=known.kv_bits, kv_seed=known.kv_seed)
    import mlx_vlm.server as _server
    # Ensure server's own stream_generate reference picks up the patched version
    import mlx_vlm.utils
    _utils = sys.modules["mlx_vlm.utils"]
    _server.stream_generate = _utils.stream_generate
    _server.main()


if __name__ == "__main__":
    main()
