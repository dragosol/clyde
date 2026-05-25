"""
agent/backends/llamacpp.py
──────────────────────────
LlamaCppBackend — wraps llama.cpp's llama-server (an OpenAI-compatible
HTTP server). Adds support for backend-specific hints that aren't in the
generic OpenAI shape:

  - `chat_template_kwargs` (e.g. {"enable_thinking": False}) for Qwen-style
    thinking toggles, forwarded through when the ModelProfile declares
    `thinking_directive: chat_template_kwarg`. Requires llama-server to be
    launched with `--jinja` so it honours Jinja-based chat templates.
  - `chat_template` (raw Jinja string) for `thinking_directive: force_template`.
  - `grammar` (GBNF) for tool-call enforcement (optional hint).
  - `cache_prompt` for the prompt-cache optimization (default-on).
  - `repeat_penalty` (llama.cpp's native name for repetition penalty).

KV-cache quantization (`--cache-type-k q4_0 --cache-type-v q4_0`) and mmap
(`--mmap`) are server-launch flags, not request-time knobs — they live in
the backend's launch script, not in this file. `backend_hints.kv_cache_*`
in the model profile are documentation hints consumed by the launch
scripts, not forwarded in requests.
"""
from __future__ import annotations

import asyncio
import json
import typing as t
from dataclasses import dataclass

import httpx

from backends.base import (
    Backend,
    ChunkKind,
    CompletionChunk,
    HealthStatus,
    Message,
    ModelProfileLike,
    Sampling,
    ThinkingMode,
    ToolSpec,
    now_ms,
)


@dataclass
class LlamaCppBackend(Backend):
    id: str
    endpoint: str
    extra: dict
    kind: str = "llamacpp"

    def __post_init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.endpoint.rstrip("/"),
            timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
        )
        self._completions_path = self.extra.get("completions_path", "/v1/chat/completions")

    async def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        sampling: Sampling,
        thinking: ThinkingMode,
        profile: ModelProfileLike,
        backend_hints: dict | None = None,
    ) -> t.AsyncIterator[CompletionChunk]:
        body = self._build_body(messages, tools, sampling, thinking, profile, backend_hints or {})
        try:
            # Inline <think>...</think> splitter state — needed when the
            # backend (mlx_vlm.server) doesn't pre-split server-side and just
            # streams raw content containing the literal `</think>` token.
            # llama.cpp itself splits to a separate `reasoning_content` field
            # via its Jinja template; for that path the buffer just stays
            # empty and this code is a no-op.
            in_thinking = True   # Qwen3 chat template prepends `<think>\n` to
                                  # the assistant turn, so the FIRST stream is
                                  # always thinking content until we see </think>
            split_buf = ""        # carries text across deltas in case
                                  # `</think>` straddles a chunk boundary
            END_TAG = "</think>"

            async with self._client.stream("POST", self._completions_path, json=body) as resp:
                if resp.status_code != 200:
                    err = await resp.aread()
                    yield CompletionChunk(
                        kind=ChunkKind.ERROR,
                        error=f"llamacpp:{resp.status_code} {err[:400]!r}",
                    )
                    return
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = ev.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}) or {}
                    finish = choices[0].get("finish_reason")
                    # Reasoning content (llama.cpp exposes `reasoning_content`
                    # when the jinja template emits a <think> channel).
                    thinking_text = delta.get("reasoning_content") or ""
                    text = delta.get("content") or ""
                    tool_calls = delta.get("tool_calls") or []
                    if thinking_text:
                        yield CompletionChunk(kind=ChunkKind.THINKING, text=thinking_text, raw=ev)
                    if text:
                        # Inline split. While we believe the model is still
                        # thinking, route deltas to THINKING. When `</think>`
                        # appears (possibly straddling chunks), emit the
                        # thinking-side prefix as THINKING and the rest as
                        # TEXT, then flip the flag for the remainder of the
                        # stream.
                        if in_thinking:
                            split_buf += text
                            idx = split_buf.find(END_TAG)
                            if idx >= 0:
                                pre = split_buf[:idx]
                                post = split_buf[idx + len(END_TAG):]
                                if pre:
                                    yield CompletionChunk(
                                        kind=ChunkKind.THINKING, text=pre, raw=ev,
                                    )
                                in_thinking = False
                                split_buf = ""
                                # Drop a leading newline left by the model
                                # after </think> — purely cosmetic.
                                if post.startswith("\n"):
                                    post = post[1:]
                                if post:
                                    yield CompletionChunk(
                                        kind=ChunkKind.TEXT, text=post, raw=ev,
                                    )
                            elif len(split_buf) > len(END_TAG):
                                # Safe to flush everything except the last
                                # 8 chars (which might be the start of the
                                # tag) as thinking.
                                flush_to = len(split_buf) - len(END_TAG)
                                yield CompletionChunk(
                                    kind=ChunkKind.THINKING,
                                    text=split_buf[:flush_to],
                                    raw=ev,
                                )
                                split_buf = split_buf[flush_to:]
                        else:
                            yield CompletionChunk(kind=ChunkKind.TEXT, text=text, raw=ev)
                    for tc in tool_calls:
                        fn = tc.get("function", {}) or {}
                        yield CompletionChunk(
                            kind=ChunkKind.TOOL_CALL,
                            tool_call={
                                "id": tc.get("id") or "",
                                "name": fn.get("name") or "",
                                "arguments_json": fn.get("arguments") or "",
                                "index": tc.get("index", 0),
                            },
                            raw=ev,
                        )
                    if finish is not None:
                        # Flush any tail held back by the splitter (e.g. the
                        # stream ended while we still had buffered text).
                        if split_buf:
                            yield CompletionChunk(
                                kind=ChunkKind.THINKING if in_thinking else ChunkKind.TEXT,
                                text=split_buf,
                                raw=ev,
                            )
                            split_buf = ""
                        yield CompletionChunk(kind=ChunkKind.FINISH, finish_reason=finish, raw=ev)
                        return
        except httpx.RequestError as e:
            yield CompletionChunk(kind=ChunkKind.ERROR, error=f"llamacpp:transport:{e!r}")
            return
        yield CompletionChunk(kind=ChunkKind.FINISH, finish_reason="stop")

    def _build_body(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        sampling: Sampling,
        thinking: ThinkingMode,
        profile: ModelProfileLike,
        hints: dict,
    ) -> dict:
        body: dict = {
            "stream": True,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    **({"name": m.name} if m.name else {}),
                    **({"tool_call_id": m.tool_call_id} if m.tool_call_id else {}),
                    **({"tool_calls": m.tool_calls} if m.tool_calls else {}),
                }
                for m in messages
            ],
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "max_tokens": sampling.max_tokens,
        }
        if sampling.top_k:
            body["top_k"] = sampling.top_k
        if sampling.repetition_penalty and sampling.repetition_penalty != 1.0:
            body["repeat_penalty"] = sampling.repetition_penalty  # llama.cpp native name
        if sampling.stop:
            body["stop"] = list(sampling.stop)
        if sampling.seed is not None:
            body["seed"] = sampling.seed
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": spec.name, "description": spec.description, "parameters": spec.parameters},
                }
                for spec in tools
            ]
            body["tool_choice"] = hints.get("tool_choice", "auto")

        # Thinking directive — three supported shapes on llama.cpp:
        directive = profile.thinking_directive
        payload_cfg = profile.thinking_directive_payload or {}
        thinking_on = self._resolve_thinking(thinking, profile)

        if directive == "chat_template_kwarg":
            # Requires llama-server launched with --jinja.
            key = payload_cfg.get("key", "enable_thinking")
            nested = payload_cfg.get("nested_under", "chat_template_kwargs")
            body[nested] = {**body.get(nested, {}), key: thinking_on}
            # mlx_vlm.server expects the same flag at the top level of the
            # request body rather than nested under chat_template_kwargs. Set
            # it there too — llama.cpp ignores unknown top-level keys, so
            # this is additive and safe for both backends.
            body[key] = thinking_on
        elif directive == "force_template":
            tpl = payload_cfg.get("template_on" if thinking_on else "template_off")
            if tpl:
                body["chat_template"] = tpl
        # `system_prompt_marker` and `not_supported` need no request-level
        # action — marker injection is the turn-loop's job, and not_supported
        # means we ignore the thinking flag entirely.

        # Merge any static chat-template kwargs the profile always wants.
        for k, v in (profile.chat_template_quirks or {}).items():
            # Only merge dicts that look like template kwargs; scalar quirks
            # like `thinking_max_tokens_multiplier` are consumed elsewhere.
            if isinstance(v, dict) and k in ("chat_template_kwargs", "extra_body"):
                merged = {**body.get(k, {}), **v}
                body[k] = merged

        # Prompt cache: cheap win on llama.cpp; default-on unless hint says otherwise.
        body["cache_prompt"] = hints.get("cache_prompt", True)
        # Optional GBNF grammar for tool-call enforcement.
        if gbnf := hints.get("grammar"):
            body["grammar"] = gbnf
        return body

    @staticmethod
    def _resolve_thinking(thinking: ThinkingMode, profile: ModelProfileLike) -> bool:
        """AUTO defers to the profile's default; explicit ON/OFF wins."""
        if thinking == ThinkingMode.ON:
            return True
        if thinking == ThinkingMode.OFF:
            return False
        # AUTO: fall back to profile hint, default OFF for Clyde parity.
        return bool((profile.chat_template_quirks or {}).get("thinking_default_on", False))

    async def probe(self, timeout_s: float = 2.0) -> HealthStatus:
        t0 = now_ms()
        try:
            r = await self._client.get("/health", timeout=timeout_s)
            if r.status_code == 200:
                # llama-server /health returns JSON like {"status":"ok", "slots_idle":N, ...}
                try:
                    js = r.json()
                    notes = f"slots_idle={js.get('slots_idle', '?')}"
                except Exception:
                    notes = ""
                return HealthStatus(reachable=True, latency_ms=now_ms() - t0, notes=notes)
        except (httpx.RequestError, asyncio.TimeoutError) as e:
            return HealthStatus(reachable=False, latency_ms=now_ms() - t0, notes=repr(e))
        return HealthStatus(reachable=False, latency_ms=now_ms() - t0)
