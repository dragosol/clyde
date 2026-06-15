# Clyde — Benchmarks

Two end-to-end benchmarks validate the Clyde agent stack. Both must pass on the same model, backend, and agent runtime. Ship gate: both passing on the same stack with no config changes between runs.

## How benchmarks MUST be executed

**All benchmarks run through Clyde's chat UI.** Type the prompt into Clyde's conversation view and let the agent handle everything — tool calls, context management, compaction, multi-turn pacing. Never bypass the agent with direct-to-llama curl requests. The agent layer is what's being tested.

Execution rules:
1. Launch Clyde from Xcode (⌘R) — never from the installed .app
2. Ensure the agent is healthy: green status indicator in Clyde, or check `/health` via relay
3. Start a fresh conversation for each benchmark run
4. Type the benchmark prompt and let it run to completion
5. Do NOT interrupt, restart, or modify settings mid-run
6. After completion, collect artifacts from the agent's output directory

## Current stack (as of 2026-04-19)

| Component | Value |
|---|---|
| Model | Qwen 3.6 35B-A3B MoE (Q4_K_M GGUF, ~22 GB) |
| Primary backend | llama.cpp (llama-server), port 8810, q4_0 KV, flash-attn, mmap |
| Secondary backend | SwiftLM, port 8812, TurboQuant KV, SSD streaming, 262K ctx |
| Agent | FastAPI, port 8801, 80-iteration cap, 60K compaction threshold |
| Thinking | ON (Qwen 3.6 native thinking) |
| Stack memory | ~22–24.6 GB total (46–50% of 48 GB M3 Max) |

---

## Benchmark 1: Paper (long-form research writing)

### Task

Write a 5000-word research paper with ≥6 cited sources, ≥3 unique fetched URLs, and a self-graded score ≥80/100. Tests multi-turn pacing, web fetch, fact accumulation, rolling summary, and phase transitions (scoping → research → writing → review).

### Prompt

Open a fresh Clyde conversation and type:

> Write a 5000-word research paper on "The Rise of Edge AI: How On-Device Machine Learning is Transforming Consumer Technology". Use web search to find at least 8 sources. Include sections on hardware accelerators, model compression, real-world applications, privacy implications, challenges, and market outlook. Grade yourself when done.

### Pass criteria

```
paper_passed = (
    self_grade >= 80
    and word_count >= 3000
    and unique_urls >= 3
    and citations_incorporation_rate >= 0.80
    and terminal_degeneration_events < 3
    and peak_context_tokens <= 250_000
    and thinking_leak_events == 0
)
```

### Current best: 2026-04-18 (Qwen 3.6, llama.cpp) — PASSED ✅

| Metric | Value | Threshold |
|---|---|---|
| Self-grade | 83/100 | ≥ 80 |
| Independent grade | 83/100 | ≥ 80 |
| Word count | 11,081 | ≥ 3,000 |
| Unique source URLs | 8 (all HTTP 200) | ≥ 3 |
| Citations | 8/8 integrated | ≥ 80% |
| Degeneration events | 0 content (12 loop-detection, all caught) | < 3 |
| Total tokens | ~244K (157K prompt + 87K gen, 98 requests) | ≤ 250K |
| Peak context | ~30,501 (compacted twice) | ≤ 250K |
| Wall time | ~15 min | — |
| Tool calls | 67 total: outline(2), web_search(9), web_fetch(5), record_fact(8), draft_section(8), self_grade(12), bash(18), memory_read(5) | — |

### What to check after the run

1. Paper artifact appears in agent output — should be a .docx or rendered in chat
2. Self-grade score is stated by the model at end of run
3. Count sections — should be ≥6 with distinct content
4. Spot-check 3 citations — URLs should be real and content should match claims
5. No raw thinking blocks leaked into the paper text

---

## Benchmark 2: Bookmark organizer (high-item classification)

### Task

Reorganize a real bookmark library into a model-derived taxonomy. 100% coverage, ≥80% placement accuracy on blind-graded 20-sample spot-check. Tests coverage invariants, taxonomy grounding, sustained pacing across 40+ turns, context ceiling management.

### Prompt

Open a fresh Clyde conversation and type:

> Organize my bookmarks. Read the bookmark file at ~/.clyde/agent/test_bookmarks/bookmarks.html and classify every single bookmark into a taxonomy you derive from the actual content. Use web_fetch for any ambiguous bookmarks. I need 100% coverage — every bookmark must be classified.

### Pass criteria

```
bookmark_passed = (
    coverage_pct == 100.0
    and taxonomy_derived_from_observations
    and blind_grade_strict >= 16 / 20
    and peak_context_tokens <= 250_000
    and terminal_degeneration_events < 3
    and no_hallucinated_ids
    and taxonomy_size in range(8, 31)
)
```

### Current best: 2026-04-18 (Qwen 3.6, llama.cpp) — PASSED ✅

| Metric | Value | Threshold |
|---|---|---|
| Coverage | 664/664 (100%) | 100% |
| Categories | 54 total (40 leaf, 14 parent groups) | 8–30 top-level |
| Taxonomy edges | 34 parent_of edges | derived from observations |
| Uncertain placements | 0 | — |
| Enrichment | web_fetch used for ambiguous items | — |
| All 9 enforcement bars | PASSED | — |
| Wall time | ~15 min | — |

### What to check after the run

1. `items.json` in test_bookmarks should show every bookmark classified
2. `taxonomy.json` should have categories derived from actual content (not generic like "Other" or "Misc")
3. Spot-check 20 random bookmarks against their assigned category — ≥16/20 should be correct
4. Asset graph in Clyde should show category nodes with edges
5. No bookmark IDs appear that don't exist in the original input

---

## Shared criteria (both benchmarks)

These are enforced by the runtime, not the prompt:

| Criterion | Threshold | How it's enforced |
|---|---|---|
| Peak context tokens | ≤ 250,000 | Agent warns at 200K, hard-compacts at 225K, kills at 245K |
| Per-turn degeneration | < 3 events per turn, max 3 total | `_content_degenerated` + thinking cap + sentence-uniqueness |
| Thinking leak | 0 — no thinking blocks in output | `_clean_content` validated across model dialects |
| Tool hallucination | 0 — no references to unregistered IDs | Tool contracts reject unknown IDs with explicit errors |
| Rolling summary | At 50% compact threshold or >50 tool ops | `rolling_summary_if_needed()` in conversation.py |

### 250K context ceiling rationale

Clyde runs on a MacBook. 250K tokens is what Qwen 3.6 on llama.cpp with q4_0 KV + mmap can sustain without hitting swap. The enforcement ladder:
- **200K** — warn, emit pacing hint to close current subtask
- **225K** — hard compact: trim tool results to 300 chars, preserve facts/taxonomy/last 3 messages
- **245K** — kill run, `benchmark_failed: context_ceiling_exceeded`

---

## Benchmark for SwiftLM (16GB Macs) — PENDING

SwiftLM (Clyde Flash) needs its own benchmark pass. Same benchmarks, same pass criteria, but on the SwiftLM backend. Key differences to watch:

- 262K context window (vs 128K on llama.cpp)
- ~11.4 tok/s decode (vs ~48 tok/s on llama.cpp) — runs will take ~4x longer
- TurboQuant KV compression beyond 8K tokens — watch for quality degradation at high context
- Memory target: OS_RAM stays under 12 GB (do NOT add `--mem-limit` flag — causes SIGSEGV)
- Prefill can take 400+ seconds at 40K tokens — agent HTTP timeout must accommodate this

To run: select "Clyde Flash (16GB)" in Clyde's settings, wait for SwiftLM to load, then run the same prompts.

---

## Historical failures and lessons

### Paper V1–V3 failures (Qwen 3.5/3.6, 2026-04-14 to 2026-04-18)

- **V1**: Qwen 3.5 stuck in record_fact loop (55 iterations, never entered drafting). Fix: phase-transition forcing and fact threshold in ModelProfile.
- **V2–V3**: Various pacing and tool-argument issues. Fix: raw argument unwrapping for Qwen's double-serialization quirk (`{"raw": "{...}"}`), prose salvage for off-pipeline output, auto-scaffold outlines.

### Bookmark direct-to-llama bypass (2026-04-14)

Direct curl to llama.cpp got 54% coverage (token truncation at max_tokens=32768). Lesson: the agent layer is not overhead — it's the mechanism that handles chunking, multi-turn context, and 100% coverage. Always benchmark through Clyde.

### Cross-cutting lessons

- Self-grade and independent grade converged at 83/100 on the paper bench — self-grade is a reliable proxy
- Both benchmarks pass on the same stack with no config changes — this was the ship gate
- Compaction is the key lever: two compaction events kept peak context under 31K despite 80+ rounds
- ~45 words of paper content per 1K tokens consumed — most budget goes to prompt eval on each request

---

## Results table

| Date | Benchmark | Model | Backend | Key metric | Grade | Peak ctx | Wall | Status |
|---|---|---|---|---|---|---|---|---|
| **2026-04-18** | **Paper** | **Qwen 3.6** | **llama.cpp** | **11,081 words, 8 URLs** | **83/100** | **~30K** | **~15m** | **✅ PASS** |
| **2026-04-18** | **Bookmark** | **Qwen 3.6** | **llama.cpp** | **664/664, 54 cats** | **—** | **—** | **~15m** | **✅ PASS** |
| 2026-04-14 | Paper | Qwen 3.5 | llama.cpp | 1,241 words (stuck) | — | — | 49m killed | ❌ pacing |
| 2026-04-14 | Bookmark | Qwen 3.5 | llama.cpp (direct) | 652/1200 (54%) | — | — | ~23m | ❌ truncation |
| Historical | Paper | Gemma4 26B | MLX | 3,813 words | 89/100 | — | — | ✅ (paper only) |
| **Pending** | **Paper** | **Qwen 3.6** | **SwiftLM** | — | — | — | — | **⏳** |
| **Pending** | **Bookmark** | **Qwen 3.6** | **SwiftLM** | — | — | — | — | **⏳** |
