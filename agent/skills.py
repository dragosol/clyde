"""
skills.py — Skill system for Clyde / Clyde
=================================================

A *skill* bundles together:
  - a curated tool allowlist (subset of ToolRegistry)
  - a system prompt fragment (appended to the lean base prompt)
  - activation hints used by the heuristic router
  - a small behavioural profile (e.g., thinking on/off)

There are five built-in skills: General (default), Research, Quick
Research (news / fast lookup), Code / Files, and Memory.

Activation model
----------------
Each conversation has one active skill at a time. The skill can be:

  1. Set explicitly by the user via the /v1/skills/activate endpoint.
  2. Chosen autonomously by ``route_skill()`` on the first user message
     of a conversation, *or* re-evaluated mid-conversation if the user
     hasn't locked it.

The user always wins — once they pick (or deactivate) a skill
explicitly, auto-routing is suppressed for that conversation unless
they re-enable it.

This module is intentionally dependency-free so it can be imported
from tools.py, conversation.py, and agent.py without circularity.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Iterable, Callable, Any


# ─── Phase-gated skill architecture (v2) ──────────────────────────────

@dataclass
class SkillPhase:
    """One phase of a structured skill workflow.

    During this phase, ONLY the tools in `tools` (+ the skill's
    shared_tools) are visible to the model. Tools from other phases
    are hidden at the registry level. The model literally cannot skip
    phases because the tools it would need don't exist.
    """
    name: str                              # "research", "outline", "draft", "revise", "verify"
    tools: list[str]                       # tools available IN this phase (excl. shared)
    prompt_hint: str                       # injected as system message while phase is active
    exit_condition: str = ""               # key into SkillState (checked as bool)
    max_iterations: int = 20              # per-phase iteration cap
    inference_cap: int = 100              # tool calls per model inference (organizer uses 2)


@dataclass
class SkillState:
    """Per-conversation phase-gated state. Updated by tool results.

    Persisted in the asset graph via graph_write. After compaction,
    graph_read restores this state so the model resumes in the right phase.
    """
    current_phase_idx: int = 0
    phase_iterations: int = 0
    fact_count: int = 0
    search_count: int = 0
    has_outline: bool = False
    all_sections_drafted: bool = False
    revision_done: bool = False
    grade_score: int = 0
    total_words: int = 0
    target_words: int = 0
    items_ingested: int = 0
    categories_count: int = 0
    classified_count: int = 0
    total_items: int = 0
    taxonomy_approved: bool = False
    delivered: bool = False
    research_phase_done: bool = False

    def check_exit(self, condition_key: str) -> bool:
        """Check if a phase exit condition is met."""
        if not condition_key:
            return False
        val = getattr(self, condition_key, False)
        if isinstance(val, bool):
            return val
        if isinstance(val, int):
            return val > 0
        return bool(val)

    def on_tool_result(self, tool_name: str, result: str) -> None:
        """Update state based on tool execution results."""
        if tool_name == "record_fact" and "Fact #" in result:
            self.fact_count += 1
        elif tool_name == "web_search":
            self.search_count += 1
        elif tool_name in ("research_outline", "creative_outline") and "OK" in result:
            self.has_outline = True
        elif tool_name == "organize_progress" and "total=" in result.lower():
            # Parse "total=664" from organize_progress output
            import re as _re
            m = _re.search(r'total[=:]?\s*(\d+)', result, _re.IGNORECASE)
            if m:
                self.total_items = int(m.group(1))
                if self.total_items > 0:
                    self.items_ingested = self.total_items
        elif tool_name == "parse_bookmarks" and "OK: Parsed" in result:
            m = re.search(r'Parsed (\d+)', result)
            if m:
                self.items_ingested = int(m.group(1))
        elif tool_name == "organize_batch_write":
            # Count classified items from result
            m = re.search(r'(\d+)\s*(?:classified|written|updated)', result, re.IGNORECASE)
            if m:
                self.classified_count += int(m.group(1))
        elif tool_name == "creative_review" and "PASS" in result:
            self.revision_done = True
        elif tool_name == "self_grade":
            m = re.search(r'(\d+)/20', result)
            if m:
                self.grade_score = int(m.group(1))
        elif tool_name == "graph_write" and "verify" in result.lower():
            if "ALL_PASS" in result:
                self.delivered = True


# ─── Skill definition (legacy + v2 phases) ────────────────────────────

@dataclass(slots=False)
class Skill:
    name: str                    # stable id (e.g. "general")
    label: str                   # user-facing display name
    description: str             # one-line description for the picker
    icon: str                    # SF Symbol name for Clyde's tile
    tool_allowlist: list[str]    # tool names this skill exposes (legacy — all phases combined)
    prompt_fragment: str         # appended to the lean base prompt
    activation_keywords: list[str] = field(default_factory=list)
    activation_phrases: list[str] = field(default_factory=list)
    enable_thinking: bool = True
    enforce_research_budget: bool = False
    require_planning: bool = False
    max_tool_calls_per_turn: int = 100

    # v2 phase-gated fields (optional — skills without phases work as before)
    phases: list[SkillPhase] = field(default_factory=list)
    shared_tools: list[str] = field(default_factory=list)

    def get_phase_tools(self, state: SkillState) -> list[str] | None:
        """Return the tool list for the current phase, or None if no phases.

        When None, the legacy tool_allowlist is used. When a list is
        returned, ONLY those tools (+ shared_tools + graph_read/graph_write)
        are visible to the model.
        """
        if not self.phases:
            return None
        if state.current_phase_idx >= len(self.phases):
            return None
        phase = self.phases[state.current_phase_idx]
        return list(set(phase.tools + self.shared_tools + ["graph_read", "graph_write"]))

    def get_phase_hint(self, state: SkillState) -> str:
        """Return the prompt hint for the current phase."""
        if not self.phases or state.current_phase_idx >= len(self.phases):
            return ""
        return self.phases[state.current_phase_idx].prompt_hint

    def advance_phase(self, state: SkillState) -> str | None:
        """Check exit condition + advance. Returns new phase name or None."""
        if not self.phases or state.current_phase_idx >= len(self.phases):
            return None
        phase = self.phases[state.current_phase_idx]
        # Check exit condition
        should_advance = (
            state.check_exit(phase.exit_condition)
            or state.phase_iterations >= phase.max_iterations
        )
        if should_advance:
            state.current_phase_idx += 1
            state.phase_iterations = 0
            if state.current_phase_idx < len(self.phases):
                return self.phases[state.current_phase_idx].name
            return "__complete__"
        return None


# ─── Built-in skill catalogue ─────────────────────────────────────────

_SHARED_BASE_TOOLS = [
    # Always available regardless of skill — safety valves and primitives.
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "glob_search",
    "grep_search",
    "memory_read",
    "memory_list",
    "ask_user",
]


_GENERAL_FRAGMENT = """# Style

You are a capable, friendly local assistant. Answer what is asked — no more,
no less. Keep responses compact. Use tools when they help; skip them when
a direct answer suffices. Do not invoke research or document-grading
tools for casual questions.

# Weather

ALWAYS use get_weather for ANY weather question. It handles both:
- **Local**: omit location → uses GPS. "what's the weather?" → get_weather({})
- **Any city**: pass location. "weather in Tokyo" → get_weather({"location": "Tokyo"})
Never use web_search for weather — get_weather covers all locations via Open-Meteo.
"""


_RESEARCH_FRAGMENT = """# Academic Research Methodology

This conversation is in **Research mode**. Produce publication-quality
work through a rigorous 5-phase pipeline.

## Phase 1 — Scoping (research_outline)
Before any searching, understand the question and create a structured
outline: sub-topics, thesis, word targets per section. Call
research_outline to register it, then ask_user for approval.

## Phase 2 — Deep Research (web_search + web_fetch + record_fact)
You have a **research budget of 12 total web_search + web_fetch calls**.
Plan efficiently. Record every useful finding with record_fact
immediately — include source URL, confidence, and category. Facts
survive context compaction; raw pages do not.

## Phase 3 — Synthesis (no tool calls)
Mentally review recorded facts; group by section; identify strongest
evidence; begin writing.

## Phase 4 — Writing (draft_section)
Begin writing once you have ≥8 recorded facts. One section at a time.
Weave [1], [2] citations inline. Only cite URLs you actually fetched.

## Phase 5 — Review (self_grade)
Grade the draft; iterate until ≥70/100 or two improvement rounds.
"""


_QUICK_RESEARCH_FRAGMENT = """# Quick Research Mode

This conversation is in **Quick Research mode** — fast, lightweight web
lookups. NOT an academic paper. No outline, no phases, no grading.

Use web_search to find what's asked, optionally web_fetch the top one or
two URLs for detail, and reply with a compact digest: key points in
plain prose (or a short bullet list when it genuinely helps), followed
by a sources line listing the URLs. Do **not** call research_outline,
draft_section, self_grade, or record_fact — those belong to the full
Research skill. If the task actually needs a paper, tell the user and
wait for them to switch skill.
"""


_CODE_FRAGMENT = """# Code / Files Mode

This conversation is in **Code / Files mode** — working on local code
or files. Prefer targeted edits over rewrites: read the file once,
reason about the change, then call edit_file with precise old_string /
new_string pairs. Use bash for tests, grep, and diagnostics. Use
absolute paths (with ~ or /Users/). Keep changes tightly scoped. Do not
invoke research tools.
"""


_MEMORY_FRAGMENT = """# Memory Mode

This conversation is in **Memory mode** — curating the 3-layer memory
store. Use memory_list to audit the index, memory_read to inspect,
memory_write / memory_update to refine, and memory_delete for stale
entries. Keep index lines ≤150 chars. Do not do research or file edits
in this mode.
"""


GENERAL_SKILL = Skill(
    name="general",
    label="General",
    description="Default chat — direct answers, no research scaffolding",
    icon="bubble.left.and.bubble.right",
    tool_allowlist=_SHARED_BASE_TOOLS + [
        "web_search",
        "web_fetch",
        "get_weather",
    ],
    prompt_fragment=_GENERAL_FRAGMENT,
    activation_keywords=[],  # default — wins only when nothing else matches
    enable_thinking=True,
)

RESEARCH_SKILL = Skill(
    name="research",
    label="Research",
    description="Deep multi-source research via local-deep-research. Academic papers, web, Wikipedia, arXiv.",
    icon="doc.text.magnifyingglass",
    tool_allowlist=_SHARED_BASE_TOOLS + [
        "deep_research",
        "web_search",
        "web_fetch",
        "record_fact",
        "draft_section",
        "self_grade",
        "create_document",
        "graph_read",
        "graph_write",
    ],
    prompt_fragment="""# Research Mode (powered by local-deep-research)

Use deep_research for multi-source investigations. It searches 20+ engines
(DuckDuckGo, Wikipedia, arXiv, academic DBs) and synthesizes a report.

- **quick mode**: fast summary, 1-2 min
- **detailed mode**: thorough report with citations, 3-5 min

Use web_search/web_fetch for simple lookups. Use deep_research when the
question needs multiple sources, academic papers, or synthesis.

After research, use draft_section + create_document to write the paper.
""",
    activation_keywords=[
        "research paper", "academic paper", "literature review",
        "thesis", "dissertation", "whitepaper", "write a paper",
        "publication-quality",
        "deep research", "deeper research", "in-depth research",
        "in depth research", "thorough research", "extensive research",
        "comprehensive research",
    ],
    activation_phrases=[
        r"\b(research|write) (me |us |you |them )?(a |an |the )?(paper|essay|report|thesis|article)\b",
        r"\b(do|conduct|run|need|want|perform)\s+(deeper|deep|more|further|in[- ]?depth|extensive|thorough|comprehensive|proper)\s+research\b",
        r"\b(literature|lit)\s+review\b",
        r"\bpublication[- ]quality\b",
    ],
    enable_thinking=True,
    enforce_research_budget=True,
    require_planning=True,
)

# QUICK_RESEARCH_SKILL — now uses deep_research quick mode alongside web tools
QUICK_RESEARCH_SKILL = Skill(
    name="quick_research",
    label="Quick Research",
    description="Fast web lookups, news, and quick research via local-deep-research",
    icon="sparkle.magnifyingglass",
    tool_allowlist=_SHARED_BASE_TOOLS + [
        "deep_research",
        "web_search",
        "web_fetch",
    ],
    prompt_fragment="""# Quick Research Mode

For simple lookups: use web_search + web_fetch.
For deeper questions needing multiple sources: use deep_research mode="quick".
deep_research searches 20+ engines and returns a synthesized summary in 1-2 min.
""",
    activation_keywords=[
        "news", "latest", "what's happening", "look up",
        "find out about", "recent", "headlines",
        "summarize the web", "quick search", "neuralink",
        "what happened", "any updates", "what's going on",
    ],
    activation_phrases=[
        r"\bwhat('?s| is) (the )?latest\b",
        r"\b(find|look) (me )?(up|into)\b",
        r"\bwhat('?s| is| did)? ?happen(ed|ing)?( with| in| to)?\b",
        r"\brecent (news|developments|updates)\b",
        r"\bheadlines\b",
        r"\bany (news|updates)\b",
        r"\bsearch (the )?web\b",
        r"\b(give|get|pull|find) me (some|a bunch of|several|many) (articles|results|links|sources)\b",
    ],
    enable_thinking=True,
)

CODE_SKILL = Skill(
    name="code",
    label="Code / Files",
    description="Local code and file work — edits, greps, builds, tests",
    icon="chevron.left.forwardslash.chevron.right",
    tool_allowlist=_SHARED_BASE_TOOLS + [
        "task_plan",
        "task_update",
    ],
    prompt_fragment=_CODE_FRAGMENT,
    activation_keywords=[
        "fix the bug", "refactor", "rename", "implement",
        "compile", "run the tests", "debug", "stack trace",
        "codebase", "repo", "repository", "commit", "git ",
    ],
    activation_phrases=[
        r"\bfix (the |this |my )?(bug|error|issue|crash)\b",
        r"\brefactor\b",
        r"\bwrite (a |the )?(function|class|module|script)\b",
        r"\brun (the )?tests?\b",
        r"\bdebug\b",
    ],
    enable_thinking=True,
)

MEMORY_SKILL = Skill(
    name="memory",
    label="Memory",
    description="Curate the memory store — audit, edit, prune",
    icon="brain",
    tool_allowlist=[
        "memory_read",
        "memory_write",
        "memory_update",
        "memory_delete",
        "memory_search",
        "memory_list",
        "transcript_search",
        "ask_user",
    ],
    prompt_fragment=_MEMORY_FRAGMENT,
    activation_keywords=[
        "your memory", "my memory", "memory index",
        "forget this", "remember this", "prune memory",
        "audit memory",
    ],
    activation_phrases=[
        r"\b(your|my|the) memor(y|ies)\b",
        r"\bforget\b.{0,30}\b(this|that|about|my|our)\b",
        r"\bremember (this|that|my|our|to|about)\b",
        r"\b(save|store|add) (this|that) to (memory|your memory)\b",
        r"\bprune (your )?memor(y|ies)\b",
    ],
    enable_thinking=True,
)


_ICLOUD_MACOS_FRAGMENT = """# iCloud & macOS Integration

This conversation is in **iCloud & macOS mode** — you have direct access to
Messages, Calendar, Contacts, Mail, Notes, and Reminders on the user's Mac.

## CRITICAL: Always Use `ask_user` — NEVER Ask Questions in Plain Text

**ABSOLUTE RULE**: Whenever you need the user to make a choice or confirm an action,
you MUST call the `ask_user` tool. NEVER type a question in your response text.
The `ask_user` tool renders interactive buttons in the UI that the user can tap.
Typing questions as prose forces the user to type a response, which is a bad experience.

WRONG (never do this):
> "Which one would you like to send the message to?"
> "1. matthew geddes. 1991  2. Matthew Geddes — Phone: ..."

RIGHT (always do this):
> Call `ask_user` with question="Which Matthew Geddes?" and choices like:
> ["Matthew Geddes — (613) 852-5773", "matthew geddes. 1991 — no phone", "Cancel"]

This applies to ALL user interactions:
- Disambiguation (multiple contacts, calendars, etc.)
- Confirmation before send/create/delete
- Clarifying missing info (which calendar, what time, etc.)

## Safety Rules (MANDATORY)

### Before ANY write/send operation you MUST:
1. Preview the exact content to the user in your message text
2. Immediately call `ask_user` with choices like ["Send", "Edit", "Cancel"]
3. Only execute the write tool AFTER `ask_user` returns a confirmation choice

### AFTER the user confirms (clicks "Send"):
**IMMEDIATELY call the send/write tool in your very next response.**
Do NOT call ask_user again. Do NOT call memory_read. Do NOT narrate.
Just call the tool (messages_send, mail_send, calendar_create_event, etc.).
The user already confirmed — asking again is a bug.

### Read operations are safe — execute immediately:
- messages_read, messages_list_chats, calendar_list_events, calendar_list_calendars,
  contacts_search, mail_read, mail_search, notes_search

## Workflow Patterns

**Sending a message:**
1. `contacts_search` to resolve the recipient (skip if you already know them)
2. If multiple matches → `ask_user` with each match as a choice (include phone/email in choice text)
3. Preview message text, then `ask_user` with ["Send", "Edit", "Cancel"]
4. When ask_user returns "Send" → IMMEDIATELY call `messages_send` in your next response. No extra steps.

**Creating a calendar event (including from dragged email/attachment):**
1. Parse event details from the user's request or attached content
2. `calendar_list_calendars` to see available calendars
3. `ask_user` showing event details + calendar choices
4. After confirmation → `calendar_create_event`

**Email workflow:**
1. `contacts_search` or use provided email address
2. If multiple matches → `ask_user` to pick recipient
3. Preview full email, then `ask_user` with ["Send", "Cancel"]
4. After confirmation → `mail_send`

**Reading/summarizing (no ask_user needed):**
- For message summaries: `messages_read` → summarize in response
- For calendar overview: `calendar_list_events` → format nicely
- For mail check: `mail_read` or `mail_search` → summarize

## Date Handling
Interpret natural language dates relative to today. Examples:
- "tomorrow at 2pm" → next day, 14:00
- "next Tuesday" → the upcoming Tuesday
- "Friday morning" → upcoming Friday at 9:00 AM
Pass dates in ISO 8601 format or natural language — the tools handle both.
"""

ICLOUD_MACOS_SKILL = Skill(
    name="icloud_macos",
    label="iCloud & macOS",
    description="Access Messages, Calendar, Contacts, Mail, Notes, Reminders on your Mac",
    icon="apple.logo",
    tool_allowlist=[
        # Base tools the model may still need
        "bash", "read_file", "write_file", "glob_search", "grep_search",
        "memory_read", "memory_list", "ask_user",
        # iCloud / macOS tools
        "messages_send", "messages_read", "messages_list_chats",
        "calendar_create_event", "calendar_list_events", "calendar_list_calendars",
        "contacts_search", "contacts_create",
        "mail_send", "mail_read", "mail_search",
        "notes_create", "notes_search",
        "reminders_create",
    ],
    prompt_fragment=_ICLOUD_MACOS_FRAGMENT,
    activation_keywords=[
        "send message", "send text", "send imessage", "text message",
        "send him", "send her", "send them", "send it to",
        "calendar", "create event", "schedule", "appointment",
        "contacts", "contact list", "find contact",
        "send email", "compose email", "check mail", "inbox",
        "create note", "save note", "notes app",
        "remind me", "create reminder", "set reminder",
        "group chat", "messages app", "imessage",
    ],
    activation_phrases=[
        r"\b(send|write|text|forward) (a |an )?(message|text|imessage|sms|email|mail)\b",
        r"\b(create|add|schedule|make|set up|book) (a |an )?(calendar |)(event|appointment|meeting)\b",
        r"\b(check|view|show|what.s on|look at) (my )?calendar\b",
        r"\b(search|find|look up|who is|what.s) .{0,20}(contact|number|phone|email)\b",
        r"\b(read|check|show|view|get) (my )?.{0,20}(email|mail|inbox|messages)\b",
        r"\b(save|create|make|write|add) (a |an )?(note|reminder)\b",
        r"\bremind me\b",
        r"\b(summarize|summary of) .{0,30}(chat|conversation|group|thread|messages)\b",
        r"\b(doctor|dentist|flight|meeting|booking).{0,30}(calendar|schedule|event)\b",
    ],
    enable_thinking=True,
)


_SCRIPTS_DIR = '"$HOME/Library/Application Support/Clyde/agent/skills/bulk-organizer/scripts"'

_ORGANIZER_FRAGMENT = """# Organizer Mode

You are autonomous. The system auto-continues your work — you never need
to wait for the user to say "continue". Just keep calling tools until the
job is done or you need a real decision from the user.

## Tool surface (v3 — 4 compound tools)

You have exactly four organizer tools, gated by phase. Use only the one
exposed to you on each call:

  - **`organize_ingest`** (phase: ingest) — parse a source file or
    directory into items. Creates the SOURCE graph node and returns
    representative samples for taxonomy proposal.
  - **`organize_propose_taxonomy`** (phase: taxonomy) — submit a category
    list. Blocks while the user approves it via the `ask_user` UI.
  - **`organize_classify_batch`** (phase: classify) — read the next batch
    of items (call with no `classifications`) or write classifications
    for the current batch and read the next one (call with
    `classifications`). Repeat until the tool reports DONE.
  - **`organize_export`** (phase: export) — generate the final output
    file from the saved classifications.

There are NO `organize_batch_read`, `organize_batch_write`,
`organize_update_graph`, or `parse_bookmarks` tools. State is owned by
the four compound tools above — do not try to read or write state files
directly with `read_file` / `write_file`.

## MANDATORY: Use ask_user for user decisions
- When confirming the final export: call ask_user.
- NEVER present numbered options as plain text. The user can ONLY
  interact through ask_user buttons.

## TAXONOMY PROPOSAL FORMAT
Before calling `organize_propose_taxonomy`, describe the proposed taxonomy
in plain text — list every category with count and a few examples — so
the user can read it. Then call `organize_propose_taxonomy` with the
JSON `categories` array; the tool itself collects the user's approval.

## CRITICAL: Classification MUST happen in-context
- NEVER write external Python scripts to classify items. No
  classify_*.py, no scripts that call the LLM API, no scripts that
  import requests/httpx to hit /v1/chat/completions. This crashes
  the backend every time.
- Classification works like this:
  1. Call `organize_classify_batch` (no `classifications`) to get a batch.
  2. YOU (the model) classify each item by looking at its title/URL.
  3. Call `organize_classify_batch` again with your `classifications`
     array — the tool writes them and returns the NEXT batch.
  4. Repeat until the tool reports DONE.
- You are the classifier. Use your own judgment on title + URL to
  assign categories. Do NOT delegate classification to external scripts.

## Asset Graph
The asset graph tracks your entire workflow state. It persists across
compactions, so if context is compacted you can recover by reading the
graph. The graph contains:
- Task node: current phase, progress (classified/total), state_dir
- Category nodes: each category with item counts, connected to task
- Phase nodes: workflow phase transitions
If you see "RESUME:" in a recovery message, follow it immediately.

## Workflow
1. **ingest** — call `organize_ingest(source_path=..., item_type=...)`.
   The tool parses, dedups, samples, and creates the graph nodes. Use
   the returned samples to design the taxonomy.
2. **taxonomy** — write a plain-text taxonomy summary, then call
   `organize_propose_taxonomy(categories=<JSON array>)`. Wait for the
   user's approval (the tool blocks until answered).
3. **classify** — loop `organize_classify_batch` calls. Read → classify
   → write+next. Stop when the tool reports DONE.
4. **export** — call `organize_export(format=..., output_path=...)` to
   produce the final file. Tell the user the output path.
"""

ORGANIZER_SKILL = Skill(
    name="organizer",
    label="Organizer",
    description="Bulk-organize large collections — bookmarks, files, photos",
    icon="folder.badge.gearshape",
    tool_allowlist=_SHARED_BASE_TOOLS + [
        "organize_ingest",
        "organize_propose_taxonomy",
        "organize_classify_batch",
        "organize_export",
        "bookmark_extract_all",
        "bookmark_locator",
        "graph_read",
        "graph_write",
    ],
    prompt_fragment=_ORGANIZER_FRAGMENT,
    activation_keywords=[
        "organize", "sort", "bookmarks", "rename photos",
        "classify files", "bulk organize", "clean up folder",
        "sort my bookmarks", "organize files", "organize photos",
        "bookmark folders", "categorize",
    ],
    activation_phrases=[
        r"\b(organize|sort|classify|categorize) (my |the |these )?(bookmarks|files|photos|documents|folder)\b",
        r"\bbulk (organize|sort|rename|classify)\b",
        r"\b(clean|tidy) up (my |the )?(folder|directory|desktop|downloads)\b",
        r"\brename (my |the |all |all my |all the )?(photos|files|images)\b",
        r"\b(sort|organize) .{0,20}(into|by) (folders|categories|tags)\b",
    ],
    enable_thinking=True,
    max_tool_calls_per_turn=2,
    # v3 phase-gated workflow — compound tools enforce read→write atomicity
    shared_tools=_SHARED_BASE_TOOLS + ["graph_read", "graph_write"],
    phases=[
        SkillPhase(
            name="ingest",
            tools=["organize_ingest", "bookmark_extract_all", "bookmark_locator", "web_fetch", "web_search"],
            prompt_hint=(
                "INGEST PHASE: Find and parse the source data.\n"
                "1. For bookmarks: call bookmark_extract_all to find bookmark files, "
                "then call organize_ingest with the HTML path.\n"
                "2. For files/folders: call organize_ingest with the directory path "
                "and item_type='files'.\n"
                "organize_ingest will parse, dedup, sample, and create graph nodes "
                "automatically. It returns samples — use them to propose categories."
            ),
            exit_condition="items_ingested",
            max_iterations=10,
        ),
        SkillPhase(
            name="taxonomy",
            tools=["organize_propose_taxonomy", "web_fetch", "web_search"],
            prompt_hint=(
                "TAXONOMY PHASE: Based on the samples from ingest, propose 8-15 categories.\n"
                "Call organize_propose_taxonomy with a JSON array of categories.\n"
                "The tool will ask the user for approval (blocks until they respond).\n"
                "Categories should be specific to this collection, not generic.\n"
                "Each category needs: key (snake_case), name (display), description."
            ),
            exit_condition="taxonomy_approved",
            max_iterations=5,
            inference_cap=2,
        ),
        SkillPhase(
            name="classify",
            tools=["organize_classify_batch", "web_fetch", "web_search"],
            prompt_hint=(
                "CLASSIFY PHASE: Classify all items in batches of 15.\n"
                "1. First call: organize_classify_batch (no classifications) — reads first batch.\n"
                "2. Each subsequent call: pass classifications for current batch — "
                "tool writes them AND returns the next batch automatically.\n"
                "Pattern: read → classify → call with classifications → get next batch → repeat.\n"
                "The tool tracks progress and creates graph nodes. Continue until 100%.\n\n"
                "WEB ENRICHMENT (optional): If an item's title/URL is too cryptic to classify "
                "confidently, use web_fetch or web_search to look it up. Mark those as "
                "confidence='verified'. Only do this for genuinely ambiguous items — "
                "don't slow down the batch for items you can classify from title+URL alone."
            ),
            exit_condition="classify_complete",
            max_iterations=60,
            inference_cap=2,
        ),
        SkillPhase(
            name="export",
            tools=["organize_export", "ask_user"],
            prompt_hint=(
                "EXPORT PHASE: Generate the organized output file.\n"
                "Call organize_export to create the output. Then ask the user "
                "if they want to import it back (e.g., replace browser bookmarks)."
            ),
            exit_condition="delivered",
            max_iterations=5,
        ),
    ],
)

# Creative Writing skill (NEW — v2 phase-gated)
_CREATIVE_FRAGMENT = """# Creative Writing Mode

Write polished creative prose through a structured workflow.
The skill enforces: research → outline → draft → revise → deliver.
You cannot skip phases — each phase gives you different tools.
"""

CREATIVE_WRITING_SKILL = Skill(
    name="creative_writing",
    label="Creative Writing",
    description="Short stories, scripts, poetry — structured creative workflow",
    icon="text.book.closed",
    tool_allowlist=_SHARED_BASE_TOOLS + [
        "web_search", "web_fetch", "record_fact",
        "creative_outline", "draft_section", "creative_review",
        "graph_read", "graph_write",
    ],
    prompt_fragment=_CREATIVE_FRAGMENT,
    activation_keywords=[
        "story", "fiction", "novel", "chapter", "poem", "poetry",
        "screenplay", "script", "fanfic", "short story",
    ],
    activation_phrases=[
        r"\b(write|create|draft)\b.{0,20}\b(story|fiction|novel|chapter|script|screenplay|poem|short story|fanfic)\b",
        r"\b(short story|creative writing|fiction)\b",
    ],
    enable_thinking=True,
    shared_tools=_SHARED_BASE_TOOLS + ["graph_read", "graph_write", "memory_read"],
    phases=[
        SkillPhase(
            name="research",
            tools=["web_search", "web_fetch", "record_fact"],
            prompt_hint=(
                "RESEARCH PHASE: Look up characters, settings, or source material "
                "the user referenced. Record each key detail as a fact with "
                "record_fact. Need at least 3 facts before outlining. "
                "For pure original fiction, record 3 facts about the premise/genre."
            ),
            exit_condition="has_outline",  # advances when outline exists (set in next phase)
            max_iterations=15,
        ),
        SkillPhase(
            name="outline",
            tools=["creative_outline"],
            prompt_hint=(
                "OUTLINE PHASE: Create a structured outline. Use creative_outline "
                "with title, premise, sections (one per page/chapter with summary), "
                "target_words. Every element from the user's request MUST appear "
                "in at least one section. Cannot draft until outline exists."
            ),
            exit_condition="has_outline",
            max_iterations=5,
        ),
        SkillPhase(
            name="draft",
            tools=["draft_section", "read_file"],
            prompt_hint=(
                "DRAFT PHASE: Write ONE section at a time via draft_section. "
                "400-800 words per section. Include dialogue, sensory detail. "
                "Do NOT repeat phrases. Reference recorded facts for accuracy."
            ),
            exit_condition="all_sections_drafted",
            max_iterations=30,
            inference_cap=2,
        ),
        SkillPhase(
            name="revise",
            tools=["read_file", "edit_file", "creative_review"],
            prompt_hint=(
                "REVISION PHASE: Read full draft. Check premise adherence, "
                "character coverage, repetition, word count. Use edit_file "
                "to fix. Call creative_review for structured quality report."
            ),
            exit_condition="revision_done",
            max_iterations=10,
        ),
        SkillPhase(
            name="deliver",
            tools=["read_file", "ask_user"],
            prompt_hint=(
                "DELIVERY PHASE: Present finished story to user. Read the file, "
                "give brief summary. Call graph_write action=verify."
            ),
            exit_condition="delivered",
            max_iterations=3,
        ),
    ],
)

_SKILLS: dict[str, Skill] = {
    s.name: s for s in (
        GENERAL_SKILL,
        RESEARCH_SKILL,
        QUICK_RESEARCH_SKILL,
        CODE_SKILL,
        MEMORY_SKILL,
        ICLOUD_MACOS_SKILL,
        ORGANIZER_SKILL,
        CREATIVE_WRITING_SKILL,
    )
}

DEFAULT_SKILL_NAME = GENERAL_SKILL.name


def all_skills() -> list[Skill]:
    """Return every skill in a stable display order."""
    order = ["general", "quick_research", "research", "creative_writing", "code", "memory", "icloud_macos", "organizer"]
    return [_SKILLS[n] for n in order if n in _SKILLS]


def get_skill(name: str | None) -> Skill:
    """Fetch a skill by name; fall back to the default if unknown."""
    if name and name in _SKILLS:
        return _SKILLS[name]
    return _SKILLS[DEFAULT_SKILL_NAME]


def serialize_skill(skill: Skill) -> dict:
    """JSON-safe view for API responses."""
    return {
        "name": skill.name,
        "label": skill.label,
        "description": skill.description,
        "icon": skill.icon,
        "tools": list(skill.tool_allowlist),
        "enable_thinking": skill.enable_thinking,
        "enforces_research_budget": skill.enforce_research_budget,
        "requires_planning": skill.require_planning,
    }


# ─── Heuristic router ─────────────────────────────────────────────────

def route_skill_llm(
    user_text: str,
    backend_url: str,
    model: str | None = None,
    timeout: float = 3.0,
) -> str | None:
    """Phase A: LLM-based skill router fallback.

    When the regex/keyword router (``route_skill``) cannot pick a non-default
    skill, fall back to a quick local-model completion that names the best
    skill for the user's request. Returns a valid skill name string, or
    None on timeout / parse failure / no confident match.

    The call is deliberately tiny — short system prompt, ``max_tokens=16``,
    ``temperature=0`` — so the round-trip on the local backend stays in
    the low-hundred-millisecond range. On any failure we silently return
    None and the caller falls through to the regex result.
    """
    if not user_text or not backend_url:
        return None

    valid_names = {s.name for s in all_skills()}
    skills_md = "\n".join(
        f"- {s.name} — {s.description}" for s in all_skills()
    )

    sys_prompt = (
        "You route user requests to one skill. Reply with ONLY the skill "
        "name — no explanation, no quotes, no punctuation.\n\n"
        f"Available skills:\n{skills_md}\n\n"
        f"Reply with exactly one of: {', '.join(sorted(valid_names))}"
    )

    payload = {
        "model": model or "default",
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": 16,
        "temperature": 0,
        "stream": False,
    }

    try:
        import httpx
        r = httpx.post(
            f"{backend_url.rstrip('/')}/v1/chat/completions",
            json=payload,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        raw = data["choices"][0]["message"]["content"]
        if not isinstance(raw, str):
            return None
        text = raw.strip().lower()
        # Model may add minor fluff ("research.", "research skill", etc.) —
        # tokenize and pick the first valid skill name.
        for tok in re.split(r"[\s,.\"'`:]+", text):
            if tok in valid_names:
                return tok
    except Exception:
        return None
    return None


def route_skill(user_text: str) -> str:
    """Pick a skill name from the user's message using cheap heuristics.

    Priority: explicit phrase patterns > keyword hits > default. Research
    is intentionally the **most restrictive** match — it requires clear
    paper-writing phrasing. "Find me a bunch of articles" → quick
    research, not research.
    """
    if not user_text:
        return DEFAULT_SKILL_NAME
    text = user_text.lower()

    # Phase 1: explicit phrase patterns (most specific first)
    # Research must match a phrase pattern, not just a keyword — this
    # prevents "find articles" from triggering the full paper pipeline.
    # iCloud/macOS checked first — "send a message" is very specific.
    for skill in (ICLOUD_MACOS_SKILL, ORGANIZER_SKILL, RESEARCH_SKILL, MEMORY_SKILL, CODE_SKILL, QUICK_RESEARCH_SKILL):
        for pattern in skill.activation_phrases:
            if re.search(pattern, text):
                return skill.name

    # Phase 2: keyword hits (non-research skills only — research needs
    # a phrase match, not a bare keyword)
    for skill in (ICLOUD_MACOS_SKILL, ORGANIZER_SKILL, MEMORY_SKILL, CODE_SKILL, QUICK_RESEARCH_SKILL):
        for kw in skill.activation_keywords:
            if kw in text:
                return skill.name

    return DEFAULT_SKILL_NAME


# ─── Per-conversation skill state ─────────────────────────────────────
# Tracks which skill is active for each conversation plus whether the
# user has disabled auto-routing. Thread-safe because FastAPI serves
# endpoints concurrently with conversation worker threads.

_state_lock = threading.Lock()


@dataclass(slots=True)
class ConversationSkillState:
    conversation_id: str
    skill_name: str = DEFAULT_SKILL_NAME
    auto_route: bool = True   # If false, stay on skill_name until user changes it
    user_locked: bool = False  # True once user explicitly picked (or cleared) a skill
    last_routed_turn: int = -1  # Message count at which we last ran the router
    phase_state: SkillState = field(default_factory=SkillState)  # v2 phase tracking


_conversation_state: dict[str, ConversationSkillState] = {}


def get_or_init_state(conversation_id: str) -> ConversationSkillState:
    with _state_lock:
        st = _conversation_state.get(conversation_id)
        if st is None:
            st = ConversationSkillState(conversation_id=conversation_id)
            _conversation_state[conversation_id] = st
        return st


def get_phase_state(conversation_id: str | None) -> SkillState:
    """Get the v2 phase state for a conversation."""
    if not conversation_id:
        return SkillState()
    st = get_or_init_state(conversation_id)
    return st.phase_state


def get_phase_tools(conversation_id: str | None) -> list[str] | None:
    """Get the tool list for the current phase, or None for legacy skills."""
    if not conversation_id:
        return None
    skill = get_active_skill(conversation_id)
    state = get_phase_state(conversation_id)
    return skill.get_phase_tools(state)


def get_phase_hint(conversation_id: str | None) -> str:
    """Get the prompt hint for the current phase."""
    if not conversation_id:
        return ""
    skill = get_active_skill(conversation_id)
    state = get_phase_state(conversation_id)
    return skill.get_phase_hint(state)


def notify_tool_result(conversation_id: str | None, tool_name: str, result: str) -> str | None:
    """Update phase state after a tool call. Returns new phase name if advanced."""
    if not conversation_id:
        return None
    skill = get_active_skill(conversation_id)
    state = get_phase_state(conversation_id)
    state.on_tool_result(tool_name, result)
    state.phase_iterations += 1
    return skill.advance_phase(state)


def get_active_skill(conversation_id: str | None) -> Skill:
    if not conversation_id:
        return get_skill(DEFAULT_SKILL_NAME)
    st = get_or_init_state(conversation_id)
    return get_skill(st.skill_name)


def set_active_skill(
    conversation_id: str,
    skill_name: str | None,
    *,
    user_initiated: bool = True,
    disable_auto: bool | None = None,
) -> ConversationSkillState:
    """Set the active skill for a conversation.

    user_initiated=True locks the skill (auto-routing stops affecting it
    until the user explicitly re-enables auto).
    skill_name=None means 'no skill' — effectively the General default,
    with auto-routing disabled per the user's rule ("if user deactivates
    a skill mid-chat, option appears to toggle auto-skills off").
    """
    with _state_lock:
        st = _conversation_state.setdefault(
            conversation_id, ConversationSkillState(conversation_id=conversation_id)
        )
        resolved = skill_name if (skill_name and skill_name in _SKILLS) else DEFAULT_SKILL_NAME
        st.skill_name = resolved
        if user_initiated:
            st.user_locked = True
            # When user deactivates (skill_name None) — default to disabling auto
            if skill_name is None and disable_auto is None:
                disable_auto = True
        if disable_auto is not None:
            st.auto_route = not disable_auto
        return st


def set_auto_route(conversation_id: str, enabled: bool) -> ConversationSkillState:
    with _state_lock:
        st = _conversation_state.setdefault(
            conversation_id, ConversationSkillState(conversation_id=conversation_id)
        )
        st.auto_route = enabled
        return st


def auto_route_for_turn(
    conversation_id: str,
    user_text: str,
    turn_index: int,
    backend_url: str | None = None,
    model: str | None = None,
) -> Skill:
    """Called by conversation.py at the top of each user turn.

    Routing strategy:
      1. Regex/keyword router (cheap, fast, deterministic).
      2. **Phase A LLM fallback**: if regex returned default AND we're not
         already on a non-default skill, ask the local model to pick. This
         catches semantic intent the regex misses (e.g. "do deeper research
         and write me a paper" — pronouns/qualifiers throw off the patterns).
      3. **Sticky**: if everything still resolves to default but the
         conversation is mid-flight on a non-default skill, keep it.
         Prevents "send him this gif too" reverting from icloud_macos.
    """
    st = get_or_init_state(conversation_id)
    if not st.auto_route or st.user_locked:
        return get_skill(st.skill_name)

    picked = route_skill(user_text)

    # Phase A — LLM fallback. Only trigger when regex landed on default and
    # we're not stickily holding a non-default skill from a prior turn.
    if (
        picked == DEFAULT_SKILL_NAME
        and st.skill_name == DEFAULT_SKILL_NAME
        and backend_url
    ):
        llm_pick = route_skill_llm(user_text, backend_url=backend_url, model=model)
        if llm_pick and llm_pick in _SKILLS and llm_pick != DEFAULT_SKILL_NAME:
            picked = llm_pick

    # Sticky: keep current non-default skill if the new message looks default.
    if picked == DEFAULT_SKILL_NAME and st.skill_name != DEFAULT_SKILL_NAME:
        return get_skill(st.skill_name)

    st.skill_name = picked
    st.last_routed_turn = turn_index
    return get_skill(picked)


def serialize_state(st: ConversationSkillState) -> dict:
    return {
        "conversation_id": st.conversation_id,
        "active_skill": st.skill_name,
        "auto_route": st.auto_route,
        "user_locked": st.user_locked,
    }


# ─── Tool filtering helper ────────────────────────────────────────────

def filter_tool_definitions(
    all_defs: Iterable[dict],
    skill: Skill,
) -> list[dict]:
    """Return only the OpenAI tool definitions whose function name is in
    ``skill.tool_allowlist``. Preserves original order.

    ``switch_skill`` is the meta-tool the model uses to self-correct mid-
    conversation (Phase B). It is always exposed regardless of the active
    skill's allowlist so the model can switch *out* of any skill.
    """
    allowed = set(skill.tool_allowlist)
    allowed.add("switch_skill")
    out = []
    for d in all_defs:
        fn = (d or {}).get("function", {})
        name = fn.get("name") if isinstance(fn, dict) else None
        if name and name in allowed:
            out.append(d)
    return out
