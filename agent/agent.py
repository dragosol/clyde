#!/usr/bin/env python3
"""
Clyde — Agent Server
============================
OpenAI-compatible API server that sits between Msty and the MLX backend.

  Msty (:8801) → agent.py → MLX server (:8800)
                    ↕
        conversation runtime + tools + memory

This is the main entry point. It:
  1. Serves an OpenAI-compatible chat completions endpoint
  2. Manages a ConversationRuntime per session
  3. Injects memory context into the system prompt
  4. Handles the tool-calling agent loop
  5. Returns the final response to Msty
"""

import asyncio
import concurrent.futures
import json
import os
import re
import signal
import queue
import sys
import threading
import time
import uuid
import logging
import yaml
from pathlib import Path

# Add agent dir to path
sys.path.insert(0, str(Path(__file__).parent))

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

from conversation import ConversationRuntime, TurnSummary
from memory import ensure_dirs
import skills as skills_mod

# ── Thinking on by default. This used to default OFF, which meant the
# model never emitted <think> blocks. Clyde's UI will collapse/pill
# these, so we want them streamed.
os.environ.setdefault("CLYDE_THINKING_MODE", "on")  # Clyde thinking UI + inspector toggle landed

# Phase-B: BackendRouter for per-request model → (backend, profile) resolution.
try:
    from routing import BackendRouter
    from model_profiles.registry import ModelProfileRegistry
    _ROUTER = BackendRouter(profiles=ModelProfileRegistry())
except Exception as _router_exc:
    import logging as _l
    _l.getLogger('agent').warning(f'BackendRouter init failed ({_router_exc!r}); using legacy config.yaml path')
    _ROUTER = None

# ProcessManager — lazy-load backends, idle reaper
try:
    from process_manager import get_process_manager as _get_pm
    _PM = _get_pm()
except Exception as _pm_exc:
    import logging as _l
    _l.getLogger('agent').warning(f'ProcessManager init failed ({_pm_exc!r}); backends stay externally managed')
    _PM = None

# ─── Config ───
CFG_PATH = Path(__file__).parent / "config.yaml"
with open(CFG_PATH) as f:
    CFG = yaml.safe_load(f)

HOST = CFG["server"]["host"]
PORT = CFG["server"]["port"]
BACKEND_URL = CFG["backend"]["url"]
BACKEND_MODEL = CFG["backend"]["model"]
MAX_ITERATIONS = CFG["agent"]["max_iterations"]
MAX_TOKENS = CFG["agent"]["max_tokens"]
TEMPERATURE = CFG["agent"]["temperature"]
COMPACT_THRESHOLD = CFG["session"]["compact_after_tokens"]
PRESERVE_RECENT = CFG["session"]["preserve_recent_messages"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("agent")

app = FastAPI(title="Clyde Agent")

# ─── Graceful Shutdown ───

_shutting_down = False

@app.post("/shutdown")
async def shutdown_endpoint():
    """Graceful shutdown — called by Clyde's stopAgent() or watchdog."""
    global _shutting_down
    if _shutting_down:
        return JSONResponse({"status": "already_shutting_down"})
    log.info("Graceful shutdown requested via /shutdown")

    # Schedule shutdown after response is sent
    async def _do_shutdown():
        await asyncio.sleep(0.5)
        _graceful_exit()

    asyncio.create_task(_do_shutdown())
    return JSONResponse({"status": "shutting_down"})


# ─── Clyde App Watchdog ───
# If the Clyde app isn't running, we have no frontend — shut down.
# Simple `pgrep -x Clyde` check every 5s. Covers SIGKILL, Xcode stop, crash.

def _start_clyde_watchdog():
    """Monitor whether Clyde (our parent process) is still alive.

    When Clyde dies (SIGKILL, Xcode stop, crash), macOS reparents us to
    launchd (PID 1).  We detect this by checking os.getppid() against the
    PID we recorded at startup.

    We avoid pgrep because the agent inherits Clyde's App Sandbox and
    cannot enumerate other processes from inside it.
    """
    original_ppid = os.getppid()
    log.info(f"Clyde watchdog: parent PID = {original_ppid}")

    # Headless / launchd case: if we were already adopted by launchd (PID 1)
    # at boot, we were not started by Clyde. Don't self-terminate — the agent
    # is being run standalone (headless bench, systemd, launchd plist, relay).
    if original_ppid == 1 or os.environ.get("CLYDE_NO_WATCHDOG") == "1":
        log.info("Clyde watchdog: skipped (headless boot — parent is launchd or flag set)")
        return

    def _watch():
        # Grace period — Clyde may still be initialising
        time.sleep(10)
        misses = 0
        while not _shutting_down:
            time.sleep(5)
            current_ppid = os.getppid()
            if current_ppid == original_ppid:
                misses = 0
            else:
                misses += 1
                log.info(f"Clyde watchdog: parent changed {original_ppid}→{current_ppid} (miss {misses}/2)")
                if misses >= 2:
                    log.warning(f"Clyde (PID {original_ppid}) is gone — self-terminating")
                    _graceful_exit()
                    return

    t = threading.Thread(target=_watch, daemon=True, name="clyde-watchdog")
    t.start()
    log.info(f"Clyde app watchdog started (parent PID {original_ppid})")


def _graceful_exit():
    """Save all sessions and SIGTERM ourselves."""
    global _shutting_down
    _shutting_down = True
    for conv_id, runtime in list(_runtimes.items()):
        try:
            runtime.session.save()
            log.info(f"  Saved session {conv_id}")
        except Exception:
            pass
    os.kill(os.getpid(), signal.SIGTERM)


def _extract_user_message(messages: list) -> str:
    """
    Extract the latest user message text, handling both string and
    multimodal content (OpenAI format: list of text/image_url parts).
    Uses file_sidecar for lazy-loaded extraction from docx, xlsx, pdf, pptx.
    """
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = []
            extracted_files = []
            image_count = 0
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:image/"):
                        # Image attachment — run through vision sidecar
                        try:
                            from file_sidecar import extract_image_from_data_url
                            user_text = " ".join(text_parts) if text_parts else "Describe this image in detail."
                            log.info(f"Running vision model on image attachment...")
                            description = extract_image_from_data_url(url, user_text)
                            if description and not description.startswith("["):
                                extracted_files.append(("image", description))
                                continue
                            else:
                                log.warning(f"Vision extraction returned: {description}")
                                image_count += 1
                        except Exception as e:
                            log.warning(f"Vision sidecar failed: {e}")
                            image_count += 1
                    elif url.startswith("data:video/"):
                        # Video attachment — run through vision sidecar
                        try:
                            from file_sidecar import extract_video
                            import base64 as b64mod
                            raw = url.split(",", 1)[1] if "," in url else url
                            video_bytes = b64mod.b64decode(raw)
                            user_text = " ".join(text_parts) if text_parts else "Describe what happens in this video."
                            log.info(f"Running vision model on video attachment...")
                            description = extract_video(video_bytes, user_text)
                            if description and not description.startswith("["):
                                extracted_files.append(("video", description))
                                continue
                        except Exception as e:
                            log.warning(f"Video sidecar failed: {e}")
                            image_count += 1
                    elif url.startswith("data:"):
                        # Document attachment — extract via sidecar
                        try:
                            from file_sidecar import extract_from_data_url
                            extracted, ftype = extract_from_data_url(url)
                            if extracted and ftype not in ("error", "unsupported"):
                                extracted_files.append((ftype, extracted))
                                continue
                        except Exception as e:
                            log.warning(f"Sidecar extraction failed: {e}")
                    image_count += 1

            msg = " ".join(text_parts)

            for ftype, content_text in extracted_files:
                if ftype == "image":
                    msg += f"\n\n--- Vision analysis of attached image ---\n{content_text}\n--- End of vision analysis ---"
                elif ftype == "video":
                    msg += f"\n\n--- Vision analysis of attached video ---\n{content_text}\n--- End of vision analysis ---"
                else:
                    msg += f"\n\n--- Attached {ftype.upper()} file content ---\n{content_text}\n--- End of {ftype.upper()} ---"

            if image_count > 0:
                msg += (
                    f"\n\n[{image_count} attachment(s) could not be processed. "
                    f"Please describe their content or try a different format.]"
                )

            # Save raw attachments to temp files so macOS tools (messages_send,
            # mail_send) can reference them by path when forwarding/attaching.
            saved_paths = []
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                url = part.get("image_url", {}).get("url", "")
                if not url.startswith("data:"):
                    continue
                try:
                    import base64 as b64mod, tempfile, mimetypes
                    from urllib.parse import unquote
                    header, raw_b64 = url.split(",", 1) if "," in url else ("", url)
                    file_bytes = b64mod.b64decode(raw_b64)
                    # Parse header: data:image/gif;name=hello-hi.gif;base64
                    header_parts = header.replace("data:", "").split(";")
                    mime = header_parts[0] if header_parts else "application/octet-stream"
                    # Extract original filename from ;name= parameter
                    original_name = None
                    for hp in header_parts:
                        if hp.startswith("name="):
                            original_name = unquote(hp[5:])
                            break
                    if original_name:
                        filename = original_name
                    else:
                        ext = mimetypes.guess_extension(mime) or ".bin"
                        if ext == ".jpe":
                            ext = ".jpg"
                        filename = f"attachment-{len(saved_paths)}{ext}"
                    # Use ~/Pictures/ClydeAttachments so Messages.app can
                    # access the file. Messages is sandboxed and CANNOT read
                    # from /tmp. ~/Pictures is in Messages' sandbox allowlist.
                    tmp_dir = Path.home() / "Pictures" / "ClydeAttachments"
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    tmp_file = tmp_dir / filename
                    tmp_file.write_bytes(file_bytes)
                    saved_paths.append(str(tmp_file))
                    log.info(f"Saved attachment to {tmp_file} ({len(file_bytes)} bytes)")
                except Exception as e:
                    log.warning(f"Failed to save attachment to disk: {e}")

            if saved_paths:
                paths_str = ", ".join(saved_paths)
                msg += (
                    f"\n\n[Attachments saved to disk for forwarding: {paths_str}. "
                    f"Use these paths with messages_send or mail_send attachment parameter.]"
                )

            return msg
    return ""

# Per-conversation runtimes keyed by conversation ID
_runtimes = {}          # conv_id -> ConversationRuntime
_current_conv_id = None # str or None
# Legacy single runtime for clients that don't send a conversation ID
_runtime = None         # ConversationRuntime or None

# Session persistence directory
SESSIONS_DIR = Path(CFG["paths"]["sessions_dir"]).expanduser()
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _make_runtime(
    conversation_id: str | None = None,
    logical_model: str | None = None,
) -> ConversationRuntime:
    """Create a new ConversationRuntime with standard config and persistence wiring.

    Phase-B: if a BackendRouter is available and `logical_model` is given (or a
    default route is configured), resolve the route and feed its (endpoint,
    backend_model_id, profile) into the runtime. Otherwise fall back to the
    legacy config.yaml BACKEND_URL / BACKEND_MODEL values, so iter23-path
    deployments still boot unchanged.
    """
    url = BACKEND_URL
    model = BACKEND_MODEL
    profile = None
    if _ROUTER is not None:
        try:
            route = _ROUTER.resolve(logical_model)
            url = route.backend.endpoint
            model = route.backend_model_id
            profile = route.profile
            log.info(
                f"Router resolved route={route.logical_name} -> "
                f"backend={route.backend.id} ({url}) model={model} "
                f"profile={profile.id if profile else '<none>'}"
            )
        except Exception as exc:
            log.warning(f"router.resolve({logical_model!r}) failed ({exc!r}); "
                        f"falling back to config.yaml backend")
    rt = ConversationRuntime(
        backend_url=url,
        backend_model=model,
        max_iterations=MAX_ITERATIONS,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        profile=profile,
    )
    rt._compact_threshold = COMPACT_THRESHOLD
    rt._preserve_recent = PRESERVE_RECENT
    rt._session_dir = SESSIONS_DIR
    rt._conversation_id = conversation_id
    return rt


def get_runtime(conversation_id=None, logical_model: str | None = None):
    global _runtime, _current_conv_id, _runtimes

    if conversation_id:
        # If conversation ID changed, switch to (or create) the right runtime
        if conversation_id not in _runtimes:
            # Reset folder permissions for this new session
            from tools import reset_session_permissions
            reset_session_permissions()

            rt = _make_runtime(conversation_id, logical_model=logical_model)

            # Try to restore session from disk (survives agent restarts)
            if rt.load_session():
                log.info(f"Resumed conversation {conversation_id[:8]}... from disk")
            else:
                log.info(f"Created new runtime for conversation {conversation_id[:8]}...")

            _runtimes[conversation_id] = rt

            # Prune old runtimes from memory (keep last 10)
            # Disk sessions are NOT deleted — they can be resumed later
            if len(_runtimes) > 10:
                oldest_key = next(iter(_runtimes))
                _runtimes[oldest_key].save_session()
                del _runtimes[oldest_key]
                log.info(f"Evicted runtime {oldest_key[:8]}... from memory (session on disk)")

        # Reset permissions whenever switching conversations (per-session)
        if _current_conv_id != conversation_id:
            from tools import reset_session_permissions
            reset_session_permissions()
            log.info(f"Switched to conversation {conversation_id[:8]}..., permissions reset")

        # Tell tools which conversation is active so graph nodes are
        # attributed correctly, and make sure a skill state exists.
        try:
            from tools import set_current_conversation_id
            set_current_conversation_id(conversation_id)
        except Exception as _sce:
            log.warning(f"set_current_conversation_id failed: {_sce}")
        skills_mod.get_or_init_state(conversation_id)

        _current_conv_id = conversation_id
        rt = _runtimes[conversation_id]

        # Sync backend URL: if the default route or explicit logical_model
        # changed, update the runtime so it talks to the right server.
        if _ROUTER is not None:
            try:
                route = _ROUTER.resolve(logical_model)
                new_url = route.backend.endpoint
                if new_url != rt.backend_url:
                    log.info(f"Backend switch in conversation {conversation_id[:8]}...: "
                             f"{rt.backend_url} → {new_url} (route={route.logical_name})")
                    rt.backend_url = new_url
                    rt.backend_model = route.backend_model_id
                    if route.profile:
                        rt.profile = route.profile
            except Exception:
                pass

        return rt

    # Legacy fallback: no conversation ID
    if _runtime is None:
        _runtime = _make_runtime(logical_model=logical_model)
    return _runtime


# ─── Health / Models (Msty discovery) ───

@app.get("/v1/models")
@app.get("/models")
async def list_models():
    """Enumerate all configured routes as OpenAI-shaped model records.

    Phase B: every BackendRouter route is exposed as a model id, so Clyde's
    picker and Msty's model-list both see every route, not just the default.
    """
    created = int(time.time())
    if _ROUTER is not None:
        try:
            ids = [r.logical_name for r in _ROUTER.all_routes()] or [BACKEND_MODEL]
        except Exception:
            ids = [BACKEND_MODEL]
    else:
        ids = [BACKEND_MODEL]
    return JSONResponse({
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": created,
              "owned_by": "local", "root": name} for name in ids
        ]
    })

@app.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    return {"id": "clyde", "object": "model", "created": int(time.time()), "owned_by": "local"}

@app.get("/health")
async def health():
    return {"status": "ok"}


# ─── Ollama-Compatible Endpoints ───
# These allow Msty's "Local AI" provider to talk to Clyde
# using the Ollama protocol (same as Ollama's REST API).

@app.get("/api/tags")
async def ollama_tags():
    """List available models (Ollama format)."""
    return {
        "models": [{
            "name": "clyde:latest",
            "model": "clyde:latest",
            "modified_at": "2026-04-01T00:00:00Z",
            "size": 0,
            "digest": "clyde",
            "details": {
                "parent_model": "",
                "format": "gguf",
                "family": "qwen3",
                "families": ["qwen3"],
                "parameter_size": "35B",
                "quantization_level": "Q4_K_M"
            }
        }]
    }


@app.get("/api/version")
async def ollama_version():
    return {"version": "0.6.2"}


@app.get("/")
async def root():
    return "Ollama is running"


@app.head("/")
async def root_head():
    return JSONResponse(content="", status_code=200)


@app.post("/api/show")
async def ollama_show(request: Request):
    """Return model info (Ollama format)."""
    return {
        "modelfile": "",
        "parameters": "",
        "template": "",
        "details": {
            "parent_model": "",
            "format": "gguf",
            "family": "qwen3",
            "families": ["qwen3"],
            "parameter_size": "35B",
            "quantization_level": "Q4_K_M"
        },
        "model_info": {
            "general.architecture": "qwen3moe",
            "general.parameter_count": 35000000000,
        }
    }


@app.post("/api/chat")
async def ollama_chat(request: Request):
    """
    Ollama-compatible /api/chat endpoint.
    Msty sends requests in Ollama format; we translate, run the agent loop,
    and return responses in Ollama format.
    """
    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", True)  # Ollama defaults to streaming

    # Extract latest user message (handles multimodal content from attachments)
    user_msg = _extract_user_message(messages)

    if not user_msg:
        if stream:
            return StreamingResponse(
                _ollama_stream_response("No user message found."),
                media_type="application/x-ndjson"
            )
        return JSONResponse(_ollama_response("No user message found."))

    # Handle commands
    if user_msg.strip().lower() in ("/reset", "/clear", "/new"):
        global _runtime
        _runtime = None
        text = "Session reset. Starting fresh."
        if stream:
            return StreamingResponse(
                _ollama_stream_response(text),
                media_type="application/x-ndjson"
            )
        return JSONResponse(_ollama_response(text))

    # Run agent loop
    runtime = get_runtime()
    try:
        runtime.compact_if_needed(
            preserve_recent=PRESERVE_RECENT,
            token_threshold=COMPACT_THRESHOLD
        )
    except Exception as e:
        log.error(f"Compaction failed (Ollama endpoint): {e}")

    if stream:
        event_queue = queue.Queue()
        def on_event(event_type, **kwargs):
            event_queue.put((event_type, kwargs))
        return StreamingResponse(
            _ollama_live_stream(runtime, user_msg, event_queue, on_event),
            media_type="application/x-ndjson"
        )

    loop = asyncio.get_event_loop()
    summary = await loop.run_in_executor(None, runtime.run_turn, user_msg)
    return JSONResponse(_ollama_response(summary.assistant_text))


def _ollama_response(content: str) -> dict:
    """Non-streaming Ollama response."""
    return {
        "model": "clyde:latest",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "total_duration": 0,
        "load_duration": 0,
        "prompt_eval_count": 0,
        "prompt_eval_duration": 0,
        "eval_count": 0,
        "eval_duration": 0
    }


async def _ollama_stream_response(content: str):
    """Stream response as Ollama NDJSON chunks."""
    chunk_size = 20
    for i in range(0, len(content), chunk_size):
        chunk = content[i:i + chunk_size]
        data = {
            "model": "clyde:latest",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "message": {"role": "assistant", "content": chunk},
            "done": False
        }
        yield json.dumps(data) + "\n"
        await asyncio.sleep(0.02)

    # Final done message
    data = {
        "model": "clyde:latest",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": "stop",
        "total_duration": 0,
        "load_duration": 0,
        "prompt_eval_count": 0,
        "prompt_eval_duration": 0,
        "eval_count": 0,
        "eval_duration": 0
    }
    yield json.dumps(data) + "\n"


def _ollama_chunk(content: str) -> str:
    return json.dumps({
        "model": "clyde:latest",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message": {"role": "assistant", "content": content},
        "done": False
    }) + "\n"


def _ollama_done() -> str:
    return json.dumps({
        "model": "clyde:latest",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": "stop",
        "total_duration": 0,
        "load_duration": 0,
        "prompt_eval_count": 0,
        "prompt_eval_duration": 0,
        "eval_count": 0,
        "eval_duration": 0
    }) + "\n"


async def _ollama_live_stream(runtime, user_msg: str, event_queue: queue.Queue, on_event):
    """Live streaming for Ollama protocol with status events."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.run_turn, user_msg, on_event)
        status_sent = False

        while not future.done():
            try:
                event_type, data = event_queue.get_nowait()
                if event_type == "thinking":
                    think_text = data.get("content", "")
                    if think_text:
                        yield _ollama_chunk("*thinking...*\n\n")
                        yield _ollama_chunk(f"<think>{think_text}</think>\n\n")
                    else:
                        yield _ollama_chunk("*thinking...*\n\n")
                    status_sent = True
                elif event_type == "narration":
                    narration = data.get("text", "")
                    if narration:
                        yield _ollama_chunk(narration + "\n\n")
                        status_sent = True
                elif event_type == "tool_start":
                    name = data.get("name", "tool")
                    preview = data.get("args_preview", "")
                    tag = f"*using {name}*"
                    if preview:
                        tag += f" `{preview.replace(chr(10), ' ; ')[:300]}`"
                    yield _ollama_chunk(tag + "\n")
                    status_sent = True
                elif event_type == "tool_done":
                    name = data.get("name", "tool")
                    status = "error" if data.get("is_error") else "done"
                    summary = data.get("summary", "")
                    # Emit full tool output as base64 block for Clyde UI
                    output = data.get("output", "")
                    if output:
                        import base64 as _b64
                        _enc = _b64.b64encode(output.encode("utf-8", errors="replace")).decode("ascii")
                        yield _ollama_chunk(f"<<tool_output:{_enc}>>\n")
                    if summary:
                        tag = f"*{name} {status} · {summary}*"
                    else:
                        tag = f"*{name} {status}*"
                    yield _ollama_chunk(tag + "\n\n")
                elif event_type == "question":
                    q_data = json.dumps({
                        "id": data.get("id", ""),
                        "question": data.get("question", ""),
                        "choices": data.get("choices", []),
                        "allowOther": data.get("allow_other", True),
                    })
                    yield _ollama_chunk(f"<<question:{q_data}>>\n")
                    status_sent = True
                elif event_type == "permission_request":
                    p_data = json.dumps({
                        "id": data.get("id", ""),
                        "path": data.get("path", ""),
                        "folder": data.get("folder", ""),
                    })
                    yield _ollama_chunk(f"<<permission_request:{p_data}>>\n")
                    status_sent = True
                elif event_type == "compacting":
                    est = data.get("estimated_tokens", 0)
                    thresh = data.get("threshold", 0)
                    yield _ollama_chunk(f"*compacting · {est // 1000}K/{thresh // 1000}K tokens*\n")
                    status_sent = True
                elif event_type == "compact_done":
                    before = data.get("tokens_before", 0)
                    after = data.get("tokens_after", 0)
                    err = data.get("error", "")
                    if err:
                        yield _ollama_chunk(f"*compact_done · error*\n\n")
                    else:
                        yield _ollama_chunk(f"*compact_done · {before // 1000}K→{after // 1000}K tokens*\n\n")
                elif event_type == "recovering":
                    stage = data.get("stage", "")
                    detail = data.get("detail", "")
                    yield _ollama_chunk(f"*recovering · {stage} · {detail}*\n")
                elif event_type == "recovery_done":
                    detail = data.get("detail", "")
                    yield _ollama_chunk(f"*recovery_done · {detail}*\n\n")
                elif event_type == "generating":
                    pass  # iteration count suppressed from stream
                elif event_type == "metrics":
                    # Live tok/s + phase + tokens — Clyde renders under the
                    # streaming cursor as a small metrics chip.
                    m_data = json.dumps({
                        "phase": data.get("phase", ""),
                        "tokens": data.get("tokens", 0),
                        "tps": data.get("tps", 0.0),
                        "elapsed": data.get("elapsed", 0.0),
                        "thinking_chars": data.get("thinking_chars", 0),
                        "content_chars": data.get("content_chars", 0),
                    })
                    # SEPARATE SSE event with `clyde_metrics` — never via
                    # delta.content. Avoids leaking into thinking/content
                    # streams that Clyde routes by tag boundary.
                    _m_chunk = {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "clyde",
                        "choices": [{"index": 0, "delta": {"clyde_metrics": json.loads(m_data)}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(_m_chunk)}\n\n"
                elif event_type == "context":
                    # Conversation context pressure — tokens / threshold.
                    # Dedicated SSE channel (`clyde_context`) to keep it
                    # out of content/thinking streams. Also re-emit on
                    # compaction events so the graph shows the drop.
                    _ctx_payload = {
                        "tokens": data.get("tokens", 0),
                        "threshold": data.get("threshold", 0),
                        "messages": data.get("messages", 0),
                    }
                    _c_chunk = {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "clyde",
                        "choices": [{"index": 0, "delta": {"clyde_context": _ctx_payload}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(_c_chunk)}\n\n"
            except queue.Empty:
                await asyncio.sleep(0.05)

        # Drain remaining events — process them
        while not event_queue.empty():
            try:
                event_type, data = event_queue.get_nowait()
                if event_type == "narration":
                    narration = data.get("text", "")
                    if narration:
                        yield _ollama_chunk(narration + "\n\n")
                        status_sent = True
                elif event_type == "tool_start":
                    name = data.get("name", "tool")
                    preview = data.get("args_preview", "")
                    tag = f"*using {name}*"
                    if preview:
                        tag += f" `{preview.replace(chr(10), ' ; ')[:300]}`"
                    yield _ollama_chunk(tag + "\n")
                    status_sent = True
                elif event_type == "tool_done":
                    name = data.get("name", "tool")
                    status = "error" if data.get("is_error") else "done"
                    summary = data.get("summary", "")
                    # Emit full tool output as base64 block for Clyde UI
                    output = data.get("output", "")
                    if output:
                        import base64 as _b64
                        _enc = _b64.b64encode(output.encode("utf-8", errors="replace")).decode("ascii")
                        yield _ollama_chunk(f"<<tool_output:{_enc}>>\n")
                    if summary:
                        tag = f"*{name} {status} · {summary}*"
                    else:
                        tag = f"*{name} {status}*"
                    yield _ollama_chunk(tag + "\n\n")
                elif event_type == "question":
                    q_data = json.dumps({
                        "id": data.get("id", ""),
                        "question": data.get("question", ""),
                        "choices": data.get("choices", []),
                        "allowOther": data.get("allow_other", True),
                    })
                    yield _ollama_chunk(f"<<question:{q_data}>>\n")
                    status_sent = True
            except queue.Empty:
                break

        try:
            summary = future.result()
        except Exception as e:
            log.error(f"Agent turn raised exception: {e}")
            yield _ollama_chunk(f"\n\nError: {e}")
            yield _ollama_done()
            return

        if status_sent:
            yield _ollama_chunk("\n---\n\n")

        text = summary.assistant_text or ""
        # Guard: never send a completely empty final response to the client
        if not text.strip() and status_sent:
            text = "Done."
        elif not text.strip():
            text = "I wasn't able to generate a response. Please try again."
            log.warning("Agent returned empty assistant_text with no tool activity")

        words = text.split(" ")
        buf = ""
        for word in words:
            buf += word + " "
            if len(buf) >= 15:
                yield _ollama_chunk(buf)
                buf = ""
                await asyncio.sleep(0.01)
        if buf:
            yield _ollama_chunk(buf)

        yield _ollama_done()


# ─── Answer Endpoint (for ask_user questions) ───

@app.post("/v1/answer")
async def submit_answer(request: Request):
    """
    Receive a user's answer to an ask_user question.
    Clyde sends: {"conversation_id": "...", "question_id": "...", "answer": "..."}
    This unblocks the agent's run_turn thread that's waiting on the answer.
    """
    body = await request.json()
    conv_id = body.get("conversation_id")
    question_id = body.get("question_id")
    answer = body.get("answer", "")

    if not question_id:
        return JSONResponse({"error": "question_id required"}, status_code=400)

    # Try _runtimes dict first, then fall back to singleton _runtime (Ollama mode)
    runtime = None
    if conv_id:
        runtime = _runtimes.get(conv_id)
    if not runtime and _runtime and _runtime._pending_question_id == question_id:
        runtime = _runtime
        log.info(f"Answer: falling back to singleton _runtime for question {question_id}")
    if not runtime:
        return JSONResponse({"error": f"No active runtime for question {question_id}"}, status_code=404)

    if runtime._pending_question_id != question_id:
        return JSONResponse({"error": f"No pending question {question_id}"}, status_code=404)

    runtime.submit_answer(question_id, answer)
    log.info(f"Answer submitted for {conv_id[:8]}... question {question_id}: {answer[:80]}")
    return JSONResponse({"status": "ok"})


# ─── Folder Permission Endpoint ───

@app.post("/v1/grant_folder")
async def grant_folder(request: Request):
    """
    Receive the user's response to a folder permission request.
    Clyde sends: {"conversation_id": "...", "permission_id": "...", "granted": true, "path": "/Users/.../Desktop"}
    This unblocks the tool execution thread that's waiting on permission.
    """
    body = await request.json()
    conv_id = body.get("conversation_id")
    perm_id = body.get("permission_id")
    granted = body.get("granted", False)
    path = body.get("path", "")

    if not conv_id or not perm_id:
        return JSONResponse({"error": "conversation_id and permission_id required"}, status_code=400)

    runtime = _runtimes.get(conv_id)
    if not runtime:
        # Try legacy runtime
        if _runtime and _runtime._pending_permission_id == perm_id:
            runtime = _runtime
        else:
            return JSONResponse({"error": f"No active runtime for conversation {conv_id}"}, status_code=404)

    runtime.submit_permission(perm_id, granted, path)
    log.info(f"Permission {'granted' if granted else 'denied'} for {conv_id[:8]}... path={path}")
    return JSONResponse({"status": "ok"})


@app.get("/v1/allowed_folders")
async def list_allowed_folders():
    """Return the list of currently allowed directories."""
    from tools import get_allowed_dirs
    return JSONResponse({"folders": get_allowed_dirs()})


# ─── Skills (Clyde Inspector Skills tile) ──────────────────────────────

@app.get("/v1/skills")
async def list_skills():
    """List every built-in skill with metadata for the Clyde picker."""
    return JSONResponse({
        "skills": [skills_mod.serialize_skill(s) for s in skills_mod.all_skills()],
        "default": skills_mod.DEFAULT_SKILL_NAME,
    })


@app.get("/v1/skills/active/{conversation_id}")
async def get_active_skill_for_conv(conversation_id: str):
    """Return the currently active skill + auto-route state for a conversation."""
    st = skills_mod.get_or_init_state(conversation_id)
    skill = skills_mod.get_skill(st.skill_name)
    return JSONResponse({
        **skills_mod.serialize_state(st),
        "skill": skills_mod.serialize_skill(skill),
    })


@app.post("/v1/skills/activate")
async def activate_skill(req: Request):
    """Set the active skill for a conversation. Body:
      { conversation_id: str,
        skill_name: str | null,         # null = deactivate (General + auto off)
        disable_auto: bool (optional)   # explicit auto-route toggle
      }
    """
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    conv_id = body.get("conversation_id")
    if not conv_id:
        return JSONResponse({"error": "conversation_id required"}, status_code=400)
    skill_name = body.get("skill_name")
    disable_auto = body.get("disable_auto")
    st = skills_mod.set_active_skill(
        conv_id, skill_name, user_initiated=True, disable_auto=disable_auto,
    )
    skill = skills_mod.get_skill(st.skill_name)
    return JSONResponse({
        **skills_mod.serialize_state(st),
        "skill": skills_mod.serialize_skill(skill),
    })


@app.post("/v1/skills/auto_route")
async def toggle_auto_route(req: Request):
    """Toggle auto-routing for a conversation without changing the
    currently-active skill. Body: { conversation_id, enabled: bool }."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    conv_id = body.get("conversation_id")
    enabled = bool(body.get("enabled", True))
    if not conv_id:
        return JSONResponse({"error": "conversation_id required"}, status_code=400)
    st = skills_mod.set_auto_route(conv_id, enabled)
    skill = skills_mod.get_skill(st.skill_name)
    return JSONResponse({
        **skills_mod.serialize_state(st),
        "skill": skills_mod.serialize_skill(skill),
    })


# ─── Asset Graph (Clyde Inspector Graph tile) ──────────────────────────

@app.get("/v1/graph/{conversation_id}")
async def get_graph(conversation_id: str):
    """Return the per-conversation asset graph. Shape matches Clyde's
    GraphData: {nodes, edges, version}."""
    try:
        from tools import get_graph_data
        return JSONResponse(get_graph_data(conversation_id))
    except Exception as e:
        log.warning(f"graph endpoint failed: {e}")
        return JSONResponse({"nodes": [], "edges": [], "version": 0})


@app.put("/v1/graph/{conversation_id}")
async def put_graph(conversation_id: str, request: Request):
    """Accept a graph push from Clyde (re-seeding after agent restart).
    Clyde calls this when it detects the agent returned an empty graph
    but Clyde has locally-persisted data."""
    try:
        from tools import put_graph_data
        body = await request.json()
        result = put_graph_data(conversation_id, body)
        return JSONResponse(result)
    except Exception as e:
        log.warning(f"graph PUT failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─── Routes (Clyde Settings pane) ───

@app.get("/v1/routes")
async def list_routes():
    """Enumerate all configured routes with per-backend health.

    Shape consumed by Clyde's ModelsSettingsPane. When _ROUTER is None we
    synthesize a single legacy route so old clients still see something.
    """
    if _ROUTER is None:
        return JSONResponse({
            "default_route": BACKEND_MODEL,
            "routes": [{
                "name": BACKEND_MODEL,
                "backend": "legacy-config",
                "model": BACKEND_MODEL,
                "profile": None,
                "healthy": True,
                "latency_ms": None,
                "notes": "router not available; using config.yaml backend",
                "context_window": None,
                "thinking_directive": None,
            }]
        })
    try:
        probe = await _ROUTER.probe_all(timeout_s=1.5)
    except Exception as exc:
        probe = {}
        log.warning(f"probe_all failed: {exc!r}")
    out = []
    for r in _ROUTER.all_routes():
        backend_probe = probe.get(r.backend.id) or {}
        # Check if this route's backend is currently loaded
        loaded = False
        if _PM and _PM.is_managed(r.backend_id):
            mb = _PM._managed.get(r.backend_id)
            loaded = mb is not None and mb.pid is not None and _PM._is_process_alive(mb.pid)
        sampling = getattr(r.profile, "sampling_defaults", {}) or {}
        out.append({
            "name": r.logical_name,
            "backend": r.backend_id,
            "backend_type": getattr(r.backend, "kind", "unknown"),
            "model": r.backend_model_id,
            "model_short": Path(r.backend_model_id).name if "/" in r.backend_model_id else r.backend_model_id,
            "profile": getattr(r.profile, "id", None),
            "family": getattr(r.profile, "family", None),
            "healthy": bool(backend_probe.get("reachable", False)),
            "loaded": loaded,
            "latency_ms": backend_probe.get("latency_ms"),
            "notes": backend_probe.get("notes") or None,
            "context_window": getattr(r.profile, "context_window", None),
            "thinking_directive": str(getattr(r.profile, "thinking_directive", None) or ""),
            "thinking_mode_default": str(getattr(r.profile, "thinking_mode_default", "off")),
            "sampling_defaults": sampling,
            "managed": _PM.is_managed(r.backend_id) if _PM else False,
        })
    return JSONResponse({
        "default_route": _ROUTER._default_logical_name,
        "routes": out,
    })


@app.post("/v1/default_route")
async def set_default_route(request: Request):
    """Persist a new default route to ~/.clyde/backends.yaml.

    Body: {"route": "<logical-name>"}. Updates the yaml's top-level
    `default:` field in place and refreshes the router's cached value so
    the new default takes effect for the current process. Returns the
    previous + new default names.
    """
    if _ROUTER is None:
        return JSONResponse({"error": "router not available"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    new_default = body.get("route")
    if not new_default or new_default not in _ROUTER._routes:
        return JSONResponse(
            {"error": f"unknown route {new_default!r}"},
            status_code=400,
        )
    previous = _ROUTER._default_logical_name
    # Rewrite backends.yaml's `default:` line. Keep everything else byte-
    # identical — we only touch the one line the user cares about.
    import re as _re
    cfg_path = _ROUTER.config_path
    try:
        text = cfg_path.read_text()
        new_text, n = _re.subn(
            r"^default:\s*.*$",
            f"default: {new_default}",
            text,
            count=1,
            flags=_re.MULTILINE,
        )
        if n == 0:
            # No existing default: line — prepend.
            new_text = f"default: {new_default}\n" + text
        cfg_path.write_text(new_text)
    except Exception as exc:
        return JSONResponse(
            {"error": f"could not write yaml: {exc!r}"},
            status_code=500,
        )
    _ROUTER._default_logical_name = new_default
    log.info(f"[routes] default set to {new_default!r} (was {previous!r})")
    await _kill_local_backends_if_external(new_default)
    return JSONResponse({
        "previous_default": previous,
        "default_route": new_default,
        "path": str(cfg_path),
    })


async def _kill_local_backends_if_external(route_name: str | None):
    """Kill all managed (local) backends when the active route is external."""
    if _PM is None or _ROUTER is None or not route_name:
        return
    route = _ROUTER._routes.get(route_name)
    if route and _PM.is_managed(route.backend_id):
        return
    for bid in list(_PM._managed.keys()):
        mb = _PM._managed[bid]
        if mb.pid and _PM._is_process_alive(mb.pid):
            log.info(f"[exclusive] killing local backend {bid} (pid {mb.pid}) — external route active")
            _PM._kill_process(mb)


@app.post("/v1/custom_endpoint")
async def set_custom_endpoint(request: Request):
    """Set a custom OpenAI-compatible endpoint as the active backend.

    Body: {"endpoint": "http://192.168.1.100:1234"}
    Creates/updates a 'custom' route pointing at the given URL and
    makes it the default. No YAML editing needed — paste a URL, go.
    Send {"endpoint": ""} or {"endpoint": null} to clear and revert
    to the previous default.
    """
    if _ROUTER is None:
        return JSONResponse({"error": "router not available"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    endpoint = (body.get("endpoint") or "").strip().rstrip("/")

    if not endpoint:
        if "custom" in _ROUTER._routes:
            del _ROUTER._routes["custom"]
            del _ROUTER._backends["custom"]
        fallback = next((k for k in _ROUTER._routes if k != "custom"), None)
        if fallback:
            _ROUTER._default_logical_name = fallback
        log.info("[routes] custom endpoint cleared, reverted to %s", fallback)
        return JSONResponse({"status": "cleared", "default_route": fallback})

    from backends import make_backend
    from routing import Route
    try:
        backend = make_backend({
            "id": "custom",
            "type": "llamacpp",
            "endpoint": endpoint,
        })
        _ROUTER._backends["custom"] = backend
        profile = _ROUTER.profiles.resolve("qwen3_5-moe")
        _ROUTER._routes["custom"] = Route(
            logical_name="custom",
            backend=backend,
            backend_id="custom",
            backend_model_id=body.get("model", "default"),
            profile=profile,
        )
        _ROUTER._default_logical_name = "custom"
        log.info("[routes] custom endpoint set: %s", endpoint)
        await _kill_local_backends_if_external("custom")
        return JSONResponse({
            "status": "active",
            "endpoint": endpoint,
            "default_route": "custom",
        })
    except Exception as exc:
        return JSONResponse(
            {"error": f"failed to create backend: {exc!r}"},
            status_code=500,
        )


# ─── Chat Completions ───

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    # Read conversation ID from header (Clyde sends this)
    conv_id = request.headers.get("x-conversation-id")

    # Extract the latest user message (handles multimodal content from attachments)
    user_msg = _extract_user_message(messages)

    if not user_msg:
        return JSONResponse(_chat_response("No user message found."))

    # Handle /reset command
    if user_msg.strip().lower() in ("/reset", "/clear", "/new"):
        global _runtime, _runtimes
        if conv_id and conv_id in _runtimes:
            del _runtimes[conv_id]
        else:
            _runtime = None
        # Also delete the persisted session file so it doesn't reload
        if conv_id:
            session_file = SESSIONS_DIR / f"{conv_id}.json"
            if session_file.exists():
                session_file.unlink()
                log.info(f"Deleted session file for {conv_id[:8]}...")
        return JSONResponse(_chat_response("Session reset. Starting fresh."))

    # Run the agent loop
    runtime = get_runtime(conv_id, logical_model=body.get("model"))

    # Lazy-load: ensure the backend server is running before the turn.
    # Also marks the backend as active so the reaper won't kill it.
    if _ROUTER is not None and _PM is not None:
        try:
            route = _ROUTER.resolve(body.get("model"))
            if _PM.is_managed(route.backend_id):
                ok = await _PM.ensure_running(route.backend_id, model=route.backend_model_id)
                if not ok:
                    log.error("lazy-load failed for backend %s", route.backend_id)
                _PM.mark_request_start(route.backend_id)
                # Store backend_id on the runtime so we can mark_request_end
                # after the turn completes (including crash recovery).
                runtime._active_backend_id = route.backend_id
        except Exception as e:
            log.warning("lazy-load check failed: %r", e)

    # Compact if needed before the turn
    est_tokens = runtime.session.estimate_tokens()
    log.info(f"[CONTEXT] Before turn: ~{est_tokens} tokens, {len(runtime.session.messages)} messages (threshold: {COMPACT_THRESHOLD})")
    needs_compact = est_tokens >= COMPACT_THRESHOLD and len(runtime.session.messages) > PRESERVE_RECENT + 1
    try:
        runtime.compact_if_needed(
            preserve_recent=PRESERVE_RECENT,
            token_threshold=COMPACT_THRESHOLD
        )
    except Exception as e:
        log.error(f"Compaction failed (SSE endpoint): {e}")
    _pre_turn_compacted = needs_compact  # Flag for streaming to emit event

    if stream:
        # Live streaming: run agent loop in thread, stream status + final text via SSE
        event_queue = queue.Queue()

        def on_event(event_type, **kwargs):
            event_queue.put((event_type, kwargs))

        # If pre-turn compaction happened, inject the event so Clyde sees it
        if _pre_turn_compacted:
            after_tokens = runtime.session.estimate_tokens()
            event_queue.put(("compacting", {"estimated_tokens": est_tokens, "threshold": COMPACT_THRESHOLD}))
            event_queue.put(("compact_done", {"tokens_before": est_tokens, "tokens_after": after_tokens}))

        return StreamingResponse(
            _live_stream(runtime, user_msg, event_queue, on_event),
            media_type="text/event-stream"
        )
    else:
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(None, runtime.run_turn, user_msg)
        text = summary.assistant_text or ""
        if not text.strip():
            text = "Done." if summary.tool_calls_made > 0 else "I wasn't able to generate a response. Please try again."
            log.warning(f"Non-streaming: empty assistant_text (tools={summary.tool_calls_made})")
        return JSONResponse(_chat_response(text))


# ─── Response Helpers ───

def _chat_response(content: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "clyde",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    }


def _sse_chunk(content: str) -> str:
    """Format a single SSE content chunk."""
    data = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "clyde",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
    }
    return f"data: {json.dumps(data)}\n\n"


def _sse_done() -> str:
    data = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "clyde",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    }
    return f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n"


async def _live_stream(runtime, user_msg: str, event_queue: queue.Queue, on_event):
    """
    Run agent loop in a thread, streaming status events and final text via SSE.
    Status events appear as italic text blocks before the answer.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.run_turn, user_msg, on_event)

        status_sent = False
        thinking_stream_started = False  # Track if real-time thinking is open
        thinking_already_streamed = False  # Track if thinking was fully streamed (prevents duplicate)

        # Poll for events while agent loop runs
        while not future.done():
            try:
                event_type, data = event_queue.get_nowait()

                if event_type == "thinking_delta":
                    # Real-time thinking token from _call_backend streaming
                    delta = data.get("delta", "")
                    if delta:
                        if not thinking_stream_started:
                            yield _sse_chunk("<think>")
                            thinking_stream_started = True
                        yield _sse_chunk(delta)
                elif event_type == "thinking":
                    think_text = data.get("content", "")
                    if think_text:
                        if thinking_stream_started:
                            # Already streamed thinking in real-time, just close the tag
                            yield _sse_chunk("</think>\n\n")
                            thinking_already_streamed = True
                        else:
                            # Batch mode: emit the whole block at once
                            yield _sse_chunk("*thinking...*\n\n")
                            yield _sse_chunk("<think>")
                            words = think_text.split(" ")
                            buf = ""
                            for word in words:
                                buf += word + " "
                                if len(buf) >= 20:
                                    yield _sse_chunk(buf)
                                    buf = ""
                                    await asyncio.sleep(0.01)
                            if buf:
                                yield _sse_chunk(buf)
                            yield _sse_chunk("</think>\n\n")
                            thinking_already_streamed = True
                    else:
                        if thinking_stream_started:
                            yield _sse_chunk("</think>\n\n")
                            thinking_already_streamed = True
                        else:
                            yield _sse_chunk("*thinking...*\n\n")
                    thinking_stream_started = False  # Reset for next turn
                    status_sent = True
                elif event_type == "narration":
                    # Natural language narration before a tool call
                    narration = data.get("text", "")
                    if narration:
                        yield _sse_chunk(narration + "\n\n")
                        status_sent = True
                elif event_type == "tool_start":
                    name = data.get("name", "tool")
                    preview = data.get("args_preview", "")
                    tag = f"*using {name}*"
                    if preview:
                        tag += f" `{preview.replace(chr(10), ' ; ')[:300]}`"
                    yield _sse_chunk(tag + "\n")
                    status_sent = True
                elif event_type == "tool_done":
                    name = data.get("name", "tool")
                    status = "error" if data.get("is_error") else "done"
                    summary = data.get("summary", "")
                    output = data.get("output", "")
                    if output:
                        import base64 as _b64
                        _enc = _b64.b64encode(output.encode("utf-8", errors="replace")).decode("ascii")
                        yield _sse_chunk(f"<<tool_output:{_enc}>>\n")
                    if summary:
                        tag = f"*{name} {status} · {summary}*"
                    else:
                        tag = f"*{name} {status}*"
                    yield _sse_chunk(tag + "\n\n")
                elif event_type == "question":
                    # Emit question marker for Clyde to parse and render
                    q_data = json.dumps({
                        "id": data.get("id", ""),
                        "question": data.get("question", ""),
                        "choices": data.get("choices", []),
                        "allowOther": data.get("allow_other", True),
                    })
                    yield _sse_chunk(f"<<question:{q_data}>>\n")
                    status_sent = True
                elif event_type == "permission_request":
                    p_data = json.dumps({
                        "id": data.get("id", ""),
                        "path": data.get("path", ""),
                        "folder": data.get("folder", ""),
                    })
                    yield _sse_chunk(f"<<permission_request:{p_data}>>\n")
                    status_sent = True
                elif event_type == "compacting":
                    est = data.get("estimated_tokens", 0)
                    thresh = data.get("threshold", 0)
                    yield _sse_chunk(f"*compacting · {est // 1000}K/{thresh // 1000}K tokens*\n")
                    status_sent = True
                elif event_type == "compact_progress":
                    phase = data.get("phase", "")
                    pct = data.get("progress", 0)
                    detail = data.get("detail", "")
                    yield _sse_chunk(f"*compact_progress · {phase} · {pct}% · {detail}*\n")
                elif event_type == "compact_done":
                    before = data.get("tokens_before", 0)
                    after = data.get("tokens_after", 0)
                    err = data.get("error", "")
                    # Also push the post-compact token count through the
                    # dedicated context channel so the Performance tile's
                    # "Context Pressure" graph shows the drop.
                    if not err and after > 0:
                        _cd_chunk = {
                            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": "clyde",
                            "choices": [{"index": 0, "delta": {
                                "clyde_context": {
                                    "tokens": after,
                                    "threshold": 0,  # unchanged; Clyde reuses last known
                                    "messages": 0,
                                }
                            }, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(_cd_chunk)}\n\n"
                    if err:
                        yield _sse_chunk(f"*compact_done · error*\n\n")
                    else:
                        yield _sse_chunk(f"*compact_done · {before // 1000}K→{after // 1000}K tokens*\n\n")
                elif event_type == "recovering":
                    stage = data.get("stage", "")
                    detail = data.get("detail", "")
                    yield _sse_chunk(f"*recovering · {stage} · {detail}*\n")
                elif event_type == "recovery_done":
                    detail = data.get("detail", "")
                    yield _sse_chunk(f"*recovery_done · {detail}*\n\n")
                elif event_type == "generating":
                    pass  # iteration count suppressed from stream
                elif event_type == "metrics":
                    # Live tok/s + phase + tokens — Clyde renders under the
                    # streaming cursor as a small metrics chip.
                    m_data = json.dumps({
                        "phase": data.get("phase", ""),
                        "tokens": data.get("tokens", 0),
                        "tps": data.get("tps", 0.0),
                        "elapsed": data.get("elapsed", 0.0),
                        "thinking_chars": data.get("thinking_chars", 0),
                        "content_chars": data.get("content_chars", 0),
                    })
                    # SEPARATE SSE event with `clyde_metrics` — never via
                    # delta.content. Avoids leaking into thinking/content
                    # streams that Clyde routes by tag boundary.
                    _m_chunk = {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "clyde",
                        "choices": [{"index": 0, "delta": {"clyde_metrics": json.loads(m_data)}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(_m_chunk)}\n\n"
                elif event_type == "context":
                    # Conversation context pressure — tokens / threshold.
                    # Dedicated SSE channel (`clyde_context`) to keep it
                    # out of content/thinking streams. Also re-emit on
                    # compaction events so the graph shows the drop.
                    _ctx_payload = {
                        "tokens": data.get("tokens", 0),
                        "threshold": data.get("threshold", 0),
                        "messages": data.get("messages", 0),
                    }
                    _c_chunk = {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "clyde",
                        "choices": [{"index": 0, "delta": {"clyde_context": _ctx_payload}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(_c_chunk)}\n\n"
            except queue.Empty:
                await asyncio.sleep(0.05)

        # Close any open thinking stream before draining
        if thinking_stream_started:
            yield _sse_chunk("</think>\n\n")
            thinking_stream_started = False
            thinking_already_streamed = True  # Prevent duplicate in drain loop

        # Drain remaining events — process them instead of discarding
        while not event_queue.empty():
            try:
                event_type, data = event_queue.get_nowait()
                if event_type == "thinking_delta":
                    pass  # Already closed above, ignore late deltas
                elif event_type == "thinking":
                    if thinking_already_streamed:
                        # Thinking was already streamed in real-time — skip duplicate
                        status_sent = True
                        continue
                    think_text = data.get("content", "")
                    if think_text and not thinking_stream_started:
                        yield _sse_chunk("*thinking...*\n\n")
                        yield _sse_chunk("<think>")
                        words = think_text.split(" ")
                        buf = ""
                        for word in words:
                            buf += word + " "
                            if len(buf) >= 20:
                                yield _sse_chunk(buf)
                                buf = ""
                                await asyncio.sleep(0.01)
                        if buf:
                            yield _sse_chunk(buf)
                        yield _sse_chunk("</think>\n\n")
                    status_sent = True
                elif event_type == "narration":
                    narration = data.get("text", "")
                    if narration:
                        yield _sse_chunk(narration + "\n\n")
                        status_sent = True
                elif event_type == "tool_start":
                    name = data.get("name", "tool")
                    preview = data.get("args_preview", "")
                    tag = f"*using {name}*"
                    if preview:
                        tag += f" `{preview.replace(chr(10), ' ; ')[:300]}`"
                    yield _sse_chunk(tag + "\n")
                    status_sent = True
                elif event_type == "tool_done":
                    name = data.get("name", "tool")
                    status = "error" if data.get("is_error") else "done"
                    summary = data.get("summary", "")
                    output = data.get("output", "")
                    if output:
                        import base64 as _b64
                        _enc = _b64.b64encode(output.encode("utf-8", errors="replace")).decode("ascii")
                        yield _sse_chunk(f"<<tool_output:{_enc}>>\n")
                    if summary:
                        tag = f"*{name} {status} · {summary}*"
                    else:
                        tag = f"*{name} {status}*"
                    yield _sse_chunk(tag + "\n\n")
                elif event_type == "question":
                    q_data = json.dumps({
                        "id": data.get("id", ""),
                        "question": data.get("question", ""),
                        "choices": data.get("choices", []),
                        "allowOther": data.get("allow_other", True),
                    })
                    yield _sse_chunk(f"<<question:{q_data}>>\n")
                    status_sent = True
                elif event_type == "permission_request":
                    p_data = json.dumps({
                        "id": data.get("id", ""),
                        "path": data.get("path", ""),
                        "folder": data.get("folder", ""),
                    })
                    yield _sse_chunk(f"<<permission_request:{p_data}>>\n")
                    status_sent = True
                elif event_type == "compacting":
                    est = data.get("estimated_tokens", 0)
                    thresh = data.get("threshold", 0)
                    yield _sse_chunk(f"*compacting · {est // 1000}K/{thresh // 1000}K tokens*\n")
                    status_sent = True
                elif event_type == "compact_progress":
                    phase = data.get("phase", "")
                    pct = data.get("progress", 0)
                    detail = data.get("detail", "")
                    yield _sse_chunk(f"*compact_progress · {phase} · {pct}% · {detail}*\n")
                elif event_type == "compact_done":
                    before = data.get("tokens_before", 0)
                    after = data.get("tokens_after", 0)
                    err = data.get("error", "")
                    # Also push the post-compact token count through the
                    # dedicated context channel so the Performance tile's
                    # "Context Pressure" graph shows the drop.
                    if not err and after > 0:
                        _cd_chunk = {
                            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": "clyde",
                            "choices": [{"index": 0, "delta": {
                                "clyde_context": {
                                    "tokens": after,
                                    "threshold": 0,  # unchanged; Clyde reuses last known
                                    "messages": 0,
                                }
                            }, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(_cd_chunk)}\n\n"
                    if err:
                        yield _sse_chunk(f"*compact_done · error*\n\n")
                    else:
                        yield _sse_chunk(f"*compact_done · {before // 1000}K→{after // 1000}K tokens*\n\n")
            except queue.Empty:
                break

        # Get the result
        try:
            summary = future.result()
        except Exception as e:
            log.error(f"Agent turn raised exception: {e}")
            yield _sse_chunk(f"\n\nError: {e}")
            yield _sse_done()
            return

        # Add separator if we showed status
        if status_sent:
            yield _sse_chunk("\n---\n\n")

        # Stream the final text in word-sized chunks for natural feel
        text = summary.assistant_text or ""
        # Belt-and-suspenders: strip any residual thinking + tool_call tags
        text = re.sub(r'<\|channel>thought\s*.*?(?:<channel\|>|<\|channel>)', '', text, flags=re.DOTALL)
        text = re.sub(r'<\|channel>thought\s*.*', '', text, flags=re.DOTALL)
        text = re.sub(r'<\|think\|>.*?(?:<\|/think\|>)', '', text, flags=re.DOTALL)
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        # Tool-call tag leakage (Qwen3.6-A3B hybrid + closed/unclosed wrappers)
        text = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL)
        text = re.sub(r'<function=\w+\s*>.*?</function>', '', text, flags=re.DOTALL)
        text = re.sub(r'<function=\w+\s*>.*', '', text, flags=re.DOTALL)
        text = re.sub(r'<tool_call>.*', '', text, flags=re.DOTALL)
        text = re.sub(r'</?tool_call>|</?function>|</?parameter[^>]*>|</?export>', '', text)
        text = text.strip()
        # Guard: never send a completely empty final response to the client
        if not text.strip() and status_sent:
            text = "Done."
        elif not text.strip():
            text = "I wasn't able to generate a response. Please try again."
            log.warning("Agent returned empty assistant_text with no tool activity")

        words = text.split(" ")
        buf = ""
        for word in words:
            buf += word + " "
            if len(buf) >= 15:  # ~3 words per chunk
                yield _sse_chunk(buf)
                buf = ""
                await asyncio.sleep(0.01)
        if buf:
            yield _sse_chunk(buf)

        yield _sse_done()

        # Release the in-flight guard so the idle reaper can consider
        # this backend for shutdown again. Without this, a completed
        # request holds the in_flight flag forever and the backend
        # never gets reaped.
        _bid = getattr(runtime, '_active_backend_id', None)
        if _bid and _PM:
            _PM.mark_request_end(_bid)


# ─── Main ───

@app.on_event("startup")
async def _on_startup():
    """Start the backend process reaper on server startup."""
    if _PM:
        _PM.start_reaper()
        log.info("ProcessManager idle reaper started")
    # If default route is external, kill any local backends that might
    # be lingering from a previous session.
    if _ROUTER:
        await _kill_local_backends_if_external(_ROUTER._default_logical_name)

@app.on_event("shutdown")
async def _on_shutdown():
    """Clean up managed backends on agent exit."""
    if _PM:
        _PM.shutdown_all()
        log.info("ProcessManager shut down all managed backends")

# ─── Backend status endpoint ───

@app.get("/v1/backends")
async def list_backends():
    """Return status of all managed backend processes."""
    if _PM:
        return {"backends": _PM.status()}
    return {"backends": []}


@app.get("/v1/metrics")
async def stack_metrics():
    """Full Clyde stack metrics: memory, throughput, KV cache, turboquant status."""
    import resource
    import subprocess as _sp

    def _rss_mb(pattern: str) -> float:
        """Get total RSS in MB for processes matching pattern."""
        try:
            out = _sp.check_output(
                f"ps aux | grep '{pattern}' | grep -v grep | awk '{{sum+=$6}} END {{print sum+0}}'",
                shell=True, text=True, timeout=3
            ).strip()
            return round(float(out) / 1024, 1) if out else 0.0
        except Exception:
            return 0.0

    def _pid(pattern: str) -> int | None:
        try:
            out = _sp.check_output(f"pgrep -f '{pattern}'", shell=True, text=True, timeout=3).strip()
            return int(out.split()[0]) if out else None
        except Exception:
            return None

    def _parse_llama_log() -> dict:
        """Extract memory breakdown and throughput from llama-server log."""
        log_path = "/tmp/cm-llamacpp.log"
        result = {"model_mb": None, "context_mb": None, "compute_mb": None,
                  "prompt_tps": None, "eval_tps": None, "last_output_tokens": None,
                  "kv_tokens": None, "checkpoint_mb": None}
        try:
            with open(log_path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return result

        for line in reversed(lines):
            # Memory breakdown: model, context, compute
            if "MTL0" in line and result["model_mb"] is None:
                import re
                nums = re.findall(r'\d+', line)
                # Format: total = free + (self = model + context + compute) + unaccounted
                # Indices vary, parse by position after the "=" signs
                m = re.search(r'model\s+context\s+compute', line) or True
                parts = line.split("|")
                if len(parts) >= 3:
                    inner = parts[2]
                    # e.g. "22370 = 21098 +     782 +     489"
                    eq_parts = inner.split("=")
                    if len(eq_parts) >= 2:
                        sum_parts = eq_parts[-1].strip().rstrip(")").split("+")
                        if len(sum_parts) >= 3:
                            result["model_mb"] = int(sum_parts[0].strip())
                            result["context_mb"] = int(sum_parts[1].strip())
                            result["compute_mb"] = int(sum_parts[2].strip().split(")")[0].strip())

            # Throughput
            if "prompt eval time" in line and result["prompt_tps"] is None:
                import re
                m = re.search(r'([\d.]+)\s+tokens per second', line)
                if m:
                    result["prompt_tps"] = float(m.group(1))

            if "eval time" in line and "prompt" not in line and result["eval_tps"] is None:
                import re
                m = re.search(r'([\d.]+)\s+tokens per second', line)
                if m:
                    result["eval_tps"] = float(m.group(1))
                m2 = re.search(r'/\s*(\d+)\s+tokens', line)
                if m2:
                    result["last_output_tokens"] = int(m2.group(1))

            # KV tokens
            if "n_tokens =" in line and result["kv_tokens"] is None:
                import re
                m = re.search(r'n_tokens\s*=\s*(\d+)', line)
                if m:
                    result["kv_tokens"] = int(m.group(1))

            # Checkpoint size
            if "context checkpoint" in line and result["checkpoint_mb"] is None:
                import re
                m = re.search(r'size\s*=\s*([\d.]+)', line)
                if m:
                    result["checkpoint_mb"] = float(m.group(1))

        return result

    # Gather process info
    llama_rss = _rss_mb("llama-server")
    agent_rss = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576, 1)  # self
    clyde_rss = _rss_mb("Clyde.app/Contents/MacOS/Clyde")

    llama_pid = _pid("llama-server.*--port.*8810")
    stack_total = round(llama_rss + agent_rss + clyde_rss, 1)

    # Parse launch args
    launch_args = ""
    if llama_pid:
        try:
            launch_args = _sp.check_output(f"ps -p {llama_pid} -o args=", shell=True, text=True, timeout=3).strip()
        except Exception:
            pass

    import re
    ctx_size = None
    gpu_layers = None
    cache_k = None
    cache_v = None
    m = re.search(r'ctx-size\s+(\d+)', launch_args)
    if m: ctx_size = int(m.group(1))
    m = re.search(r'n-gpu-layers\s+(\d+)', launch_args)
    if m: gpu_layers = int(m.group(1))
    m = re.search(r'cache-type-k\s+(\S+)', launch_args)
    if m: cache_k = m.group(1)
    m = re.search(r'cache-type-v\s+(\S+)', launch_args)
    if m: cache_v = m.group(1)

    log_data = _parse_llama_log()

    turboquant_active = (cache_k == "q4_0" and cache_v == "q4_0")
    flash_attn = "--flash-attn" in launch_args
    mmap_on = "--mmap" in launch_args

    total_mem_gb = round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024**3), 1) if hasattr(os, "sysconf") else None

    return {
        "system": {"total_ram_gb": total_mem_gb},
        "processes": {
            "llama_server": {"pid": llama_pid, "alive": llama_pid is not None, "rss_mb": llama_rss},
            "agent": {"rss_mb": agent_rss},
            "clyde": {"rss_mb": clyde_rss},
        },
        "stack_total_mb": stack_total,
        "stack_pct": round(stack_total / (total_mem_gb * 1024) * 100, 1) if total_mem_gb else None,
        "config": {
            "ctx_size": ctx_size,
            "gpu_layers": gpu_layers,
            "cache_type_k": cache_k,
            "cache_type_v": cache_v,
            "flash_attn": flash_attn,
            "mmap": mmap_on,
        },
        "gpu_memory_mb": {
            "model": log_data["model_mb"],
            "kv_cache": log_data["context_mb"],
            "compute": log_data["compute_mb"],
        },
        "kv_state": {
            "tokens_in_cache": log_data["kv_tokens"],
            "checkpoint_mb": log_data["checkpoint_mb"],
        },
        "throughput": {
            "prompt_tps": log_data["prompt_tps"],
            "eval_tps": log_data["eval_tps"],
            "last_output_tokens": log_data["last_output_tokens"],
        },
        "turboquant": {
            "active": turboquant_active,
            "flash_attn_required": flash_attn,
            "status": "ACTIVE" if (turboquant_active and flash_attn) else "MISCONFIGURED",
        },
    }


@app.post("/v1/stop_backends")
async def stop_all_backends():
    """Kill every managed backend process. Called by Clyde before
    switching models to guarantee a clean slate — belt-and-suspenders
    on top of ProcessManager's exclusive mode."""
    if _PM:
        _PM.stop_all_backends()
        return {"ok": True, "message": "all managed backends stopped"}
    return {"ok": True, "message": "no process manager"}


# ─── Settings endpoint ───
# Exposes ALL configurable parameters so Clyde's settings panel can
# read and write them. Covers agent config, session/compaction, model
# profile sampling, phase overrides, and backend hints.

def _profile_for_route(route_name: str | None = None) -> dict:
    """Extract profile data as a serializable dict."""
    if _ROUTER is None:
        return {}
    try:
        route = _ROUTER.resolve(route_name)
        p = route.profile
        phases = {}
        for name, pp in (p.phase_overrides or {}).items():
            phases[name] = {
                "temperature": pp.temperature,
                "top_p": pp.top_p,
                "top_k": pp.top_k,
                "repetition_penalty": pp.repetition_penalty,
                "max_tokens_floor": pp.max_tokens_floor,
            }
        return {
            "id": p.id,
            "family": p.family,
            "context_window": p.context_window,
            "thinking_directive": str(p.thinking_directive or ""),
            "thinking_mode_default": str(p.thinking_mode_default or "off"),
            "tool_schema_strategy": p.tool_schema_strategy,
            "sampling_defaults": dict(p.sampling_defaults or {}),
            "phase_overrides": phases,
            "chat_template_quirks": dict(p.chat_template_quirks or {}),
            "notes": p.notes or "",
        }
    except Exception:
        return {}

@app.get("/v1/settings")
async def get_settings(route: str | None = None):
    """Return all configurable parameters for the current (or specified) route.

    Consumed by Clyde's LM Studio-style settings panel. Shape:
      - agent: max_iterations, max_tokens, temperature, tool_timeout
      - session: compact_after_tokens, preserve_recent_messages
      - profile: context_window, sampling_defaults, phase_overrides, ...
      - backend: idle_timeout, managed status
      - env: thinking_mode, repetition overrides
    """
    # Agent-level config (from config.yaml, mutable at runtime)
    agent_cfg = {
        "max_iterations": MAX_ITERATIONS,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "tool_timeout": CFG["agent"].get("tool_timeout", 30),
    }
    # Session / compaction
    session_cfg = {
        "compact_after_tokens": COMPACT_THRESHOLD,
        "preserve_recent_messages": PRESERVE_RECENT,
    }
    # Profile for the active route
    profile_data = _profile_for_route(route)

    # Backend process info
    backend_info = {}
    if _PM and _ROUTER:
        try:
            r = _ROUTER.resolve(route)
            mb = _PM._managed.get(r.backend_id)
            if mb:
                backend_info = {
                    "managed": True,
                    "idle_timeout": mb.idle_timeout,
                    "startup_timeout": mb.startup_timeout,
                    "running": mb.pid is not None and _PM._is_process_alive(mb.pid),
                    "model_loaded": mb.model_loaded,
                }
            else:
                backend_info = {"managed": False}
        except Exception:
            backend_info = {"managed": False}

    # Environment overrides (the user can tweak these)
    env_cfg = {
        "thinking_mode": os.environ.get("CLYDE_THINKING_MODE", "on"),
        "temp_structured": os.environ.get("CLYDE_TEMP_STRUCTURED"),
        "temp_free": os.environ.get("CLYDE_TEMP_FREE"),
        "top_p_override": os.environ.get("CLYDE_TOP_P"),
        "repetition_penalty": os.environ.get("CLYDE_REPETITION_PENALTY"),
        "repetition_context_size": os.environ.get("CLYDE_REPETITION_CONTEXT_SIZE"),
    }

    return JSONResponse({
        "agent": agent_cfg,
        "session": session_cfg,
        "profile": profile_data,
        "backend": backend_info,
        "env": env_cfg,
    })


@app.put("/v1/settings")
async def update_settings(request: Request):
    """Update runtime settings. Persists to config.yaml + reloads.

    Accepts partial updates — only the keys sent are changed.
    """
    global MAX_ITERATIONS, MAX_TOKENS, TEMPERATURE, COMPACT_THRESHOLD, PRESERVE_RECENT

    body = await request.json()
    changed = []

    # Agent-level
    if "agent" in body:
        a = body["agent"]
        if "max_iterations" in a:
            MAX_ITERATIONS = int(a["max_iterations"])
            CFG["agent"]["max_iterations"] = MAX_ITERATIONS
            changed.append("max_iterations")
        if "max_tokens" in a:
            MAX_TOKENS = int(a["max_tokens"])
            CFG["agent"]["max_tokens"] = MAX_TOKENS
            changed.append("max_tokens")
        if "temperature" in a:
            TEMPERATURE = float(a["temperature"])
            CFG["agent"]["temperature"] = TEMPERATURE
            changed.append("temperature")
        if "tool_timeout" in a:
            CFG["agent"]["tool_timeout"] = int(a["tool_timeout"])
            changed.append("tool_timeout")

    # Session / compaction
    if "session" in body:
        s = body["session"]
        if "compact_after_tokens" in s:
            COMPACT_THRESHOLD = int(s["compact_after_tokens"])
            CFG["session"]["compact_after_tokens"] = COMPACT_THRESHOLD
            changed.append("compact_after_tokens")
        if "preserve_recent_messages" in s:
            PRESERVE_RECENT = int(s["preserve_recent_messages"])
            CFG["session"]["preserve_recent_messages"] = PRESERVE_RECENT
            changed.append("preserve_recent_messages")

    # Environment overrides
    if "env" in body:
        e = body["env"]
        for key, env_var in [
            ("thinking_mode", "CLYDE_THINKING_MODE"),
            ("temp_structured", "CLYDE_TEMP_STRUCTURED"),
            ("temp_free", "CLYDE_TEMP_FREE"),
            ("top_p_override", "CLYDE_TOP_P"),
            ("repetition_penalty", "CLYDE_REPETITION_PENALTY"),
            ("repetition_context_size", "CLYDE_REPETITION_CONTEXT_SIZE"),
        ]:
            if key in e:
                val = e[key]
                if val is None or val == "":
                    os.environ.pop(env_var, None)
                else:
                    os.environ[env_var] = str(val)
                changed.append(key)

    # Backend idle timeout
    if "backend" in body and "idle_timeout" in body["backend"]:
        if _PM and _ROUTER:
            try:
                # Apply to all managed backends
                timeout = int(body["backend"]["idle_timeout"])
                for mb in _PM._managed.values():
                    mb.idle_timeout = timeout
                changed.append("idle_timeout")
            except Exception as exc:
                log.warning(f"Failed to update idle_timeout: {exc!r}")

    # Persist config.yaml
    if changed:
        try:
            with open(CFG_PATH, "w") as f:
                yaml.dump(CFG, f, default_flow_style=False, sort_keys=False)
            log.info(f"Settings updated: {changed}")
        except Exception as exc:
            log.error(f"Failed to persist config.yaml: {exc!r}")
            return JSONResponse({"ok": False, "error": str(exc), "changed": changed}, status_code=500)

    return JSONResponse({"ok": True, "changed": changed})


if __name__ == "__main__":
    ensure_dirs()
    log.info(f"Clyde Agent starting on {HOST}:{PORT}")
    log.info(f"Backend: {BACKEND_URL}")
    log.info(f"Max iterations: {MAX_ITERATIONS} (config: {CFG['agent']['max_iterations']})")
    log.info(f"Compact threshold: {COMPACT_THRESHOLD} tokens (mid-turn: {int(COMPACT_THRESHOLD * 0.70)})")
    log.info(f"Tools: {get_runtime().tool_registry.names()}")

    # Start Clyde app watchdog — self-terminates if Clyde isn't running
    # Skip watchdog when launched standalone (relay, CLI, testing)
    if os.environ.get("CLYDE_NO_WATCHDOG"):
        log.info("Watchdog disabled (CLYDE_NO_WATCHDOG set)")
    else:
        _start_clyde_watchdog()

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
