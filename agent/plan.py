"""
Clyde — Task Plan State
==============================

Persistent multi-step plan tracking, scoped to a single conversation.

Lifecycle:
  1. Model calls task_plan(...) to create plan.json in the session dir
  2. plan.status = "pending_approval" until user approves via ask_user
  3. On approval, status flips to "active"
  4. Model calls task_update(step_id, "in_progress" | "done" | "failed", notes?)
  5. When all steps are done OR task_complete is called, plan is archived
  6. Archived plans live alongside the active one as plan-archive-<ts>.json

State file lives at:
  ~/.clyde/sessions/<conv_id>/plan.json
  ~/.clyde/sessions/<conv_id>/plan-archive-<unix_ts>.json

This module is intentionally I/O-light. Each operation reads/writes a small
JSON file. There's no caching layer because the file is small (<10 KB) and
correctness across the streaming server / tool / agent boundary matters more
than micro-perf.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger("plan")


# ─── Constants ───

STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

STEP_STATUS_PENDING = "pending"
STEP_STATUS_IN_PROGRESS = "in_progress"
STEP_STATUS_DONE = "done"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_SKIPPED = "skipped"

# After this many consecutive failures on a single step, escalate to user.
MAX_STEP_RETRIES = 3


# ─── Data ───

@dataclass
class PlanStep:
    id: str
    description: str
    status: str = STEP_STATUS_PENDING
    notes: str = ""
    # Past failed attempts: each entry is "approach: short description — reason it failed".
    # The model reads this on retries and is told NOT to repeat any of them.
    failed_attempts: list[str] = field(default_factory=list)
    # True if the user (or stop button) interrupted the step mid-execution.
    # On resume, the model should inspect the output file and pick up rather
    # than restart from scratch.
    was_interrupted: bool = False
    # Step type: normal, user_interjection (ad-hoc query inserted mid-plan),
    # subplan (placeholder for a nested plan keyed by subplan_id).
    type: str = "normal"
    subplan_id: Optional[str] = None  # Set when type == "subplan"
    # Telemetry: how many model iterations this step consumed once it
    # entered in_progress. Lets the UI surface "Step 3 took 12 iters" and
    # gives the resume path / future enforcement a hard signal for stuck steps.
    iterations_used: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PlanStep":
        return cls(
            id=d.get("id") or f"s_{uuid.uuid4().hex[:6]}",
            description=d.get("description", ""),
            status=d.get("status", STEP_STATUS_PENDING),
            notes=d.get("notes", ""),
            failed_attempts=list(d.get("failed_attempts", []) or []),
            was_interrupted=bool(d.get("was_interrupted", False)),
            type=d.get("type", "normal"),
            subplan_id=d.get("subplan_id"),
            iterations_used=int(d.get("iterations_used", 0) or 0),
        )


@dataclass
class Plan:
    id: str
    goal: str
    status: str = STATUS_PENDING_APPROVAL
    output_file: Optional[str] = None
    steps: list[PlanStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    parent_step_id: Optional[str] = None  # Set for nested plans
    parent_plan_id: Optional[str] = None  # Set for nested plans

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "output_file": self.output_file,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "parent_step_id": self.parent_step_id,
            "parent_plan_id": self.parent_plan_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(
            id=d.get("id") or f"plan_{uuid.uuid4().hex[:8]}",
            goal=d.get("goal", ""),
            status=d.get("status", STATUS_ACTIVE),
            output_file=d.get("output_file"),
            steps=[PlanStep.from_dict(s) for s in d.get("steps", [])],
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            completed_at=d.get("completed_at"),
            parent_step_id=d.get("parent_step_id"),
            parent_plan_id=d.get("parent_plan_id"),
        )

    # ─── Step lookups ───

    def get_step(self, step_id: str) -> Optional[PlanStep]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def next_pending_step(self) -> Optional[PlanStep]:
        """Return the first step that is pending or in_progress (resume target)."""
        for s in self.steps:
            if s.status in (STEP_STATUS_IN_PROGRESS, STEP_STATUS_PENDING):
                return s
        return None

    def is_done(self) -> bool:
        """All steps are done or skipped (failed steps prevent completion)."""
        if not self.steps:
            return False
        return all(s.status in (STEP_STATUS_DONE, STEP_STATUS_SKIPPED) for s in self.steps)

    def has_failed_step(self) -> bool:
        return any(s.status == STEP_STATUS_FAILED for s in self.steps)

    def progress_summary(self) -> str:
        """Short '3/10 done' style summary."""
        total = len(self.steps)
        done = sum(1 for s in self.steps if s.status == STEP_STATUS_DONE)
        return f"{done}/{total} done"


# ─── Storage ───

def _plan_path(session_dir: Path, conv_id: str) -> Path:
    """Path to the active plan.json for a conversation."""
    return _conv_dir(session_dir, conv_id) / "plan.json"


def _conv_dir(session_dir: Path, conv_id: str) -> Path:
    """
    Per-conversation directory for plan files.

    Sessions today live as flat <conv_id>.json files in session_dir. We put
    plans in a sibling subdirectory <conv_id>/ so they don't pollute the
    flat session listing and can hold multiple files (active + archives).
    """
    d = session_dir / conv_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_plan(session_dir: Path, conv_id: str) -> Optional[Plan]:
    """Load the active plan for a conversation, if any."""
    if not session_dir or not conv_id:
        return None
    p = _plan_path(session_dir, conv_id)
    if not p.exists():
        return None
    try:
        with open(p) as f:
            data = json.load(f)
        return Plan.from_dict(data)
    except Exception as e:
        log.error(f"Failed to load plan from {p}: {e}")
        return None


def save_plan(session_dir: Path, conv_id: str, plan: Plan) -> None:
    """Persist the active plan to disk."""
    if not session_dir or not conv_id:
        return
    plan.updated_at = time.time()
    p = _plan_path(session_dir, conv_id)
    try:
        with open(p, "w") as f:
            json.dump(plan.to_dict(), f, indent=2)
    except Exception as e:
        log.error(f"Failed to save plan to {p}: {e}")


def archive_plan(session_dir: Path, conv_id: str, plan: Plan) -> Optional[Path]:
    """
    Soft-archive a plan: move plan.json → plan-archive-<ts>.json.

    Soft archive (not delete) so the model can find it later if the user
    references it. Returns the new path or None if there was nothing to
    archive.
    """
    if not session_dir or not conv_id:
        return None
    src = _plan_path(session_dir, conv_id)
    if not src.exists():
        return None
    plan.completed_at = time.time()
    if plan.status not in (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED):
        plan.status = STATUS_COMPLETED
    ts = int(plan.completed_at)
    dst = _conv_dir(session_dir, conv_id) / f"plan-archive-{ts}-{plan.id}.json"
    try:
        with open(dst, "w") as f:
            json.dump(plan.to_dict(), f, indent=2)
        src.unlink()
        log.info(f"Plan {plan.id} archived → {dst.name}")
        return dst
    except Exception as e:
        log.error(f"Failed to archive plan {plan.id}: {e}")
        return None


def list_archived_plans(session_dir: Path, conv_id: str) -> list[Plan]:
    """Return all archived plans for a conversation, newest first."""
    if not session_dir or not conv_id:
        return []
    d = _conv_dir(session_dir, conv_id)
    archives: list[tuple[float, Plan]] = []
    for p in d.glob("plan-archive-*.json"):
        try:
            with open(p) as f:
                data = json.load(f)
            plan = Plan.from_dict(data)
            archives.append((plan.completed_at or 0, plan))
        except Exception as e:
            log.warning(f"Skipping unreadable archive {p}: {e}")
    archives.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in archives]


def reactivate_archived_plan(
    session_dir: Path,
    conv_id: str,
    plan_id: str,
) -> Optional[Plan]:
    """
    Bring an archived plan back as the active plan. Used when the user says
    'you forgot to do xyz on that thing' and the model needs to resume work.

    The reactivated plan keeps its original ID and steps. The caller is
    expected to mutate the steps (e.g., reset failed/done flags on the parts
    that need redoing) and then save_plan it.

    If a different plan is already active, this fails — there can be only one.
    """
    active = load_plan(session_dir, conv_id)
    if active is not None:
        log.warning(f"Cannot reactivate {plan_id}: another plan is already active")
        return None
    d = _conv_dir(session_dir, conv_id)
    for p in d.glob(f"plan-archive-*-{plan_id}.json"):
        try:
            with open(p) as f:
                data = json.load(f)
            plan = Plan.from_dict(data)
            plan.status = STATUS_ACTIVE
            plan.completed_at = None
            plan.updated_at = time.time()
            save_plan(session_dir, conv_id, plan)
            p.unlink()  # Remove from archive
            log.info(f"Reactivated archived plan {plan_id}")
            return plan
        except Exception as e:
            log.error(f"Failed to reactivate {plan_id} from {p}: {e}")
            return None
    log.warning(f"No archived plan found with id={plan_id}")
    return None


# ─── Plan creation ───

def create_plan(
    session_dir: Path,
    conv_id: str,
    goal: str,
    steps: list[dict],
    output_file: Optional[str] = None,
    parent_step_id: Optional[str] = None,
    parent_plan_id: Optional[str] = None,
    auto_active: bool = False,
) -> Plan:
    """
    Create a new plan and persist it.

    `auto_active=True` skips the pending_approval state — used for nested
    plans created from inside an already-approved parent plan.
    """
    plan = Plan(
        id=f"plan_{uuid.uuid4().hex[:8]}",
        goal=goal,
        status=STATUS_ACTIVE if auto_active else STATUS_PENDING_APPROVAL,
        output_file=output_file,
        steps=[
            PlanStep(
                id=f"s_{uuid.uuid4().hex[:6]}",
                description=str(s.get("description", "") if isinstance(s, dict) else s),
                status=STEP_STATUS_PENDING,
            )
            for s in steps
        ],
        parent_step_id=parent_step_id,
        parent_plan_id=parent_plan_id,
    )
    save_plan(session_dir, conv_id, plan)
    return plan


# ─── Plan state injection (used by conversation.py) ───

def render_plan_block(plan: Plan, full: bool = True) -> str:
    """
    Build the human-readable plan state block that gets injected into the
    model's context every iteration while a plan is active.

    `full=True` includes step descriptions and notes (the user requested this).
    `full=False` returns a compact title-only listing.
    """
    if not plan or not plan.steps:
        return ""

    lines: list[str] = []
    lines.append(f"[Active plan: {plan.goal}]")
    if plan.output_file:
        lines.append(f"  Output → {plan.output_file}")
    lines.append(f"  Status: {plan.status} ({plan.progress_summary()})")
    lines.append("")

    icon_map = {
        STEP_STATUS_DONE: "✓",
        STEP_STATUS_IN_PROGRESS: "▶",
        STEP_STATUS_PENDING: "·",
        STEP_STATUS_FAILED: "✗",
        STEP_STATUS_SKIPPED: "→",
    }
    for s in plan.steps:
        icon = icon_map.get(s.status, "·")
        prefix = f"  {icon} {s.id}: {s.description}"
        if s.was_interrupted and s.status == STEP_STATUS_IN_PROGRESS:
            prefix += "  [interrupted — resume from artifact]"
        if s.type == "user_interjection":
            prefix += "  [user query]"
        elif s.type == "subplan":
            prefix += f"  [subplan {s.subplan_id}]"
        lines.append(prefix)
        if full and s.notes:
            for note_line in s.notes.splitlines():
                lines.append(f"      · {note_line}")
        if full and s.failed_attempts:
            lines.append(f"      ⚠ Previously tried (do NOT repeat):")
            for fa in s.failed_attempts:
                lines.append(f"        - {fa}")

    return "\n".join(lines)


def render_plan_for_system_prompt(plan: Plan) -> str:
    """Wrapper section for injection into the system message."""
    if not plan:
        return ""
    body = render_plan_block(plan, full=True)
    if not body:
        return ""
    return (
        "# Active Task Plan\n"
        "You have an active multi-step plan in this conversation. The current\n"
        "state is below. Continue executing pending steps until the plan is\n"
        "done, the user stops you, or a step needs user input. After each step,\n"
        "call task_update to mark progress before starting the next.\n\n"
        + body
    )
