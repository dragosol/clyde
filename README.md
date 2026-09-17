# Clyde

[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](#requirements)
[![Status: Archived](https://img.shields.io/badge/status-archived-inactive.svg)](#project-status)
A local-first AI assistant for macOS: a native SwiftUI chat client on top of a Python agent harness. Everything runs on your machine: no cloud round-trip, no account, no telemetry.

**This project is archived.** It works, it is not finished, and I am not continuing it. It is published so anyone who wants the harness, the client, or the ideas can take them. See [Project status](#project-status) and [What I learned](#what-i-learned). The second one is probably worth more than the code.

---

## What it is

Two halves, either of which is useful on its own.

**The harness** (`agent/`): a Python agent runtime that does the unglamorous work: tool execution, multi-step planning, permissions, session persistence, context management, memory consolidation, and backend routing. Roughly 10,000 lines across `conversation.py` and `tools.py` alone. It speaks to any OpenAI-compatible endpoint, so the model is a swappable detail.

**The client** (`app/`): a native macOS SwiftUI app. Not an Electron wrapper. Chat with custom-drawn bubbles, a live asset-graph view of what the agent knows, an inspector panel, permission prompts, and a settings surface for switching backends at runtime.

## Features

**Client**
- Native SwiftUI, custom chat-bubble geometry, animated neural-net background, custom spiral cursor
- **Asset graph**: a live, explorable graph of entities the agent has accumulated, with expandable panel and full-window view (`AssetGraphView`, `GraphDrawing`, `GraphExpandPanel`, `AssetDetailView`)
- Inspector panel and sidebar for session and asset navigation
- Runtime backend switching with a machine-aware default on first launch
- Per-tool permission prompts surfaced in the UI
- Debug view exposing agent internals and timings

**Harness**
- Tool execution layer: shell, file read/write, search, document creation, memory operations
- Multi-step planning (`plan.py`) and model routing (`routing.py`)
- Persistent memory with automatic consolidation (`memory.py`, `memory_ops.py`, `autodream.py`)
- Asset/knowledge graph construction (`assets.py`)
- Session persistence and process supervision with auto-recovery (`session.py`, `process_manager.py`)
- Skills system for packaged multi-step capabilities (`skills/`)
- macOS integration bridge (`macos_bridge.py`)
- Automatic context compaction

## Requirements

- macOS, Xcode 15+
- Python 3.11+
- Any OpenAI-compatible inference server (LM Studio, llama.cpp, Ollama, vLLM). The original ran MLX locally.

## Getting started

```bash
# harness
cd agent && pip install -r requirements.txt && python -m agent
# client
open app/Clyde.xcodeproj   # build and run the Clyde scheme
```

Point the client at your backend in Settings → Models. `agent/backends.yaml` is the backend config; endpoints in it are placeholders.

Deeper docs live in `app/Clyde/`: `ARCHITECTURE.md`, `IMPLEMENTATION.md`, `EXTENDING.md`, `RUNNING.md`, `TESTING.md`, `VISUAL_GUIDE.md`, `START_HERE.md`.

## Project status

Discontinued as of mid-2026. It was heading for the App Store as a paid local-first assistant; the market filled with wrappers and the economics stopped making sense. Rather than sit on it, it is MIT-licensed and public.

Known-unfinished: the asset graph does not always refresh reliably under load, small models frequently ignore the graph and skills entirely, and model switching had races where more than one backend could be resident at once.

## What I learned

These are the real findings, and they cost the most to get. If you are building an agent harness, they may save you the same weeks.

**1. Prefill dominates, and it will look like a hang.** A `graph_write` call that appeared to freeze was not a tool bug. It was a 16K-token prefill on a 27B model taking 3+ minutes until the watchdog killed it. Before blaming a tool, measure time-to-first-token.

**2. Read-without-write is the real context killer.** The agent called a batch-read tool 10+ times consecutively without ever calling the corresponding write. Context grew without bound because nothing was ever externalised. The fix was atomic read-write tools that persist as they read. **Write to disk before you compact.** Anything only in the context window is one summarization away from gone.

**3. Scaffolding crowds out the task.** A 24 KB system prompt expanded to 8 to 15K tokens per turn once tool schemas, memory sections, the graph section and skill hints were templated in. Worse, small models never used the graph, the layered memory, or skill switching, so most of that budget was spent on features they ignored. Ship a compact prompt mode for small backends.

**4. Don't health-check on every call.** An HTTP probe before each backend call added seconds to every turn's TTFT.

**5. Small models need worked examples, not rule lists.** Prohibitions ("do not output X") were followed far less reliably than a single concrete example of a good response.

## Layout

```
app/        SwiftUI macOS client (Xcode project)
agent/      Python agent harness: tools, memory, planning, routing
benchmarks/ backend benchmark harness
```

## Related, not included

The original setup used two third-party MLX projects that are **not** vendored here, since they are not mine to redistribute:

- [`turboquant-mlx`](https://github.com/arozanov/turboquant-mlx): KV-cache compression for MLX
- `mlx-flash`: MIT, © Flash-Mode Contributors

## Licence

MIT, see [LICENSE](LICENSE). Take it, fork it, ship it.
