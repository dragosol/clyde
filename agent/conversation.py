"""
Clyde — Conversation Runtime
====================================
Aligned with claw-code: rust/crates/runtime/src/conversation.rs

ConversationRuntime:
  - Holds session state, API client, tool executor
  - run_turn(): user input → API call → parse tool uses → execute → loop
  - Compaction when context grows too large
  - Session persistence
"""

from __future__ import annotations
import json


class _P22SalvageRefused(Exception):
    """P22: raised to abort salvage when fact gate not met."""
    pass


import os
import re
import time
import logging
import threading
import uuid
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Callable

import httpx

from session import (
    Session, ConversationMessage, TextBlock, ToolUseBlock, ToolResultBlock
)
from tools import ToolRegistry, set_context_budget, estimate_file_tokens
from system_prompt import render_system_prompt
from memory import save_transcript
import skills as skills_mod

log = logging.getLogger("conversation")


@dataclass
class TurnSummary:
    """Result of a single conversation turn."""
    assistant_text: str
    tool_calls_made: int
    iterations: int


class ConversationRuntime:
    """
    Core agent loop. Aligned with claw-code ConversationRuntime<C, T>.

    Flow:
      1. User sends message
      2. System prompt (with memory index) is prepended
      3. Request sent to MLX backend
      4. Parse response: if tool_calls → execute → feed results back → loop
      5. If no tool_calls → return final text to user
      6. Save transcript
    """

    def __init__(
        self,
        backend_url: str,
        backend_model: str,
        max_iterations: int = 100,
        max_tokens: int = 32768,
        temperature: float = 0.7,
        profile: "ModelProfile | None" = None,
        profile_registry: "ModelProfileRegistry | None" = None,
    ):
        self.backend_url = backend_url
        self._initial_backend_url = backend_url  # for stale-check
        self.backend_model = backend_model
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.temperature = temperature
        # Phase-A backend abstraction (ADR-001): resolve profile from registry
        # or caller. `profile` is None in legacy deployments; all helpers below
        # fall back to hardcoded values in that case.
        self.profile = profile
        if self.profile is None:
            try:
                if profile_registry is None:
                    from agent.model_profiles.registry import ModelProfileRegistry
                    profile_registry = ModelProfileRegistry()
                self.profile = profile_registry.resolve(backend_model)
                log.info(
                    f"ModelProfile resolved: {self.profile.id} "
                    f"(family={self.profile.family}, thinking_directive={self.profile.thinking_directive})"
                )
            except Exception as exc:
                log.warning(f"ModelProfile resolution failed ({exc!r}); using legacy hardcoded paths")
                self.profile = None
        self.tool_registry = ToolRegistry()
        self.session = Session()
        self._session_dir: Path | None = None  # Set by agent.py for persistence
        self._conversation_id: str | None = None  # Set by agent.py

        # ask_user: question/answer synchronization
        self._question_event = threading.Event()  # Signals when answer arrives
        self._pending_question_id: str | None = None
        self._pending_answer: str | None = None

        # folder_permission: synchronization (same pattern as ask_user)
        self._permission_event = threading.Event()
        self._pending_permission_id: str | None = None
        self._permission_granted: bool = False
        self._permission_emit = None  # Will be set to on_event during run_turn


    # ─── Phase-A backend abstraction helpers (ADR-001) ───

    # Legacy hardcoded floors — used when no profile is available OR when
    # profile doesn't specify a floor for a phase. Shapes match iter22/23.
    _LEGACY_FLOORS_THINKING_ON = {
        "writing": 10000, "review": 10000, "synthesis": 6000,
        "scoping": 6000, "research": 6000,
    }
    _LEGACY_FLOORS_THINKING_OFF = {
        "writing": 6000, "review": 6000, "synthesis": 3000,
        "scoping": 2048, "research": 2048,
    }

    def _max_tokens_floor_for_phase(self, phase: str, thinking_on: bool) -> int:
        """Resolve max_tokens floor for a phase.

        Order of precedence:
          1. profile.phase_overrides[phase].max_tokens_floor × thinking multiplier
             (multiplier comes from profile.chat_template_quirks;
              thinking_max_tokens_multiplier default 1.0 keeps value unchanged)
          2. legacy hardcoded tables (iter22/23 parity)
        """
        if self.profile is not None:
            po = self.profile.phase_overrides.get(phase)
            if po is not None and getattr(po, "max_tokens_floor", None):
                base = int(po.max_tokens_floor)
                if thinking_on:
                    mult = float(
                        (self.profile.chat_template_quirks or {})
                        .get("thinking_max_tokens_multiplier", 1.0)
                    )
                    return int(round(base * mult))
                return base
        table = self._LEGACY_FLOORS_THINKING_ON if thinking_on else self._LEGACY_FLOORS_THINKING_OFF
        return table.get(phase, 2048 if not thinking_on else 6000)

    def _thinking_payload(self, thinking_on: bool) -> dict:
        """Build the per-request payload fragment that controls thinking mode.

        Dispatches on ModelProfile.thinking_directive:
          - chat_template_kwarg: {"chat_template_kwargs": {<key>: bool}}
          - system_prompt_marker (Qwen): no body kwarg (marker is injected upstream)
          - force_template (llama.cpp): backend picks the template via extras
          - not_supported: no-op

        Legacy fallback: when profile is None, matches iter22/23 exactly —
        {"chat_template_kwargs": {"enable_thinking": thinking_on}}.
        """
        if self.profile is None:
            # mlx_vlm.server reads `enable_thinking` at the top level; llama.cpp
            # reads it nested under chat_template_kwargs. Emit both so the same
            # payload works across both backend types.
            return {
                "chat_template_kwargs": {"enable_thinking": thinking_on},
                "enable_thinking": thinking_on,
            }
        directive = self.profile.thinking_directive
        payload_cfg = self.profile.thinking_directive_payload or {}
        if directive == "chat_template_kwarg":
            key = payload_cfg.get("key", "enable_thinking")
            nested = payload_cfg.get("nested_under", "chat_template_kwargs")
            return {nested: {key: thinking_on}, key: thinking_on}
        if directive == "system_prompt_marker":
            # Marker injection happens in the system_prompt path, not in the
            # body kwargs. Nothing to add here.
            return {}
        if directive == "force_template":
            # llama.cpp-style: pick a chat template name per mode.
            on_tpl = payload_cfg.get("template_on")
            off_tpl = payload_cfg.get("template_off")
            tpl = on_tpl if thinking_on else off_tpl
            return {"chat_template": tpl} if tpl else {}
        # not_supported / unknown
        return {}

    def _refresh_backend_url(self) -> None:
        """Re-resolve backend URL if the current one is unreachable.

        Only fires when the current backend_url doesn't respond to a
        quick probe. When it does fire, reads the router's current
        default route endpoint. This avoids the stale-port problem
        without triggering router re-initialization.
        """
        try:
            # Quick probe — if current backend responds, don't touch anything
            import httpx
            try:
                with httpx.Client(timeout=1.5) as hc:
                    r = hc.get(f"{self.backend_url}/v1/models")
                if r.status_code == 200:
                    return  # Current backend alive, no change needed
            except Exception:
                pass  # Dead — try to re-resolve

            # Read the router's resolved state without re-initializing.
            # agent._ROUTER is already initialized at startup; we just
            # read its cached state here.
            import sys
            agent_mod = sys.modules.get("agent")
            if agent_mod is None:
                return
            router = getattr(agent_mod, "_ROUTER", None)
            if router is None:
                return
            route = router.resolve(None)
            new_url = route.backend.endpoint
            new_model = route.backend_model_id
            if new_url != self.backend_url:
                log.info(
                    f"Backend URL refreshed: {self.backend_url} → {new_url} "
                    f"(model: {self.backend_model} → {new_model})"
                )
                self.backend_url = new_url
                self.backend_model = new_model
        except Exception as e:
            log.debug(f"_refresh_backend_url failed: {e}")

    def _get_phase_filtered_tools(self) -> list[dict]:
        """Return tool definitions filtered by the current skill phase.

        If the active skill has phases, only tools for the current phase
        are returned. The model literally cannot call tools from other
        phases — they don't exist in the registry it sees.
        """
        from skills import get_phase_tools, get_active_skill
        phase_tools = get_phase_tools(self._conversation_id)
        skill = get_active_skill(self._conversation_id)

        if phase_tools is not None:
            # Phase-gated: filter to only the phase's tools
            all_defs = self.tool_registry.definitions(skill=skill)
            return [t for t in all_defs
                    if t.get("function", {}).get("name") in phase_tools]
        # Legacy: return all tools for the skill
        return self.tool_registry.definitions(skill=skill)

    def _get_think_grammar(self, phase: str) -> str | None:
        """Return a GBNF grammar for structured thinking, or None to skip.

        Constrains the <think> block to a few structured lines instead of
        3000+ tokens of verbose CoT. 22x fewer thinking tokens, same
        accuracy (andthattoo 2026-04-25, HumanEval+ / LiveCodeBench).

        Grammar is skill/phase-dependent — each task type gets a focused
        scratchpad format. The answer/tool-call after </think> is fully
        unconstrained so the model can emit any content.

        Only effective on llama.cpp (native GBNF support at sampler level).
        mlx_vlm ignores unknown keys — grammar silently drops. Future:
        implement logit masking in the mlx generate loop.
        """
        # Env var kill switch: CLYDE_THINK_GRAMMAR=off disables
        if os.environ.get("CLYDE_THINK_GRAMMAR", "on").lower() == "off":
            return None

        # Get active skill for skill-specific grammar
        try:
            from skills import get_active_skill
            _skill = get_active_skill(self._conversation_id)
            _skill_name = _skill.name if _skill else "general"
        except Exception:
            _skill_name = "general"

        # Skill-specific grammars. Each forces the think block into a
        # compact structured format. The `answer` production is fully
        # permissive — model can emit any text, tool calls, markdown, etc.
        _GRAMMARS = {
            # General / code: GOAL → APPROACH → EDGE CASES
            "general": (
                'root   ::= think answer\n'
                'think  ::= "<think>\\n" '
                '"GOAL: " line '
                '"APPROACH: " line '
                '"EDGE: " line '
                '"</think>\\n\\n"\n'
                'line   ::= [^\\n]+ "\\n"\n'
                'answer ::= [\\x09\\x0A\\x0D\\x20-\\x7E\\xC0-\\xFF]+\n'
            ),
            "code": (
                'root   ::= think answer\n'
                'think  ::= "<think>\\n" '
                '"GOAL: " line '
                '"APPROACH: " line '
                '"EDGE: " line '
                '"</think>\\n\\n"\n'
                'line   ::= [^\\n]+ "\\n"\n'
                'answer ::= [\\x09\\x0A\\x0D\\x20-\\x7E\\xC0-\\xFF]+\n'
            ),
            # Research: what to find + where + how to organize
            "research": (
                'root   ::= think answer\n'
                'think  ::= "<think>\\n" '
                '"TASK: " line '
                '"SOURCES: " line '
                '"PLAN: " line '
                '"</think>\\n\\n"\n'
                'line   ::= [^\\n]+ "\\n"\n'
                'answer ::= [\\x09\\x0A\\x0D\\x20-\\x7E\\xC0-\\xFF]+\n'
            ),
            "quick_research": (
                'root   ::= think answer\n'
                'think  ::= "<think>\\n" '
                '"QUERY: " line '
                '"PLAN: " line '
                '"</think>\\n\\n"\n'
                'line   ::= [^\\n]+ "\\n"\n'
                'answer ::= [\\x09\\x0A\\x0D\\x20-\\x7E\\xC0-\\xFF]+\n'
            ),
            # Creative writing: premise + structure + what to include
            "creative_writing": (
                'root   ::= think answer\n'
                'think  ::= "<think>\\n" '
                '"PREMISE: " line '
                '"STRUCTURE: " line '
                '"ELEMENTS: " line '
                '"</think>\\n\\n"\n'
                'line   ::= [^\\n]+ "\\n"\n'
                'answer ::= [\\x09\\x0A\\x0D\\x20-\\x7E\\xC0-\\xFF]+\n'
            ),
            # Organizer: what to classify + strategy
            "organizer": (
                'root   ::= think answer\n'
                'think  ::= "<think>\\n" '
                '"TASK: " line '
                '"STRATEGY: " line '
                '"</think>\\n\\n"\n'
                'line   ::= [^\\n]+ "\\n"\n'
                'answer ::= [\\x09\\x0A\\x0D\\x20-\\x7E\\xC0-\\xFF]+\n'
            ),
            # Memory: what to recall + why
            "memory": (
                'root   ::= think answer\n'
                'think  ::= "<think>\\n" '
                '"RECALL: " line '
                '"PURPOSE: " line '
                '"</think>\\n\\n"\n'
                'line   ::= [^\\n]+ "\\n"\n'
                'answer ::= [\\x09\\x0A\\x0D\\x20-\\x7E\\xC0-\\xFF]+\n'
            ),
        }

        return _GRAMMARS.get(_skill_name, _GRAMMARS["general"])

    # ─── Session Persistence ───

    def _session_path(self) -> Path | None:
        """Return the file path for this conversation's session, or None."""
        if self._session_dir and self._conversation_id:
            return self._session_dir / f"{self._conversation_id}.json"
        return None

    def save_session(self):
        """Persist session state to disk. Called after each turn."""
        path = self._session_path()
        if not path:
            return
        try:
            self.session.save(path)
            log.info(f"Session saved: {path.name} ({len(self.session.messages)} messages, ~{self.session.estimate_tokens()} tokens)")
        except Exception as e:
            log.error(f"Failed to save session: {e}")

    def load_session(self) -> bool:
        """
        Try to load a previous session from disk. Returns True if loaded.
        Called when resuming a conversation after agent restart.
        """
        path = self._session_path()
        if not path or not path.exists():
            return False
        try:
            loaded = Session.load(path)
            self.session = loaded
            log.info(
                f"Session restored: {path.name} "
                f"({len(loaded.messages)} messages, ~{loaded.estimate_tokens()} tokens)"
            )
            return True
        except Exception as e:
            log.error(f"Failed to load session from {path}: {e}")
            return False

    def submit_answer(self, question_id: str, answer: str):
        """
        Submit a user's answer to a pending ask_user question.
        Called from agent.py's /v1/answer endpoint.
        Unblocks the run_turn thread that's waiting for the answer.
        """
        if self._pending_question_id != question_id:
            log.warning(
                f"Answer for question {question_id} doesn't match pending "
                f"{self._pending_question_id}, ignoring"
            )
            return
        self._pending_answer = answer
        self._question_event.set()
        log.info(f"Answer received for question {question_id}: {answer[:100]}")

    def submit_permission(self, permission_id: str, granted: bool, path: str | None = None):
        """
        Submit the user's response to a folder permission request.
        Called from agent.py's /v1/grant_folder endpoint.
        """
        if self._pending_permission_id != permission_id:
            log.warning(
                f"Permission response for {permission_id} doesn't match pending "
                f"{self._pending_permission_id}, ignoring"
            )
            return
        self._permission_granted = granted
        if granted and path:
            from tools import grant_folder
            grant_folder(path)
        self._permission_event.set()
        log.info(f"Permission {'granted' if granted else 'denied'} for {permission_id}: {path}")

    # ─── Preemptive Permission Detection ───

    # Well-known folders under ~ that require permission.
    # Maps lowercased keywords to the folder name under $HOME.
    _KNOWN_FOLDERS = {
        "desktop": "Desktop",
        "documents": "Documents",
        "downloads": "Downloads",
        "pictures": "Pictures",
        "movies": "Movies",
        "music": "Music",
        "applications": "Applications",
        "library": "Library",
        "public": "Public",
        "sites": "Sites",
        ".ssh": ".ssh",
        ".config": ".config",
    }

    # Regex patterns to detect folder references in user messages
    _FOLDER_PATTERNS = [
        # Explicit path references: ~/Desktop, ~/Documents/foo
        re.compile(r'~/(\w[\w.\-]*)'),
        # Phrase references: "my desktop", "the downloads folder", "in documents"
        re.compile(r'\b(?:my|the|in|on|from|to|into|under|inside)\s+(' + '|'.join(_KNOWN_FOLDERS.keys()) + r')\b', re.IGNORECASE),
        # Standalone folder names at word boundaries (capitalized for disambiguation)
        re.compile(r'\b(' + '|'.join(v for v in _KNOWN_FOLDERS.values()) + r')\b'),
        # Absolute paths: /Users/*/Desktop
        re.compile(r'/Users/\w+/(\w[\w.\-]*)'),
    ]

    def _detect_folders_in_message(self, text: str) -> list[Path]:
        """
        Scan a user message for references to folders that might need permission.
        Returns a list of resolved Paths for folders that are NOT yet allowed.
        """
        from tools import get_allowed_dirs, _AGENT_HOME
        home = Path.home()
        allowed = [Path(d) for d in get_allowed_dirs()]
        needed = []

        for pattern in self._FOLDER_PATTERNS:
            for match in pattern.finditer(text):
                folder_name = match.group(1)
                # Map to canonical folder name
                canonical = self._KNOWN_FOLDERS.get(folder_name.lower(), folder_name)
                target = (home / canonical).resolve()

                # Skip if it's the agent home or already allowed
                if target == _AGENT_HOME:
                    continue
                already_allowed = False
                for a in allowed:
                    try:
                        target.relative_to(a)
                        already_allowed = True
                        break
                    except ValueError:
                        continue
                if already_allowed:
                    continue

                # Only include if the folder actually exists on disk
                if target.exists() and target not in needed:
                    needed.append(target)

        return needed

    def _preemptive_permission_check(self, user_input: str, emit):
        """
        Before any tool calls, check if the user's message references folders
        that aren't yet allowed. If so, ask permission upfront via question card.
        """
        needed_folders = self._detect_folders_in_message(user_input)
        if not needed_folders:
            return

        home = Path.home()
        for folder in needed_folders:
            display = str(folder).replace(str(home), "~")
            question_text = (
                f"Clyde needs access to {display}\n\n"
                f"You mentioned this folder. Allow Clyde to read and "
                f"modify files here for this conversation?"
            )

            log.info(f"Preemptive permission request for {folder}")
            q_id = f"q_{uuid.uuid4().hex[:8]}"
            emit("question",
                 id=q_id,
                 question=question_text,
                 choices=["Allow", "Deny"],
                 allow_other=False)

            self._pending_question_id = q_id
            self._question_event.clear()
            # Short timeout — if user doesn't respond quickly, the tool-level
            # permission check will catch it later. Don't block the turn.
            answered = self._question_event.wait(timeout=15)

            if answered and self._pending_answer:
                answer = self._pending_answer
                self._pending_question_id = None
                self._pending_answer = None

                if answer == "Allow":
                    from tools import grant_folder
                    grant_folder(str(folder))
                    log.info(f"Preemptive access granted: {folder}")
                else:
                    log.info(f"Preemptive access denied: {folder}")
            else:
                self._pending_question_id = None
                self._pending_answer = None
                log.info(f"Preemptive permission timed out for {folder} (15s) — will ask again at tool call")

    def run_turn(self, user_input: str, on_event: Optional[Callable] = None) -> TurnSummary:
        """
        Execute a full conversation turn.
        Loops until the model produces a response with no tool calls.

        on_event: optional callback(event_type: str, **kwargs) for live status.
          Events: "thinking", "tool_start", "tool_done", "generating"
        """
        def emit(event_type, **kwargs):
            if on_event:
                try:
                    on_event(event_type, **kwargs)
                except Exception:
                    pass

        def _compact_progress(phase, pct, detail=""):
            """Bridge compact_if_needed progress → SSE events."""
            emit("compact_progress", phase=phase, progress=pct, detail=detail)

        # Set up folder permission callback for tools
        from tools import set_permission_callback

        def _request_permission(file_path: str, folder_path: str) -> bool:
            """
            Called by tools when accessing a path outside allowed directories.
            Emits a permission_request event, blocks until user responds.
            Returns True if granted.
            """
            perm_id = f"perm_{uuid.uuid4().hex[:8]}"
            log.info(f"Permission requested for {folder_path} (id={perm_id})")

            emit("permission_request",
                 id=perm_id,
                 path=file_path,
                 folder=folder_path)

            self._pending_permission_id = perm_id
            self._permission_event.clear()
            self._permission_granted = False

            # Block for up to 30 seconds — user should respond quickly
            answered = self._permission_event.wait(timeout=30)

            self._pending_permission_id = None
            if answered and self._permission_granted:
                log.info(f"Permission granted for {folder_path}")
                return True
            else:
                log.info(f"Permission denied or timed out for {folder_path}")
                return False

        set_permission_callback(_request_permission)

        # Add user message to session
        self.session.messages.append(ConversationMessage.user_text(user_input))

        # Auto-stub a `request` node in the asset graph on the first
        # user message of a conversation. Ensures the graph always has
        # a root node even if the model skips the create_request
        # instruction. The model is still expected to enrich the
        # requirements via an explicit `graph_write action=create_request`
        # — dedup_key matches on title prefix so an explicit call
        # overwrites the stub with full structured requirements.
        try:
            _user_turns = sum(
                1 for m in self.session.messages
                if getattr(m, "role", "") == "user"
            )
            if _user_turns == 1:
                from tools import _exec_graph_write
                _stub_title = user_input.strip().splitlines()[0][:100] if user_input.strip() else "User request"
                _exec_graph_write({
                    "action": "create_request",
                    "title": _stub_title,
                    "body": user_input[:500],
                    "skill": "general",
                    "requirements": {},
                })
        except Exception as _se:
            log.warning(f"Auto-stub request node failed: {_se}")

        # ── Skill routing ──
        # At the top of each user turn, let the router pick a skill
        # based on the user's message. The router is a no-op if:
        #   - no conversation_id (legacy path), or
        #   - user has locked the skill, or
        #   - auto-routing has been disabled for this conversation.
        try:
            if self._conversation_id:
                routed = skills_mod.auto_route_for_turn(
                    self._conversation_id,
                    user_input,
                    turn_index=len(self.session.messages),
                    backend_url=self.backend_url,
                    model=getattr(self, "logical_model", None) or getattr(self, "model", None),
                )
                emit("skill_active",
                     skill=routed.name,
                     label=routed.label,
                     auto_route=skills_mod.get_or_init_state(self._conversation_id).auto_route,
                     user_locked=skills_mod.get_or_init_state(self._conversation_id).user_locked)
        except Exception as e:
            log.warning(f"skill routing failed: {e}")

        # ── Preemptive Folder Permission Check ──
        # Scan the user's message for folder references BEFORE any tool calls.
        # If the message mentions Desktop, Documents, Downloads, etc., ask
        # permission upfront so the user never sees "PERMISSION_DENIED".
        self._preemptive_permission_check(user_input, emit)

        iterations = 0
        tool_calls_made = 0
        final_text = ""
        consecutive_empty = 0  # Track consecutive empty responses from model
        recent_tool_calls = []  # Track (name, args_key) to detect loops
        mlx_confirmed_alive = False  # True after first successful backend call
        turn_output_tokens = 0  # Track total tool output tokens this turn
        TURN_OUTPUT_CAP = 150000  # Max tokens of tool output per turn (~60% of context)
        self._consecutive_nudges = 0  # Reset narration nudge counter each turn
        _trailing_intent_retry = False  # True when looping back after trailing-intent nudge
        _last_trailing_text = ""  # Captured trailing intent text for self-continuation
        _self_continuations = 0  # How many compact-and-continue cycles we've done
        _MAX_SELF_CONTINUATIONS = 6  # Hard cap to prevent infinite loops (raised for multi-step research tasks)

        while True:
            iterations += 1
            if iterations > self.max_iterations:
                log.warning(f"Hit max_iterations ({self.max_iterations}), ending turn")
                final_text = (
                    f"I've completed {tool_calls_made} tool operations across "
                    f"{iterations - 1} rounds. To continue this task, just ask me to keep going."
                )
                # Store it in session so the model has continuity
                self.session.messages.append(
                    ConversationMessage.assistant_text(final_text)
                )
                break

            log.info(f"Turn iteration {iterations}/{self.max_iterations}")

            # ── Patch 11 (Iteration 5): Auto-finalize self_grade trigger ──
            # After ≥ 4 successful drafts AND 3 consecutive turns without a new
            # draft AND in writing/review phase, inject synthetic self_grade call
            # to force .docx save before the benchmark timeout.
            try:
                from tools import get_draft_sections
                _af_drafts = get_draft_sections() or {}
                _af_current_count = sum(
                    1 for s in _af_drafts.values()
                    if isinstance(s, dict) and s.get("words", 0) > 0
                )
                _af_prev_count = getattr(self, "_auto_finalize_prev_drafts", 0)
                _af_stale = getattr(self, "_auto_finalize_stale_turns", 0)
                _af_done = getattr(self, "_auto_finalize_fired", False)
                if _af_current_count > _af_prev_count:
                    self._auto_finalize_stale_turns = 0
                else:
                    self._auto_finalize_stale_turns = _af_stale + 1
                self._auto_finalize_prev_drafts = _af_current_count
                if (
                    not _af_done
                    and _af_current_count >= 4
                    and self._auto_finalize_stale_turns >= 3
                ):
                    try:
                        _af_phase = self._detect_phase()
                    except Exception:
                        _af_phase = None
                    if _af_phase in ("writing", "review"):
                        log.warning(
                            f"Patch 11 AUTO-FINALIZE: {_af_current_count} drafts, "
                            f"{self._auto_finalize_stale_turns} stale turns — "
                            "forcing self_grade directly (bypassing model)"
                        )
                        self._auto_finalize_fired = True
                        try:
                            _af_tc_id = f"autofinal_{uuid.uuid4().hex[:8]}"
                            _af_args = {
                                "reason": (
                                    "Patch 11 auto-finalize: "
                                    f"{_af_current_count} drafts stalled — "
                                    "grading and saving the paper now."
                                )
                            }
                            self.session.messages.append(
                                ConversationMessage.assistant_with_blocks([
                                    TextBlock(text=(
                                        "[Auto-finalize: grading paper now "
                                        "to save .docx output.]"
                                    )),
                                    ToolUseBlock(
                                        id=_af_tc_id,
                                        name="self_grade",
                                        input=_af_args,
                                    ),
                                ])
                            )
                            _af_result = self.tool_registry.execute(
                                "self_grade", _af_args
                            )
                            log.warning(
                                f"Patch 11 self_grade executed: "
                                f"{str(_af_result)[:300]}"
                            )
                            self.session.messages.append(
                                ConversationMessage.tool_result(
                                    tool_use_id=_af_tc_id,
                                    tool_name="self_grade",
                                    output=str(_af_result),
                                    is_error=False,
                                )
                            )
                            emit(
                                "tool_done",
                                name="self_grade",
                                is_error=False,
                                summary="Auto-finalized (Patch 11)",
                                output=str(_af_result)[:800],
                            )
                            tool_calls_made += 1
                        except Exception as _afx:
                            log.error(f"Patch 11 direct exec failed: {_afx}")
            except Exception as _afe:
                log.warning(f"Auto-finalize check failed: {_afe}")

            # Pre-call context guard: compact BEFORE building API messages
            # if we're already over the safe limit. This prevents sending a
            # payload to MLX that will OOM the GPU.
            pre_check_tokens = self.session.estimate_tokens()
            safe_limit = getattr(self, '_compact_threshold', 200000)
            # Also update context budget at the start of each iteration
            set_context_budget(safe_limit, pre_check_tokens)
            # Publish context pressure for Clyde's inspector tile.
            emit("context",
                 tokens=pre_check_tokens,
                 threshold=safe_limit,
                 messages=len(self.session.messages))

            # Knob #4: Rolling (progressive) summary runs BEFORE full compaction.
            # Trims bulky old tool_result blocks once we're above 50% of the
            # compaction threshold, smoothing context growth so the big compact
            # hits less often (and has less material to summarize when it does).
            if os.environ.get("CLYDE_ROLLING_SUMMARY", "1") == "1":
                try:
                    if self.rolling_summary_if_needed(
                        soft_threshold_frac=float(
                            os.environ.get("CLYDE_ROLLING_THRESHOLD_FRAC", "0.5")
                        )
                    ):
                        pre_check_tokens = self.session.estimate_tokens()
                except Exception as _re:
                    log.warning(f"Rolling summary failed: {_re}")

            if pre_check_tokens > safe_limit:
                log.warning(f"Pre-call guard: ~{pre_check_tokens} tokens > {safe_limit} limit, compacting first")
                emit("compacting", estimated_tokens=pre_check_tokens, threshold=safe_limit)
                try:
                    preserve = getattr(self, '_preserve_recent', 4)
                    self.compact_if_needed(preserve_recent=preserve, token_threshold=safe_limit,
                                           on_progress=_compact_progress)
                    after = self.session.estimate_tokens()
                    log.info(f"Pre-call compaction done: {pre_check_tokens} → {after} tokens")
                    emit("compact_done", tokens_before=pre_check_tokens, tokens_after=after)
                except Exception as e:
                    log.error(f"Pre-call compaction failed: {e}")

            # Build messages for API call
            api_messages = self._build_api_messages()

            # Call backend with retry on transient failures.
            # Skip health check on mid-turn iterations — MLX is single-threaded,
            # so pinging /v1/models while it's generating our previous response
            # will timeout and falsely trigger recovery.
            emit("generating", iteration=iterations)
            response = self._call_backend(api_messages, skip_health_check=mlx_confirmed_alive, emit=emit)

            if "error" not in response:
                mlx_confirmed_alive = True

            if "error" in response:
                err_msg = response["error"]

                # ── Timeout recovery (NOT OOM) ──
                # MLX timed out. CRITICAL: MLX is single-threaded. When we
                # timeout, the server is STILL generating the previous response.
                # We must wait for MLX to finish (drain) before sending any
                # new request, otherwise it'll just queue and timeout again.
                if response.get("timeout"):
                    if _self_continuations < _MAX_SELF_CONTINUATIONS:
                        _self_continuations += 1
                        _est_tokens = self.session.estimate_tokens()
                        log.warning(
                            f"Backend timeout (continuation #{_self_continuations}/{_MAX_SELF_CONTINUATIONS}). "
                            f"Context ~{_est_tokens} tokens. Restarting backend then compacting."
                        )

                        # ── Step 1: Restart backend ──
                        # After a timeout, the server may still be generating.
                        # Kill and restart (~10-15s), then retry.
                        import time as _time
                        emit("recovering", stage="restarting",
                             detail="Restarting model server...")
                        log.warning("Timeout recovery: restarting backend to cancel stuck generation")
                        self._kill_backend_server()
                        _time.sleep(2)
                        self._restart_backend_server()

                        # Wait for backend to come back (tight 2s polls, 60s max)
                        _backend_back = False
                        for _ri in range(30):
                            _time.sleep(2)
                            emit("recovering", stage="loading",
                                 detail=f"Loading model... {min(95, (_ri + 1) * 3)}%")
                            try:
                                with httpx.Client(timeout=3.0) as _hc:
                                    _hc.get(f"{self.backend_url}/v1/models")
                                _backend_back = True
                                log.info(f"Backend restarted successfully after {(_ri + 1) * 2}s")
                                break
                            except Exception:
                                continue
                        if _backend_back:
                            emit("recovery_done", detail="Model reloaded")
                        else:
                            log.error("Backend failed to restart after timeout recovery")
                            emit("recovering", stage="failed",
                                 detail="Model failed to restart")

                        # ── Step 2: Compact (emergency mode — skip LLM summarize) ──
                        # Only compact if context is large enough to warrant it.
                        # At <10K tokens the timeout was a slow-thinking/transient
                        # backend issue, not an oversized payload — compacting here
                        # destroys the conversation and forces the model to redo
                        # work it already completed.
                        _COMPACT_MIN_TOKENS = 10000
                        if _est_tokens < _COMPACT_MIN_TOKENS:
                            log.warning(
                                f"Context too small for timeout-compaction "
                                f"({_est_tokens} < {_COMPACT_MIN_TOKENS} tokens) — "
                                f"skipping compaction, retrying as-is"
                            )
                        else:
                            emit("compacting", estimated_tokens=_est_tokens,
                                 threshold=self._compact_threshold)
                            try:
                                _preserve = getattr(self, '_preserve_recent', 4)
                                self.compact_if_needed(
                                    preserve_recent=_preserve,
                                    token_threshold=_est_tokens,
                                    on_progress=lambda **kw: emit("compact_progress", **kw),
                                    emergency=True,  # Fast text-based summary, no LLM call
                                )
                                _after = self.session.estimate_tokens()
                                log.info(f"Timeout compaction done: {_est_tokens} → {_after} tokens")
                                emit("compact_done", tokens_before=_est_tokens,
                                     tokens_after=_after)
                            except Exception as _ce:
                                log.error(f"Timeout compaction failed: {_ce}")

                        # ── Step 3: Inject continuation and retry ──
                        _cont_text = _last_trailing_text or "the user's request"
                        _cont_msg = (
                            f"[SYSTEM: The previous attempt timed out. "
                            f"Context has been compacted. Continue working on: {_cont_text}\n"
                            f"Call the appropriate tool immediately. Do not repeat previous work.]"
                        )
                        self.session.messages.append(ConversationMessage.user_text(_cont_msg))
                        self._consecutive_nudges = 0
                        _trailing_intent_retry = False
                        continue  # Retry with compacted context + drained MLX

                    # All self-continuations exhausted — last resort: simple retry
                    log.warning(f"All {_MAX_SELF_CONTINUATIONS} timeout retries exhausted. Final attempt.")
                    import time as _time
                    _time.sleep(5)
                    response = self._call_backend(api_messages, skip_health_check=True, emit=emit)
                    if "error" in response:
                        final_text = (
                            "The AI backend took too long to respond after multiple retries. "
                            f"Please try again with a simpler request. (Error: {response['error'][:200]})"
                        )
                        self.session.messages.append(
                            ConversationMessage.assistant_text(final_text)
                        )
                        break

                # ── GPU OOM / backend crash recovery ──
                # If the backend crashed (OOM or other), perform emergency
                # compaction to shrink context, restart server, retry once.
                elif response.get("gpu_oom"):
                    log.error(f"Backend crash detected: {err_msg}")
                    estimated = self.session.estimate_tokens()

                    # Only compact if context is large enough to warrant it.
                    if estimated < 10000:
                        log.warning(f"Context too small for OOM ({estimated} tokens) — retrying without compaction")
                        import time as _time
                        _time.sleep(5)
                        response = self._call_backend(api_messages, emit=emit)
                        if "error" in response:
                            final_text = (
                                "The model server crashed unexpectedly. "
                                f"Please try again. (Error: {response['error'][:200]})"
                            )
                            self.session.messages.append(
                                ConversationMessage.assistant_text(final_text)
                            )
                            break
                    else:
                        emit("compacting", estimated_tokens=estimated, threshold=0)
                        log.warning(f"Emergency compaction: ~{estimated} tokens, {len(self.session.messages)} messages")

                        # Aggressive compaction: keep only last 2 messages
                        # Emergency mode: skip LLM summary (backend just crashed)
                        try:
                            self.compact_if_needed(
                                preserve_recent=2,
                                token_threshold=1,  # Force compaction regardless
                                on_progress=_compact_progress,
                                emergency=True
                            )
                            after = self.session.estimate_tokens()
                            log.info(f"Emergency compaction done: {estimated} → {after} tokens")
                            emit("compact_done", tokens_before=estimated, tokens_after=after)
                        except Exception as ce:
                            log.error(f"Emergency compaction failed: {ce}")
                            emit("compact_done", tokens_before=estimated, tokens_after=estimated, error=str(ce))

                        # Self-heal: kill zombie + restart backend
                        import time as _time
                        emit("recovering", stage="restarting", detail="Model server crashed — restarting...")
                        self._kill_backend_server()
                        _time.sleep(1)
                        _restarted = self._restart_backend_server()

                        # If _restart_backend_server skipped (ProcessManager-
                        # owned backend), re-resolve URL and wait for it.
                        # No async — we're in a thread pool worker.
                        if not _restarted:
                            self._refresh_backend_url()
                            log.info(f"Crash recovery: waiting for {self.backend_url}")

                        # Wait for backend to come back (tight 2s polls, 60s max)
                        log.info("Waiting for backend to recover from crash...")
                        _backend_back = False
                        for _i in range(30):
                            _time.sleep(2)
                            emit("recovering", stage="loading", detail=f"Loading model... {min(95, (_i+1)*3)}%")
                            try:
                                with httpx.Client(timeout=2.0) as hc:
                                    hc.get(f"{self.backend_url}/v1/models")
                                _backend_back = True
                                break
                            except Exception:
                                continue
                        if _backend_back:
                            emit("recovery_done", detail="Model server recovered")
                        else:
                            emit("recovering", stage="failed", detail="Model server did not restart")

                        # Retry with compacted context
                        api_messages = self._build_api_messages()
                        response = self._call_backend(api_messages, emit=emit)
                        if "error" in response:
                            # ── Inject graph state for recovery ──
                            _graph_recovery = self._get_graph_recovery_context()
                            final_text = (
                                "The model server ran out of memory. Context has been compacted. "
                                "Resuming from saved state."
                                f"{_graph_recovery}"
                            )
                            self.session.messages.append(
                                ConversationMessage.assistant_text(final_text)
                            )
                            # Instead of breaking, inject recovery and let the model continue
                            self.session.messages.append(
                                ConversationMessage.user_text(
                                    f"[SYSTEM: Backend recovered after OOM. Context was compacted. "
                                    f"{_graph_recovery}\n"
                                    f"Resume working from where you left off. Call the next tool now.]"
                                )
                            )
                            api_messages = self._build_api_messages()
                            response = self._call_backend(api_messages, emit=emit)
                            if "error" in response:
                                final_text = (
                                    "The model server could not recover. "
                                    f"Please try again. (Error: {response['error'][:200]})"
                                )
                                self.session.messages.append(
                                    ConversationMessage.assistant_text(final_text)
                                )
                                break

                # ── Backend server unreachable — fast self-healing ──
                # Quick recovery: check if backend is truly dead vs just busy,
                # kill zombie only if needed, restart, poll with tight intervals.
                elif response.get("mlx_down"):
                    import time as _time
                    log.warning("Backend server unreachable — fast self-healing...")
                    emit("recovering", stage="detecting", detail="Reconnecting to backend...")

                    # Quick sanity: try a longer timeout (5s) — might just be slow.
                    try:
                        with httpx.Client(timeout=5.0) as hc:
                            hc.get(f"{self.backend_url}/v1/models")
                        log.info("Backend responded on second check (5s timeout) — false alarm")
                        emit("recovery_done", detail="Backend is responsive")
                        response = self._call_backend(api_messages, skip_health_check=True, emit=emit)
                    except Exception:
                        pass  # Truly dead — proceed with recovery

                    if "error" in response and response.get("mlx_down"):
                        # Step 1: Kill zombie if process exists but won't respond
                        _killed = self._kill_backend_server()
                        if _killed:
                            emit("recovering", stage="restarting", detail="Killed zombie, restarting...")
                            _time.sleep(1)
                        else:
                            emit("recovering", stage="restarting", detail="Starting backend server...")

                        # Step 2: Launch backend
                        self._restart_backend_server()

                        # Step 3: Poll with tight 2s intervals, up to 60s
                        backend_recovered = False
                        for attempt in range(30):
                            _time.sleep(2)
                            pct = min(95, (attempt + 1) * 3)
                            emit("recovering", stage="loading", detail=f"Loading model... {pct}%")
                            try:
                                with httpx.Client(timeout=2.0) as hc:
                                    hc.get(f"{self.backend_url}/v1/models")
                                backend_recovered = True
                                break
                            except Exception:
                                continue

                        if backend_recovered:
                            log.info("Backend self-healed, retrying request...")
                            emit("recovery_done", detail="Back online")
                            response = self._call_backend(api_messages, skip_health_check=True, emit=emit)
                            if "error" not in response:
                                mlx_confirmed_alive = True
                        else:
                            emit("recovering", stage="failed", detail="Backend did not start")
                            log.error("Backend did not recover after 60s")

                # Retry once on other transient failures
                elif any(k in err_msg.lower() for k in ("disconnect", "timeout", "reset", "broken pipe")):
                    log.warning(f"Transient backend error, retrying: {err_msg}")
                    import time as _time
                    _time.sleep(2)
                    response = self._call_backend(api_messages, emit=emit)

                if "error" in response:
                    # ── Auto-recovery: compact + retry instead of giving up ──
                    _recovery_attempts = getattr(self, '_backend_error_retries', 0) + 1
                    self._backend_error_retries = _recovery_attempts
                    if _recovery_attempts <= 3:
                        log.warning(f"Backend error recovery #{_recovery_attempts}/3: compacting and retrying")
                        emit("recovering", stage="auto_retry",
                             detail=f"Backend error — compacting context and retrying ({_recovery_attempts}/3)...")

                        # Re-resolve backend URL so we hit the right port.
                        self._refresh_backend_url()
                        # Probe the backend — if it's already up, skip the wait.
                        # If not, poll for up to 30s. ProcessManager's idle reaper
                        # or Clyde's AgentManager will re-spawn if needed; we just
                        # need to wait for it. No async calls from this thread.
                        import time as _rec_time
                        _rec_deadline = _rec_time.monotonic() + 30
                        while _rec_time.monotonic() < _rec_deadline:
                            try:
                                with httpx.Client(timeout=2.0) as _hc:
                                    _r = _hc.get(f"{self.backend_url}/v1/models")
                                if _r.status_code == 200:
                                    log.info(f"Recovery: backend {self.backend_url} responding")
                                    break
                            except Exception:
                                pass
                            _rec_time.sleep(3)
                        else:
                            log.warning(f"Recovery: backend {self.backend_url} still down after 30s")

                        # Only compact if context is actually large enough
                        # to benefit. At 3K tokens, compaction destroys
                        # the conversation for zero memory savings — the
                        # error is a backend issue, not a context issue.
                        _pre_recovery_tokens = self.session.estimate_tokens()
                        _recovery_compact_floor = int(
                            getattr(self, '_compact_threshold', 120000) * 0.3
                        )
                        if _pre_recovery_tokens >= _recovery_compact_floor:
                            self.compact_if_needed(emergency=True)
                        else:
                            log.info(
                                f"Backend error recovery: skipping compact "
                                f"({_pre_recovery_tokens} < {_recovery_compact_floor} floor)"
                            )
                        _graph_recovery = self._get_graph_recovery_context()
                        if _graph_recovery:
                            self.session.messages.append(
                                ConversationMessage.user_text(
                                    f"[SYSTEM: Backend recovered after error. Context was compacted. "
                                    f"{_graph_recovery}\nResume working from where you left off. "
                                    f"Call next tool now.]"
                                )
                            )
                        else:
                            self.session.messages.append(
                                ConversationMessage.user_text(
                                    "[SYSTEM: Backend recovered after error. Context was compacted. "
                                    "Resume working from where you left off.]"
                                )
                            )

                        import time as _time
                        _time.sleep(3)
                        api_messages = self._build_api_messages()
                        response = self._call_backend(api_messages, emit=emit)
                        if "error" not in response:
                            emit("recovery_done", detail="Resumed after error recovery")
                            # Reset retry counter on success
                            self._backend_error_retries = 0
                            continue  # Continue the main loop with the good response
                    # Final failure — show error
                    final_text = f"The model server could not recover. Please try again."
                    break

            # ── Content degeneration recovery ──
            # Model produced repetitive garbage (detected by streaming monitor).
            # Discard the garbage, inject a corrective nudge, and retry.
            if response.get("content_degenerated") and "error" not in response:
                _degen_retries = getattr(self, '_degen_retries', 0) + 1
                self._degen_retries = _degen_retries
                if _degen_retries <= 3:
                    log.warning(f"Content degeneration recovery #{_degen_retries}: discarding garbage, injecting correction")
                    emit("recovering", stage="degeneration",
                         detail=f"Output loop detected — retrying (attempt {_degen_retries}/3)")
                    # Don't save the degenerated response to conversation history.
                    # Instead, inject a corrective message and retry.
                    import time as _time
                    from tools import get_recorded_facts, get_research_outline, get_research_call_count
                    _facts = get_recorded_facts()
                    _outline = get_research_outline()
                    _research_count = get_research_call_count()
                    _budget_exhausted = _research_count >= 12

                    if _budget_exhausted:
                        # Research phase is over — push the model to write
                        _correction_parts = [
                            "[SYSTEM: Your previous response contained repetitive text and was discarded.\n"
                            "IMPORTANT: Your research budget is EXHAUSTED. You MUST stop researching.\n"
                            "Your ONLY next action is to call draft_section to write one section of the paper.\n"
                            "Use your recorded facts below as source material.\n"
                            "Keep your text SHORT — just say which section you're drafting, then call draft_section.]"
                        ]
                    else:
                        _correction_parts = [
                            "[SYSTEM: Your previous response was discarded because it contained repetitive/looping text. "
                            "This is a generation quality issue — NOT your fault. Here's what to do:\n"
                            "1. Take a breath. Look at the conversation context.\n"
                            "2. Identify your NEXT concrete action (a tool call).\n"
                            "3. Call that tool IMMEDIATELY in your response.\n"
                            "4. Keep your text output SHORT — just explain what you're doing, then call the tool.\n"
                            "DO NOT narrate at length. DO NOT repeat previous statements. Just act.]"
                        ]
                    if _facts:
                        _fl = [f"  {i+1}. {f['fact']}" + (f" [source: {f.get('source','')}]" if f.get('source') else "")
                               for i, f in enumerate(_facts[:20])]
                        _correction_parts.append(f"\nYour recorded facts ({len(_facts)} total):\n" + "\n".join(_fl))
                    if _outline:
                        _pending_secs = [s for s in _outline.get("sections", []) if s.get("status") in ("pending", "researched")]
                        if _pending_secs:
                            _next_sec = _pending_secs[0]
                            if _budget_exhausted:
                                _correction_parts.append(
                                    f"\nNEXT ACTION: Call draft_section for section '{_next_sec.get('title', '?')}' "
                                    f"(section {_next_sec.get('id', '?')}). Write 400-600 words using your recorded facts. "
                                    f"Include inline citations [1], [2] etc."
                                )
                            else:
                                _correction_parts.append(
                                    f"\nNext section to work on: '{_next_sec.get('title', '?')}' "
                                    f"(section {_next_sec.get('id', '?')}). "
                                    f"Research budget: {12 - _research_count} calls remaining. "
                                    f"Record facts from what you've already fetched, then continue."
                                )
                    self.session.messages.append(ConversationMessage.user_text("\n".join(_correction_parts)))
                    _time.sleep(3)  # Brief wait for MLX to finish old generation
                    continue  # Retry the main loop
                else:
                    log.error(f"Content degeneration persists after {_degen_retries} retries — giving up")
                    # Fall through to process whatever content exists

            # Reset degeneration counter on clean response
            if not response.get("content_degenerated"):
                self._degen_retries = 0

            # Parse the response — defensive: handle missing/malformed fields
            choices = response.get("choices", [])
            if not choices:
                log.warning("Backend returned no choices, treating as empty response")
                message = {}
            else:
                message = choices[0].get("message", {})
            content = message.get("content", "") or ""
            tool_calls = message.get("tool_calls") or []
            # Some models return tool_calls as None instead of []
            if tool_calls is None:
                tool_calls = []

            # Extract and emit thinking content — model-agnostic
            # Priority: explicit reasoning field > embedded tags
            think_content = ""
            reasoning = message.get("reasoning_content", "") or message.get("reasoning", "") or message.get("thinking", "") or ""
            if reasoning:
                think_content = reasoning.strip()
            else:
                # Try all known thinking tag formats (greedy to catch full blocks)
                think_patterns = [
                    # Legacy model thinking patterns: <|channel>thought ... <channel|> (flexible whitespace, optional close)
                    r'<\|channel>thought\s*(.*?)(?:<channel\|>|<\|channel>|$)',
                    # Legacy alt: <|think|> ... <|/think|>
                    r'<\|think\|>(.*?)(?:<\|/think\|>|$)',
                    # Qwen / DeepSeek: <think> ... </think>
                    r'<think>(.*?)(?:</think>|$)',
                    # Claude-style: <thinking> ... </thinking>
                    r'<thinking>(.*?)(?:</thinking>|$)',
                    r'<internal_thought>(.*?)(?:</internal_thought>|$)',
                ]
                for pat in think_patterns:
                    m = re.search(pat, content, re.DOTALL)
                    if m:
                        think_content = m.group(1).strip()
                        break
            if think_content:
                log.info(f"Emitting thinking ({len(think_content)} chars)")
                emit("thinking", content=think_content)

            # Parse tool calls from multiple formats (model-agnostic)
            if not tool_calls and content:
                tool_calls = self._parse_tool_calls_from_content(content)

            # ── Knob #3: Reject non-tool-call outputs during writing phase ──
            # During the writing/review phase, free-form prose with no tool call
            # means the model silently exited the pipeline. Inject a correction
            # and retry — don't accept "I finished the paper" unless drafts exist.
            if (
                not tool_calls
                and os.environ.get("CLYDE_REJECT_PROSE_IN_WRITING", "1") == "1"
            ):
                try:
                    _strict_phase = self._detect_phase()
                    # ── Patch 13 (Iteration 6): strict tool enforcement in ALL
                    # research-pipeline phases, not just writing/review. When the
                    # model hits a 403 or ambiguous state in scoping/research/
                    # synthesis and returns no tool call, we used to accept the
                    # response as final (= silent pipeline exit). Now we coerce
                    # the next-expected tool call back into the loop.
                    if _strict_phase in ("scoping", "research", "synthesis", "writing", "review"):
                        _strict_retries = getattr(self, "_strict_phase_retries", 0) + 1
                        self._strict_phase_retries = _strict_retries
                        if _strict_retries <= 2:
                            log.warning(
                                f"Phase={_strict_phase}: model returned prose without tool call "
                                f"(retry {_strict_retries}/2) — injecting tool-call correction"
                            )
                            from tools import get_research_outline, get_draft_sections, get_recorded_facts
                            _outline = get_research_outline() or {}
                            _drafts = get_draft_sections() or {}
                            _pending = [
                                s for s in _outline.get("sections", [])
                                if s.get("id") not in _drafts
                                or _drafts.get(s.get("id"), {}).get("words", 0) == 0
                            ]
                            try:
                                _fact_count = len(get_recorded_facts() or [])
                            except Exception:
                                _fact_count = 0
                            # Phase-specific next action
                            if _strict_phase == "scoping" and not _outline:
                                # ── Auto-scaffold: create outline if model can't ──
                                # After 1 retry, auto-create a default outline so the
                                # pipeline can progress. Qwen 3.6 often can't call
                                # research_outline but CAN do web_search and write prose.
                                if _strict_retries >= 2:
                                    try:
                                        from tools import _exec_research_outline, _exec_record_fact
                                        # Extract topic from user's first message
                                        _user_msgs = [m for m in self.session.messages if m.role == "user"]
                                        _topic = "Research Paper"
                                        if _user_msgs:
                                            _first = _user_msgs[0].content[:500]
                                            # Try to extract paper topic
                                            import re as _re_topic
                                            _topic_match = _re_topic.search(
                                                r'(?:paper|essay|report|thesis)\s+(?:on|about)\s+(.+?)(?:\.|$)',
                                                _first, _re_topic.IGNORECASE
                                            )
                                            if _topic_match:
                                                _topic = _topic_match.group(1).strip()[:200]
                                            elif len(_first) < 200:
                                                _topic = _first
                                        _scaffold_result = _exec_research_outline({
                                            "title": _topic,
                                            "sections": [
                                                "Introduction",
                                                "Historical Background",
                                                "Current State and Key Developments",
                                                "Technical Analysis",
                                                "Industry Landscape and Players",
                                                "Challenges and Limitations",
                                                "Future Outlook",
                                                "Conclusion",
                                            ],
                                            "target_words": 5000,
                                            "thesis": f"A comprehensive analysis of {_topic}",
                                        })
                                        log.warning(f"AUTO-SCAFFOLD: created default outline for '{_topic}'")
                                        # Auto-record placeholder facts from web content in context
                                        _auto_facts = self._extract_facts_from_context()
                                        for _af in _auto_facts[:8]:
                                            _exec_record_fact(_af)
                                        if _auto_facts:
                                            log.warning(f"AUTO-SCAFFOLD: recorded {min(len(_auto_facts), 8)} facts from context")
                                        self._strict_phase_retries = 0
                                        # Re-detect phase after scaffolding
                                        _outline = get_research_outline()
                                        _fact_count = len(get_recorded_facts() or [])
                                    except Exception as _scaf_err:
                                        log.warning(f"AUTO-SCAFFOLD failed: {_scaf_err}")
                                _correction = (
                                    "[SYSTEM: Your last response was prose with no tool call. "
                                    "We are in SCOPING phase — you MUST call research_outline "
                                    "now to define the paper's structure (8 sections, thesis, "
                                    "5000-word target)."
                                )
                            elif _strict_phase in ("scoping", "research") and _fact_count < 6:
                                _correction = (
                                    "[SYSTEM: Your last response was prose with no tool call. "
                                    f"We are in the {_strict_phase.upper()} phase — you MUST "
                                    "call a tool. Options: web_search for a new query, web_fetch "
                                    "for a different source (avoid sciencedirect/researchgate/"
                                    "mdpi which return 403 — try britannica, wikipedia, ieee, "
                                    "springer, nature.com, arxiv, batteryuniversity, news sites), "
                                    "or record_fact to save what you've already learned. "
                                    f"Current fact count: {_fact_count}."
                                )
                            elif _pending and (_strict_phase in ("writing", "review") or _fact_count >= 3):
                                _nxt = _pending[0]
                                _correction = (
                                    "[SYSTEM: Your last response was prose with no tool call. "
                                    f"We are in the {_strict_phase.upper()} phase — you MUST call a tool.\n\n"
                                    f"NEXT ACTION: call draft_section for '{_nxt.get('title','?')}' "
                                    f"(section {_nxt.get('id','?')}). Write 400-600 words with inline citations."
                                )
                            else:
                                _correction = (
                                    "[SYSTEM: Your last response was prose with no tool call. "
                                    "All sections are drafted. Call self_grade to evaluate the paper "
                                    "(the tool will auto-save the paper as a .docx)."
                                )
                            self.session.messages.append(ConversationMessage.user_text(_correction + "]"))
                            continue
                        else:
                            # ── Patch 10 (Iteration 5): Prose-to-draft-section salvage ──
                            # When strict-phase retries exhaust in writing phase, the model
                            # is emitting usable section content as prose but failing to wrap
                            # it in a draft_section tool_call. Rather than throw the content
                            # away ("accept prose as final" = silent data loss), extract the
                            # prose body and synthesize a draft_section call with it.
                            _salvaged = False
                            if _strict_phase in ("scoping", "research", "writing", "review"):
                                try:
                                    _prose_body = self._clean_content(content or "").strip()
                                    # Strip any stray tool-call scaffolding
                                    _prose_body = re.sub(
                                        r'^\s*(?:```(?:json|tool_code)?\s*)?',
                                        '', _prose_body, flags=re.IGNORECASE
                                    ).strip()
                                    _prose_body = re.sub(r'\s*```\s*$', '', _prose_body).strip()
                                    # Accept if body has substantive content — heading OR ≥ 200 words
                                    _word_count_est = len(_prose_body.split())
                                    _has_heading = bool(re.search(r'(?m)^#{1,3}\s+\S', _prose_body))
                                    if _word_count_est >= 200 and (_has_heading or _word_count_est >= 300):
                                        from tools import (
                                            get_research_outline, get_draft_sections,
                                            _exec_research_outline, _exec_record_fact,
                                            get_recorded_facts,
                                        )
                                        _outline = get_research_outline() or {}
                                        _drafts = get_draft_sections() or {}

                                        # ── Auto-scaffold outline if missing ──
                                        if not _outline:
                                            _user_msgs = [m for m in self.session.messages if m.role == "user"]
                                            _topic = "Research Paper"
                                            if _user_msgs:
                                                _first = _user_msgs[0].content[:500]
                                                _topic_match = re.search(
                                                    r'(?:paper|essay|report|thesis)\s+(?:on|about)\s+(.+?)(?:\.|$)',
                                                    _first, re.IGNORECASE
                                                )
                                                if _topic_match:
                                                    _topic = _topic_match.group(1).strip()[:200]
                                            _exec_research_outline({
                                                "title": _topic,
                                                "sections": [
                                                    "Introduction", "Historical Background",
                                                    "Current State and Key Developments",
                                                    "Technical Analysis",
                                                    "Industry Landscape and Players",
                                                    "Challenges and Limitations",
                                                    "Future Outlook", "Conclusion",
                                                ],
                                                "target_words": 5000,
                                            })
                                            _outline = get_research_outline() or {}
                                            log.warning(f"Patch 10 AUTO-SCAFFOLD: outline for '{_topic}'")

                                        # ── Auto-record facts if below gate ──
                                        _p22_facts_now = len(get_recorded_facts() or [])
                                        if _p22_facts_now < 4:
                                            _auto_facts = self._extract_facts_from_context()
                                            for _af in _auto_facts[:6]:
                                                _exec_record_fact(_af)
                                            _p22_facts_now = len(get_recorded_facts() or [])
                                            log.warning(f"Patch 10 AUTO-SCAFFOLD: recorded facts, now {_p22_facts_now}")

                                        _pending = [
                                            s for s in _outline.get("sections", [])
                                            if s.get("id") not in _drafts
                                            or _drafts.get(s.get("id"), {}).get("words", 0) == 0
                                        ]

                                        # ── Multi-section salvage for large prose ──
                                        if _word_count_est >= 1000 and len(_pending) > 1:
                                            # Split by headings or evenly
                                            _heading_parts = re.split(r'(?m)^#{1,3}\s+', _prose_body)
                                            _heading_parts = [p.strip() for p in _heading_parts if p.strip() and len(p.split()) >= 30]

                                            if len(_heading_parts) >= 3:
                                                from tools import _exec_draft_section
                                                for _i, _part in enumerate(_heading_parts):
                                                    if _i < len(_pending):
                                                        if "[" not in _part:
                                                            _part = _part.rstrip() + " [1]"
                                                        _exec_draft_section({
                                                            "section_id": _pending[_i]["id"],
                                                            "content": _part,
                                                        })
                                                _salvaged = True
                                                log.warning(
                                                    f"Patch 10 MULTI-SALVAGE: distributed {_word_count_est}w "
                                                    f"across {min(len(_heading_parts), len(_pending))} sections"
                                                )
                                                self._strict_phase_retries = 0
                                                # Inject a nudge to call self_grade
                                                self.session.messages.append(
                                                    ConversationMessage.user_text(
                                                        "[SYSTEM: Your paper content has been auto-captured into the "
                                                        "draft pipeline. Call self_grade now to evaluate and save as .docx.]"
                                                    )
                                                )
                                                continue
                                            else:
                                                # No headings — distribute evenly
                                                from tools import _exec_draft_section
                                                _words = _prose_body.split()
                                                _chunk_sz = max(300, len(_words) // len(_pending))
                                                _off = 0
                                                for _ps in _pending:
                                                    _chunk = " ".join(_words[_off:_off + _chunk_sz])
                                                    if _chunk.strip():
                                                        if "[" not in _chunk:
                                                            _chunk = _chunk.rstrip() + " [1]"
                                                        _exec_draft_section({"section_id": _ps["id"], "content": _chunk})
                                                    _off += _chunk_sz
                                                _salvaged = True
                                                log.warning(
                                                    f"Patch 10 EVEN-SALVAGE: distributed {_word_count_est}w "
                                                    f"evenly across {len(_pending)} sections"
                                                )
                                                self._strict_phase_retries = 0
                                                self.session.messages.append(
                                                    ConversationMessage.user_text(
                                                        "[SYSTEM: Your paper content has been auto-captured. "
                                                        "Call self_grade now to evaluate and save as .docx.]"
                                                    )
                                                )
                                                continue

                                        if _pending:
                                            _target_id = _pending[0].get("id")
                                            _target_title = _pending[0].get("title", "")
                                            log.warning(
                                                f"Patch 10 SALVAGE: synthesizing draft_section for "
                                                f"'{_target_title}' (id={_target_id}) from {_word_count_est}w prose"
                                            )
                                            # Inject synthetic tool_call into the current response path.
                                            # The outer loop expects tool_calls to be processed next turn,
                                            # so we set it and fall through.
                                            tool_calls = [{
                                                "id": f"salvage_{_target_id}_{int(time.time())}",
                                                "type": "function",
                                                "function": {
                                                    "name": "draft_section",
                                                    "arguments": json.dumps({
                                                        "section_id": _target_id,
                                                        "content": _prose_body,
                                                    }),
                                                },
                                            }]
                                            _salvaged = True
                                            self._strict_phase_retries = 0
                                except _P22SalvageRefused:
                                    _salvaged = False
                                except Exception as _e:
                                    log.warning(f"Patch 10 salvage failed: {_e}")
                            if not _salvaged:
                                log.warning(
                                    f"Phase={_strict_phase}: strict-phase retries exhausted — "
                                    "accepting prose output as final"
                                )
                                self._strict_phase_retries = 0
                    else:
                        self._strict_phase_retries = 0
                except Exception as _e:
                    log.warning(f"Strict phase check failed: {_e}")

            if not tool_calls:
                # No tool calls → check if model is mid-task or truly done
                final_text = self._clean_content(content)

                # Guard: if model returned completely empty content, don't
                # just silently end — this can happen with some models/configs
                if not final_text.strip():
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        log.error(f"Model returned {consecutive_empty} consecutive empty responses, aborting turn")
                        final_text = "I encountered an issue generating a response. Please try again."
                        break
                    # Inject a synthetic user prod — the model is mid-task
                    # with no tool call and no text. Remind it to act.
                    log.warning(
                        f"Model returned empty content (attempt "
                        f"{consecutive_empty}/3) — injecting continue nudge"
                    )
                    self.session.messages.append(
                        ConversationMessage.user_text(
                            "Continue with the next tool call. Do not emit "
                            "prose — invoke a tool directly, or finish the "
                            "task if it's complete."
                        )
                    )
                    continue

                consecutive_empty = 0

                # ── Code block extraction ──
                # Some models output bash/python commands as markdown
                # code blocks instead of tool calls. Detect and auto-execute.
                # BUT: only when the model is TRYING to execute (narration intent),
                # NOT when it's presenting a command FOR the user to run.
                _code_block_match = re.search(
                    r'```(?:bash|sh|shell|zsh)?\s*\n(.+?)\n```',
                    final_text, re.DOTALL
                )
                if _code_block_match:
                    _extracted_cmd = _code_block_match.group(1).strip()
                    # Check if the model is PRESENTING a command to the user
                    # vs. trying to execute it itself
                    _text_before_block = final_text[:_code_block_match.start()].lower()
                    _presenting_hints = [
                        "here's the command", "here is the command",
                        "you can run", "you could run",
                        "run this", "copy this", "use this command",
                        "here's how", "here is how",
                        "the command is", "the command would be",
                        "try running", "try this",
                        "execute this yourself", "run it yourself",
                        "you'll need to run", "you will need to run",
                        "paste this", "to do this, run",
                        "here's a script", "here is a script",
                    ]
                    _is_presenting = any(h in _text_before_block for h in _presenting_hints)
                    # ── Patch 12 (Iteration 5): skip JSON / nested-fence blocks ──
                    # If the extracted "command" starts with ```json or contains
                    # unbalanced triple-backticks, it's nested markdown not bash.
                    # Running it as shell produces an "unexpected EOF" error.
                    _looks_like_json_block = bool(
                        _extracted_cmd
                        and (
                            _extracted_cmd.lstrip().startswith("```")
                            or _extracted_cmd.count("```") >= 1
                            or _extracted_cmd.lstrip().lower().startswith("json")
                            or (
                                _extracted_cmd.lstrip().startswith("{")
                                and _extracted_cmd.rstrip().endswith("}")
                                and ("\"url\"" in _extracted_cmd or "\"query\"" in _extracted_cmd)
                            )
                        )
                    )
                    if _is_presenting:
                        log.info(f"Code block detected but model is presenting to user — NOT auto-executing")
                    elif _looks_like_json_block:
                        log.info(f"Patch 12: code block looks like JSON/tool args, not bash — skipping auto-exec")
                    elif _extracted_cmd and len(_extracted_cmd) < 2000:
                        log.info(f"Code block extraction: auto-executing bash: {_extracted_cmd[:100]}")
                        # Emit only the narration text BEFORE the code block
                        _narration_before = final_text[:_code_block_match.start()].strip()
                        if _narration_before:
                            emit("narration", text=_narration_before)
                            self.session.messages.append(
                                ConversationMessage.assistant_text(_narration_before)
                            )

                        # Execute as a bash tool call
                        _tc_id = f"auto_bash_{uuid.uuid4().hex[:8]}"
                        _bash_args = {"command": _extracted_cmd}
                        emit("tool_start", name="bash", args_preview=_extracted_cmd[:300])
                        log.info(f"  Auto-tool: bash({_extracted_cmd[:100]})")
                        try:
                            _bash_result = self.tool_registry.execute("bash", _bash_args)
                        except Exception as _be:
                            log.error(f"  Auto-bash failed: {_be}")
                            _bash_result = f"ERROR: {_be}"
                        _bash_is_error = _bash_result.startswith("ERROR")
                        _bash_summary = self._tool_summary("bash", _bash_args, _bash_result, _bash_is_error)
                        emit("tool_done", name="bash", is_error=_bash_is_error, summary=_bash_summary, output=_bash_result)
                        log.info(f"  Auto-bash result: {_bash_result[:200]}")

                        # Add as proper tool_use + tool_result so the model sees it
                        self.session.messages.append(
                            ConversationMessage(
                                role="assistant",
                                blocks=[ToolUseBlock(id=_tc_id, name="bash", input=_bash_args)]
                            )
                        )
                        self.session.messages.append(
                            ConversationMessage.tool_result(_tc_id, "bash", _bash_result, _bash_is_error)
                        )
                        # Add nudge to keep going
                        self.session.messages.append(
                            ConversationMessage.user_text(
                                "[Instruction: Present the tool result to the user with a brief explanation. "
                                "If there are more steps, describe what you will do next AND call the next tool "
                                "in the same response. Do NOT repeat the same tool call you just made. "
                                "Progress to the NEXT step of the task.]"
                            )
                        )
                        tool_calls_made += 1
                        continue  # Loop back for next iteration

                # ── Smart Completion Check ──
                # Two-tier system to detect when the model stops mid-task:
                #
                # Tier 1 — TRAILING INTENT: Substantive responses (>200 chars)
                #   where the model did useful work but the LAST few lines
                #   promise more action that never happened. We emit the good
                #   content to the user and inject a targeted nudge quoting
                #   the model's own unfulfilled promise.
                #
                # Tier 2 — PURE NARRATION: Short responses that are entirely
                #   "I will do X" without any tool call. Generic escalating nudge.
                if iterations < self.max_iterations:
                    action_hints = [
                        "i will start by", "i'll start by",
                        "i will read", "i'll read",
                        "i will check", "i'll check",
                        "i will look", "i'll look",
                        "i will search", "i'll search",
                        "i will use", "i'll use",
                        "i will run", "i'll run",
                        "i will call", "i'll call",
                        "i will try", "i'll try",
                        "i will fix", "i'll fix",
                        "i will retry", "i'll retry",
                        "i will parse", "i'll parse",
                        "i will classify", "i'll classify",
                        "i will organize", "i'll organize",
                        "let me read", "let me check", "let me look",
                        "let me search", "let me run",
                        "let me try", "let me retry", "let me fix",
                        "let me parse", "let me classify", "let me organize",
                        "let me use", "let me grab", "let me get",
                        "let me continue", "let me proceed", "let me write",
                        "let me create", "let me build", "let me update",
                        "let me sample", "let me batch", "let me dive",
                        "next step", "step 2", "step 3", "step 4",
                        "now proceed", "now i will", "now i'll",
                        "let me now", "moving on to", "next, i",
                        "i will now", "i'll now", "continuing with",
                        "proceed to", "let's now",
                        "trying with", "retry with",
                        "now i'll classify", "now classifying",
                        "now i'll process", "now processing",
                        "now i'll batch", "now batching",
                        "now i'll continue", "now continuing",
                        "going to", "about to",
                        "next batch", "next, batch",
                        "first, ", "first i", "first let",
                        "starting with",
                        # End-of-line cliffhangers (mid-task pacing)
                        "let me grab", "let me get",
                        "another batch", "more batches",
                        # Declarative / needs-to patterns (mlx route common)
                        "i need to", "we need to",
                        "now i need to", "i should",
                        "now let me", "now i'll", "i must",
                        "i have to",
                        "the next step", "next i need",
                        "let's read", "let's check", "let's run",
                        "let's parse", "let's classify", "let's organize",
                        "let's now",
                        "i'll proceed", "i'll move on",
                        "i'll follow up", "i will follow up",
                        "calling", "invoking",
                        "i'm going to", "we're going to",
                        "going to call", "going to run",
                        "going to use", "going to read",
                        "ok, let me", "okay, let me",
                        "alright, let me", "great, let me",
                        "great. now", "good. now", "ok. now",
                        "i will list", "i'll list",
                        "i will open", "i'll open",
                        "i will write", "i'll write",
                        "i will create", "i'll create",
                        "i will update", "i'll update",
                        "i will execute", "i'll execute",
                        "i will analyze", "i'll analyze",
                    ]
                    text_lower = final_text.lower()

                    # ── Tier 1: Trailing Intent Detection ──
                    # Check only the LAST ~5 lines for forward-looking intent.
                    # This catches: long useful response + "I will now read X" at the end
                    # without false-positiving on mentions of intent in the middle.
                    _lines = final_text.strip().splitlines()
                    # Use the WHOLE response for short single-line turns (the
                    # mlx-vlm path commonly emits 80-200 char one-liners like
                    # "Let me fetch the docs:" — without this they'd never
                    # match the 2-line tail-only check below).
                    _tail = "\n".join(_lines[-5:]).lower() if len(_lines) >= 2 else final_text.lower()
                    # Drop the legacy 200-char floor: it was anti-false-positive
                    # but kills llama.cpp-style short turns from the mlx route.
                    # Keep a TINY floor (3 chars) just to skip empty/whitespace.
                    # Short turns like "Continuing..." or "Got it. Now batch
                    # read 30-44." absolutely deserve a trailing-intent check —
                    # otherwise the model exits a multi-step task with one
                    # short line that promises more work it never delivers.
                    _is_substantive = len(final_text.strip()) > 3

                    # False positives: polite closings, not actual task intent
                    _false_positives = [
                        "if you need", "if you want", "let me know",
                        "happy to help", "anything else", "would you like",
                        "feel free to", "don't hesitate",
                    ]
                    _tail_is_false_positive = any(fp in _tail for fp in _false_positives)

                    # Generic patterns that catch ANY verb after common
                    # continuation markers — eliminates the whack-a-mole
                    # of adding "let me fetch", "let me pull", etc. one
                    # by one. Matches:
                    #   "let me <word>"  — always intent
                    #   "then <word>"    — always continuation
                    #   "i'll <word>"    — always intent
                    #   "i will <word>"  — always intent
                    _generic_intent = bool(re.search(
                        r'\b(?:let me|then i\'ll|then i will|then start|'
                        r'then i\'m going to|, then )\s*\w',
                        _tail,
                    ))
                    _trailing_has_intent = (
                        not _tail_is_false_positive
                        and (any(hint in _tail for hint in action_hints)
                             or _generic_intent)
                    )

                    if _trailing_has_intent and _is_substantive:
                        # Tier 1: The model did useful work but trailed off
                        # with an unfulfilled promise. Emit the good content
                        # and nudge with its own words.
                        _nudge_count = getattr(self, '_consecutive_nudges', 0) + 1
                        self._consecutive_nudges = _nudge_count
                        _trailing_text = "\n".join(_lines[-3:]).strip()
                        _last_trailing_text = _trailing_text  # Save for self-continuation
                        log.info(
                            f"Trailing intent detected (nudge #{_nudge_count}): "
                            f"'{_trailing_text[:120]}...'"
                        )

                        if _nudge_count > 3:
                            # Nudges exhausted — compact and self-continue instead of giving up.
                            if _self_continuations < _MAX_SELF_CONTINUATIONS:
                                _self_continuations += 1
                                log.warning(
                                    f"Nudges exhausted, self-continuing #{_self_continuations}: "
                                    f"compact + re-inject trailing intent"
                                )
                                # Emit what we have so far
                                self.session.messages.append(
                                    ConversationMessage.assistant_text(final_text)
                                )
                                emit("narration", text=final_text)

                                # Only actually compact when context has grown
                                # large — otherwise the self-continuation path
                                # was nuking research/writing context at
                                # 1-5K tokens, which destroys facts the model
                                # just gathered. Floor at 60% of the normal
                                # compact threshold (default 36K of 60K).
                                # IMPORTANT: emit("compacting") MUST be inside the
                                # guard, otherwise the UI shows a "Compacting…"
                                # banner that never receives a compact_done event
                                # (sticky banner bug, observed during Phase 4).
                                _before_tokens = self.session.estimate_tokens()
                                _compact_floor = int(
                                    getattr(self, '_compact_threshold', 60000) * 0.6
                                )
                                if _before_tokens >= _compact_floor:
                                    emit("compacting", estimated_tokens=_before_tokens,
                                         threshold=self._compact_threshold)
                                    try:
                                        _preserve = getattr(self, '_preserve_recent', 4)
                                        self.compact_if_needed(
                                            preserve_recent=_preserve,
                                            token_threshold=_before_tokens,
                                            on_progress=lambda **kw: emit("compact_progress", **kw)
                                        )
                                        _after = self.session.estimate_tokens()
                                        log.info(
                                            f"Self-continuation compaction done, "
                                            f"{_before_tokens}→{_after} tokens"
                                        )
                                        emit("compact_done",
                                             tokens_before=_before_tokens,
                                             tokens_after=_after)
                                    except Exception as _ce:
                                        log.error(f"Self-continuation compaction failed: {_ce}")
                                        # Clear banner even on failure
                                        emit("compact_done",
                                             tokens_before=_before_tokens,
                                             tokens_after=_before_tokens,
                                             error=str(_ce))
                                else:
                                    log.info(
                                        f"Self-continuation: skipping compaction "
                                        f"({_before_tokens} < {_compact_floor} floor) "
                                        f"— re-inject intent only, preserve research context"
                                    )

                                # Re-inject the unfulfilled intent + plan state + facts as a fresh instruction
                                _cont_parts = [
                                    "[SYSTEM: You have been working on a multi-step task. "
                                    "You completed some steps but still need to do this:\n"
                                    f"  {_trailing_text}\n"
                                ]
                                # Include plan state so model knows where it is
                                try:
                                    from tools import _active_plan, get_recorded_facts
                                    if _active_plan:
                                        _pending = [s for s in _active_plan["steps"] if s["status"] == "pending"]
                                        _done = [s for s in _active_plan["steps"] if s["status"] == "done"]
                                        _cont_parts.append(
                                            f"\nPlan progress: {len(_done)}/{len(_active_plan['steps'])} steps done."
                                        )
                                        if _pending:
                                            _cont_parts.append(
                                                f"Next pending: {_pending[0]['step_id']} — {_pending[0]['description']}"
                                            )
                                    _facts = get_recorded_facts()
                                    if _facts:
                                        _fact_lines = []
                                        for _fi, _ff in enumerate(_facts, 1):
                                            _fl = f"  {_fi}. {_ff['fact']}"
                                            if _ff.get('source'):
                                                _fl += f" [source: {_ff['source']}]"
                                            _fact_lines.append(_fl)
                                        _cont_parts.append(
                                            f"\nRecorded Facts ({len(_facts)} total):\n"
                                            + "\n".join(_fact_lines[:30])
                                            + "\n\nUse these facts directly in your output. "
                                            "Do NOT re-search or re-record them — they are already saved."
                                        )
                                except Exception:
                                    pass
                                _cont_parts.append(
                                    "\nContinue NOW. Call the appropriate tool immediately. "
                                    "Do not repeat previous analysis — just proceed with the next action.]"
                                )
                                _cont_msg = "\n".join(_cont_parts)
                                self.session.messages.append(
                                    ConversationMessage.user_text(_cont_msg)
                                )
                                self._consecutive_nudges = 0  # Fresh start
                                _trailing_intent_retry = True
                                continue
                            else:
                                log.warning(
                                    f"Max self-continuations ({_MAX_SELF_CONTINUATIONS}) reached. "
                                    f"Ending turn."
                                )
                                self._consecutive_nudges = 0
                                self.session.messages.append(
                                    ConversationMessage.assistant_text(final_text)
                                )
                                break

                        # Emit the full response (it has useful content)
                        self.session.messages.append(
                            ConversationMessage.assistant_text(final_text)
                        )
                        emit("narration", text=final_text)

                        # Targeted nudge: quote what the model said it would do
                        _nudge_msg = (
                            f'[SYSTEM: You ended your response by saying:\n'
                            f'  "{_trailing_text}"\n'
                            f'But you stopped without doing it. You MUST follow through NOW. '
                            f'Call the appropriate tool (read_file, bash, write_file, etc.) '
                            f'immediately. Do not repeat what you just said — just call the tool.]'
                        )
                        self.session.messages.append(
                            ConversationMessage.user_text(_nudge_msg)
                        )
                        _trailing_intent_retry = True  # Mark for timeout handling
                        continue  # Loop back to call backend

                    # ── Tier 2: Pure Narration Detection ──
                    # Short or entirely-narration responses where the model
                    # is just stating intent without doing anything.
                    should_continue = any(hint in text_lower for hint in action_hints)

                    if should_continue:
                        # Pure narration (no useful substantive content)
                        _nudge_count = getattr(self, '_consecutive_nudges', 0) + 1
                        self._consecutive_nudges = _nudge_count
                        log.info(f"Pure narration detected (nudge #{_nudge_count}): '{final_text[:80]}...'")

                        if _nudge_count > 3:
                            # Nudges exhausted — compact and self-continue
                            if _self_continuations < _MAX_SELF_CONTINUATIONS:
                                _self_continuations += 1
                                log.warning(
                                    f"Tier 2 nudges exhausted, self-continuing #{_self_continuations}"
                                )
                                self.session.messages.append(
                                    ConversationMessage.assistant_text(final_text)
                                )
                                # Only compact if context has grown enough to
                                # benefit. Otherwise (e.g. degenerate-thinking
                                # nudge spiral on small conversations) compaction
                                # destroys completed work and keeps the model
                                # looping. Same 60%-of-threshold floor as the
                                # primary self-continuation path above.
                                _t2_before_tokens = self.session.estimate_tokens()
                                _t2_compact_floor = int(
                                    getattr(self, '_compact_threshold', 60000) * 0.6
                                )
                                if _t2_before_tokens >= _t2_compact_floor:
                                    emit("compacting",
                                         estimated_tokens=_t2_before_tokens,
                                         threshold=self._compact_threshold)
                                    try:
                                        _preserve = getattr(self, '_preserve_recent', 4)
                                        self.compact_if_needed(
                                            preserve_recent=_preserve,
                                            token_threshold=_t2_before_tokens,
                                            on_progress=lambda **kw: emit("compact_progress", **kw)
                                        )
                                        _t2_after = self.session.estimate_tokens()
                                        emit("compact_done",
                                             tokens_before=_t2_before_tokens,
                                             tokens_after=_t2_after)
                                    except Exception as _ce:
                                        log.error(f"Tier 2 self-continuation compaction failed: {_ce}")
                                        emit("compact_done",
                                             tokens_before=_t2_before_tokens,
                                             tokens_after=_t2_before_tokens,
                                             error=str(_ce))
                                else:
                                    log.info(
                                        f"Tier 2 self-continuation: skipping compaction "
                                        f"({_t2_before_tokens} < {_t2_compact_floor} floor) "
                                        f"— re-inject intent only"
                                    )
                                # Extract what the model was trying to do + plan state
                                _intent_text = final_text.strip()[-300:]
                                _cont_parts2 = [
                                    f"[SYSTEM: You have been narrating without calling tools. "
                                    f"Context has been compacted to give you more room. "
                                    f"You said: \"{_intent_text}\"\n"
                                ]
                                try:
                                    from tools import _active_plan, get_recorded_facts
                                    if _active_plan:
                                        _pending2 = [s for s in _active_plan["steps"] if s["status"] == "pending"]
                                        _done2 = [s for s in _active_plan["steps"] if s["status"] == "done"]
                                        _cont_parts2.append(
                                            f"Plan progress: {len(_done2)}/{len(_active_plan['steps'])} done."
                                        )
                                        if _pending2:
                                            _cont_parts2.append(
                                                f"Next: {_pending2[0]['step_id']} — {_pending2[0]['description']}"
                                            )
                                    _facts2 = get_recorded_facts()
                                    if _facts2:
                                        _fact_lines2 = []
                                        for _fi2, _ff2 in enumerate(_facts2, 1):
                                            _fl2 = f"  {_fi2}. {_ff2['fact']}"
                                            if _ff2.get('source'):
                                                _fl2 += f" [source: {_ff2['source']}]"
                                            _fact_lines2.append(_fl2)
                                        _cont_parts2.append(
                                            f"Recorded Facts ({len(_facts2)} total):\n"
                                            + "\n".join(_fact_lines2[:30])
                                            + "\nUse these facts directly. Do NOT re-search them."
                                        )
                                except Exception:
                                    pass
                                _cont_parts2.append("Stop narrating. Call the tool NOW.]")
                                _cont_msg = "\n".join(_cont_parts2)
                                self.session.messages.append(
                                    ConversationMessage.user_text(_cont_msg)
                                )
                                self._consecutive_nudges = 0
                                _trailing_intent_retry = True
                                continue
                            else:
                                log.warning(f"Max self-continuations reached, ending turn")
                                self._consecutive_nudges = 0
                                self.session.messages.append(
                                    ConversationMessage.assistant_text(final_text)
                                )
                                break

                        self.session.messages.append(
                            ConversationMessage.assistant_text(final_text)
                        )
                        emit("narration", text=final_text)

                        if _nudge_count == 1:
                            _nudge_msg = (
                                "[SYSTEM: You just said what you would do but did NOT call any tool. "
                                "You MUST call the tool NOW. Do not narrate — emit the tool call immediately.]"
                            )
                        else:
                            _nudge_msg = (
                                "[SYSTEM: You have narrated your intent multiple times without calling a tool. "
                                "This is your final warning. You MUST emit a tool_call in your next response. "
                                "Do NOT write bash commands in code blocks — use the bash tool. "
                                "Do NOT describe what you will do — DO IT by calling the tool function.]"
                            )
                        self.session.messages.append(
                            ConversationMessage.user_text(_nudge_msg)
                        )
                        continue  # Loop back to call backend again
                    else:
                        # Model gave a real response (no action hints), reset nudge counter
                        self._consecutive_nudges = 0

                self.session.messages.append(
                    ConversationMessage.assistant_text(final_text)
                )
                break

            consecutive_empty = 0  # Got tool calls, reset counter

            # Has tool calls → execute them and loop
            # Normalize tool calls to a consistent format
            tool_calls = self._normalize_tool_calls(tool_calls)

            # To enforce sequential narration: if the model batched multiple
            # tool calls, only keep the FIRST one. The rest are dropped so the
            # model loops back and can narrate before each subsequent call.
            if len(tool_calls) > 1:
                log.info(f"  Model batched {len(tool_calls)} tool calls — keeping only the first")
                tool_calls = tool_calls[:1]

            # ── Loop detection: catch the model calling the same tool
            # with the same arguments repeatedly (e.g. read_file on the
            # same path 2+ times). When detected, replace the tool call
            # with a nudge to try a DIFFERENT approach.
            clean_text = self._clean_content(content)
            tc0 = tool_calls[0]
            fn0 = tc0.get("function", tc0)
            tool_name = fn0.get("name", "")
            tool_args = fn0.get("arguments", {})

            # Build a hashable key from (tool_name, sorted args).
            # For file-related tools, normalize the path so that
            # "Desktop/foo.csv" and "/Users/x/Desktop/foo.csv" match.
            _norm_args = dict(tool_args) if isinstance(tool_args, dict) else {}
            if tool_name in ("read_file", "write_file", "edit_file") and "path" in _norm_args:
                try:
                    import os as _os
                    from pathlib import Path as _P
                    _norm_args["path"] = str(_P(_os.path.expanduser(_norm_args["path"])).resolve())
                except Exception:
                    pass
            try:
                args_key = json.dumps(_norm_args, sort_keys=True) if isinstance(_norm_args, dict) else str(tool_args)
            except (TypeError, ValueError):
                args_key = str(tool_args)
            call_signature = (tool_name, args_key)
            recent_tool_calls.append(call_signature)

            # Count how many times this exact call appeared in recent history
            duplicate_count = sum(1 for c in recent_tool_calls if c == call_signature)
            if duplicate_count >= 2:
                log.warning(f"Loop detected: {tool_name} called {duplicate_count} times with same args, breaking loop")
                loop_break_msg = (
                    f"[LOOP DETECTED: You have called {tool_name} with the same arguments "
                    f"{duplicate_count} times. You already have the result from this tool. "
                    f"STOP calling {tool_name} again. Instead, use the data you already have "
                    f"to complete the task. If the user asked you to update/modify/write something, "
                    f"call write_file or edit_file NOW with the changes. If you are stuck, "
                    f"tell the user what is blocking you instead of repeating the same tool call.]"
                )
                # Add the loop-break as a user message and skip this tool execution
                self.session.messages.append(ConversationMessage.user_text(loop_break_msg))
                emit("narration", text="Adjusting approach...")
                recent_tool_calls.clear()  # Reset so it can try fresh
                continue  # Loop back to call backend with the nudge

            # ALWAYS emit narration before tool calls. This is what Clyde
            # shows as "Let me..." before the tool status markers. If the
            # model provided text, use it. Otherwise generate a fallback.
            # This must be unconditional so Clyde always has something to show.

            if clean_text:
                emit("narration", text=clean_text)
            else:
                narration = self._generate_narration(tool_name, tool_args)
                emit("narration", text=narration or f"Using {tool_name}...")

            # Add assistant message with tool calls to session
            blocks = []
            if clean_text:
                blocks.append(TextBlock(text=clean_text))
            for tc in tool_calls:
                tc_id = tc.get("id", f"call_{uuid.uuid4().hex[:8]}")
                fn = tc.get("function", tc)
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                blocks.append(ToolUseBlock(id=tc_id, name=name, input=args))

            self.session.messages.append(
                ConversationMessage.assistant_with_blocks(blocks)
            )

            # Execute each tool call
            for tc in tool_calls:
                tc_id = tc.get("id", f"call_{uuid.uuid4().hex[:8]}")
                fn = tc.get("function", tc)
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}

                # ── ask_user: special handling ──
                # Instead of executing normally, emit a question event and
                # block until the user answers via /v1/answer.
                if name == "ask_user":
                    question_id = f"q_{uuid.uuid4().hex[:8]}"
                    question_text = args.get("question", "How would you like to proceed?")
                    choices = args.get("choices", [])
                    allow_other = args.get("allow_other", True)

                    log.info(f"  ask_user: {question_text} | choices={choices}")
                    emit("question",
                         id=question_id,
                         question=question_text,
                         choices=choices,
                         allow_other=allow_other)

                    # Block indefinitely until answer arrives.
                    # The model pauses here — no timeout. User takes as long
                    # as they need. Clyde posts to /v1/answer when ready.
                    #
                    # KV cache keepalive: poll the event every 60s so we can
                    # ping the backend and prevent llama.cpp from evicting
                    # the prompt cache slot during the wait.
                    self._pending_question_id = question_id
                    self._question_event.clear()
                    log.info(f"  ask_user: blocking until user responds (no timeout, KV keepalive every 60s)")
                    answered = False
                    while not answered:
                        answered = self._question_event.wait(timeout=60.0)
                        if not answered:
                            # Keepalive: touch health endpoint to signal activity
                            try:
                                httpx.get(f"{self.backend_url}/v1/models", timeout=2.0)
                            except Exception:
                                pass  # backend might be busy, that's fine

                    if answered and self._pending_answer:
                        result = f"User answered: {self._pending_answer}"
                        is_error = False
                        log.info(f"  ask_user result: {result}")
                    else:
                        # Should only happen if event is set without an answer
                        # (e.g. conversation cancelled). Treat as non-fatal.
                        result = "User dismissed the question without answering."
                        is_error = False
                        log.warning(f"  ask_user dismissed for {question_id}")

                    self._pending_question_id = None
                    self._pending_answer = None

                    # Emit tool_done for ask_user
                    emit("tool_done", name=name, is_error=is_error,
                         summary=result[:80] if not is_error else "timed out")
                    tool_calls_made += 1

                    self.session.messages.append(
                        ConversationMessage.tool_result(tc_id, name, result, is_error)
                    )
                    continue  # Skip normal tool execution below

                # ── Pre-tool context budget + dynamic compaction ──
                # Calculate dynamic thresholds based on the upcoming operation.
                # For read_file, estimate the file size and ensure we have room.
                # For other tools, use a conservative estimate.
                _compact_threshold = getattr(self, '_compact_threshold', 200000)
                _pre_tool_tokens = self.session.estimate_tokens()

                # Estimate how many tokens this tool call will ADD to context
                _estimated_tool_output = 2000  # Default conservative estimate
                if name == "read_file" and args.get("path"):
                    try:
                        from tools import _resolve_path
                        _target_path = _resolve_path(args["path"])
                        if _target_path.exists() and _target_path.is_file():
                            _estimated_tool_output = estimate_file_tokens(_target_path)
                    except Exception:
                        pass
                elif name == "bash":
                    _estimated_tool_output = 5000  # Bash can produce variable output
                elif name in ("grep_search", "glob_search"):
                    _estimated_tool_output = 3000

                # Dynamic pre-tool threshold: compact if current tokens + estimated
                # tool output would exceed the compact threshold, leaving 30K for
                # model response headroom.
                _response_headroom = 30000
                _needed = _pre_tool_tokens + _estimated_tool_output + _response_headroom
                if _needed > _compact_threshold:
                    log.warning(
                        f"Pre-tool compaction: ~{_pre_tool_tokens} current + "
                        f"~{_estimated_tool_output} estimated output + "
                        f"{_response_headroom} headroom = ~{_needed} > {_compact_threshold} threshold"
                    )
                    emit("compacting", estimated_tokens=_pre_tool_tokens, threshold=_compact_threshold)
                    try:
                        _preserve = getattr(self, '_preserve_recent', 4)
                        self.compact_if_needed(preserve_recent=_preserve, token_threshold=_pre_tool_tokens,
                                               on_progress=_compact_progress)
                        _after = self.session.estimate_tokens()
                        log.info(f"Pre-tool compaction done: {_pre_tool_tokens} → {_after} tokens")
                        emit("compact_done", tokens_before=_pre_tool_tokens, tokens_after=_after)
                        _pre_tool_tokens = _after  # Update for budget
                    except Exception as _e:
                        log.error(f"Pre-tool compaction failed: {_e}")

                # ── Update context budget for tools ──
                # This lets tools (especially read_file) adapt their output size
                # based on how much context is actually available.
                _total_budget = _compact_threshold  # Use compact threshold as effective max
                set_context_budget(_total_budget, _pre_tool_tokens)

                # Build a human-readable preview for the tool start tag
                tool_preview = self._tool_preview(name, args)
                emit("tool_start", name=name, args_preview=tool_preview)
                log.info(f"  Tool: {name}({json.dumps(args)[:100]})")

                # ── Patch 3 (Iteration 2): Phase-aware tool redirect ──
                # In writing/review phase, reject write_file / edit_file / bash
                # attempts that are really the model laundering a draft_section
                # into the wrong tool. In iteration 1 the model emitted
                #   write_file({"path":"$HOME/sec_5.md","content":"\"The"})
                #   bash({"command":"\"printf"})
                # when it should have called draft_section. Return a corrective
                # error so the model retries with the correct tool.
                _phase_for_redirect = self._detect_phase()
                if (
                    _phase_for_redirect in ("scoping", "research", "writing", "review")
                    and name in ("write_file", "edit_file", "bash")
                    and os.environ.get("CLYDE_PHASE_TOOL_REDIRECT", "1") == "1"
                ):
                    try:
                        from tools import (
                            get_research_outline, get_draft_sections,
                            _exec_draft_section, _exec_research_outline,
                            _exec_record_fact, get_recorded_facts,
                        )
                        _o = get_research_outline() or {}
                        _d = get_draft_sections() or {}

                        # ── Auto-redirect: extract content from write_file and
                        # inject it into the draft pipeline ──
                        _wf_content = ""
                        if name == "write_file":
                            _wf_content = args.get("content", "")
                            # Handle malformed JSON (model wraps in {"raw": "{\"path\":...}"})
                            if not _wf_content and "raw" in args:
                                try:
                                    _raw = json.loads(args["raw"])
                                    _wf_content = _raw.get("content", "")
                                except (json.JSONDecodeError, TypeError):
                                    _wf_content = str(args.get("raw", ""))
                        elif name == "bash":
                            _cmd = args.get("command", "")
                            # Extract content from printf/echo/cat heredoc
                            import re as _re_bash
                            _heredoc_match = _re_bash.search(r"(?:cat\s*<<|printf|echo)\s*['\"]?(.*)", _cmd, _re_bash.DOTALL)
                            if _heredoc_match:
                                _wf_content = _heredoc_match.group(1)

                        _wf_words = len(_wf_content.split()) if _wf_content else 0

                        # If content is substantial (≥100 words), auto-inject into pipeline
                        if _wf_words >= 100:
                            log.warning(
                                f"  Phase={_phase_for_redirect}: AUTO-REDIRECT {name} "
                                f"({_wf_words}w) → draft pipeline"
                            )
                            # Ensure outline exists
                            if not _o:
                                _user_msgs = [m for m in self.session.messages if m.role == "user"]
                                _topic = "Research Paper"
                                if _user_msgs:
                                    _first = _user_msgs[0].content[:500]
                                    import re as _re_topic2
                                    _topic_match = _re_topic2.search(
                                        r'(?:paper|essay|report|thesis)\s+(?:on|about)\s+(.+?)(?:\.|$)',
                                        _first, _re_topic2.IGNORECASE
                                    )
                                    if _topic_match:
                                        _topic = _topic_match.group(1).strip()[:200]
                                _exec_research_outline({
                                    "title": _topic,
                                    "sections": [
                                        "Introduction", "Historical Background",
                                        "Current State and Key Developments",
                                        "Technical Analysis",
                                        "Industry Landscape and Players",
                                        "Challenges and Limitations",
                                        "Future Outlook", "Conclusion",
                                    ],
                                    "target_words": 5000,
                                })
                                _o = get_research_outline() or {}
                                log.warning(f"  AUTO-REDIRECT: created default outline for '{_topic}'")

                            # Ensure facts exist (bypass P17 gate)
                            if len(get_recorded_facts() or []) < 4:
                                _auto_facts = self._extract_facts_from_context()
                                for _af in _auto_facts[:6]:
                                    _exec_record_fact(_af)
                                log.warning(f"  AUTO-REDIRECT: recorded {min(len(_auto_facts), 6)} facts from context")

                            # Split content into sections by headings or distribute evenly
                            _sections = _o.get("sections", [])
                            import re as _re_split
                            _heading_parts = _re_split.split(r'(?m)^#{1,3}\s+', _wf_content)
                            _heading_parts = [p.strip() for p in _heading_parts if p.strip() and len(p.split()) >= 30]

                            if len(_heading_parts) >= 3:
                                # Content has headings — map to outline sections
                                for i, part in enumerate(_heading_parts):
                                    if i < len(_sections):
                                        _sid = _sections[i]["id"]
                                        _exec_draft_section({"section_id": _sid, "content": part})
                                        log.info(f"    → drafted {_sid} ({len(part.split())}w)")
                            else:
                                # Single block — split evenly across pending sections
                                _pending_secs = [
                                    s for s in _sections
                                    if s.get("id") not in _d
                                    or _d.get(s.get("id"), {}).get("words", 0) < 200
                                ]
                                if _pending_secs:
                                    _chunk_size = max(300, _wf_words // len(_pending_secs))
                                    _words_list = _wf_content.split()
                                    _offset = 0
                                    for _ps in _pending_secs:
                                        _chunk = " ".join(_words_list[_offset:_offset + _chunk_size])
                                        if _chunk.strip():
                                            # Auto-add [1] citation if missing
                                            if "[" not in _chunk:
                                                _chunk = _chunk.rstrip() + " [1]"
                                            _exec_draft_section({"section_id": _ps["id"], "content": _chunk})
                                            log.info(f"    → drafted {_ps['id']} ({len(_chunk.split())}w)")
                                        _offset += _chunk_size

                            _d = get_draft_sections() or {}
                            _total_w = sum(d.get("words", 0) for d in _d.values())
                            result = (
                                f"Content auto-captured into draft pipeline ({_total_w} words "
                                f"across {len(_d)} sections).\n"
                                f"Your content has been saved. Now call self_grade to evaluate "
                                f"the paper and generate the final .docx."
                            )
                            is_error = False
                            emit("tool_done", name=name, is_error=False,
                                 summary=f"auto-redirected {_wf_words}w → {len(_d)} draft sections")
                            tool_calls_made += 1
                            self.session.messages.append(
                                ConversationMessage.tool_result(tc_id, name, result, is_error)
                            )
                            continue

                        # Short/empty content from write_file — return error as before
                        _pending_sections = [
                            s for s in _o.get("sections", [])
                            if s.get("id") not in _d
                            or _d.get(s.get("id"), {}).get("words", 0) < 200
                        ]
                        _next_sec = _pending_sections[0] if _pending_sections else None
                        if _next_sec:
                            _redirect_msg = (
                                f"ERROR: {name} is NOT the right tool during the "
                                f"{_phase_for_redirect.upper()} phase. You are writing a "
                                f"research paper — section bodies go through draft_section, "
                                f"not {name}.\n\n"
                                f"REQUIRED: call draft_section with:\n"
                                f"  section_id: '{_next_sec.get('id','?')}'\n"
                                f"  content: the FULL body text ({_next_sec.get('target_words', 500)} words) "
                                f"for '{_next_sec.get('title','?')}' with inline citations [1], [2].\n\n"
                                f"Do NOT use write_file, edit_file, or bash to save sections. "
                                f"The self_grade tool auto-saves the final paper as a .docx."
                            )
                        else:
                            _redirect_msg = (
                                f"ERROR: {name} is NOT the right tool during the "
                                f"{_phase_for_redirect.upper()} phase. All sections are drafted. "
                                f"Call self_grade next — it evaluates the paper and auto-saves it "
                                f"as a .docx. Do NOT use {name}."
                            )
                        log.warning(
                            f"  Phase={_phase_for_redirect}: redirecting {name} → draft_section/self_grade"
                        )
                        result = _redirect_msg
                        is_error = True
                        emit("tool_done", name=name, is_error=True,
                             summary=f"redirected → draft_section (phase={_phase_for_redirect})")
                        tool_calls_made += 1
                        self.session.messages.append(
                            ConversationMessage.tool_result(tc_id, name, result, is_error)
                        )
                        continue
                    except Exception as _re_err:
                        log.warning(f"  Phase redirect check failed: {_re_err}")

                # Execute with timeout protection
                try:
                    result = self.tool_registry.execute(name, args)
                except Exception as e:
                    log.error(f"  Tool execution crashed: {name}: {e}")
                    result = f"ERROR: Tool {name} failed: {e}"

                # ── PREEMPT_COMPACT handling ──
                # If a tool (typically read_file) signals it needs more context,
                # compact now and retry the tool call once.
                if result.startswith("PREEMPT_COMPACT:"):
                    log.warning(f"PREEMPT_COMPACT signal from {name}: {result[:200]}")
                    emit("compacting", estimated_tokens=self.session.estimate_tokens(),
                         threshold=_compact_threshold)
                    try:
                        _preserve = getattr(self, '_preserve_recent', 4)
                        # Force compaction by using current token count as threshold
                        self.compact_if_needed(
                            preserve_recent=_preserve,
                            token_threshold=self.session.estimate_tokens(),
                            on_progress=_compact_progress
                        )
                        _after = self.session.estimate_tokens()
                        log.info(f"PREEMPT compaction done, now ~{_after} tokens")
                        emit("compact_done", tokens_before=_pre_tool_tokens, tokens_after=_after)

                        # Update budget and retry the tool
                        set_context_budget(_total_budget, _after)
                        try:
                            result = self.tool_registry.execute(name, args)
                        except Exception as e2:
                            log.error(f"  Tool retry after PREEMPT failed: {name}: {e2}")
                            result = f"ERROR: Tool {name} failed after compaction: {e2}"

                        # If still PREEMPT after compaction, the file is genuinely
                        # too large — let the error message through as-is
                        if result.startswith("PREEMPT_COMPACT:"):
                            result = (
                                f"ERROR: File still too large after compaction. "
                                f"~{_after} tokens in use. "
                                f"Use offset/limit params or bash grep to read portions."
                            )
                    except Exception as _ce:
                        log.error(f"PREEMPT compaction failed: {_ce}")
                        result = (
                            f"ERROR: Could not free enough context to read this file. "
                            f"Use offset/limit params or bash grep to read portions."
                        )

                # ── Folder Permission Interception ──
                # If a tool hit PERMISSION_DENIED, ask the user via the existing
                # question card UI. If they allow, grant the folder and re-run.
                if result.startswith("PERMISSION_DENIED:"):
                    import re as _re
                    # Extract the path from the error message
                    _path_match = _re.search(r"Cannot access (.+?)\.", result)
                    _denied_path = _path_match.group(1) if _path_match else "unknown"

                    # Figure out the top-level folder to request
                    from pathlib import Path as _P
                    _resolved = _P(_denied_path).resolve()
                    _home = _P.home()
                    _target = _resolved if _resolved.is_dir() else _resolved.parent
                    # Walk up to the first child of $HOME (or stop at $HOME itself)
                    while (_target != _home
                           and _target.parent != _target
                           and _target.parent != _home):
                        _target = _target.parent
                    _display = str(_target).replace(str(_home), "~")

                    # Auto-deny dangerous directories — root, /System, /usr, etc.
                    # These are always wrong (model used a bad relative path).
                    _dangerous = {_P("/"), _P("/System"), _P("/usr"), _P("/bin"),
                                  _P("/sbin"), _P("/var"), _P("/etc"), _P("/tmp"),
                                  _P("/private"), _P("/Library"), _P("/Applications")}
                    if _target in _dangerous or _target == _P("/"):
                        log.warning(f"  Auto-denied dangerous directory: {_target}")
                        result = (
                            f"ERROR: Access to {_target} was auto-denied (system/root directory). "
                            f"You used a relative path '{args.get('path', '')}' that resolved to "
                            f"a system directory. Use an ABSOLUTE path starting with ~ or /Users/. "
                            f"If you accessed this file before, check the previous tool result for "
                            f"the correct absolute path."
                        )
                        is_error = True
                        emit("tool_done", name=name, is_error=True, summary="bad path — use absolute path")
                        tool_calls_made += 1
                        self.session.messages.append(
                            ConversationMessage.tool_result(tc_id, name, result, is_error)
                        )
                        continue  # Skip permission prompt, go to next iteration

                    # Describe what the tool was trying to do
                    if name == "bash":
                        _action = f'Run command: {args.get("command", "")[:120]}'
                    elif name == "read_file":
                        _action = f'Read file: {args.get("path", "")}'
                    elif name == "write_file":
                        _action = f'Write file: {args.get("path", "")}'
                    elif name == "edit_file":
                        _action = f'Edit file: {args.get("path", "")}'
                    elif name == "glob_search":
                        _action = f'Search files in: {args.get("path", "")}'
                    elif name == "grep_search":
                        _action = f'Search content in: {args.get("path", "")}'
                    else:
                        _action = f'Access path: {_denied_path}'

                    _question_text = (
                        f"Clyde needs access to {_display}\n\n"
                        f"Action: {_action}\n\n"
                        f"Full path: {_target}"
                    )

                    log.info(f"  Permission denied for {_target}, asking user...")
                    _perm_q_id = f"q_{uuid.uuid4().hex[:8]}"
                    emit("question",
                         id=_perm_q_id,
                         question=_question_text,
                         choices=["Allow", "Deny"],
                         allow_other=False)

                    self._pending_question_id = _perm_q_id
                    self._question_event.clear()
                    _answered = self._question_event.wait(timeout=120)

                    if _answered and self._pending_answer:
                        _answer = self._pending_answer
                        self._pending_question_id = None
                        self._pending_answer = None

                        if _answer.startswith("Allow"):
                            from tools import grant_folder, _grant_organize_dir
                            grant_folder(str(_target))
                            # Also approve for organize tools (so batch_read/write don't re-ask)
                            if name.startswith("organize_"):
                                _state_dir = args.get("state_dir", "")
                                if _state_dir:
                                    _grant_organize_dir(_state_dir)
                            log.info(f"  User granted access to {_target}")
                            emit("tool_done", name=name, is_error=False,
                                 summary=f"access granted to {_display}")

                            # Re-execute the tool now that permission is granted
                            try:
                                result = self.tool_registry.execute(name, args)
                            except Exception as e:
                                result = f"ERROR: Tool {name} failed: {e}"
                            # Fall through to normal result processing below
                        else:
                            result = f"Access to {_display} was denied by the user."
                            emit("tool_done", name=name, is_error=True,
                                 summary=f"access denied to {_display}")
                    else:
                        self._pending_question_id = None
                        self._pending_answer = None
                        result = f"Access to {_display} was denied (no response)."
                        emit("tool_done", name=name, is_error=True, summary="permission timed out")

                    # Record the result and continue
                    is_error = "denied" in result.lower() or result.startswith("ERROR")
                    tool_calls_made += 1
                    self.session.messages.append(
                        ConversationMessage.tool_result(tc_id, name, result, is_error)
                    )
                    continue  # Skip the normal tool_done/result below

                is_error = result.startswith("ERROR")

                # ── Adaptive result truncation ──
                # Instead of a fixed 30K char cap, calculate based on available
                # context. This ensures tool output fits without triggering
                # immediate compaction, while maximizing information density.
                _current_tokens = self.session.estimate_tokens()
                _available_tokens = max(0, _compact_threshold - _current_tokens - 30000)
                # Convert to chars: available tokens × 4, with floor and ceiling
                _adaptive_max_chars = max(8000, min(_available_tokens * 4, 120000))
                MAX_RESULT_CHARS = _adaptive_max_chars
                if len(result) > MAX_RESULT_CHARS:
                    original_len = len(result)
                    half = MAX_RESULT_CHARS // 2
                    result = (
                        result[:half]
                        + f"\n\n[...truncated {original_len - MAX_RESULT_CHARS} chars...]\n\n"
                        + result[-half:]
                    )
                    log.info(f"  Truncated result from {original_len} to ~{MAX_RESULT_CHARS} chars (adaptive)")

                # Track turn output budget
                _result_tokens = len(result) // 4
                turn_output_tokens += _result_tokens
                if turn_output_tokens > TURN_OUTPUT_CAP:
                    log.warning(
                        f"Turn output cap reached: {turn_output_tokens} tokens "
                        f"(cap: {TURN_OUTPUT_CAP}). Forcing compaction."
                    )
                    # Don't block the result, but force compaction before next iteration.
                    # Only compact if conversation context is actually large — output
                    # cap can fire from a single huge tool result, but if total
                    # context is tiny, compaction destroys completed work.
                    _toc_before = self.session.estimate_tokens()
                    _TOC_COMPACT_MIN_TOKENS = 10000
                    if _toc_before < _TOC_COMPACT_MIN_TOKENS:
                        log.info(
                            f"Turn output cap: skipping compaction "
                            f"({_toc_before} < {_TOC_COMPACT_MIN_TOKENS} tokens) "
                            f"— resetting output counter only"
                        )
                        turn_output_tokens = 0  # Reset counter, preserve context
                    else:
                        emit("compacting", estimated_tokens=_toc_before,
                             threshold=_compact_threshold)
                        try:
                            _preserve = getattr(self, '_preserve_recent', 4)
                            self.compact_if_needed(
                                preserve_recent=_preserve,
                                token_threshold=_toc_before,
                                on_progress=_compact_progress
                            )
                            _after = self.session.estimate_tokens()
                            emit("compact_done", tokens_before=_toc_before,
                                 tokens_after=_after)
                            turn_output_tokens = 0  # Reset after compaction
                        except Exception as _e:
                            log.error(f"Turn output cap compaction failed: {_e}")
                            emit("compact_done", tokens_before=_toc_before,
                                 tokens_after=_toc_before, error=str(_e))

                # Build a short summary for the tool done tag
                tool_summary = self._tool_summary(name, args, result, is_error)
                emit("tool_done", name=name, is_error=is_error, summary=tool_summary, output=result)
                log.info(f"  Result: {result[:200]}")
                tool_calls_made += 1

                self.session.messages.append(
                    ConversationMessage.tool_result(tc_id, name, result, is_error)
                )

                # v2 phase controller: update state + check phase transition
                try:
                    new_phase = skills_mod.notify_tool_result(
                        self._conversation_id, name, result
                    )
                    if new_phase and new_phase != "__complete__":
                        log.info(f"Phase transition → {new_phase}")
                        emit("phase_change", phase=new_phase)
                        # Inject phase hint so model knows what tools are available
                        _hint = skills_mod.get_phase_hint(self._conversation_id)
                        if _hint:
                            self.session.messages.append(
                                ConversationMessage.user_text(
                                    f"[SYSTEM: Phase changed to {new_phase}. {_hint}]"
                                )
                            )
                    elif new_phase == "__complete__":
                        log.info("All phases complete — presenting output")
                except Exception as _pe:
                    pass  # Phase tracking must never break tool execution

            # Mid-turn compaction: if context is getting large, compact now
            # to prevent OOM / context overflow before the next model call.
            # Dynamic threshold: leave enough room for max_tokens response +
            # a safety buffer, rather than using a fixed percentage.
            mid_turn_threshold = getattr(self, '_compact_threshold', 200000)
            _max_response = getattr(self, 'max_tokens', 131072)
            # Compact if current usage + max possible response would exceed threshold
            # Add 10K buffer for tool definitions and message framing
            mid_turn_limit = mid_turn_threshold - _max_response - 10000
            mid_turn_limit = max(mid_turn_limit, int(mid_turn_threshold * 0.40))  # Floor at 40%
            estimated_tokens = self.session.estimate_tokens()
            if estimated_tokens > mid_turn_limit:
                log.warning(f"Mid-turn compaction triggered: ~{estimated_tokens} tokens > {mid_turn_limit} limit")
                emit("compacting", estimated_tokens=estimated_tokens, threshold=mid_turn_limit)
                try:
                    preserve = getattr(self, '_preserve_recent', 4)
                    self.compact_if_needed(
                        preserve_recent=preserve,
                        token_threshold=mid_turn_limit,
                        on_progress=_compact_progress
                    )
                    after_tokens = self.session.estimate_tokens()
                    log.info(f"Mid-turn compaction done, now ~{after_tokens} tokens")
                    emit("compact_done", tokens_before=estimated_tokens, tokens_after=after_tokens)

                    # After mid-turn compaction, inject graph recovery context
                    # so the model knows where it was in the organizer pipeline
                    _graph_recovery = self._get_graph_recovery_context()
                    if _graph_recovery:
                        self.session.messages.append(
                            ConversationMessage.user_text(
                                f"[SYSTEM: Context compacted mid-turn. {_graph_recovery}\n"
                                f"Resume from the RESUME directive above. Call next tool now.]"
                            )
                        )
                        log.info("Injected graph recovery context after mid-turn compaction")
                except Exception as e:
                    log.error(f"Mid-turn compaction failed: {e}")
                    emit("compact_done", tokens_before=estimated_tokens, tokens_after=estimated_tokens, error=str(e))

            # ── Skill-based inference cap (auto-continue) ──
            # For skills with max_tool_calls_per_turn, we cap each MODEL
            # INFERENCE to that many tool calls (limiting thinking-token waste),
            # but auto-inject a "[continue]" user message so the turn keeps
            # going without real user input. The turn only truly ends when:
            #   - The model calls ask_user (needs real user input)
            #   - The model produces no tool calls (natural stop)
            #   - A hard outer cap (40 calls) is reached
            _active_skill = skills_mod.get_active_skill(self._conversation_id)
            _inference_cap = getattr(_active_skill, 'max_tool_calls_per_turn', 100)
            _hard_cap = 80  # absolute max — enough for 300 items at 15/batch

            # Check if last tool was ask_user — if so, the turn must end
            # to wait for real user input
            _last_tool_name = recent_tool_calls[-1][0] if recent_tool_calls else ""
            _last_tool_result = recent_tool_calls[-1][1] if (recent_tool_calls and len(recent_tool_calls[-1]) > 1) else ""

            if tool_calls_made >= _hard_cap:
                log.info(f"  Hard cap ({_hard_cap}) reached, ending turn")
                if not final_text:
                    final_text = self._clean_content(content) if content else ""
                self.session.messages.append(
                    ConversationMessage.user_text(
                        f"[TURN LIMIT: {tool_calls_made} tool calls. STOP. Present progress to the user.]"
                    )
                )
                break

            _did_auto_continue = False
            if (_inference_cap < 100
                    and tool_calls_made >= _inference_cap
                    and tool_calls_made % _inference_cap == 0
                    and _last_tool_name != "ask_user"):
                # Auto-continue: inject a synthetic continue so the model
                # keeps working without waiting for real user input.
                log.info(f"  Skill '{_active_skill.name}' inference cap ({_inference_cap}) — auto-continuing (total: {tool_calls_made})")
                emit("status", text=f"Working... ({tool_calls_made} steps)")
                self.session.messages.append(
                    ConversationMessage.user_text(
                        "[AUTO-CONTINUE: Keep working. Call next tool now.]"
                    )
                )
                _did_auto_continue = True
                # Don't break — let the loop continue to the next model call

            # Nudge: after tool execution, remind the model what to do next.
            # SKIP if auto-continue just fired — those instructions take priority.
            if tool_calls_made > 0 and not _did_auto_continue:
                last_tool = _last_tool_name
                last_result = _last_tool_result

                if last_tool == "ask_user":
                    # After ask_user, the user already made their choice.
                    nudge = (
                        "[Instruction: The user just answered your question via the interactive prompt. "
                        "ACT on their answer NOW. If they said 'Send', call the send tool (messages_send, "
                        "mail_send, etc.) immediately. If they said 'Cancel', acknowledge and stop. "
                        "Do NOT call ask_user again — they already answered. Do NOT call memory_read "
                        "or any other tool first. Execute the confirmed action IMMEDIATELY.]"
                    )
                elif last_tool == "read_file":
                    nudge = (
                        "[Instruction: You now have the file contents. Present a brief summary to the user. "
                        "If the user asked you to update, modify, or change the file, call write_file or "
                        "edit_file NOW with the updated content. Do NOT call read_file again on the same file — "
                        "you already have its contents. Move forward to the next action step.]"
                    )
                else:
                    nudge = (
                        "[Instruction: Present the tool result to the user with a brief explanation. "
                        "If there are more steps, describe what you will do next AND call the next tool "
                        "in the same response. Do NOT repeat the same tool call you just made. "
                        "Progress to the NEXT step of the task.]"
                    )
                self.session.messages.append(ConversationMessage.user_text(nudge))

        # Save transcript
        try:
            raw_messages = [{"role": "user", "content": user_input}]
            save_transcript(raw_messages, final_text)
        except Exception as e:
            log.warning(f"Failed to save transcript: {e}")

        # Persist session to disk so conversations survive agent restarts
        self.save_session()

        return TurnSummary(
            assistant_text=final_text,
            tool_calls_made=tool_calls_made,
            iterations=iterations,
        )

    def _build_api_messages(self) -> list[dict]:
        """Convert session messages to OpenAI format for the backend."""
        active_skill = skills_mod.get_active_skill(self._conversation_id)
        _is_external = not any(p in self.backend_url for p in [":8810", ":8814", ":8815"])
        system_prompt = render_system_prompt(
            active_skill=active_skill,
            compact=_is_external,
            model_name=self.backend_model,
        )

        # v2 phase hint: append current phase instructions to system prompt
        _phase_hint = skills_mod.get_phase_hint(self._conversation_id)
        if _phase_hint:
            _phase_state = skills_mod.get_phase_state(self._conversation_id)
            _phase_idx = _phase_state.current_phase_idx
            _phases = active_skill.phases
            _phase_name = _phases[_phase_idx].name if _phase_idx < len(_phases) else "complete"
            system_prompt += (
                f"\n\n# Current Phase: {_phase_name.upper()}\n\n"
                f"{_phase_hint}\n\n"
                f"IMPORTANT: Only the tools listed above are available in this phase. "
                f"Do not try to call tools from other phases."
            )

        messages = [{"role": "system", "content": system_prompt}]

        for msg in self.session.messages:
            messages.append(msg.to_openai_message())

        return messages

    def _detect_phase(self) -> str:
        """Detect the current research pipeline phase from tool-state.

        Returns one of: 'scoping', 'research', 'synthesis', 'writing', 'review', 'general'.
        Used for phase-aware sampling (lower temperature during structured phases).
        """
        try:
            from tools import (
                get_research_outline,
                get_recorded_facts,
                get_draft_sections,
                get_research_call_count,
            )
            outline = get_research_outline()
            facts = get_recorded_facts() or []
            drafts = get_draft_sections() or {}
            research_calls = get_research_call_count()

            if not outline:
                # ── Fix: Research skill active but no outline yet ──
                # When the Research skill is active (model was asked to write a
                # paper), treat the conversation as in-pipeline even if the model
                # hasn't called research_outline yet. This ensures strict tool
                # enforcement fires and correction nudges guide the model.
                try:
                    import skills as _sk
                    _active = _sk.get_active_skill(self._conversation_id)
                    if _active and _active.name == "research":
                        if research_calls >= 3:
                            return "writing"  # enough research, model should write
                        elif research_calls > 0:
                            return "research"
                        else:
                            return "scoping"
                except Exception:
                    pass
                return "general"
            if not facts and research_calls < 3:
                return "scoping"
            if drafts:
                # If any section still missing and there's no self_grade yet, we're writing.
                total_sections = len(outline.get("sections", []))
                done = sum(1 for d in drafts.values() if d.get("words", 0) > 0)
                if done >= total_sections:
                    return "review"
                return "writing"
            # ── Patch 6 / 7 (Iterations 3-4): advance to writing once research done ──
            # Previous thresholds kept phase in "research" too long. If outline +
            # facts exist, move to writing so Patch 3 fires for write_file/bash junk
            # and anti-narration prose rejection activates. Models don't need much
            # research before they should start drafting — 3 facts is sufficient.
            if len(facts) >= 6 or research_calls >= 8:
                return "writing"
            if len(facts) >= 3:
                return "writing"
            if research_calls >= 4:
                return "writing"
            return "research"
        except Exception:
            return "general"

    def _phase_sampling_params(self, phase: str) -> dict:
        """Return sampling params tuned for a given phase.

        Phase-B precedence:
          1. profile.phase_overrides[phase] (from model_profiles/<id>.yaml)
          2. profile.sampling_defaults
          3. env-var fallbacks (legacy iter22/23 knob #1)

        Env vars still win over profile defaults when set explicitly, so the
        existing benchmarking harness can A/B without editing YAML.
        """
        temp_structured = float(os.environ.get("CLYDE_TEMP_STRUCTURED", "0.3"))
        temp_free = float(os.environ.get("CLYDE_TEMP_FREE", str(self.temperature)))
        top_p_env = os.environ.get("CLYDE_TOP_P")
        rep_pen_env = os.environ.get("CLYDE_REPETITION_PENALTY")
        rep_ctx = int(os.environ.get("CLYDE_REPETITION_CONTEXT_SIZE", "128"))

        structured_phases = {"scoping", "research", "writing", "review"}
        legacy_temperature = temp_structured if phase in structured_phases else temp_free
        legacy_top_p = float(top_p_env) if top_p_env is not None else 0.92
        legacy_rep_pen = float(rep_pen_env) if rep_pen_env is not None else 1.05

        # Profile layer (Phase B)
        prof = getattr(self, "profile", None)
        po = None
        samp_def = {}
        if prof is not None:
            po = prof.phase_overrides.get(phase) if getattr(prof, "phase_overrides", None) else None
            samp_def = getattr(prof, "sampling_defaults", {}) or {}

        def _pick(po_attr, def_key, env_val, legacy_val):
            # env > phase_override > sampling_default > legacy
            if env_val is not None:
                return env_val
            if po is not None and getattr(po, po_attr, None) is not None:
                return getattr(po, po_attr)
            if def_key in samp_def and samp_def[def_key] is not None:
                return samp_def[def_key]
            return legacy_val

        # Temperature env-fallback is special: CLYDE_TEMP_STRUCTURED only
        # applies as legacy. The profile's phase_overrides.temperature always
        # beats it unless neither CLYDE_TEMP_STRUCTURED nor
        # CLYDE_TEMP_FREE is explicitly set.
        explicit_temp_env = (
            "CLYDE_TEMP_STRUCTURED" in os.environ
            or "CLYDE_TEMP_FREE" in os.environ
        )
        temperature = _pick(
            "temperature", "temperature",
            legacy_temperature if explicit_temp_env else None,
            legacy_temperature,
        )
        top_p = _pick("top_p", "top_p", float(top_p_env) if top_p_env is not None else None, legacy_top_p)
        rep_pen = _pick(
            "repetition_penalty", "repetition_penalty",
            float(rep_pen_env) if rep_pen_env is not None else None, legacy_rep_pen,
        )

        return {
            "temperature": float(temperature),
            "top_p": float(top_p),
            "repetition_penalty": float(rep_pen),
            "repetition_context_size": rep_ctx,
        }

    def _call_backend(self, messages: list[dict], skip_health_check: bool = False, emit=None) -> dict:
        """
        Call the MLX backend using STREAMING mode with inactivity timeout.

        Streaming solves the fundamental timeout problem: instead of guessing
        how long a response will take (impossible with variable-length output),
        we stream tokens and only timeout if NO new data arrives for 60s.

        A fast 500-token response completes in ~15s.
        A slow 5000-token response takes ~3min but never triggers timeout
        because tokens keep arriving.
        A stuck/degenerate generation triggers timeout after 60s of silence.

        emit: optional callback(event_type, **kwargs) for real-time progress.
              Used to stream thinking tokens to the UI as they arrive.

        Returns the same dict format as the non-streaming version so the
        rest of the code doesn't need to change.
        """
        import time as _time

        # Re-resolve backend URL in case the user switched models or
        # ProcessManager relaunched on a different port.
        self._refresh_backend_url()

        # ─── Phase detection for phase-aware sampling (Knob #1 + #3) ───
        _phase = self._detect_phase()
        _sampling = self._phase_sampling_params(_phase)
        log.info(f"Phase detected: {_phase} | sampling: T={_sampling['temperature']:.2f} "
                 f"top_p={_sampling['top_p']:.2f} rep_pen={_sampling['repetition_penalty']:.2f}")

        # ── Patch 8 (Iteration 4): phase-aware max_tokens ──
        # Writing phase needs room for long draft_section arguments. 2048 was too
        # tight — a 500-word section body = ~1000 tokens of content + overhead for
        # thinking tags + tool_call JSON structure. Raise to 6000 in writing/review.
        # P36: when thinking is enabled, raise the per-turn ceiling so thinking tokens
        # plus content/tool_calls fit within budget. Thinking-off keeps original tight floor.
        _thinking_on = os.environ.get("CLYDE_THINKING_MODE", "off").lower() != "off"
        # Phase-A: delegate to helper so ModelProfile.phase_overrides can own these.
        _phase_max_tokens_floor = self._max_tokens_floor_for_phase(_phase, _thinking_on)
        payload = {
            "model": self.backend_model,
            "messages": messages,
            "max_tokens": min(self.max_tokens, _phase_max_tokens_floor),
            "temperature": _sampling["temperature"],
            "top_p": _sampling["top_p"],
            "repetition_penalty": _sampling["repetition_penalty"],
            "repetition_context_size": _sampling["repetition_context_size"],
            "stream": True,
            "tools": self._get_phase_filtered_tools(),
            # Prompt cache: llama.cpp's big win on tool-loop turns where
            # the first N messages are identical across iterations. The
            # mlx_vlm OpenAI shim ignores unknown keys, so forwarding is
            # safe on every route — free on mlx, massive on llama.cpp.
            "cache_prompt": True,
            **self._thinking_payload(_thinking_on),
        }

        # Structured CoT: constrain <think> block with a tiny GBNF grammar.
        # 22x fewer thinking tokens, same accuracy (andthattoo 2026-04-25).
        # CANNOT use grammar + tools simultaneously on llama.cpp (400 error).
        # Only send grammar when tools array is empty (rare — mostly for
        # direct Q&A without tool access). For tool-call turns, thinking
        # is unconstrained — the agent's degeneration detection handles it.
        _has_tools = bool(payload.get("tools"))
        if _thinking_on and not _has_tools:
            _think_grammar = self._get_think_grammar(_phase)
            if _think_grammar:
                payload["grammar"] = _think_grammar
        # Knob #3: Structured output forcing.
        # During writing/review phases, strongly encourage tool calls over free-form
        # prose. tool_choice="required" tells MLX-LM (when supported) to emit a tool
        # call. If the server ignores it, fall back to our recovery loop, which will
        # inject a correction message on non-tool-call outputs.
        _tool_choice_required_phases = os.environ.get(
            "CLYDE_TOOL_CHOICE_REQUIRE_PHASES", "writing,review"
        ).split(",")
        if _phase in {p.strip() for p in _tool_choice_required_phases if p.strip()}:
            payload["tool_choice"] = "required"

        INACTIVITY_TIMEOUT = 60.0  # Kill if no new data for 60s
        CONNECT_TIMEOUT = 30.0     # Max time to establish connection
        PREFILL_TIMEOUT = 120.0    # Max time for first token (prefill phase)

        # Quick health check before committing. Skip if we confirmed alive
        # recently (within 30s) to avoid adding 1-3s TTFT on every turn.
        import time as _time_hc2
        _now = _time_hc2.monotonic()
        _last_ok = getattr(self, '_last_health_ok', 0.0)
        _health_cache_ttl = 30.0  # trust "alive" for 30s

        if not skip_health_check and (_now - _last_ok) > _health_cache_ttl:
            try:
                _active_port = int(self.backend_url.rstrip("/").split(":")[-1])
            except Exception:
                _active_port = 0
            # Managed backends on 8814/8815 can cold-load for a while.
            # Local llama.cpp on 8810 warms fast (already-mmap'd).
            _deadline = 60.0 if _active_port in (8814, 8815) else 6.0
            _poll_interval = 2.0
            import time as _time_hc
            _start = _time_hc.monotonic()
            _reachable = False
            _last_err = ""
            _emitted_warming = False
            while _time_hc.monotonic() - _start < _deadline:
                try:
                    with httpx.Client(timeout=3.0) as hc:
                        r = hc.get(f"{self.backend_url}/v1/models")
                    if r.status_code == 200:
                        _reachable = True
                        break
                    # 503 = warming (llama.cpp behavior). Retry.
                    _last_err = f"HTTP {r.status_code}"
                except Exception as _e:
                    _last_err = repr(_e)
                # Emit ONE recovering event so the UI reflects the wait.
                if emit and not _emitted_warming:
                    emit("recovering", stage="warming",
                         detail="Backend loading model…")
                    _emitted_warming = True
                _time_hc.sleep(_poll_interval)
            if not _reachable:
                log.warning(
                    f"Backend health check failed after {_deadline:.0f}s — "
                    f"server on :{_active_port or '?'} is down ({_last_err})"
                )
                self._last_health_ok = 0.0  # force recheck next time
                return {"error": "Cannot connect to model server. Attempting auto-recovery...", "mlx_down": True}
            self._last_health_ok = _time_hc2.monotonic()  # cache success
            if _emitted_warming and emit:
                emit("recovery_done", detail="Backend ready")
        elif skip_health_check or (_now - _last_ok) <= _health_cache_ttl:
            pass  # health cached or skipped — no delay

        log.info(f"Calling backend (streaming, {INACTIVITY_TIMEOUT}s inactivity timeout, {len(messages)} messages)")

        # Accumulate the streamed response into OpenAI-compatible format
        content_parts = []
        thinking_parts = []
        tool_calls_map = {}  # index → {id, type, function: {name, arguments}}
        finish_reason = None
        first_token_received = False
        model_name = self.backend_model
        # Inline <think>...</think> splitter state — initialised on first
        # content delta; see the splitter block below for semantics.
        _inline_think_state = None

        # ── Live metrics: tok/sec + phase tracking ──
        # Approximate token count via word count (≈1 word = ~1 token avg).
        # Emit a `metrics` event every ~0.5s during gen so Clyde can show
        # a live tok/s readout under the streaming cursor.
        _metrics_first_tok_time: float | None = None
        _metrics_last_emit_time: float = 0.0
        _metrics_chars_total: int = 0
        _metrics_thinking_chars: int = 0
        _metrics_in_inline_think: bool = False  # True when inside <think> in content stream
        _metrics_emit_interval = 0.5  # seconds

        def _maybe_emit_metrics(phase: str, *, force: bool = False) -> None:
            nonlocal _metrics_last_emit_time, _metrics_first_tok_time
            now_ = _time.monotonic()
            if _metrics_first_tok_time is None:
                _metrics_first_tok_time = now_
            elapsed = now_ - _metrics_first_tok_time
            # Skip the first ~300 ms — tok count is still tiny, denominator
            # is near-zero, and the computed tps flies up to 1000+ tok/s.
            # That's the spike the user saw. Wait until we have a real
            # sample window before emitting.
            if elapsed < 0.3:
                return
            if not force and (now_ - _metrics_last_emit_time) < _metrics_emit_interval:
                return
            _metrics_last_emit_time = now_
            # Rough token count: chars/4 is the standard heuristic
            tok_total = max(1, (_metrics_chars_total + _metrics_thinking_chars) // 4)
            tok_per_sec = tok_total / elapsed
            # Clamp the reported tps to a sane ceiling — no real local
            # decoder does >500 tok/s, so anything past that is a
            # measurement artifact (prefill tail, thinking dump, etc.).
            if tok_per_sec > 500:
                tok_per_sec = 500.0
            emit("metrics",
                 phase=phase,
                 tokens=tok_total,
                 elapsed=round(elapsed, 2),
                 tps=round(tok_per_sec, 1),
                 thinking_chars=_metrics_thinking_chars,
                 content_chars=_metrics_chars_total)

        # Dynamic prefill timeout. Cold prompt-eval at ~100 tok/s means a
        # 17K-token request needs ~3 minutes to first token, comfortably
        # past the static 120s floor. Estimate prompt size from message
        # content (~4 chars/token) and grant proportional patience.
        # Empirically observed: skill-switch mid-conversation invalidates
        # the prompt cache and the entire context must be re-evaluated;
        # without this scaling the agent reports "could not recover" while
        # llama-server is still happily processing the prompt.
        _total_chars = 0
        for _m in messages:
            _c = _m.get("content")
            if isinstance(_c, str):
                _total_chars += len(_c)
            elif isinstance(_c, list):
                for _part in _c:
                    if isinstance(_part, dict):
                        _t = _part.get("text") or _part.get("content") or ""
                        if isinstance(_t, str):
                            _total_chars += len(_t)
        _estimated_tokens = max(1, _total_chars // 4)
        _prefill_timeout_dynamic = max(
            PREFILL_TIMEOUT,
            float(_estimated_tokens) / 100.0 * 2.0 + 30.0,
        )
        log.info(
            "Prefill read timeout: %.1fs (est %d tokens, %d chars)",
            _prefill_timeout_dynamic, _estimated_tokens, _total_chars,
        )

        try:
            with httpx.Client(timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT,
                read=_prefill_timeout_dynamic,  # Scales with prompt size
                write=30.0,
                pool=30.0,
            )) as client:
                # Phase 2c.A prefix cache: emit `x-clyde-cache-key: <conv_id>`
                # so forked mlx-flash's PrefixCacheStore can skip prefill on
                # turn 2+ of this conversation. Silently ignored by other
                # backends (llama.cpp, Ollama, OpenAI) — they have no such
                # header, plain HTTP.
                _prefix_headers: dict[str, str] = {}
                if self._conversation_id:
                    _prefix_headers["x-clyde-cache-key"] = f"clyde-convo-{self._conversation_id}"
                pass  # payload ready
                with client.stream(
                    "POST",
                    f"{self.backend_url}/v1/chat/completions",
                    json=payload,
                    headers=_prefix_headers or None,
                ) as stream:
                    if stream.status_code == 500:
                        body = stream.read().decode("utf-8", errors="replace")[:500]
                        log.error(f"Backend 500 (likely GPU OOM): {body}")
                        return {"error": f"Backend crashed (HTTP 500): {body}", "gpu_oom": True}
                    if stream.status_code != 200:
                        body = stream.read().decode("utf-8", errors="replace")[:500]
                        return {"error": f"Backend returned {stream.status_code}: {body}"}

                    last_data_time = _time.monotonic()
                    # Track when content/tool output started vs thinking-only
                    THINKING_TIMEOUT = float(os.environ.get("CLYDE_THINKING_TIMEOUT", "90.0"))
                    thinking_start_time = None  # When thinking-only generation began
                    has_content_or_tool = False  # True once we get any non-thinking token
                    # Knob #2 — Thinking budget cap (separate from content).
                    # Hard ceiling on thinking characters: beyond this, the model is
                    # almost certainly looping. Truncate and let recovery kick in.
                    # Default 4000 chars ≈ 1000 tokens ≈ the useful reasoning band
                    # before thinking loops start repeating.
                    THINKING_CHAR_CAP = int(os.environ.get("CLYDE_THINKING_CHAR_CAP", "4000"))
                    # Smart degeneration detection (no hard caps — just quality monitoring)
                    _thinking_degen_window = []  # Sliding window of recent thinking chunks
                    _DEGEN_WINDOW_SIZE = 30      # Chunks to keep in window
                    # Knob #2 — faster detection: check every 5 chunks during thinking
                    # (was 20). Catches loops roughly 4× faster, trading a few extra
                    # ratio computations for meaningfully lower wasted tokens.
                    _DEGEN_CHECK_INTERVAL = int(os.environ.get("CLYDE_DEGEN_CHECK_INTERVAL", "5"))
                    _thinking_chunk_count = 0
                    _thinking_char_count = 0  # Running total of thinking chars for cap check
                    # Content degeneration detection (catches loops in output text)
                    _content_degen_window = []
                    _content_chunk_count = 0
                    _content_degenerated = False  # Set True when content loop detected
                    _content_start_time = None  # When content generation began (no tool call yet)
                    # Patch 9 (Iter 4): give writing phase more runway for long tool_call args
                    CONTENT_MAX_TIME = 180.0 if _phase in ("writing", "review") else 90.0
                    _response_start_time = _time.monotonic()  # Overall response timer
                    RESPONSE_MAX_TIME = 120.0  # Max 2 min total response time (any token type)

                    for raw_line in stream.iter_lines():
                        now = _time.monotonic()

                        # Check inactivity timeout
                        if first_token_received and (now - last_data_time) > INACTIVITY_TIMEOUT:
                            log.warning(f"Inactivity timeout ({INACTIVITY_TIMEOUT}s) — model stopped producing tokens")
                            return {"error": f"Model stopped generating (no output for {INACTIVITY_TIMEOUT}s)", "timeout": True}

                        # Check thinking-only timeout: if we've been getting ONLY
                        # thinking tokens for too long, the model is stuck in a
                        # reasoning loop and will never emit a tool call.
                        if thinking_start_time and not has_content_or_tool:
                            thinking_elapsed = now - thinking_start_time
                            if thinking_elapsed > THINKING_TIMEOUT:
                                log.warning(f"Thinking timeout ({THINKING_TIMEOUT}s) — model stuck in reasoning loop, forcing stop")
                                _content_degenerated = True  # Treat as degeneration for recovery
                                break

                        line = raw_line.strip()
                        if not line or line == "data: [DONE]":
                            if line == "data: [DONE]":
                                finish_reason = finish_reason or "stop"
                            continue
                        if line.startswith("data: "):
                            line = line[6:]

                        try:
                            chunk = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue

                        last_data_time = _time.monotonic()

                        if "model" in chunk:
                            model_name = chunk["model"]

                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {})

                            # Regular content
                            if "content" in delta and delta["content"]:
                                if not first_token_received:
                                    first_token_received = True
                                    log.info(f"First token received after {now - last_data_time + (now - last_data_time):.1f}s")

                                # Track + emit live metrics.
                                # For MLX/inline-think models, thinking arrives
                                # via delta["content"] inside <think>…</think>,
                                # NOT via delta["thinking"]. Track which phase
                                # we're in so the inspector's thinking/writing
                                # split is correct.
                                _dc = delta["content"]
                                if "<think>" in _dc:
                                    _metrics_in_inline_think = True
                                if "</think>" in _dc:
                                    # Split: chars before </think> are thinking,
                                    # chars after are content.
                                    _split = _dc.split("</think>", 1)
                                    _metrics_thinking_chars += len(_split[0])
                                    _metrics_chars_total += len(_split[1]) if len(_split) > 1 else 0
                                    _metrics_in_inline_think = False
                                elif _metrics_in_inline_think:
                                    _metrics_thinking_chars += len(_dc)
                                else:
                                    _metrics_chars_total += len(_dc)
                                _maybe_emit_metrics("thinking" if _metrics_in_inline_think else "content")

                                # Qwen3-style prompt templates inject `<think>\n`
                                # at the assistant turn's tail, so the first
                                # streamed content is thinking prose followed by
                                # `</think>`. Clyde's Swift stream parser uses
                                # `<think>` as the boundary to route to the
                                # thinking channel — but that literal tag never
                                # leaves the server (it's in the prompt). Prepend
                                # it on the first content delta so the parser
                                # enters thinking state. llama.cpp's jinja
                                # template already emits reasoning_content
                                # separately, making this a no-op there.
                                _delta_content = delta["content"]
                                if _inline_think_state is None:
                                    _inline_think_state = {"injected": False}
                                if (not _inline_think_state["injected"]
                                    and not thinking_parts
                                    and "<think>" not in _delta_content):
                                    _delta_content = "<think>" + _delta_content
                                    _inline_think_state["injected"] = True
                                content_parts.append(_delta_content)
                                has_content_or_tool = True

                                # Content degeneration detection (mirrors thinking detection)
                                # Aggressive: check every 10 chunks after 20, window of 40
                                _content_degen_window.append(delta["content"])
                                _content_chunk_count += 1
                                if len(_content_degen_window) > 40:
                                    _content_degen_window = _content_degen_window[-40:]
                                if _content_chunk_count % 10 == 0 and _content_chunk_count > 20:
                                    _cw_text = "".join(_content_degen_window)
                                    if len(_cw_text) > 200:
                                        _cw_words = _cw_text.split()
                                        if len(_cw_words) > 30:
                                            _cw_unique = len(set(_cw_words)) / len(_cw_words)
                                            if _cw_unique < 0.15:
                                                log.warning(f"Content degeneration: word repetition (unique ratio {_cw_unique:.2f})")
                                                _content_degenerated = True
                                                break
                                        _cw_sents = [s.strip() for s in _cw_text.split('.') if len(s.strip()) > 10]
                                        if len(_cw_sents) > 5:
                                            _cw_sent_unique = len(set(_cw_sents)) / len(_cw_sents)
                                            if _cw_sent_unique < 0.25:
                                                log.warning(f"Content degeneration: sentence loop (unique ratio {_cw_sent_unique:.2f})")
                                                _content_degenerated = True
                                                break

                                # Content generation time limit (backstop for undetected loops)
                                if _content_start_time is None:
                                    _content_start_time = _time.monotonic()
                                elif (_time.monotonic() - _content_start_time) > CONTENT_MAX_TIME:
                                    log.warning(f"Content generation timeout ({CONTENT_MAX_TIME}s) — forcing break")
                                    _content_degenerated = True
                                    break

                                # ── Patch 4 (Iteration 3): inline <think> tag cap ──
                                # Some models route thinking via delta["content"] inside
                                # <think>…</think> or <|channel>thought…<channel|> tags,
                                # NOT via delta["thinking"]. The Patch 2 cap in the
                                # thinking branch therefore never fires. Check for
                                # unclosed think tags in the content stream and apply
                                # the same hard ceiling.
                                if _content_chunk_count % 5 == 0:
                                    _combined_for_think = "".join(content_parts)
                                    _open_pats = [
                                        r'<\|channel>thought',
                                        r'<\|think\|>',
                                        r'<think>',
                                        r'<thinking>',
                                        r'<internal_thought>',
                                    ]
                                    _close_pats = [
                                        r'<channel\|>',
                                        r'<\|/think\|>',
                                        r'</think>',
                                        r'</thinking>',
                                        r'</internal_thought>',
                                    ]
                                    _last_open_end = -1
                                    for _op in _open_pats:
                                        _ms = list(re.finditer(_op, _combined_for_think))
                                        if _ms:
                                            _pend = _ms[-1].end()
                                            if _pend > _last_open_end:
                                                _last_open_end = _pend
                                    if _last_open_end >= 0:
                                        _after_open = _combined_for_think[_last_open_end:]
                                        _has_close = any(
                                            re.search(_cp, _after_open) for _cp in _close_pats
                                        )
                                        if not _has_close and len(_after_open) > THINKING_CHAR_CAP:
                                            log.warning(
                                                f"Inline <think> cap hit ({len(_after_open)} chars > "
                                                f"{THINKING_CHAR_CAP}) — hard stop (Patch 4)"
                                            )
                                            _content_degenerated = True
                                            break

                            # Thinking content from streaming API
                            # llama.cpp sends "reasoning_content", MLX sends "thinking"
                            _think_delta = delta.get("thinking") or delta.get("reasoning_content") or ""
                            if _think_delta:
                                if not first_token_received:
                                    first_token_received = True
                                # Start the thinking timer on first thinking token
                                if thinking_start_time is None:
                                    thinking_start_time = _time.monotonic()

                                # Collect thinking, with a hard char cap (Knob #2).
                                thinking_parts.append(_think_delta)
                                _thinking_chunk_count += 1
                                _thinking_char_count += len(_think_delta)

                                # Enforce hard thinking budget cap (Patch 2 — Iteration 2).
                                # Previously this had a `not has_content_or_tool` escape hatch,
                                # which let the model emit 8k+ chars of thinking as long as a
                                # short content/tool chunk slipped in. In iteration 1 that
                                # resulted in 5876-char and 8857-char thinking blocks that
                                # were never truncated. Now it's a hard ceiling regardless.
                                if _thinking_char_count > THINKING_CHAR_CAP:
                                    log.warning(
                                        f"Thinking cap hit ({_thinking_char_count} chars > "
                                        f"{THINKING_CHAR_CAP}) — hard stop "
                                        f"(has_content_or_tool={has_content_or_tool})"
                                    )
                                    _content_degenerated = True
                                    break

                                # Smart degeneration detection: check quality periodically
                                # without limiting productive thinking
                                _thinking_degen_window.append(_think_delta)
                                if len(_thinking_degen_window) > _DEGEN_WINDOW_SIZE:
                                    _thinking_degen_window = _thinking_degen_window[-_DEGEN_WINDOW_SIZE:]

                                if _thinking_chunk_count % _DEGEN_CHECK_INTERVAL == 0:
                                    current_len = sum(len(p) for p in thinking_parts)
                                    window_text = "".join(_thinking_degen_window)
                                    # Test 1: Character-level repetition (e.g. "e_e_e_e_e_e")
                                    if len(window_text) > 100:
                                        char_set = set(window_text.replace(" ", "").replace("\n", ""))
                                        if len(char_set) < 8:
                                            log.warning(f"Thinking degeneration: character-level repetition at {current_len} chars (only {len(char_set)} unique chars)")
                                            break
                                    # Test 2: Word-level repetition (same words over and over)
                                    words = window_text.split()
                                    if len(words) > 30:
                                        unique_ratio = len(set(words)) / len(words)
                                        if unique_ratio < 0.15:
                                            log.warning(f"Thinking degeneration: word repetition at {current_len} chars (unique ratio {unique_ratio:.2f})")
                                            break
                                    # Test 3: Sentence-level loops (same sentence patterns repeating)
                                    sentences = [s.strip() for s in window_text.split('.') if len(s.strip()) > 10]
                                    if len(sentences) > 5:
                                        unique_sent_ratio = len(set(sentences)) / len(sentences)
                                        if unique_sent_ratio < 0.3:
                                            log.warning(f"Thinking degeneration: sentence loop at {current_len} chars (unique ratio {unique_sent_ratio:.2f})")
                                            break
                                    # Log periodic progress for long thinking (informational only)
                                    if current_len > 5000:
                                        log.info(f"Thinking progress: {current_len} chars, {_thinking_chunk_count} chunks, unique word ratio {unique_ratio:.2f}")

                                # Stream thinking to UI in real-time so user sees progress
                                if emit:
                                    try:
                                        emit("thinking_delta", delta=_think_delta)
                                    except Exception:
                                        pass

                                # Track + emit live metrics during thinking
                                _metrics_thinking_chars += len(_think_delta)
                                _maybe_emit_metrics("thinking")

                            # Tool calls (streamed incrementally)
                            if "tool_calls" in delta:
                                if not first_token_received:
                                    first_token_received = True
                                has_content_or_tool = True
                                _content_start_time = None  # Reset content timer on tool call
                                # Early bail-out: if model is generating absurd numbers of tool calls
                                if len(tool_calls_map) > 10:
                                    if len(tool_calls_map) == 11:
                                        log.warning("Tool call flood detected (>10) — ignoring excess")
                                    continue
                                for tc in delta["tool_calls"]:
                                    idx = tc.get("index", 0)
                                    if idx not in tool_calls_map:
                                        tool_calls_map[idx] = {
                                            "id": tc.get("id", f"call_{idx}"),
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""},
                                        }
                                    if "function" in tc:
                                        fn = tc["function"]
                                        if "name" in fn and fn["name"]:
                                            tool_calls_map[idx]["function"]["name"] = fn["name"]
                                        if "arguments" in fn:
                                            tool_calls_map[idx]["function"]["arguments"] += fn["arguments"]

                            if "finish_reason" in choice and choice["finish_reason"]:
                                finish_reason = choice["finish_reason"]

        except httpx.ConnectError:
            return {"error": "Cannot connect to model server. Attempting auto-recovery...", "mlx_down": True}
        except httpx.ReadTimeout as e:
            if not first_token_received:
                log.warning(f"Prefill timeout ({PREFILL_TIMEOUT}s): backend didn't produce any tokens")
                return {"error": f"Model server timed out during prefill: {e}", "timeout": True}
            else:
                log.warning(f"Read timeout after partial generation ({len(content_parts)} content chunks)")
                # Fall through to build response from what we got
        except (httpx.ReadError, httpx.RemoteProtocolError) as e:
            log.error(f"Backend connection lost mid-request (likely OOM crash): {e}")
            return {"error": f"Model server crashed mid-request: {e}", "gpu_oom": True}
        except Exception as e:
            err_str = str(e).lower()
            if any(k in err_str for k in ("broken pipe", "connection reset", "eof", "peer closed")):
                log.error(f"Backend connection died (likely OOM): {e}")
                return {"error": f"Model server connection died: {e}", "gpu_oom": True}
            return {"error": str(e)}

        # Assemble into standard OpenAI response format
        content = "".join(content_parts)
        thinking = "".join(thinking_parts)
        tool_calls_list = [tool_calls_map[k] for k in sorted(tool_calls_map.keys())] if tool_calls_map else None

        message = {"role": "assistant", "content": content}
        if thinking:
            message["thinking"] = thinking
        if tool_calls_list:
            message["tool_calls"] = tool_calls_list

        result = {
            "model": model_name,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or "stop",
            }],
        }

        token_count = len(content_parts) + len(thinking_parts)
        log.info(f"Streaming complete: {token_count} chunks, finish={finish_reason}")
        if _content_degenerated:
            result["content_degenerated"] = True
            log.warning("Response marked as content-degenerated — caller should discard and retry")
        return result

    # ─── Self-Healing: Backend Restart Helpers ───

    def _kill_backend_server(self) -> bool:
        """
        Kill any existing backend (llama-server) processes. Fast — no HTTP check
        needed since we only call this when we know the server isn't responding.
        Returns True if processes were killed.
        """
        import subprocess as _sp
        import signal as _sig

        try:
            # Try llama-server first, fall back to mlx_vlm for legacy
            for proc_name in ["llama-server", "mlx_vlm"]:
                result = _sp.run(
                    ["pgrep", "-f", proc_name],
                    capture_output=True, text=True, timeout=3
                )
                if result.returncode == 0 and result.stdout.strip():
                    pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
                    for pid in pids:
                        log.warning(f"Killing {proc_name} process PID {pid}")
                        try:
                            os.kill(pid, _sig.SIGKILL)
                        except ProcessLookupError:
                            pass
                    return len(pids) > 0
            return False

        except Exception as e:
            log.error(f"Backend kill failed: {e}")
            return False

    # Keep old name as alias for any code still referencing it
    _kill_zombie_mlx = _kill_backend_server

    def _restart_backend_server(self) -> bool:
        """
        Attempt to restart the backend server process (llama-server).
        Returns True if the process was successfully launched.

        Guard: skip when the active backend is managed by ProcessManager
        (mlx-vlm on 8814, mlx-flash on 8815, any custom managed backend).
        Spawning a hardcoded llama-server in those cases lands a SECOND
        server on a DIFFERENT port than the active route, which nobody
        talks to — it wastes GPU RAM and confuses the model.
        """
        import subprocess as _sp
        import yaml as _yaml

        # Parse active port — if it's not the llama.cpp default (8810),
        # ProcessManager owns the lifecycle and will re-launch on next
        # request. Our job is just to return cleanly.
        try:
            _active_port = int(self.backend_url.rstrip("/").split(":")[-1])
        except Exception:
            _active_port = 8810
        if _active_port != 8810:
            log.info(
                f"_restart_backend_server: active backend on :{_active_port} "
                f"— owned by ProcessManager, skipping hardcoded llama-server "
                f"restart (will lazy-load on next request)"
            )
            return False

        try:
            cfg_path = Path(__file__).parent / "config.yaml"
            with open(cfg_path) as f:
                cfg = _yaml.safe_load(f)

            model_path = cfg["backend"]["model"]
            port = int(cfg["backend"]["url"].split(":")[-1])
            agent_home = Path(cfg["paths"]["agent_home"]).expanduser()

            # llama-server command
            cmd = [
                "/opt/homebrew/bin/llama-server",
                "-m", str(Path.home() / ".cache" / "lm-studio" / "models" / model_path),
                "--port", str(port),
                "--host", "0.0.0.0",
                "-ngl", "99",
                "-c", "32768",
            ]

            log.info(f"Starting backend server: {' '.join(cmd)}")

            log_path = "/tmp/clyde-backend.log"
            log_file = open(log_path, "a")

            process = _sp.Popen(
                cmd,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )

            pid_path = agent_home / ".backend-server.pid"
            pid_path.write_text(str(process.pid))
            log.info(f"Backend server started with PID {process.pid}")

            return True

        except Exception as e:
            log.error(f"Failed to restart backend server: {e}")
            return False

    # Keep old name as alias
    _restart_mlx_server = _restart_backend_server

    def _get_graph_recovery_context(self) -> str:
        """
        Extract asset-graph state as injection text after compaction.

        General-purpose across every skill: walks request, phase,
        artifact, source, fact node roles. Keeps a legacy fallback for
        conversations that only have the older organize_* node roles
        (pre-universal-roles on-disk graphs).
        """
        try:
            from tools import _graph_store, _current_conversation_id
            conv_id = _current_conversation_id or self._conversation_id
            if not conv_id or conv_id not in _graph_store:
                return ""
            store = _graph_store[conv_id]
            nodes = store.get("nodes", [])
            if not nodes:
                return ""

            parts = ["\n\n## Asset Graph State (working memory — source of truth after compaction)"]
            import json as _json

            requests = [n for n in nodes
                        if n.get("metadata", {}).get("node_role") == "request"]
            phases = [n for n in nodes
                      if n.get("metadata", {}).get("node_role") == "phase"]
            artifacts = [n for n in nodes
                         if n.get("metadata", {}).get("node_role") == "artifact"]
            sources = [n for n in nodes
                       if n.get("metadata", {}).get("node_role") == "source"]
            facts = [n for n in nodes
                     if n.get("metadata", {}).get("node_role") == "fact"
                     or n.get("type") == "fact" and n.get("metadata", {}).get("node_role") != "artifact"]

            for req in requests:
                meta = req.get("metadata", {})
                parts.append(f"\nREQUEST [{req['id']}]: {req['title']}")
                parts.append(f"  status: {meta.get('status', 'unknown')}")
                parts.append(f"  skill: {meta.get('skill', 'general')}")
                if meta.get("source_file"):
                    parts.append(f"  source: {meta['source_file']}")
                try:
                    reqs = _json.loads(meta.get("requirements", "{}"))
                    for k, v in reqs.items():
                        parts.append(f"  req.{k}: {v}")
                except Exception:
                    raw = meta.get("requirements", "")
                    if raw and raw != "{}":
                        parts.append(f"  requirements: {raw}")

            if phases:
                parts.append("")
                for phase in sorted(phases, key=lambda n: n.get("created_at", 0)):
                    meta = phase.get("metadata", {})
                    status = meta.get("status", "pending")
                    prog = ""
                    total = meta.get("progress_total", "")
                    cur = meta.get("progress_current", "0")
                    if total and total != "0":
                        prog = f" ({cur}/{total})"
                    parts.append(f"PHASE [{phase['id']}]: {phase['title']} [{status}]{prog}")
                    if status == "active":
                        sd = meta.get("state_dir", "")
                        ns = meta.get("next_batch_start", "0")
                        bs = meta.get("batch_size", "15")
                        if sd:
                            parts.append(
                                f"  RESUME: call organize_batch_read state_dir={sd} "
                                f"start={ns} count={bs}"
                            )
                        else:
                            parts.append("  RESUME: continue from where this phase left off.")

            if artifacts:
                parts.append("")
                for art in artifacts:
                    meta = art.get("metadata", {})
                    line = f"ARTIFACT [{art['id']}]: {art['title']}"
                    if meta.get("artifact_type"):
                        line += f" (type={meta['artifact_type']})"
                    if meta.get("satisfies"):
                        line += f" → satisfies: {meta['satisfies']}"
                    parts.append(line)

            if sources:
                parts.append("")
                parts.append(f"SOURCES CONSULTED ({len(sources)}) — do not re-fetch:")
                for src in sources[:30]:
                    parts.append(f"  • {src['title']}")
                if len(sources) > 30:
                    parts.append(f"  … and {len(sources) - 30} more")

            if facts:
                parts.append("")
                parts.append(f"FACTS RECORDED ({len(facts)})")
                for f in facts[:20]:
                    meta = f.get("metadata", {})
                    cat = meta.get("category", "")
                    parts.append(f"  • [{cat}] {f['title']}")
                if len(facts) > 20:
                    parts.append(f"  … and {len(facts) - 20} more")

            # Legacy organize-only fallback for on-disk graphs that
            # predate the universal node roles.
            if not requests and not phases:
                task_node = None
                for n in nodes:
                    meta = n.get("metadata", {})
                    if meta.get("node_role") == "organize_task":
                        task_node = n
                        phase = meta.get("phase", "unknown")
                        total = meta.get("total_items", "?")
                        classified = meta.get("classified", "0")
                        state_dir = meta.get("state_dir", "")
                        parts.append(
                            f"\nLEGACY_TASK: {n['title']} | phase={phase} | "
                            f"progress={classified}/{total}"
                        )
                        if state_dir:
                            parts.append(f"STATE_DIR: {state_dir}")
                        break
                cat_nodes = [n for n in nodes
                             if n.get("metadata", {}).get("node_role") == "organize_category"]
                if cat_nodes:
                    parts.append(
                        f"CATEGORIES ({len(cat_nodes)}): "
                        f"{', '.join(n['title'] for n in cat_nodes)}"
                    )
                if task_node:
                    meta = task_node.get("metadata", {})
                    next_start = meta.get("next_batch_start", "0")
                    state_dir = meta.get("state_dir", "")
                    phase = meta.get("phase", "")
                    if phase == "classify" and state_dir:
                        parts.append(
                            f"RESUME: organize_batch_read state_dir={state_dir} "
                            f"start={next_start} count=15"
                        )
                    elif phase == "review":
                        parts.append("RESUME: present classification summary.")

            if len(parts) <= 1:
                return ""
            parts.append("")
            parts.append(
                "After compaction: call `graph_read` for full state. "
                "Follow any RESUME directive immediately."
            )
            return "\n".join(parts)
        except Exception as e:
            log.warning(f"Graph recovery context failed: {e}")
            return ""

    def _parse_tool_calls_from_content(self, content: str) -> list[dict]:
        """
        Parse tool calls embedded in content text. Fully model-agnostic:
        handles every known format that LLMs produce for tool/function calls.

        Supported formats (checked in order, first match wins):
          1. Qwen XML:          <tool_call>{"name": ..., "arguments": ...}</tool_call>
          2. Mistral prefix:    [TOOL_CALL] {"name": ..., "arguments": ...}
          3. XML attribute:     <tool call="func" args='{"key":"val"}'/>
          4. XML invoke:        <invoke name="func"><parameter name="key">val</parameter></invoke>
          5. Function-call:     func_name({"key": "val"})  or  func_name(key="val")
          6. Markdown code:     ```tool_code\n{"name":...}\n```
          7. Bare JSON:         {"name": ..., "arguments": ...}
        """
        calls = []

        # ---- Format 1: <tool_call> XML tags (Qwen/generic) ----
        for match in re.finditer(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', content, re.DOTALL):
            tc = self._try_parse_tool_json(match.group(1))
            if tc:
                calls.append(tc)
        if calls:
            return calls

        # ---- Format 1b: hallucinated `<function=name><parameter=key>val</parameter></function>` ----
        # Qwen3.6-A3B (and friends) sometimes emit this hybrid when prompted
        # to call a tool. The shape:
        #   <tool_call>            # optional outer wrapper
        #     <function=NAME>
        #       <parameter=KEY> VAL </parameter>
        #       ...
        #     </function>
        #   </tool_call>
        # Also handles a no-arg / JSON-arg fallback where the inner is just
        # a `{...}` blob.
        for fm in re.finditer(
            r"<function=(\w+)\s*>(.*?)</function>",
            content, re.DOTALL,
        ):
            fname = fm.group(1)
            inner = fm.group(2)
            args_obj: dict = {}
            for pm in re.finditer(
                r"<parameter=(\w+)\s*>(.*?)</parameter>",
                inner, re.DOTALL,
            ):
                key = pm.group(1)
                val = pm.group(2).strip()
                # Coerce types: bool, int, json
                if val.lower() in ("true", "false"):
                    args_obj[key] = (val.lower() == "true")
                else:
                    try:
                        args_obj[key] = json.loads(val)
                    except Exception:
                        args_obj[key] = val
            # If no <parameter=...> blocks found, try a JSON {...} blob.
            if not args_obj:
                jm = re.search(r"\{.*\}", inner, re.DOTALL)
                if jm:
                    try:
                        args_obj = json.loads(jm.group(0))
                    except Exception:
                        pass
            calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": fname,
                    "arguments": json.dumps(args_obj),
                },
            })
        if calls:
            return calls

        # ---- Format 2: [TOOL_CALL] prefix (Mistral) ----
        for match in re.finditer(r'\[TOOL_CALL\]\s*(\{.*?\})', content, re.DOTALL):
            tc = self._try_parse_tool_json(match.group(1))
            if tc:
                calls.append(tc)
        if calls:
            return calls

        # ---- Format 3: XML attribute style ----
        # <tool call="func_name" args='{"key":"value"}'/>
        # <tool call="func_name" args="{&quot;key&quot;:&quot;value&quot;}"/>
        for match in re.finditer(
            r'<tool\s+call=["\'](\w+)["\']\s+args=["\'](.+?)["\']\s*/?>',
            content, re.DOTALL
        ):
            tc = self._parse_xml_attr_tool_call(match.group(1), match.group(2))
            if tc:
                calls.append(tc)
        if calls:
            return calls

        # ---- Format 4: XML invoke style (Claude/Anthropic XML) ----
        # <invoke name="func"><parameter name="key">value</parameter></invoke>
        # Also: <function_call><invoke ...>...</invoke></function_call>
        invoke_pattern = r'<invoke\s+name=["\'](\w+)["\']\s*>(.*?)</invoke>'
        for match in re.finditer(invoke_pattern, content, re.DOTALL):
            func_name = match.group(1)
            params_block = match.group(2)
            args = {}
            for pm in re.finditer(r'<parameter\s+name=["\'](\w+)["\']\s*>(.*?)</parameter>', params_block, re.DOTALL):
                pname, pval = pm.group(1), pm.group(2).strip()
                args[pname] = self._coerce_value(pval)
            calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "function": {"name": func_name, "arguments": args}
            })
        if calls:
            return calls

        # ---- Format 5: Function-call style ----
        # func_name({"key": "val", ...})  or  func_name(key="val", key2=123)
        tool_names = "|".join(re.escape(t["function"]["name"]) for t in self.tool_registry.definitions())
        if tool_names:
            # 6a: func_name({json})
            for match in re.finditer(
                rf'(?<!\w)({tool_names})\(\s*(\{{.*?\}})\s*\)',
                content, re.DOTALL
            ):
                func_name = match.group(1)
                try:
                    args = json.loads(match.group(2))
                except json.JSONDecodeError:
                    try:
                        args = json.loads(match.group(2).replace("'", '"'))
                    except json.JSONDecodeError:
                        continue
                calls.append({
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "function": {"name": func_name, "arguments": args if isinstance(args, dict) else {}}
                })

            # 6b: func_name(key="val", key2=123) — Python-style kwargs
            if not calls:
                for match in re.finditer(
                    rf'(?<!\w)({tool_names})\(([^)]+)\)',
                    content, re.DOTALL
                ):
                    func_name = match.group(1)
                    kwargs_str = match.group(2).strip()
                    args = self._parse_kwargs(kwargs_str)
                    if args:
                        calls.append({
                            "id": f"call_{uuid.uuid4().hex[:8]}",
                            "function": {"name": func_name, "arguments": args}
                        })
        if calls:
            return calls

        # ---- Format 6: Markdown code block with tool JSON ----
        # ```tool_code\n{"name":"func","arguments":{...}}\n```
        for match in re.finditer(r'```(?:tool_code|json|tool)?\s*\n(\{.*?\})\s*\n```', content, re.DOTALL):
            tc = self._try_parse_tool_json(match.group(1))
            if tc:
                calls.append(tc)
        if calls:
            return calls

        # ---- Format 7: Bare JSON (last resort) ----
        for match in re.finditer(r'\{[^{}]*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\}', content, re.DOTALL):
            tc = self._try_parse_tool_json(match.group(0))
            if tc:
                calls.append(tc)

        return calls

    def _parse_xml_attr_tool_call(self, func_name: str, args_raw: str) -> dict | None:
        """Parse a tool call from XML attribute format, handling HTML entities."""
        # Decode HTML entities
        args_decoded = (
            args_raw
            .replace('&quot;', '"')
            .replace('&amp;', '&')
            .replace('&lt;', '<')
            .replace('&gt;', '>')
            .replace('&apos;', "'")
        )
        # Try direct JSON parse
        for attempt in [args_decoded, args_decoded.replace("'", '"')]:
            try:
                args = json.loads(attempt)
                return {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "function": {
                        "name": func_name,
                        "arguments": args if isinstance(args, dict) else {}
                    }
                }
            except json.JSONDecodeError:
                continue
        log.warning(f"XML-attr tool call: failed to parse args for {func_name}: {args_decoded[:200]}")
        return None

    @staticmethod
    def _parse_kwargs(s: str) -> dict:
        """Parse Python-style keyword arguments: key='val', key2=123."""
        args = {}
        for match in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))', s):
            key = match.group(1)
            val = match.group(2) if match.group(2) is not None else (
                match.group(3) if match.group(3) is not None else match.group(4)
            )
            args[key] = ConversationRuntime._coerce_value(val)
        return args

    @staticmethod
    def _coerce_value(val: str):
        """Convert a string value to the appropriate Python type."""
        if val.lower() == 'true':
            return True
        if val.lower() == 'false':
            return False
        if val.lower() == 'null' or val.lower() == 'none':
            return None
        try:
            return int(val)
        except ValueError:
            pass
        try:
            return float(val)
        except ValueError:
            pass
        # Try JSON array/object
        if val.startswith(('[', '{')):
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                pass
        return val

    def _try_parse_tool_json(self, raw: str) -> dict | None:
        """Try to parse a JSON string into a normalized tool call dict."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Try fixing common JSON issues: trailing commas, single quotes
            cleaned = raw.replace("'", '"').rstrip().rstrip(",")
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                log.warning(f"Failed to parse tool call JSON: {raw[:100]}")
                return None

        # Normalize to OpenAI format regardless of what the model produced
        name = data.get("name") or data.get("function") or data.get("tool") or ""
        args = data.get("arguments") or data.get("parameters") or data.get("input") or {}

        if not name:
            return None

        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                # Try cleaning common escaping issues
                try:
                    cleaned = args.replace('\\"', '"')
                    args = json.loads(cleaned)
                except json.JSONDecodeError:
                    args = {"raw": args}

        # Unwrap "raw" wrapper from double-serialization
        if isinstance(args, dict) and "raw" in args and len(args) == 1:
            raw_val = args["raw"]
            if isinstance(raw_val, str):
                try:
                    unwrapped = json.loads(raw_val)
                    if isinstance(unwrapped, dict):
                        args = unwrapped
                except json.JSONDecodeError:
                    try:
                        unwrapped = json.loads(raw_val.replace('\\"', '"'))
                        if isinstance(unwrapped, dict):
                            args = unwrapped
                    except json.JSONDecodeError:
                        pass

        return {
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "function": {
                "name": name,
                "arguments": args
            }
        }

    @staticmethod
    def _normalize_tool_calls(tool_calls: list[dict]) -> list[dict]:
        """
        Normalize tool calls to a consistent format regardless of model.
        Ensures every tool call has: id, function.name, function.arguments (dict).
        """
        normalized = []
        for tc in tool_calls:
            tc_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"

            fn = tc.get("function", tc)
            name = fn.get("name", "")
            args = fn.get("arguments", {})

            # Some models return arguments as a JSON string, not a dict
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}

            # Some models return None for arguments
            if args is None:
                args = {}

            # ── Qwen 3.6 "raw" wrapper fix ──
            # When the model double-serializes arguments, we get:
            #   {"raw": "{\"section_id\":\"sec_1\", \"content\":\"...\"}" }
            # Unwrap the "raw" field and try to parse it as JSON.
            if isinstance(args, dict) and "raw" in args and len(args) == 1:
                raw_val = args["raw"]
                if isinstance(raw_val, str):
                    try:
                        unwrapped = json.loads(raw_val)
                        if isinstance(unwrapped, dict):
                            args = unwrapped
                            log.info(f"  Unwrapped 'raw' args for {name}: {list(unwrapped.keys())}")
                    except json.JSONDecodeError:
                        # Try cleaning: sometimes the model includes extra escaping
                        try:
                            cleaned = raw_val.replace('\\"', '"').replace("\\'", "'")
                            unwrapped = json.loads(cleaned)
                            if isinstance(unwrapped, dict):
                                args = unwrapped
                                log.info(f"  Unwrapped cleaned 'raw' args for {name}: {list(unwrapped.keys())}")
                        except json.JSONDecodeError:
                            pass  # Keep as {"raw": ...}

            if not name:
                log.warning(f"Skipping tool call with no name: {tc}")
                continue

            normalized.append({
                "id": tc_id,
                "function": {
                    "name": name,
                    "arguments": args
                }
            })

        return normalized

    def _extract_facts_from_context(self) -> list[dict]:
        """Extract factual findings from tool results in the conversation.

        Scans assistant messages for web_search/web_fetch results and extracts
        URL + snippet pairs that can be auto-recorded as facts. Used by the
        auto-scaffold mechanism when Qwen 3.6 skips record_fact.
        """
        facts = []
        seen_urls = set()
        for msg in self.session.messages:
            if msg.role != "tool":
                continue
            content = msg.content or ""
            # Extract URLs and snippets from web_search results
            # Format: "1. Title\n   URL: https://...\n   Snippet text"
            import re as _re_facts
            for match in _re_facts.finditer(
                r'\d+\.\s+(.+?)\n\s+URL:\s+(https?://\S+)\n\s+(.*?)(?=\n\d+\.|\n\n|$)',
                content, _re_facts.DOTALL
            ):
                title, url, snippet = match.group(1).strip(), match.group(2).strip(), match.group(3).strip()
                if url in seen_urls or not snippet:
                    continue
                seen_urls.add(url)
                facts.append({
                    "fact": f"{title}: {snippet[:300]}",
                    "source": url,
                    "category": "general",
                    "confidence": "medium",
                })
            # Extract from web_fetch results — take first paragraph as fact
            if "http" in content and len(content) > 500:
                # Try to find a URL in the content
                url_match = _re_facts.search(r'(https?://\S+)', content)
                if url_match:
                    url = url_match.group(1).rstrip(')')
                    if url not in seen_urls:
                        seen_urls.add(url)
                        # Take first 300 chars of non-URL content as the fact
                        _text = _re_facts.sub(r'https?://\S+', '', content[:1000]).strip()
                        if len(_text) > 50:
                            facts.append({
                                "fact": _text[:300],
                                "source": url,
                                "category": "general",
                                "confidence": "medium",
                            })
        return facts

    def _clean_content(self, content: str) -> str:
        """
        Remove ALL tool-call markup and thinking blocks from content.
        Model-agnostic: strips every format we can parse + common thinking tags.
        """
        # --- Thinking blocks ---
        # Legacy model patterns
        content = re.sub(r'<\|channel>thought\s*.*?(?:<channel\|>|<\|channel>)', '', content, flags=re.DOTALL)
        content = re.sub(r'<\|channel>thought\s*.*', '', content, flags=re.DOTALL)  # unclosed fallback
        content = re.sub(r'<\|think\|>.*?(?:<\|/think\|>)', '', content, flags=re.DOTALL)
        content = re.sub(r'<\|think\|>.*', '', content, flags=re.DOTALL)  # unclosed fallback
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)  # Qwen / generic
        content = re.sub(r'<think>.*', '', content, flags=re.DOTALL)  # unclosed fallback
        content = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL)  # Claude-style

        # --- Tool call blocks ---
        # Format 1: Qwen XML (closed)
        content = re.sub(r'<tool_call>.*?</tool_call>', '', content, flags=re.DOTALL)
        # Format 1b: hybrid `<function=NAME>...</function>` (Qwen3.6-A3B). Strip
        # this BEFORE the unclosed-tool_call fallback so the inner `</function>`
        # doesn't get caught by the open-only `<tool_call>` strip.
        content = re.sub(r'<function=\w+\s*>.*?</function>', '', content, flags=re.DOTALL)
        # Some models emit `</export>` or stray closing tags inside the hybrid
        # block; sweep those once more in case the close-`</function>` is gone.
        content = re.sub(r'<function=\w+\s*>.*?(?=<tool_call>|\Z)', '', content, flags=re.DOTALL)
        # Format 1c: orphan `<tool_call>` open with no close (model truncated)
        content = re.sub(r'<tool_call>.*?(?=<tool_call>|\Z)', '', content, flags=re.DOTALL)
        # Stray closers
        content = re.sub(r'</tool_call>', '', content)
        content = re.sub(r'</function>', '', content)
        content = re.sub(r'</parameter>', '', content)
        content = re.sub(r'</export>', '', content)
        # Format 2: Mistral prefix
        content = re.sub(r'\[TOOL_CALL\]\s*\{.*?\}', '', content, flags=re.DOTALL)
        # Format 3: XML attribute style
        content = re.sub(r'<tool\s+call=["\'][^"\']+["\']\s+args=["\'].*?["\']\s*/?>', '', content, flags=re.DOTALL)
        # Format 4: XML invoke style
        content = re.sub(r'<(?:function_call>)?\s*<invoke\s+name=["\'][^"\']+["\']\s*>.*?</invoke>\s*(?:</function_call>)?', '', content, flags=re.DOTALL)
        content = re.sub(r'<invoke\s+name=["\'][^"\']+["\']\s*>.*?</invoke>', '', content, flags=re.DOTALL)
        # Format 5: Bare function-call style: func_name(key="val") or func_name({json})
        _tool_names = self.tool_registry.names() if hasattr(self, 'tool_registry') else []
        if _tool_names:
            _tn_pattern = '|'.join(re.escape(n) for n in _tool_names)
            content = re.sub(rf'(?<!\w)(?:{_tn_pattern})\(.*?\)\s*', '', content, flags=re.DOTALL)
        # Format 6: Markdown tool code blocks
        content = re.sub(r'```(?:tool_code|tool)\s*\n\{.*?\}\s*\n```', '', content, flags=re.DOTALL)

        # Note: Formats 5 (function-call) and 7 (bare JSON) are NOT cleaned here
        # because they're too likely to match legitimate content. They're only
        # used as last-resort parsing and the content will show in the response.

        return content.strip()

    @staticmethod
    def _generate_narration(name: str, args) -> str:
        """Generate a natural language narration for a tool call when the model didn't provide one."""
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if name == "web_search":
            query = args.get("query", "")
            return f"Let me search for {query}..." if query else "Let me search for that..."
        elif name == "web_fetch":
            url = args.get("url", "")
            # Extract domain
            import re as _re
            domain_match = _re.search(r'https?://([^/]+)', url)
            domain = domain_match.group(1) if domain_match else "that page"
            return f"Let me fetch {domain}..."
        elif name == "bash":
            cmd = args.get("command", "")
            if not cmd:
                return "Let me run a command..."
            # Give context based on the command
            first_word = cmd.strip().split()[0] if cmd.strip() else ""
            if first_word in ("ls", "dir"):
                return "Let me check what's in that directory..."
            elif first_word in ("cat", "head", "tail", "less"):
                return "Let me read that file..."
            elif first_word in ("grep", "rg", "find"):
                return "Let me search for that..."
            elif first_word in ("df", "du"):
                return "Let me check disk usage..."
            elif first_word in ("ps", "top", "htop"):
                return "Let me check running processes..."
            elif first_word in ("ping", "curl", "wget"):
                return "Let me check the network..."
            elif first_word in ("python", "python3", "node"):
                return "Let me run that script..."
            elif first_word in ("cd", "mkdir", "rm", "mv", "cp"):
                return "Let me manage those files..."
            elif first_word == "git":
                return "Let me check the repository..."
            elif first_word in ("brew", "apt", "pip", "npm"):
                return "Let me handle that package..."
            else:
                short = cmd[:50] + ("..." if len(cmd) > 50 else "")
                return f"Let me run `{short}`..."
        elif name == "read_file":
            path = args.get("path", args.get("file_path", ""))
            fname = path.rsplit("/", 1)[-1] if "/" in path else path
            return f"Let me read {fname}..." if fname else "Let me read that file..."
        elif name == "write_file":
            path = args.get("path", args.get("file_path", ""))
            fname = path.rsplit("/", 1)[-1] if "/" in path else path
            return f"Let me write {fname}..." if fname else "Let me write that file..."
        elif name == "edit_file":
            path = args.get("path", args.get("file_path", ""))
            fname = path.rsplit("/", 1)[-1] if "/" in path else path
            return f"Let me edit {fname}..." if fname else "Let me edit that file..."
        elif name == "glob_search":
            pattern = args.get("pattern", "")
            return f"Let me find files matching {pattern}..." if pattern else "Let me search for files..."
        elif name == "grep_search":
            pattern = args.get("pattern", "")
            return f"Let me search for \"{pattern}\"..." if pattern else "Let me search the codebase..."
        elif name == "memory_read":
            return "Let me check my memory..."
        elif name == "memory_search":
            return "Let me search my memory..."
        elif name == "memory_write":
            return "Let me save that to memory..."
        elif name == "ask_user":
            return ""  # No narration needed — the question itself is the narration
        else:
            return f"Let me use {name}..."

    @staticmethod
    def _tool_preview(name: str, args: dict) -> str:
        """
        Human-readable one-liner for tool_start tags.
        MUST return a single-line string safe for SSE (no newlines).
        """
        # Defensive: ensure args is a dict
        if not isinstance(args, dict):
            args = {}

        def _sanitize(s: str, max_len: int = 120) -> str:
            """Collapse newlines and truncate for safe SSE emission."""
            s = " ; ".join(line.strip() for line in s.splitlines() if line.strip())
            return s[:max_len]

        if name == "web_search":
            return _sanitize(args.get("query", ""), 80)
        elif name == "web_fetch":
            return _sanitize(args.get("url", ""), 80)
        elif name == "bash":
            return _sanitize(args.get("command", ""), 120)
        elif name in ("write_file", "read_file", "edit_file"):
            return _sanitize(args.get("path", args.get("file_path", "")), 80)
        elif name == "glob_search":
            return _sanitize(args.get("pattern", ""), 80)
        elif name == "grep_search":
            return _sanitize(args.get("pattern", ""), 80)
        elif name in ("memory_read", "memory_search"):
            return _sanitize(args.get("query", args.get("key", "")), 80)
        elif name == "memory_write":
            return _sanitize(args.get("key", ""), 80)
        else:
            raw = json.dumps(args)
            return _sanitize(raw, 80) if len(raw) > 2 else ""

    @staticmethod
    def _tool_summary(name: str, args: dict, result: str, is_error: bool) -> str:
        """
        Short summary for tool_done tags.
        MUST return a non-empty single-line string safe for SSE.
        """
        # Defensive: ensure args is a dict
        if not isinstance(args, dict):
            args = {}

        def _safe(s: str, max_len: int = 120) -> str:
            """Collapse to single line and truncate."""
            s = s.replace("\n", " ").replace("\r", "").strip()
            return s[:max_len] if s else "ok"

        if is_error:
            # Include a snippet of the error for debugging
            snippet = result[:60].replace("\n", " ") if result else ""
            return f"error: {snippet}" if snippet else "error"

        try:
            if name == "web_search":
                lines = [l for l in result.split("\n") if l and l[0].isdigit() and ". " in l[:5]]
                return f"{len(lines)} results"
            elif name == "web_fetch":
                kb = len(result) / 1024
                if kb >= 1:
                    return f"{kb:.1f} KB"
                return f"{len(result)} bytes"
            elif name == "bash":
                cmd = args.get("command", "").strip()
                if cmd:
                    import shlex
                    try:
                        parts = shlex.split(cmd)
                    except ValueError:
                        parts = cmd.split()
                    if len(parts) >= 3 and parts[0] in ("mv", "cp"):
                        dest = parts[-1]
                        return _safe(f"moved to {dest}")
                lines = [l for l in result.strip().split("\n") if l.strip()]
                if len(lines) == 0:
                    return "ok"
                elif len(lines) == 1:
                    return _safe(lines[0], 60)
                else:
                    return f"{len(lines)} lines"
            elif name == "read_file":
                lines = result.strip().split("\n")
                return f"{len(lines)} lines"
            elif name == "write_file":
                path = args.get("path", args.get("file_path", ""))
                if path:
                    return _safe(f"saved {path}")
                return "saved"
            elif name == "edit_file":
                return "applied"
            elif name in ("create_document", "create_pdf", "create_spreadsheet", "create_presentation"):
                return _safe(result, 120)
            elif name == "memory_search":
                lines = [l for l in result.strip().split("\n") if l.strip()]
                return f"{len(lines)} matches" if lines else "no matches"
            else:
                # Unknown tool — give a generic summary based on result size
                if not result.strip():
                    return "ok"
                lines = result.strip().split("\n")
                if len(lines) == 1:
                    return _safe(lines[0], 60)
                return f"{len(lines)} lines"
        except Exception as e:
            log.warning(f"Error building tool summary for {name}: {e}")
            return "ok"

    def rolling_summary_if_needed(self, soft_threshold_frac: float = 0.5) -> bool:
        """Knob #4 — Progressive (rolling) summarization.

        Lighter-weight than compact_if_needed: instead of replacing old messages
        with a single summary, truncate bulky tool_result blocks in older
        messages while preserving their headers and the message structure.
        Kicks in at half the full compaction threshold, so we smooth context
        growth instead of snapping it down hard at 90%.

        Strategy:
          * Keep the last N messages verbatim (N = preserve_recent).
          * For messages older than N, truncate any tool_result content to
            its first 1200 chars + "[truncated by rolling summary]".
          * Outline / facts / drafts are held in the tool-state singletons
            anyway, so we're not losing information — just reclaiming space.

        Returns True if any trimming happened.
        """
        try:
            soft_threshold = int(getattr(self, '_compact_threshold', 200000) * soft_threshold_frac)
            estimated = self.session.estimate_tokens()
            if estimated < soft_threshold:
                return False

            preserve_recent = int(os.environ.get("CLYDE_ROLLING_PRESERVE_RECENT", "6"))
            max_old_tool_chars = int(os.environ.get("CLYDE_ROLLING_TOOL_RESULT_CHARS", "1200"))

            if len(self.session.messages) <= preserve_recent + 1:
                return False

            cut = len(self.session.messages) - preserve_recent
            trimmed = 0
            saved_chars = 0
            for msg in self.session.messages[:cut]:
                if msg.role != "tool":
                    continue
                for block in getattr(msg, "content", []) or []:
                    if not isinstance(block, ToolResultBlock):
                        continue
                    cur = getattr(block, "content", "") or ""
                    if not isinstance(cur, str):
                        continue
                    if len(cur) > max_old_tool_chars:
                        saved_chars += len(cur) - max_old_tool_chars
                        block.content = (
                            cur[:max_old_tool_chars]
                            + f"\n\n[... {len(cur) - max_old_tool_chars} chars "
                            + "trimmed by rolling summary — full data preserved in tool state]"
                        )
                        trimmed += 1

            if trimmed:
                log.info(
                    f"Rolling summary: trimmed {trimmed} old tool_result blocks, "
                    f"reclaimed ~{saved_chars // 4} tokens (was ~{estimated})"
                )
            return trimmed > 0
        except Exception as e:
            log.warning(f"Rolling summary failed: {e}")
            return False

    def compact_if_needed(self, preserve_recent: int = 4, token_threshold: int = 10000,
                          on_progress=None, emergency: bool = False):
        """
        Compact session if token estimate exceeds threshold.
        Aligned with claw-code compact.rs: summarize old messages, keep recent N.

        on_progress: optional callback(phase, progress_pct, detail) for live UI updates.
          phase: "analyzing" | "summarizing" | "replacing" | "done"
          progress_pct: 0-100
          detail: human-readable string

        emergency: if True, skip LLM summary and use fast text-based fallback
          (used when backend just crashed and can't handle another request).

        IMPORTANT: The cut point must not split an assistant→tool pair.
        If a tool message references a tool_call_id from a discarded assistant
        message, the backend will reject it.
        """
        estimated = self.session.estimate_tokens()
        if estimated < token_threshold or len(self.session.messages) <= preserve_recent + 1:
            return

        def progress(phase, pct, detail=""):
            if on_progress:
                try:
                    on_progress(phase, pct, detail)
                except Exception:
                    pass

        log.info(f"Compacting session: ~{estimated} tokens, {len(self.session.messages)} messages")
        progress("analyzing", 0, f"Analyzing {len(self.session.messages)} messages...")

        # Find a safe cut point: start from preserve_recent messages back,
        # then walk backwards until the first message at the cut isn't an
        # orphaned tool result (i.e. make sure we don't split assistant→tool pairs).
        cut = len(self.session.messages) - preserve_recent
        while cut > 0 and self.session.messages[cut].role == "tool":
            cut -= 1  # Include the preceding assistant message with tool_calls
        if cut <= 0:
            log.warning("Cannot compact: all messages are part of a tool chain")
            return

        recent = self.session.messages[cut:]
        old = self.session.messages[:cut]

        progress("analyzing", 20, f"Compacting {len(old)} messages, keeping {len(recent)} recent")

        # Build transcript of old messages for summarization
        transcript_parts = []
        for i, msg in enumerate(old):
            text = msg.text_content()
            if text:
                role = msg.role
                preview = text[:500] + ("..." if len(text) > 500 else "")
                transcript_parts.append(f"[{role}]: {preview}")
            # Emit per-message progress during analysis
            if i % 5 == 0:
                pct = 20 + int((i / max(len(old), 1)) * 30)
                progress("analyzing", min(pct, 49), f"Processing message {i+1}/{len(old)}...")

        transcript = "\n".join(transcript_parts)
        # Cap transcript sent to LLM at ~4K tokens worth
        if len(transcript) > 16000:
            transcript = transcript[:16000] + "\n\n[earlier messages truncated]"

        progress("summarizing", 50, "Generating summary...")

        # --- LLM-powered summary (non-emergency only) ---
        summary = None
        if not emergency:
            summary = self._llm_summarize(transcript, on_progress=progress)

        # Fallback: fast text-based summary
        if summary is None:
            log.info("Using fast text-based summary (LLM unavailable or emergency)")
            summary_parts = []
            for msg in old:
                text = msg.text_content()
                if text:
                    preview = text[:200] + ("..." if len(text) > 200 else "")
                    summary_parts.append(f"[{msg.role}]: {preview}")
            summary = "\n".join(summary_parts)
            if len(summary) > 3000:
                summary = summary[:3000] + "\n\n[earlier messages truncated]"

        progress("replacing", 90, "Replacing old messages...")

        # Inject recorded facts so they survive compaction
        facts_section = ""
        try:
            from tools import get_recorded_facts
            facts = get_recorded_facts()
            if facts:
                fact_lines = []
                for i, f in enumerate(facts, 1):
                    line = f"  {i}. {f['fact']}"
                    if f.get('source'):
                        line += f" [source: {f['source']}]"
                    if f.get('category'):
                        line += f" ({f['category']})"
                    fact_lines.append(line)
                facts_section = (
                    "\n\n## Recorded Facts (MUST be preserved — these survive compaction):\n"
                    + "\n".join(fact_lines)
                    + "\n\nThese facts were explicitly recorded during research. "
                    "Use ALL of them when producing the final output."
                )
        except Exception as e:
            log.warning(f"Could not inject recorded facts: {e}")

        # Inject active plan state so step tracking survives compaction
        plan_section = ""
        try:
            from tools import _active_plan
            if _active_plan:
                step_lines = []
                for s in _active_plan["steps"]:
                    step_lines.append(f"  {s['step_id']}: {s['description']} [{s['status']}]")
                plan_section = (
                    f"\n\n## Active Plan: {_active_plan['plan_id']}\n"
                    f"Goal: {_active_plan['goal']}\n"
                    + "\n".join(step_lines)
                )
                if _active_plan.get("output_file"):
                    plan_section += f"\nOutput file: {_active_plan['output_file']}"
                plan_section += (
                    "\n\nContinue executing from the next pending step. "
                    "Mark each step in_progress before starting, done when finished."
                )
        except Exception as e:
            log.warning(f"Could not inject plan state: {e}")

        # Inject research outline state
        outline_section = ""
        try:
            from tools import get_research_outline
            outline = get_research_outline()
            if outline:
                sec_lines = []
                for s in outline["sections"]:
                    sec_lines.append(
                        f"  {s['id']}: {s['title']} [{s['status']}] "
                        f"({s['words_written']}w, {s['citations_used']} cites)"
                    )
                outline_section = (
                    f"\n\n## Research Outline: {outline['title']}\n"
                    f"Target: {outline['target_words']} words\n"
                    f"Thesis: {outline.get('thesis', 'TBD')}\n"
                    + "\n".join(sec_lines)
                )
        except Exception as e:
            log.warning(f"Could not inject outline: {e}")

        # Inject drafted sections (keep word counts compact, not full text)
        drafts_section = ""
        try:
            from tools import get_draft_sections
            drafts = get_draft_sections()
            if drafts:
                total_words = sum(d["words"] for d in drafts.values())
                draft_lines = []
                for sid, d in drafts.items():
                    draft_lines.append(f"  {sid}: {d['words']}w, cites {d['citations']}")
                drafts_section = (
                    f"\n\n## Drafted Sections ({len(drafts)} done, {total_words} words total):\n"
                    + "\n".join(draft_lines)
                    + "\nFull section text is stored in memory. Continue drafting remaining sections."
                )
        except Exception as e:
            log.warning(f"Could not inject drafts: {e}")

        # Inject asset graph state so organizer can resume after compaction
        graph_section = self._get_graph_recovery_context()

        # Replace with compacted session
        continuation = (
            "This session is being continued from a previous conversation that ran out of context. "
            "The summary below covers the earlier portion of the conversation.\n\n"
            f"Summary:\n{summary}"
            f"{facts_section}"
            f"{plan_section}"
            f"{outline_section}"
            f"{drafts_section}"
            f"{graph_section}\n\n"
            "Recent messages are preserved verbatim. "
            "Continue the conversation from where it left off. "
            "If the asset graph shows a RESUME directive, follow it immediately."
        )

        self.session.messages = [
            ConversationMessage.user_text(f"[Context: {continuation}]"),
            *recent
        ]
        after = self.session.estimate_tokens()
        log.info(f"Compacted: {len(old)} old → summary, kept {len(recent)} recent messages")
        log.info(f"Compacted to {len(self.session.messages)} messages, ~{after} tokens")

        # If compaction didn't reduce by at least 30% and we still have many recent messages,
        # do aggressive truncation: strip long messages from recent to reduce context
        if after > token_threshold * 0.85 and len(recent) > 2:
            log.warning(f"Compaction ineffective ({estimated}→{after}), truncating recent messages")
            # Keep only the last 2 messages from recent
            recent_trimmed = recent[-2:]
            self.session.messages = [
                ConversationMessage.user_text(f"[Context: {continuation}]"),
                *recent_trimmed
            ]
            after = self.session.estimate_tokens()
            log.info(f"Aggressive trim: now {len(self.session.messages)} messages, ~{after} tokens")

            # If STILL too large, truncate individual message content
            if after > token_threshold * 0.7:
                for i, msg in enumerate(self.session.messages):
                    text = msg.text_content()
                    if len(text) > 8000:
                        # Truncate to first and last 3000 chars
                        truncated = text[:3000] + "\n\n[...middle truncated...]\n\n" + text[-3000:]
                        msg.blocks = [TextBlock(text=truncated)]
                after = self.session.estimate_tokens()
                log.info(f"Message truncation: ~{after} tokens")

        progress("done", 100, f"{estimated // 1000}K → {after // 1000}K tokens")

    def _llm_summarize(self, transcript: str, on_progress=None) -> str | None:
        """
        Use the model to generate a high-quality summary of old conversation.
        Returns None if the LLM call fails (caller falls back to text summary).
        Streams progress via on_progress callback.
        """
        prompt = (
            "You are summarizing a conversation that needs to be compacted to save context space. "
            "Produce a concise but thorough summary that preserves:\n"
            "- The user's original request and goals\n"
            "- Key decisions made and why\n"
            "- Important file paths, names, and values mentioned\n"
            "- What actions were taken (tools used, files read/written)\n"
            "- Current state and what remains to be done\n\n"
            "Be specific — include exact file names, paths, and key details. "
            "Write in past tense as a factual record. Keep it under 800 words.\n\n"
            "--- CONVERSATION TRANSCRIPT ---\n"
            f"{transcript}\n"
            "--- END TRANSCRIPT ---\n\n"
            "Summary:"
        )

        try:
            import httpx
            payload = {
                "model": self.backend_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2048,
                "temperature": 0.3,
                "stream": True,
            }
            summary_chunks = []
            with httpx.Client(timeout=60.0) as client:
                with client.stream(
                    "POST",
                    f"{self.backend_url}/v1/chat/completions",
                    json=payload,
                ) as resp:
                    if resp.status_code != 200:
                        log.error(f"LLM summary failed: HTTP {resp.status_code}")
                        return None

                    for line in resp.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                summary_chunks.append(token)
                                # Emit streaming progress: 50-89% range
                                total_so_far = len("".join(summary_chunks))
                                # Estimate progress based on expected ~2000 chars
                                pct = 50 + min(int((total_so_far / 2000) * 39), 39)
                                if on_progress and total_so_far % 80 < len(token):
                                    on_progress("summarizing", pct,
                                                f"Generating summary... ({total_so_far} chars)")
                        except (json.JSONDecodeError, IndexError, KeyError):
                            continue

            result = "".join(summary_chunks).strip()
            if result:
                log.info(f"LLM summary generated: {len(result)} chars")
                return result
            return None

        except Exception as e:
            log.error(f"LLM summarization failed: {e}")
            return None
