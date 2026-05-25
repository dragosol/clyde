"""
agent/backends/openai.py
────────────────────────
GenericOpenAIBackend — for any OpenAI-compatible HTTP endpoint: LiteLLM,
vLLM, TGI, a second Mac over Tailscale serving mlx_lm.server, or the real
openai.com API.

No backend-specific hints are honoured (no chat_template_kwargs, no GBNF);
model quirks must be handled by ModelProfile-level work (for example by
injecting a system_prompt_marker).
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
class GenericOpenAIBackend(Backend):
    id: str
    endpoint: str
    extra: dict
    kind: str = "openai"

    def __post_init__(self) -> None:
        headers: dict[str, str] = {}
        api_key = self.extra.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.endpoint.rstrip("/"),
            timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
            headers=headers,
        )
        self._completions_path = self.extra.get("completions_path", "/v1/chat/completions")
        self._model_id = self.extra.get("model")

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
        hints = backend_hints or {}
        body = self._build_body(messages, tools, sampling, profile, hints)
        # Phase 2c.A: forward a Clyde-scoped prefix-cache key (e.g. conv_id) as
        # a request header. The forked mlx-flash server reads this and, when
        # present, hits/misses against an on-disk PrefixCacheStore — skipping
        # long-context prefill on turn 2+ of the same conversation. Agent is
        # free to set this to a stable conv_id, or None to disable.
        request_headers: dict[str, str] = {}
        cache_key = hints.get("cache_key")
        if cache_key:
            request_headers["x-clyde-cache-key"] = str(cache_key)
        try:
            async with self._client.stream(
                "POST",
                self._completions_path,
                json=body,
                headers=request_headers or None,
            ) as resp:
                if resp.status_code != 200:
                    err = await resp.aread()
                    yield CompletionChunk(
                        kind=ChunkKind.ERROR,
                        error=f"openai:{resp.status_code} {err[:400]!r}",
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
                    text = delta.get("content") or ""
                    tool_calls = delta.get("tool_calls") or []
                    if text:
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
                        yield CompletionChunk(kind=ChunkKind.FINISH, finish_reason=finish, raw=ev)
                        return
        except httpx.RequestError as e:
            yield CompletionChunk(kind=ChunkKind.ERROR, error=f"openai:transport:{e!r}")
            return
        yield CompletionChunk(kind=ChunkKind.FINISH, finish_reason="stop")

    def _build_body(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        sampling: Sampling,
        profile: ModelProfileLike,
        hints: dict,
    ) -> dict:
        body: dict = {
            "model": hints.get("model") or self._model_id or profile.id,
            "stream": True,
            "messages": [
                {**{"role": m.role, "content": m.content},
                 **({"name": m.name} if m.name else {}),
                 **({"tool_call_id": m.tool_call_id} if m.tool_call_id else {}),
                 **({"tool_calls": m.tool_calls} if m.tool_calls else {})}
                for m in messages
            ],
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "max_tokens": sampling.max_tokens,
        }
        if sampling.stop:
            body["stop"] = list(sampling.stop)
        if sampling.seed is not None:
            body["seed"] = sampling.seed
        if tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
            body["tool_choice"] = hints.get("tool_choice", "auto")
        return body

    async def probe(self, timeout_s: float = 2.0) -> HealthStatus:
        t0 = now_ms()
        try:
            r = await self._client.get("/v1/models", timeout=timeout_s)
            if r.status_code in (200, 401):  # 401 = reachable but needs auth; still counts as up
                return HealthStatus(reachable=True, latency_ms=now_ms() - t0)
        except (httpx.RequestError, asyncio.TimeoutError) as e:
            return HealthStatus(reachable=False, latency_ms=now_ms() - t0, notes=repr(e))
        return HealthStatus(reachable=False, latency_ms=now_ms() - t0)
