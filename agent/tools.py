"""
Clyde — Tool Specs & Execution
======================================
Aligned with claw-code: rust/crates/tools/src/lib.rs

Tool registry with specs matching claw-code's mvp_tool_specs():
  bash, read_file, write_file, edit_file, glob_search, grep_search,
  + memory tools (memory_read, memory_write, memory_update, memory_delete,
    memory_search, memory_list, transcript_search)

Each tool has: name, description, input_schema, execute function.
"""

from __future__ import annotations
import json
import logging
import os
import re
import subprocess
import sys
import time
import glob as globmod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any

import yaml

# ─── Config ───
_CFG_PATH = Path(__file__).parent / "config.yaml"
with open(_CFG_PATH) as f:
    _CFG = yaml.safe_load(f)

TOOL_TIMEOUT = _CFG["agent"]["tool_timeout"]
HOME = Path.home()

# ─── Folder Permissions (per-session) ───
# The agent home is ALWAYS allowed. Everything else is granted per-session
# (per conversation) and resets when a new conversation starts.
_AGENT_HOME = Path(_CFG["paths"]["agent_home"]).expanduser().resolve()
_CLYDE_HOME = Path("~/.clyde").expanduser().resolve()
# Read-only-ish "common workspace" defaults: /tmp for scratch, ~/Library for
# app data (browser bookmarks, plists, sqlite stores). Without these, the
# model triggers a permission prompt on the FIRST tool call for any
# bookmark / browser-data / scratch-file workflow, which crushes turn-loop
# autonomy. The user can revoke by closing the conversation if anything
# escalates beyond expectations.
_DEFAULT_LIBRARY = Path("~/Library").expanduser().resolve()
_DEFAULT_TMP = Path("/tmp").resolve()
_ALLOWED_DIRS: list[Path] = [
    _AGENT_HOME, _CLYDE_HOME, _DEFAULT_LIBRARY, _DEFAULT_TMP,
]

# Recently accessed directories — used to resolve bare filenames.
# When a file is successfully read/written, its parent directory is cached here.
# This prevents the model from needing to always use absolute paths.
_RECENT_DIRS: list[Path] = []
_MAX_RECENT_DIRS = 20

# Callback for permission requests — set by conversation runtime
_permission_callback = None

def set_permission_callback(cb):
    """Set the callback for folder permission requests. Called by conversation.py."""
    global _permission_callback
    _permission_callback = cb

def reset_session_permissions():
    """
    Reset allowed directories to the default common workspace. Called when
    a new conversation starts so permissions don't carry across sessions.
    """
    global _ALLOWED_DIRS, _RECENT_DIRS, _organize_approved_dirs
    _ALLOWED_DIRS = [
        _AGENT_HOME, _CLYDE_HOME, _DEFAULT_LIBRARY, _DEFAULT_TMP,
    ]
    _RECENT_DIRS = []
    _organize_approved_dirs = set()
    logging.getLogger("tools").info(
        "Session permissions reset (agent_home + clyde + ~/Library + /tmp)"
    )

def _record_accessed_dir(path: Path):
    """Remember a directory we've accessed for smarter relative path resolution."""
    d = path.parent if path.is_file() else path
    d = d.resolve()
    if d in _RECENT_DIRS:
        _RECENT_DIRS.remove(d)
    _RECENT_DIRS.insert(0, d)
    if len(_RECENT_DIRS) > _MAX_RECENT_DIRS:
        _RECENT_DIRS.pop()

def grant_folder(path: str):
    """Grant access to a folder for the current session."""
    p = Path(path).expanduser().resolve()
    if p not in _ALLOWED_DIRS:
        _ALLOWED_DIRS.append(p)
        logging.getLogger("tools").info(f"Folder granted (session): {p}")

def get_allowed_dirs() -> list[str]:
    """Return list of currently allowed directories."""
    return [str(d) for d in _ALLOWED_DIRS]


log = logging.getLogger("tools")

# ─── Context Budget (set by conversation.py before each tool call) ───
_context_budget = {
    "total": 262000,
    "used": 0,
    "available": 262000,
}

def set_context_budget(total: int, used: int):
    """Update context budget. Called by conversation.py before each tool execution."""
    global _context_budget
    _context_budget["total"] = total
    _context_budget["used"] = used
    _context_budget["available"] = max(0, total - used)

def get_available_tokens() -> int:
    """How many tokens are available for tool output."""
    return _context_budget["available"]


# ─── File Size Estimation ───

def _detect_file_type(filename: str) -> str:
    """Classify file by extension for truncation strategy."""
    ext = Path(filename).suffix.lower()
    if ext in (".csv", ".tsv"):
        return "csv"
    if ext in (".json", ".jsonl"):
        return "json"
    if ext in (".py", ".swift", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".rb", ".sh", ".bash"):
        return "code"
    if ext in (".md", ".markdown", ".rst", ".txt"):
        return "text"
    if ext in (".log",):
        return "log"
    if ext in (".html", ".htm", ".xml", ".svg"):
        return "markup"
    if ext in (".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"):
        return "config"
    # Binary check by extension
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
               ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".flac",
               ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
               ".exe", ".dll", ".so", ".dylib", ".o", ".a",
               ".pdf", ".doc", ".xls", ".ppt",
               ".sqlite", ".db", ".bin"):
        return "binary"
    return "text"  # Default


def estimate_file_tokens(path: Path) -> int:
    """
    Estimate how many tokens a file would consume if fully read.
    Uses file size on disk + type-specific density.
    """
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return 0

    file_type = _detect_file_type(path.name)

    # Chars-per-token varies by content type
    if file_type == "csv":
        return size_bytes // 3   # CSV is dense (numbers, commas)
    elif file_type == "json":
        return size_bytes // 3   # JSON has lots of punctuation
    elif file_type == "code":
        return size_bytes // 4   # Code is moderately dense
    elif file_type == "markup":
        return size_bytes // 3   # Tags add overhead
    elif file_type == "binary":
        return 999999            # Can't read, signal impossibly large
    else:
        return size_bytes // 4   # General text


def _smart_truncate(content: str, file_type: str, max_chars: int) -> tuple[str, bool]:
    """
    Apply type-aware truncation. Returns (truncated_content, was_truncated).
    Preserves structure based on file type.
    """
    if len(content) <= max_chars:
        return content, False

    lines = content.splitlines()
    total_lines = len(lines)

    if file_type == "csv":
        # CSV: keep header + as many rows as fit + row count footer
        if total_lines <= 1:
            return content[:max_chars], True
        header = lines[0]
        data_lines = lines[1:]
        output = [header]
        char_count = len(header) + 1
        rows_included = 0
        for line in data_lines:
            if char_count + len(line) + 1 > int(max_chars * 0.85):
                break
            output.append(line)
            char_count += len(line) + 1
            rows_included += 1
        remaining = len(data_lines) - rows_included
        if remaining > 0:
            output.append(f"\n[... {remaining} more rows, {total_lines} total ...]")
        return "\n".join(output), True

    elif file_type in ("code", "config"):
        # Code: head + tail + line count (preserve imports/structure at top, logic at bottom)
        head_n = min(60, total_lines // 3)
        tail_n = min(40, total_lines // 4)
        head = lines[:head_n]
        tail = lines[-tail_n:] if tail_n > 0 else []
        omitted = total_lines - head_n - tail_n
        result = "\n".join(head)
        if omitted > 0:
            result += f"\n\n[... {omitted} lines omitted ...]\n\n"
            result += "\n".join(tail)
        result += f"\n\n[Total: {total_lines} lines]"
        return result[:max_chars], True

    elif file_type == "json":
        # JSON: first portion + item count hint
        result = content[:int(max_chars * 0.9)]
        # Try to find the total number of top-level items
        try:
            parsed = __import__("json").loads(content)
            if isinstance(parsed, list):
                result += f"\n\n[... JSON array with {len(parsed)} items total, showing first portion ...]"
            elif isinstance(parsed, dict):
                result += f"\n\n[... JSON object with {len(parsed)} keys total, showing first portion ...]"
        except Exception:
            result += f"\n\n[... truncated at {max_chars} chars, {total_lines} lines total ...]"
        return result[:max_chars], True

    elif file_type == "log":
        # Logs: first 30 lines + last 30 lines (most useful at boundaries)
        head_n = 30
        tail_n = 30
        if total_lines <= head_n + tail_n:
            return content[:max_chars], True
        head = lines[:head_n]
        tail = lines[-tail_n:]
        omitted = total_lines - head_n - tail_n
        result = "\n".join(head)
        result += f"\n\n[... {omitted} lines omitted ...]\n\n"
        result += "\n".join(tail)
        result += f"\n\n[Total: {total_lines} lines]"
        return result[:max_chars], True

    else:
        # Generic text: head + tail
        head_n = min(80, total_lines // 2)
        tail_n = min(40, total_lines // 3)
        if total_lines <= head_n + tail_n + 5:
            return content[:max_chars], True
        head = lines[:head_n]
        tail = lines[-tail_n:] if tail_n > 0 else []
        omitted = total_lines - head_n - tail_n
        result = "\n".join(head)
        if omitted > 0:
            result += f"\n\n[... {omitted} lines omitted ...]\n\n"
            result += "\n".join(tail)
        result += f"\n\n[Total: {total_lines} lines]"
        return result[:max_chars], True


def _resolve_path(raw_path: str) -> Path:
    """
    Resolve a file path intelligently:
    - Expands ~ to home directory
    - If relative, tries recently-accessed dirs first (smart context)
    - Then common base dirs: ~/, ~/Desktop/, ~/Documents/
    - Always returns an absolute Path
    """
    path = os.path.expanduser(raw_path)
    p = Path(path)

    # Already absolute and exists? Done
    if p.is_absolute():
        return p.resolve()

    # Try recently-accessed directories first — this catches bare filenames
    # like "TV_Movies_Rename_v2.csv" when we just accessed its parent dir.
    for recent_dir in _RECENT_DIRS:
        candidate = recent_dir / raw_path
        if candidate.exists():
            return candidate.resolve()

    # Try allowed directories (user already granted access to these)
    for allowed_dir in _ALLOWED_DIRS:
        candidate = allowed_dir / raw_path
        if candidate.exists():
            return candidate.resolve()

    # Try relative to home
    home_rel = HOME / raw_path
    if home_rel.exists():
        return home_rel.resolve()

    # Try common subdirectories of home
    for subdir in ("Desktop", "Documents", "Downloads"):
        candidate = HOME / subdir / raw_path
        if candidate.exists():
            return candidate.resolve()

    # Return as absolute from home (best guess for the model)
    return (HOME / raw_path).resolve()


def _check_folder_permission(path: Path) -> str | None:
    """
    Check if a path is within an allowed directory.
    Returns None if allowed, or an error string if denied.
    If a permission callback is set, it requests permission and blocks.
    """
    resolved = path.resolve()

    # Check against all allowed directories
    for allowed in _ALLOWED_DIRS:
        try:
            resolved.relative_to(allowed)
            return None  # Path is within an allowed directory
        except ValueError:
            continue

    # Not allowed — return structured error for conversation loop to intercept
    return (
        f"PERMISSION_DENIED: Cannot access {resolved}. "
        f"This path is outside the allowed directories."
    )


# ─── ToolSpec (aligned with claw-code ToolSpec) ───

@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    execute: Callable[[dict], str]

    def to_openai_tool(self) -> dict:
        """Convert to OpenAI function-calling tool format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            }
        }


# ─── Tool Implementations ───

def _extract_paths_from_command(command: str) -> list[Path]:
    """
    Extract file/directory paths from a shell command for permission checking.
    Handles: ~/path, /absolute/path, quoted paths with spaces.
    """
    import shlex
    paths = []
    # Match ~ paths and absolute paths (including quoted ones with spaces)
    # Pattern: ~/something or /Users/something or /home/something
    for pattern in [
        r'~[/\w][^\s;|>&]*',           # ~/Desktop/foo
        r'"(~[/\w][^"]*)"',             # "~/Desktop/Rename Plan"
        r"'(~[/\w][^']*)'",             # '~/Desktop/Rename Plan'
        r'/Users/\w+[/\w][^\s;|>&]*',   # $HOME/Desktop/foo
        r'"/Users/\w+[^"]*"',           # "$HOME/Desktop/Rename Plan"
    ]:
        for match in re.finditer(pattern, command):
            raw = match.group(1) if match.lastindex else match.group(0)
            raw = raw.strip('"').strip("'")
            p = Path(os.path.expanduser(raw)).resolve()
            if p != HOME and p != HOME.parent:
                paths.append(p)
    return paths


def _exec_bash(args: dict) -> str:
    command = args.get("command", "")
    if not command:
        return "ERROR: No command provided."
    dangerous = ["rm -rf /", "mkfs", "dd if=/dev/zero", "> /dev/sd"]
    if any(d in command for d in dangerous):
        return "ERROR: Command blocked for safety."

    # Redirect when model treats bookmark_locator/bookmark_extract_all as a
    # script. They are TOOLS — call them via the tool channel, not bash.
    _bm_tool_pat = re.compile(
        r"\b(bookmark_locator|bookmark_extract_all)\b(?!\.py)"
    )
    m = _bm_tool_pat.search(command)
    if m:
        name = m.group(1)
        return (
            f"REDIRECT: `{name}` is a TOOL, not a script. Do NOT run it via "
            f"bash. Call it directly: emit "
            f'<tool_call>{{"name": "{name}", "arguments": {{}}}}</tool_call>'
        )

    # Auto-execute parse_bookmarks.py shell calls as the `parse_bookmarks`
    # tool. Without this the model hits python-version / PYTHONHOME /
    # shell-quoting issues and spends turns "fixing" a script that was
    # never broken. Extract the first *.html arg and route through the
    # dedicated tool. Quiet — the user never sees the troubleshooting.
    if "parse_bookmarks.py" in command:
        # Pull first quoted or bare path that looks like an HTML file.
        _pb = re.search(
            r'(?:"([^"]+\.html)"|\'([^\']+\.html)\'|(\S+\.html))',
            command,
        )
        if _pb:
            html = _pb.group(1) or _pb.group(2) or _pb.group(3)
            # Optional second path = state_dir (common model pattern).
            _rest = command.split(_pb.group(0), 1)[1].strip()
            _sd_m = re.match(
                r'(?:"([^"]+)"|\'([^\']+)\'|(\S+))',
                _rest,
            )
            state_dir = ""
            if _sd_m:
                state_dir = _sd_m.group(1) or _sd_m.group(2) or _sd_m.group(3) or ""
            args_out = {"html_path": html}
            if state_dir:
                args_out["state_dir"] = state_dir
            result_text = _exec_parse_bookmarks(args_out)
            return (
                "AUTO-EXECUTED `parse_bookmarks` for you (your bash "
                "`python3 parse_bookmarks.py ...` was routed through the "
                "dedicated tool — avoids shell-quoting and python-version "
                f"surprises):\n\n{result_text}"
            )

    # Check folder permissions for paths in the command
    paths = _extract_paths_from_command(command)
    for p in paths:
        perm_error = _check_folder_permission(p)
        if perm_error:
            return perm_error

    timeout = min(args.get("timeout", TOOL_TIMEOUT), 120)
    # Strip PYTHONHOME/PATH inherited from the agent's bundled Python so
    # `python3` invocations from the model resolve to the system /usr/bin
    # interpreter cleanly (avoids "Could not find platform-independent
    # libraries" when ClydeEngine's python lib is incompatible).
    _bash_env = dict(os.environ)
    if "python" in command.lower():
        _bash_env.pop("PYTHONHOME", None)
        _bash_env.pop("PYTHONPATH", None)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=str(HOME), env=_bash_env,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
        if not output:
            output = f"(exit code: {result.returncode})"
        if len(output) > 10000:
            output = output[:10000] + "\n\n[output truncated at 10000 chars]"
        return output
    except subprocess.TimeoutExpired:
        return f"ERROR: Command timed out after {timeout}s."
    except Exception as e:
        return f"ERROR: {e}"


def _exec_read_file(args: dict) -> str:
    raw_path = args.get("path", "")
    if not raw_path:
        return "ERROR: No path provided."
    try:
        p = _resolve_path(raw_path)

        # Check folder permission
        perm_error = _check_folder_permission(p)
        if perm_error:
            return perm_error

        if not p.exists():
            # Provide helpful hint with the resolved path, including recently-accessed dirs
            hint_dirs = ", ".join(str(d) for d in _RECENT_DIRS[:5]) if _RECENT_DIRS else "none yet"
            return (
                f"ERROR: File not found: {p}\n"
                f"Hint: You provided '{raw_path}' which resolved to '{p}'. "
                f"Use absolute paths like ~/Desktop/folder/file.txt. "
                f"Recently accessed dirs: [{hint_dirs}]"
            )
        if p.is_dir():
            _record_accessed_dir(p)
            entries = sorted(p.iterdir())[:100]
            lines = [f"{'d' if e.is_dir() else 'f'} {e.name}" for e in entries]
            return "\n".join(lines) if lines else "(empty directory)"
        _record_accessed_dir(p)
        try:
            _graph_add_node(
                "file",
                title=p.name,
                body=str(p),
                source_tool="read_file",
                metadata={"path": str(p), "size": p.stat().st_size},
                dedup_key=f"file::{p}",
            )
        except Exception:
            pass

        # ── Pre-read context check ──
        # Estimate how many tokens this file would consume. If it exceeds
        # available context, return a PREEMPT_COMPACT signal so conversation.py
        # can compact first and then retry the read.
        file_tokens = estimate_file_tokens(p)
        available = get_available_tokens()

        # Binary files: reject outright
        file_type = _detect_file_type(p.name)
        if file_type == "binary":
            return (
                f"ERROR: Cannot read binary file: {p.name} "
                f"(type: {p.suffix}). Use bash to inspect binary files."
            )

        # File larger than entire context window: reject with helpful info
        total_budget = _context_budget["total"]
        # Reserve ~40K for system prompt + response + overhead
        max_file_tokens = total_budget - 40000
        if file_tokens > max_file_tokens:
            size_mb = p.stat().st_size / (1024 * 1024)
            return (
                f"ERROR: File too large for context window.\n"
                f"File: {p.name} ({size_mb:.1f} MB, ~{file_tokens:,} estimated tokens)\n"
                f"Max capacity: ~{max_file_tokens:,} tokens\n"
                f"Suggestion: Use offset/limit params to read a portion, or use "
                f"bash with grep/head/tail to extract relevant sections."
            )

        # File fits in context but exceeds AVAILABLE budget → signal compact first
        # Leave 20K headroom for the model's response after reading
        read_headroom = 20000
        if file_tokens > (available - read_headroom) and file_tokens <= max_file_tokens:
            return (
                f"PREEMPT_COMPACT: File {p.name} needs ~{file_tokens:,} tokens "
                f"but only ~{max(0, available - read_headroom):,} available. "
                f"Compact context first, then retry this read."
            )

        # Show the resolved path if different from what the model gave
        path_hint = ""
        resolved_str = str(p)
        home_str = str(HOME)
        display_path = resolved_str.replace(home_str, "~") if resolved_str.startswith(home_str) else resolved_str
        if raw_path != display_path and raw_path != resolved_str:
            path_hint = f"[resolved: {display_path}]\n"

        offset = int(args.get("offset", 0) or 0)
        raw_limit = args.get("limit")
        if raw_limit is not None:
            limit = max(int(raw_limit), 50)  # Floor: never read fewer than 50 lines
        else:
            limit = 2000
        full_text = p.read_text(errors="replace")
        lines = full_text.splitlines()
        selected = lines[offset:offset + limit]
        numbered = [f"{i + offset + 1}\t{line}" for i, line in enumerate(selected)]
        result = path_hint + "\n".join(numbered)

        # ── Smart truncation based on available context ──
        # Calculate max chars we can afford: use available tokens × 4 chars/token,
        # but cap at a reasonable maximum and leave room for response.
        max_result_tokens = min(available - read_headroom, max_file_tokens)
        max_result_chars = max(4000, max_result_tokens * 4)  # Floor of 4K chars

        # Also enforce a hard cap so a single read can't monopolize context
        HARD_CAP_CHARS = 120000  # ~30K tokens
        max_result_chars = min(max_result_chars, HARD_CAP_CHARS)

        if len(result) > max_result_chars:
            # Apply type-aware smart truncation
            truncated, was_truncated = _smart_truncate(result, file_type, max_result_chars)
            if was_truncated:
                result = truncated
                total_lines = len(lines)
                result += (
                    f"\n\n[Smart-truncated to fit context. "
                    f"File has {total_lines} lines total. "
                    f"Use offset/limit params or bash grep to access specific sections.]"
                )
        return result
    except Exception as e:
        return f"ERROR: {e}"


def _exec_write_file(args: dict) -> str:
    raw_path = args.get("path", "")
    content = args.get("content", "")
    if not raw_path:
        return "ERROR: No path provided."
    try:
        p = _resolve_path(raw_path)
        perm_error = _check_folder_permission(p)
        if perm_error:
            return perm_error
        p.parent.mkdir(parents=True, exist_ok=True)

        # ── Destructive overwrite safeguard ──
        # If the file already exists and the new content is dramatically
        # smaller (less than 25% of the original), auto-backup and warn.
        # This prevents accidental data destruction when the model writes
        # a truncated version of an existing file.
        _backup_note = ""
        if p.exists() and p.is_file():
            try:
                _existing_size = p.stat().st_size
                _new_size = len(content.encode("utf-8"))
                # Threshold: new content is <25% of original AND original is >100 bytes
                # (skip tiny files where a small write is normal)
                if _existing_size > 100 and _new_size < _existing_size * 0.25:
                    import shutil
                    _backup_path = p.with_suffix(p.suffix + ".backup")
                    # Don't overwrite an existing backup — use numbered suffix
                    if _backup_path.exists():
                        _i = 2
                        while _backup_path.exists():
                            _backup_path = p.with_suffix(f"{p.suffix}.backup{_i}")
                            _i += 1
                    shutil.copy2(str(p), str(_backup_path))
                    _backup_note = (
                        f"\n⚠️  WARNING: New content ({_new_size} bytes) is much smaller "
                        f"than existing file ({_existing_size} bytes). "
                        f"Auto-backed up original to: {_backup_path.name}\n"
                        f"If this was unintentional, restore from the backup."
                    )
                    log.warning(
                        f"write_file size guard: {p.name} shrinking from "
                        f"{_existing_size} to {_new_size} bytes, backed up to {_backup_path.name}"
                    )
            except Exception as _be:
                log.error(f"write_file backup check failed: {_be}")

        p.write_text(content)
        _record_accessed_dir(p)
        try:
            _graph_add_node(
                "file",
                title=p.name,
                body=str(p),
                source_tool="write_file",
                metadata={"path": str(p), "size": len(content)},
                dedup_key=f"file::{p}",
            )
        except Exception:
            pass
        return f"Written {len(content)} bytes to {p}{_backup_note}"
    except Exception as e:
        return f"ERROR: {e}"


def _exec_edit_file(args: dict) -> str:
    raw_path = args.get("path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    replace_all = args.get("replace_all", False)
    if not raw_path or not old:
        return "ERROR: path and old_string are required."
    p = _resolve_path(raw_path)
    perm_error = _check_folder_permission(p)
    if perm_error:
        return perm_error
    path = str(p)
    try:
        p = Path(path)
        if not p.exists():
            return f"ERROR: File not found: {path}"
        text = p.read_text()
        count = text.count(old)
        if count == 0:
            return "ERROR: old_string not found in file."
        if count > 1 and not replace_all:
            return f"ERROR: old_string matches {count} times. Use replace_all=true or be more specific."
        if replace_all:
            text = text.replace(old, new)
        else:
            text = text.replace(old, new, 1)
        p.write_text(text)
        return f"Edit applied to {path} ({count} replacement{'s' if count > 1 else ''})"
    except Exception as e:
        return f"ERROR: {e}"


def _exec_bookmark_locator(args: dict) -> str:
    """Enumerate browser bookmark file paths that exist on this Mac.

    Returns a multi-line table of (browser, kind, path, size) for every
    bookmark file located under the canonical macOS layout. Designed to
    replace the model's tendency to glob the entire home directory looking
    for bookmark files.
    """
    home = Path.home()
    results: list[dict] = []

    # Chromium-family: <Application Support>/<Vendor>/(<UserData>)/<Profile>/Bookmarks
    # As of 2026, Chrome + Edge + Safari still dominate by market share;
    # Brave, Opera, Arc, Vivaldi are the next tier; Dia (Browser Company's
    # AI browser, Mac-only) and Comet (Perplexity) are the notable new AI
    # entries. DuckDuckGo Privacy Browser is webkit on macOS but also ships
    # Chromium builds on some platforms. Include everything we know a canonical
    # path for so the model never has to guess.
    chromium = [
        ("Chrome", home / "Library/Application Support/Google/Chrome"),
        ("Chrome (Beta)", home / "Library/Application Support/Google/Chrome Beta"),
        ("Chrome (Canary)", home / "Library/Application Support/Google/Chrome Canary"),
        ("Chrome (Dev)", home / "Library/Application Support/Google/Chrome Dev"),
        ("Chromium", home / "Library/Application Support/Chromium"),
        ("Edge", home / "Library/Application Support/Microsoft Edge"),
        ("Edge (Beta)", home / "Library/Application Support/Microsoft Edge Beta"),
        ("Edge (Dev)", home / "Library/Application Support/Microsoft Edge Dev"),
        ("Brave", home / "Library/Application Support/BraveSoftware/Brave-Browser"),
        ("Brave (Beta)", home / "Library/Application Support/BraveSoftware/Brave-Browser-Beta"),
        ("Brave (Nightly)", home / "Library/Application Support/BraveSoftware/Brave-Browser-Nightly"),
        ("Arc", home / "Library/Application Support/Arc/User Data"),
        ("Dia", home / "Library/Application Support/Dia/User Data"),
        ("Dia (alt)", home / "Library/Application Support/Dia"),
        ("Vivaldi", home / "Library/Application Support/Vivaldi"),
        ("Opera", home / "Library/Application Support/com.operasoftware.Opera"),
        ("Opera GX", home / "Library/Application Support/com.operasoftware.OperaGX"),
        ("Opera (Air)", home / "Library/Application Support/com.operasoftware.OperaAir"),
        ("Comet", home / "Library/Application Support/Comet"),
        ("Sidekick", home / "Library/Application Support/Sidekick"),
        ("Wavebox", home / "Library/Application Support/WaveboxApp"),
        ("Yandex", home / "Library/Application Support/Yandex/YandexBrowser"),
        ("Whale", home / "Library/Application Support/Naver/Whale"),
        ("Cromite", home / "Library/Application Support/Cromite"),
        ("Ungoogled Chromium", home / "Library/Application Support/Chromium/ungoogled"),
    ]
    for name, root in chromium:
        if not root.exists():
            continue
        # Each profile dir under the root has a "Bookmarks" JSON file.
        # Profiles are named "Default", "Profile 1", "Profile 2", etc.
        try:
            for prof in sorted(root.iterdir()):
                if not prof.is_dir():
                    continue
                bmk = prof / "Bookmarks"
                if bmk.exists():
                    results.append({
                        "browser": name,
                        "kind": "json",
                        "path": str(bmk),
                        "profile": prof.name,
                        "size": bmk.stat().st_size,
                    })
        except (PermissionError, FileNotFoundError):
            pass

    # Firefox-family: <Application Support>/<Vendor>/Profiles/*.default*/places.sqlite
    firefox = [
        ("Firefox", home / "Library/Application Support/Firefox/Profiles"),
        ("Firefox (Nightly)", home / "Library/Application Support/Firefox Nightly/Profiles"),
        ("Firefox (Dev)", home / "Library/Application Support/Firefox Developer Edition/Profiles"),
        ("Zen", home / "Library/Application Support/zen/Profiles"),
        ("LibreWolf", home / "Library/Application Support/LibreWolf/Profiles"),
        ("Waterfox", home / "Library/Application Support/Waterfox/Profiles"),
        ("Floorp", home / "Library/Application Support/Floorp/Profiles"),
        ("Pale Moon", home / "Library/Application Support/Pale Moon/Profiles"),
        ("SeaMonkey", home / "Library/Application Support/SeaMonkey/Profiles"),
        ("Tor Browser", home / "Library/Application Support/TorBrowser-Data/Browser"),
        ("Mullvad Browser", home / "Library/Application Support/MullvadBrowser/Profiles"),
    ]
    for name, root in firefox:
        if not root.exists():
            continue
        try:
            for prof in sorted(root.iterdir()):
                if not prof.is_dir():
                    continue
                places = prof / "places.sqlite"
                if places.exists():
                    results.append({
                        "browser": name,
                        "kind": "sqlite (places.sqlite)",
                        "path": str(places),
                        "profile": prof.name,
                        "size": places.stat().st_size,
                    })
                # Also surface JSON backups under bookmarkbackups/ so the
                # model can fall back to them if it can't open SQLite directly.
                bbk = prof / "bookmarkbackups"
                if bbk.exists():
                    try:
                        backups = sorted(bbk.glob("bookmarks-*.json*"))
                        if backups:
                            latest = backups[-1]
                            results.append({
                                "browser": name,
                                "kind": "json backup (latest)",
                                "path": str(latest),
                                "profile": prof.name,
                                "size": latest.stat().st_size,
                            })
                    except Exception:
                        pass
        except (PermissionError, FileNotFoundError):
            pass

    # Safari: legacy + sandboxed Container locations.
    safari_paths = [
        ("Safari (legacy)", home / "Library/Safari/Bookmarks.plist"),
        ("Safari (sandboxed)",
         home / "Library/Containers/com.apple.Safari/Data/Library/Safari/Bookmarks.plist"),
        ("Safari (group container)",
         home / "Library/Group Containers/group.com.apple.Safari/Library/Safari/Bookmarks.plist"),
    ]
    for name, p in safari_paths:
        if p.exists():
            results.append({
                "browser": name,
                "kind": "plist",
                "path": str(p),
                "profile": "Default",
                "size": p.stat().st_size,
            })

    # Orion (Kagi, Webkit-based) — stores bookmarks as `favourites.plist` under
    # Defaults/bk_<N>/ per-profile. Uses NSDate objects so `plutil -convert json`
    # fails; needs plistlib with custom walker (handled in
    # extract_browser_bookmarks.py).
    orion_root = home / "Library/Application Support/Orion"
    if orion_root.exists():
        try:
            for profile_dir in sorted(orion_root.glob("**/bk_*"), key=lambda p: str(p)):
                fav = profile_dir / "favourites.plist"
                if fav.exists():
                    results.append({
                        "browser": "Orion",
                        "kind": "plist (favourites.plist)",
                        "path": str(fav),
                        "profile": profile_dir.name,
                        "size": fav.stat().st_size,
                    })
        except (PermissionError, FileNotFoundError):
            pass

    # DuckDuckGo Privacy Browser (Mac-native, Webkit-based).
    ddg_paths = [
        home / "Library/Containers/com.duckduckgo.macos.browser/Data/Library/Application Support/DuckDuckGo/Bookmarks.db",
        home / "Library/Application Support/DuckDuckGo/Bookmarks.db",
    ]
    for p in ddg_paths:
        if p.exists():
            results.append({
                "browser": "DuckDuckGo",
                "kind": "sqlite (Bookmarks.db)",
                "path": str(p),
                "profile": "Default",
                "size": p.stat().st_size,
            })

    if not results:
        return ("No browser bookmark files found in the canonical macOS "
                "locations under ~/Library. Check that the browsers are "
                "installed and have been launched at least once.")

    # Compact, model-friendly output.
    lines = [f"Found {len(results)} bookmark file(s):", ""]
    for r in results:
        size_kb = r["size"] / 1024
        lines.append(
            f"- [{r['browser']}] profile={r['profile']!r} kind={r['kind']} "
            f"size={size_kb:.1f}KB"
        )
        lines.append(f"  path: {r['path']}")
    lines.append("")
    lines.append(
        "Notes:\n"
        "  • Chromium JSON (Chrome/Edge/Brave/Arc/Dia/Vivaldi/Opera/Comet/"
        "Sidekick/Wavebox/Yandex/Whale/Cromite): parse with json.load — keys: "
        "roots → bookmark_bar / other / synced, each holding a 'children' tree.\n"
        "  • places.sqlite (Firefox/Zen/LibreWolf/Waterfox/Floorp/PaleMoon/"
        "SeaMonkey/Tor/Mullvad): open with sqlite3; bookmarks in moz_bookmarks "
        "joined to moz_places on fk=id for URLs. Copy file first (WAL lock).\n"
        "  • Safari Bookmarks.plist: binary plist behind macOS Full Disk Access. "
        "Without FDA you get `Operation not permitted` — either ask the user "
        "to grant FDA in System Settings → Privacy & Security → Full Disk "
        "Access, OR to export via File → Export Bookmarks… instead.\n"
        "  • Orion favourites.plist: custom flat-dict plist; contains NSDate "
        "objects so plutil-to-json fails — use plistlib + walk id/parentId tree.\n"
        "  • DuckDuckGo Bookmarks.db: sqlite; `bookmarks_items` table.\n"
        "  • The extract_browser_bookmarks.py script in bulk-organizer/scripts "
        "handles all of the above and emits Netscape HTML ready for "
        "parse_bookmarks.py. Run it with no args for auto-discover mode."
    )
    return "\n".join(lines)


def _exec_bookmark_extract_all(args: dict) -> str:
    """End-to-end: locate every browser bookmark file, copy + convert in one call.

    Eliminates the model's bash-typo loop and SQLite-WAL-lock pain by doing
    the locate + copy + convert pipeline server-side. Returns a summary the
    model can then feed to parse_bookmarks.py / the organize skill.
    """
    import shutil, subprocess, time as _time
    out_dir = args.get("out_dir") or f"/tmp/clyde-bookmarks-{int(_time.time())}"
    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    originals = out / "originals"
    extracted = out / "extracted_html"
    originals.mkdir(exist_ok=True)
    extracted.mkdir(exist_ok=True)

    # Step 1 — locate. Reuses the bookmark_locator implementation.
    locator_text = _exec_bookmark_locator({})
    if locator_text.startswith("No browser bookmark files found"):
        return locator_text

    # Step 2 — pull each path out of the locator output and act on it.
    home = Path.home()
    extractor = (Path(_CLYDE_HOME) / "agent" / "skills"
                 / "bulk-organizer" / "scripts" / "extract_browser_bookmarks.py")
    if not extractor.exists():
        return f"ERROR: extractor missing at {extractor}"

    summary: list[dict] = []
    paths = []
    for line in locator_text.splitlines():
        line = line.strip()
        if line.startswith("path: "):
            paths.append(Path(line[6:].strip()))

    for src in paths:
        entry = {"src": str(src), "ok": False, "skipped": "",
                 "extracted": "", "size": 0}
        try:
            # Copy to originals/ first so we never touch the live browser file.
            dst = originals / f"{src.parent.name.replace(' ', '_')}__{src.name}"
            try:
                shutil.copy2(src, dst)
            except PermissionError as pe:
                # Safari + macOS Full Disk Access — explicit error so model
                # doesn't loop trying cp variations.
                if "Safari" in str(src) or "Bookmarks.plist" in src.name:
                    entry["skipped"] = (
                        "Safari requires Full Disk Access. Either grant FDA in "
                        "System Settings → Privacy & Security → Full Disk Access "
                        "(add the host process: Terminal / Xcode / ClydeEngine), "
                        "OR ask the user to export Safari bookmarks via "
                        "File → Export Bookmarks…"
                    )
                else:
                    entry["skipped"] = f"PermissionError: {pe}"
                summary.append(entry)
                continue
            # Step 3 — convert each copy to Netscape HTML. Use the same
            # python the agent runs under (sys.executable) so optional deps
            # like `lz4` are present (system /usr/bin/python3 isn't a venv
            # and won't have lz4 even after `pip install lz4`).
            r = subprocess.run(
                [sys.executable, str(extractor), str(dst), str(extracted)],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                entry["ok"] = True
                # The extractor prints "<count> bookmarks → <out_path>".
                last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
                entry["extracted"] = last
                entry["size"] = dst.stat().st_size
            else:
                entry["skipped"] = (r.stderr.strip().splitlines() or ["(no stderr)"])[-1]
        except Exception as e:
            entry["skipped"] = f"{type(e).__name__}: {e}"
        summary.append(entry)

    n_ok = sum(1 for s in summary if s["ok"])
    n_skip = sum(1 for s in summary if not s["ok"])
    lines = [
        f"Working directory: {out}",
        f"  originals/    — copies of each browser's native bookmark file",
        f"  extracted_html/ — Netscape HTML, ready for parse_bookmarks.py",
        "",
        f"{n_ok} extracted, {n_skip} skipped:",
    ]
    for s in summary:
        if s["ok"]:
            lines.append(f"  OK   {s['extracted']}")
        else:
            lines.append(f"  SKIP {s['src']}: {s['skipped']}")

    if n_ok > 0:
        # List the .html files explicitly so the model can pass them to
        # parse_bookmarks.py without further bash discovery.
        html_files = sorted(extracted.glob("*.html"))
        lines.append("")
        lines.append("HTML files ready (full paths — pass these to parse_bookmarks.py):")
        for h in html_files:
            lines.append(f"  {h}")

    return "\n".join(lines)


def _find_active_request_node() -> str | None:
    """Most recent active request node in the current conversation."""
    conv_id = _current_conversation_id
    if not conv_id or conv_id not in _graph_store:
        return None
    store = _graph_store[conv_id]
    for n in reversed(store.get("nodes", [])):
        meta = n.get("metadata", {})
        if meta.get("node_role") == "request" and meta.get("status") == "active":
            return n["id"]
    return None


def _exec_graph_read(args: dict) -> str:
    """Read current asset graph state as structured text.

    Returns request / phase / artifact / source / fact nodes in a form
    the model can parse after compaction or before verification.
    """
    conv_id = _current_conversation_id
    if not conv_id or conv_id not in _graph_store:
        return "GRAPH_EMPTY: No graph state exists for this conversation."
    store = _graph_store[conv_id]
    nodes = store.get("nodes", [])
    if not nodes:
        return "GRAPH_EMPTY: Graph exists but is empty."
    filter_role = args.get("filter")
    request_id = args.get("request_id")

    parts: list[str] = []

    def _emit_role(role: str, label: str) -> None:
        matching = [n for n in nodes
                    if n.get("metadata", {}).get("node_role") == role]
        if not matching:
            return
        parts.append(f"\n# {label}")
        for n in sorted(matching, key=lambda x: x.get("created_at", 0)):
            meta = n.get("metadata", {})
            head = f"[{n['id']}] {n['title']}"
            if role == "phase":
                status = meta.get("status", "pending")
                prog = ""
                tot = meta.get("progress_total", "0")
                cur = meta.get("progress_current", "0")
                if tot and tot != "0":
                    prog = f" ({cur}/{tot})"
                head += f" [{status}]{prog}"
                parts.append(head)
                if status == "active":
                    if meta.get("state_dir"):
                        parts.append(f"  state_dir: {meta['state_dir']}")
                    if meta.get("next_batch_start"):
                        parts.append(f"  RESUME next_batch_start={meta['next_batch_start']}")
            elif role == "request":
                parts.append(head)
                parts.append(f"  status: {meta.get('status', 'unknown')}")
                parts.append(f"  skill: {meta.get('skill', 'general')}")
                if meta.get("source_file"):
                    parts.append(f"  source: {meta['source_file']}")
                reqs_raw = meta.get("requirements", "{}")
                try:
                    reqs = json.loads(reqs_raw)
                    for k, v in reqs.items():
                        parts.append(f"  req.{k}: {v}")
                except Exception:
                    if reqs_raw and reqs_raw != "{}":
                        parts.append(f"  requirements: {reqs_raw}")
            elif role == "artifact":
                art_type = meta.get("artifact_type", "")
                line = head
                if art_type:
                    line += f" (type={art_type})"
                if meta.get("satisfies"):
                    line += f" → satisfies: {meta['satisfies']}"
                parts.append(line)
            elif role == "source":
                parts.append(head)
                if meta.get("source_type"):
                    parts.append(f"  type: {meta['source_type']}")
            elif role == "fact":
                line = head
                conf = meta.get("confidence", "")
                cat = meta.get("category", "")
                if conf or cat:
                    line += f" [{cat or '?'}/{conf or '?'}]"
                if meta.get("source"):
                    line += f" ← {meta['source'][:80]}"
                parts.append(line)

    if filter_role:
        _emit_role(filter_role, filter_role.upper())
    else:
        _emit_role("request", "REQUESTS (original requirements)")
        _emit_role("phase", "PHASES (progress)")
        _emit_role("artifact", "ARTIFACTS (outputs)")
        _emit_role("source", "SOURCES (consulted)")
        _emit_role("fact", "FACTS (recorded)")

    if request_id:
        # Filter output further — keep only nodes whose lines reference request_id
        parts = [p for p in parts if request_id in p or p.startswith("#")]

    return "\n".join(parts).strip() or "GRAPH_EMPTY: No matching nodes."


def _exec_graph_write(args: dict) -> str:
    """Create/update request, phase, artifact, source nodes."""
    action = args.get("action", "")
    if not action:
        return "ERROR: `action` required (create_request | create_phase | update_phase | create_artifact | create_source | link_fact_to_source | verify)."

    if action == "create_request":
        title = args.get("title", "").strip()
        if not title:
            return "ERROR: create_request needs `title`."
        requirements = args.get("requirements", {})
        reqs_str = json.dumps(requirements) if isinstance(requirements, dict) else str(requirements)
        node_id = _graph_add_node(
            "task",
            title=title[:200],
            body=args.get("body", "")[:2000],
            source_tool="graph_write",
            metadata={
                "node_role": "request",
                "requirements": reqs_str,
                "skill": args.get("skill", "general"),
                "source_file": args.get("source_file", ""),
                "status": "active",
            },
            dedup_key=f"request::{title[:120]}",
        )
        return f"OK: request node created (id={node_id})"

    if action == "create_phase":
        title = args.get("title", "").strip()
        request_id = args.get("request_id", "")
        phase_name = args.get("phase_name", "").strip()
        if not title or not phase_name:
            return "ERROR: create_phase needs `title` and `phase_name`."
        node_id = _graph_add_node(
            "section",
            title=title[:200],
            body=args.get("body", "")[:1000],
            source_tool="graph_write",
            metadata={
                "node_role": "phase",
                "phase_name": phase_name,
                "status": args.get("status", "pending"),
                "progress_current": str(args.get("progress_current", "0")),
                "progress_total": str(args.get("progress_total", args.get("total", "0"))),
                "next_batch_start": str(args.get("next_batch_start", "0")),
                "state_dir": args.get("state_dir", ""),
                "batch_size": str(args.get("batch_size", "30")),
            },
            dedup_key=f"phase::{request_id}::{phase_name}",
        )
        if node_id and request_id:
            _graph_add_edge(request_id, node_id, "has_phase", note=phase_name)
        if node_id and args.get("depends_on"):
            _graph_add_edge(node_id, args["depends_on"], "depends_on")
        return f"OK: phase node created (id={node_id})"

    if action == "update_phase":
        node_id = args.get("node_id", "")
        if not node_id:
            return "ERROR: update_phase needs `node_id`."
        meta_updates: dict = {}
        for key in ("status", "progress_current", "progress_total",
                    "next_batch_start", "state_dir", "batch_size"):
            if key in args:
                meta_updates[key] = str(args[key])
        ok = _graph_update_node(
            node_id,
            body=args.get("body"),
            metadata_updates=meta_updates or None,
        )
        return "OK: phase updated" if ok else f"ERROR: node {node_id} not found"

    if action == "create_artifact":
        title = args.get("title", "").strip()
        if not title:
            return "ERROR: create_artifact needs `title`."
        phase_id = args.get("phase_id", "")
        request_id = args.get("request_id", "")
        satisfies = args.get("satisfies", "")
        artifact_type = args.get("artifact_type", "")
        node_id = _graph_add_node(
            "fact",
            title=title[:200],
            body=args.get("body", "")[:2000],
            source_tool="graph_write",
            metadata={
                "node_role": "artifact",
                "artifact_type": artifact_type,
                "count": str(args.get("count", "")),
                "satisfies": satisfies,
            },
            dedup_key=f"artifact::{phase_id}::{artifact_type}::{title[:80]}",
        )
        if node_id and phase_id:
            _graph_add_edge(phase_id, node_id, "produces", note=artifact_type)
        if node_id and request_id and satisfies:
            _graph_add_edge(node_id, request_id, "satisfies", note=satisfies)
        return f"OK: artifact created (id={node_id})"

    if action == "create_source":
        url_or_path = args.get("url") or args.get("path") or args.get("title") or ""
        if not url_or_path:
            return "ERROR: create_source needs `url` or `path`."
        node_id = _graph_add_node(
            "url" if url_or_path.startswith("http") else "file",
            title=(args.get("title") or url_or_path)[:200],
            body=url_or_path,
            source_tool="graph_write",
            metadata={
                "node_role": "source",
                "source_type": args.get("source_type", "manual"),
            },
            dedup_key=f"source::{url_or_path}",
        )
        return f"OK: source registered (id={node_id})"

    if action == "link_fact_to_source":
        fact_id = args.get("fact_id", "")
        source_id = args.get("source_id", "")
        if not fact_id or not source_id:
            return "ERROR: link_fact_to_source needs `fact_id` and `source_id`."
        _graph_add_edge(fact_id, source_id, "sourced_from")
        return f"OK: edge added ({fact_id} → {source_id})"

    if action == "verify":
        conv_id = _current_conversation_id
        if not conv_id or conv_id not in _graph_store:
            return "ERROR: no graph state"
        store = _graph_store[conv_id]
        request_id = args.get("request_id") or _find_active_request_node()
        if not request_id:
            return "ERROR: no request node found"
        req_node = next((n for n in store.get("nodes", []) if n["id"] == request_id), None)
        if not req_node:
            return f"ERROR: request node {request_id} not found"
        try:
            reqs = json.loads(req_node["metadata"].get("requirements", "{}"))
        except Exception:
            return "ERROR: could not parse requirements from request node"
        satisfied: set = set()
        for e in store.get("edges", []):
            if e.get("kind") == "satisfies" and e.get("to") == request_id:
                for n in store.get("nodes", []):
                    if n["id"] == e.get("from"):
                        sat = n.get("metadata", {}).get("satisfies")
                        if sat:
                            satisfied.add(sat)
        parts = [f"VERIFY: {req_node['title']}"]
        all_pass = True
        for key, value in reqs.items():
            if key in satisfied:
                parts.append(f"  PASS: {key} = {value}")
            else:
                parts.append(f"  FAIL: {key} = {value} (no satisfying artifact)")
                all_pass = False
        parts.append(f"RESULT: {'ALL_PASS' if all_pass else 'INCOMPLETE'}")
        return "\n".join(parts)

    return f"ERROR: unknown action '{action}'"


def _exec_parse_bookmarks(args: dict) -> str:
    """Run the bulk-organizer parser on a Netscape HTML file — NO bash.

    The model keeps shelling out `python3 parse_bookmarks.py ...` which hits
    a fresh python environment every call (PYTHONHOME / PATH surprises,
    wrong interpreter picking up, and so on). This tool runs the same
    script in-process with the agent's `sys.executable` so the env is
    identical, idempotent, and invisible to the user.
    """
    raw_path = args.get("html_path") or args.get("path") or ""
    raw_state = args.get("state_dir") or ""
    if not raw_path:
        return (
            "ERROR: parse_bookmarks needs `html_path`. Pass the Netscape "
            "HTML file path (absolute or ~-prefixed)."
        )
    import subprocess

    html_path = _resolve_path(raw_path)
    if not html_path.exists():
        return f"ERROR: File not found: {html_path}"
    state_dir = _resolve_path(raw_state) if raw_state else html_path.parent
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return f"ERROR: Can't create state_dir {state_dir}: {e}"

    script = (Path(_CLYDE_HOME) / "agent" / "skills"
              / "bulk-organizer" / "scripts" / "parse_bookmarks.py")
    if not script.exists():
        return f"ERROR: parse_bookmarks.py missing at {script}"

    # Strip inherited PYTHONHOME/PYTHONPATH so the subprocess uses the
    # same interpreter's libs (not the agent bundle's). This is the
    # whitespace-quoted-path problem the model kept hallucinating about.
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    try:
        r = subprocess.run(
            [sys.executable, str(script), str(html_path), str(state_dir)],
            capture_output=True, text=True, timeout=120, env=env,
        )
    except subprocess.TimeoutExpired:
        return "ERROR: parse_bookmarks timed out after 120s"
    except Exception as e:
        return f"ERROR: parse_bookmarks failed: {e}"

    if r.returncode != 0:
        tail = (r.stderr.strip() or r.stdout.strip() or "(no output)")[-1000:]
        return (
            f"ERROR: parse_bookmarks exited {r.returncode}\n"
            f"--- stderr/stdout tail ---\n{tail}"
        )
    out = (r.stdout or "").strip()
    # The script prints "OK: Parsed N bookmarks (<type>)\nState dir: ...".
    # Surface that verbatim + remind the model what's next.
    return (
        f"{out}\n\n"
        f"State dir: {state_dir}\n"
        f"Next: call `organize_progress` with state_dir to confirm ingest, "
        f"then `organize_batch_read` / `organize_batch_write` to classify."
    )


def _exec_glob_search(args: dict) -> str:
    pattern = args.get("pattern", "")
    raw_base = args.get("path", "~")
    base = str(_resolve_path(raw_base))
    if not pattern:
        return "ERROR: No pattern provided."

    # Hard redirect for bookmark-related patterns. Runs BEFORE the
    # permission check — without it the model gets blocked at the perm
    # gate (because $HOME isn't pre-granted) and never sees the hint to
    # use bookmark_locator. `bookmark_locator` returns every browser's
    # canonical path in one call, no globbing.
    # Browser-bookmark file globs only — NOT user-specified .html test
    # fixtures (those should fall through to a normal glob_search). The
    # narrow signals here all reference actual browser storage formats.
    _bm_signals = ("Bookmarks.plist", "places.sqlite", "favourites.plist")
    _bm_loose = "bookmark" in pattern.lower()
    _is_user_html = pattern.lower().endswith(".html") or "/test_bookmarks/" in pattern
    if (any(s in pattern for s in _bm_signals)
            or (_bm_loose and not _is_user_html and not raw_base.endswith(".html"))):
        # Auto-execute bookmark_extract_all instead of looping the model on
        # REDIRECT messages. Returns the extract result inline so the model
        # can move on to ingesting + organizing without another round trip.
        try:
            extract_out = _exec_bookmark_extract_all({})
        except Exception as e:
            extract_out = f"(bookmark_extract_all failed: {e})"
        return (
            "AUTO-EXECUTED `bookmark_extract_all` for you (your `glob_search "
            f"\"{pattern}\"` was redirected to the proper tool):\n\n"
            f"{extract_out}\n\n"
            "Next: call `parse_bookmarks.py` via bash on each HTML file above "
            "to ingest into the organize state, then run the bulk-organizer "
            "skill to classify."
        )

    perm_error = _check_folder_permission(Path(base))
    if perm_error:
        return perm_error

    # Shell out to `find` with -prune so we can bound wall-clock time
    # hard (subprocess.run timeout) AND avoid descending into the usual
    # noise dirs. Python's globmod.iglob blocks inside C-level os.walk
    # on huge trees (e.g. ~/Library) so a Python-level deadline check
    # never gets a chance to run.
    _MAX_MATCHES = 200
    _NOISE = [
        "Library", ".Trash", ".cache", ".npm", ".gradle", ".m2",
        "node_modules", ".git", ".venv", "venv", ".pyenv",
        "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "DerivedData", "build", "dist", "target", ".cargo",
        ".rustup", ".rbenv", ".nvm", ".oh-my-zsh", "Caches", "Logs",
    ]
    # Translate glob to a find-friendly -path expression. `**` in the
    # pattern means "zero or more directories"; find handles this via
    # `-path '*/<pat>/*'`. For simple patterns without `**`, fall back
    # to -name on the tail component.
    if "**" in pattern:
        # Substitute `**/foo*/**` → `*/foo*/*`; `**/*.py` → `*/*.py`.
        find_pat = pattern.replace("**/", "*/").replace("/**", "/*")
        pred = ["-path", os.path.join(base, find_pat)]
    elif "/" in pattern:
        pred = ["-path", os.path.join(base, pattern)]
    else:
        pred = ["-name", pattern]

    # Unconditionally prune noise dirs. To search *inside* one of them
    # (e.g. ~/Library/Application Support/zen), pass the noise dir as
    # `path=...` — pruning skips descendants, not the root base.
    prune_expr: list[str] = []
    for name in _NOISE:
        if prune_expr:
            prune_expr.append("-o")
        prune_expr += ["-name", name]
    cmd = [
        "find", base,
        "(", *prune_expr, ")", "-prune",
        "-o", *pred, "-print",
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:
        return f"ERROR: find timed out after 8s for '{pattern}' in {base}"
    except Exception as e:
        return f"ERROR: {e}"

    out = proc.stdout.strip()
    if not out:
        return f"No files matching '{pattern}' in {base}"
    lines = sorted(out.splitlines())[:_MAX_MATCHES]
    result = "\n".join(lines)
    total = len(out.splitlines())
    if total > _MAX_MATCHES:
        result += f"\n\n[{total} total matches, showing first {_MAX_MATCHES}]"
    return result


def _exec_grep_search(args: dict) -> str:
    pattern = args.get("pattern", "")
    raw_path = args.get("path", ".")
    path = str(_resolve_path(raw_path))
    perm_error = _check_folder_permission(Path(path))
    if perm_error:
        return perm_error
    if not pattern:
        return "ERROR: No pattern provided."
    try:
        cmd = ["grep", "-rn", "--include=*.py", "--include=*.md", "--include=*.txt",
               "--include=*.yaml", "--include=*.yml", "--include=*.json",
               "--include=*.sh", "--include=*.rs", "--include=*.toml",
               "-E", pattern, path]
        if args.get("-i", False):
            cmd.insert(2, "-i")
        context = args.get("context", args.get("-C", 0))
        if context:
            cmd.insert(2, f"-C{context}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        output = result.stdout.strip()
        if not output:
            return f"No matches for '{pattern}' in {path}"
        lines = output.splitlines()
        limit = args.get("head_limit", 50)
        if len(lines) > limit:
            lines = lines[:limit]
            lines.append(f"\n[{len(lines)} of many matches shown]")
        return "\n".join(lines)
    except subprocess.TimeoutExpired:
        return "ERROR: Search timed out."
    except Exception as e:
        return f"ERROR: {e}"


# ─── Memory Tool Implementations (delegate to memory module) ───

def _exec_memory_read(args: dict) -> str:
    from memory import read_topic
    return read_topic(args.get("filename", ""))

def _exec_memory_write(args: dict) -> str:
    from memory import write_memory
    required = ["filename", "name", "description", "type", "content"]
    missing = [k for k in required if not args.get(k)]
    if missing:
        return f"ERROR: Missing required fields: {missing}"
    return write_memory(args["filename"], args["name"], args["description"], args["type"], args["content"])

def _exec_memory_update(args: dict) -> str:
    from memory import update_memory
    if not args.get("filename") or not args.get("content"):
        return "ERROR: filename and content required."
    return update_memory(args["filename"], args["content"])

def _exec_memory_delete(args: dict) -> str:
    from memory import delete_memory
    if not args.get("filename"):
        return "ERROR: No filename provided."
    return delete_memory(args["filename"])

def _exec_memory_search(args: dict) -> str:
    from memory import search_memory
    return search_memory(args.get("query", ""))

def _exec_memory_list(args: dict) -> str:
    from memory import list_memories
    return list_memories()

def _exec_transcript_search(args: dict) -> str:
    from memory import search_transcripts
    return search_transcripts(args.get("query", ""))


# ─── Web Tool Implementations ───

# ─── Document Creation Tool Implementations (lazy-load file_sidecar) ───

def _exec_create_document(args: dict) -> str:
    from file_sidecar import create_docx
    return create_docx(args)

def _exec_create_spreadsheet(args: dict) -> str:
    from file_sidecar import create_xlsx
    return create_xlsx(args)

def _exec_create_presentation(args: dict) -> str:
    from file_sidecar import create_pptx
    return create_pptx(args)

def _exec_create_pdf(args: dict) -> str:
    from file_sidecar import create_pdf
    return create_pdf(args)


def _exec_ask_user(args: dict) -> str:
    """
    Placeholder executor for ask_user tool.
    The actual execution is intercepted by ConversationRuntime.run_turn()
    which emits a question event and blocks until the user answers.
    This function should never be called directly.
    """
    return "ERROR: ask_user must be intercepted by the conversation runtime."


def _exec_switch_skill(args: dict) -> str:
    """Phase B: model-driven skill switch.

    The streaming model can self-correct mid-conversation by calling
    ``switch_skill(skill_name=..., reason=...)`` when it realizes the
    currently-active skill doesn't fit the user's request. The next
    LLM iteration sees the new skill's tool surface and system prompt
    fragment because conversation.py re-resolves both via
    ``get_active_skill`` per iteration.
    """
    skill_name = (args.get("skill_name") or "").strip()
    reason = (args.get("reason") or "").strip()
    conv_id = _current_conversation_id
    if not conv_id:
        return "ERROR: switch_skill called outside an active conversation."
    if not skill_name:
        return "ERROR: skill_name is required."
    import skills as skills_mod
    valid = {s.name for s in skills_mod.all_skills()}
    if skill_name not in valid:
        return f"ERROR: unknown skill '{skill_name}'. Valid: {sorted(valid)}"
    skills_mod.set_active_skill(conv_id, skill_name, user_initiated=False)
    log.info(
        "[switch_skill] conv=%s → %s (reason=%r)",
        conv_id[:8] if conv_id else "?", skill_name, reason,
    )
    return (
        f"Switched active skill to '{skill_name}'. "
        f"The next iteration will use that skill's tools and instructions."
    )


# ─── Task Plan / Task Update / Record Fact Implementations ───

# In-memory plan store (per-process, keyed by plan_id)
_active_plan: dict | None = None

def _exec_task_plan(args: dict) -> str:
    """Create a multi-step execution plan. Returns plan_id and step list."""
    global _active_plan
    steps = args.get("steps", [])
    if not steps:
        return "ERROR: Must provide at least one step in 'steps' array."
    output_file = args.get("output_file", "")
    plan_id = f"plan_{int(time.time())}"
    plan_steps = []
    for i, step_text in enumerate(steps):
        plan_steps.append({
            "step_id": f"step_{i+1}",
            "description": step_text,
            "status": "pending",
            "iterations_used": 0,
        })
    _active_plan = {
        "plan_id": plan_id,
        "goal": args.get("goal", ""),
        "output_file": output_file,
        "steps": plan_steps,
        "current_step": 0,
    }
    step_list = "\n".join(
        f"  {s['step_id']}: {s['description']} [{s['status']}]"
        for s in plan_steps
    )
    return (
        f"Plan created: {plan_id}\n"
        f"Goal: {_active_plan['goal']}\n"
        f"Steps:\n{step_list}\n\n"
        f"Now call ask_user to get user approval, then start step_1."
    )

def _exec_task_update(args: dict) -> str:
    """Update a plan step's status (in_progress, done, failed)."""
    global _active_plan
    if _active_plan is None:
        return "ERROR: No active plan. Call task_plan first."
    step_id = args.get("step_id", "")
    status = args.get("status", "")
    if status not in ("in_progress", "done", "failed"):
        return f"ERROR: Invalid status '{status}'. Must be: in_progress, done, failed"
    # Find the step
    found = None
    for s in _active_plan["steps"]:
        if s["step_id"] == step_id:
            found = s
            break
    if not found:
        valid = [s["step_id"] for s in _active_plan["steps"]]
        return f"ERROR: Unknown step_id '{step_id}'. Valid IDs: {valid}"
    old_status = found["status"]
    found["status"] = status
    if status == "in_progress":
        found["iterations_used"] += 1
    # Build progress summary
    total = len(_active_plan["steps"])
    done = sum(1 for s in _active_plan["steps"] if s["status"] == "done")
    failed = sum(1 for s in _active_plan["steps"] if s["status"] == "failed")
    pending = total - done - failed - (1 if status == "in_progress" else 0)
    # Find next pending step
    next_step = None
    if status == "done":
        for s in _active_plan["steps"]:
            if s["status"] == "pending":
                next_step = s
                break
    progress = f"Progress: {done}/{total} done"
    if failed:
        progress += f", {failed} failed"
    if pending:
        progress += f", {pending} pending"
    result = f"Step {step_id}: {old_status} → {status}\n{progress}"
    if next_step and status == "done":
        result += f"\n\nNext step: {next_step['step_id']} — {next_step['description']}"
        result += f"\nCall task_update(step_id=\"{next_step['step_id']}\", status=\"in_progress\") to begin."
    elif status == "done" and not next_step:
        result += "\n\n✅ ALL STEPS COMPLETE. Plan finished!"
        if _active_plan.get("output_file"):
            result += f"\nOutput file: {_active_plan['output_file']}"
    return result

# ─── Asset Graph Store (per-conversation) ───
# Populated by record_fact / web_search / web_fetch / read_file /
# write_file. Consumed by the /v1/graph/{conversation_id} endpoint in
# agent.py and rendered by Clyde's AssetGraphView. Node types
# expected by the client: "file", "url", "query", "fact".
#
# Persistence: graphs are saved to ~/.clyde/graph/{conv_id}.json
# so they survive agent restarts. Clyde also persists its own copy and
# will re-seed the agent via PUT /v1/graph/{conv_id} if it detects a
# stale (empty) response.

import json as _json
import pathlib as _pathlib

_GRAPH_DIR = _pathlib.Path.home() / ".clyde" / "graph"
_GRAPH_DIR.mkdir(parents=True, exist_ok=True)

_graph_store: dict[str, dict] = {}
_current_conversation_id: str | None = None


def _graph_disk_path(conv_id: str) -> _pathlib.Path:
    return _GRAPH_DIR / f"{conv_id}.json"


def _graph_save_to_disk(conv_id: str) -> None:
    """Persist the graph for conv_id to disk (atomic write)."""
    store = _graph_store.get(conv_id)
    if not store or not store["nodes"]:
        return
    path = _graph_disk_path(conv_id)
    serializable = {
        "nodes": list(store["nodes"]),
        "edges": list(store["edges"]),
        "version": store["version"],
    }
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(_json.dumps(serializable), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        log.warning(f"[GRAPH] disk save failed for {conv_id[:8]}: {e}")


def _graph_load_from_disk(conv_id: str) -> dict | None:
    """Load a previously-persisted graph, or None."""
    path = _graph_disk_path(conv_id)
    if not path.exists():
        return None
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        # Rebuild the dedup key index
        node_keys = {}
        for n in data.get("nodes", []):
            key = f"{n['type']}::{n['title']}"
            node_keys[key] = n["id"]
        return {
            "nodes": data.get("nodes", []),
            "edges": data.get("edges", []),
            "version": data.get("version", 0),
            "_node_keys": node_keys,
        }
    except Exception as e:
        log.warning(f"[GRAPH] disk load failed for {conv_id[:8]}: {e}")
        return None


def set_current_conversation_id(conv_id: str | None):
    """Called by agent.py get_runtime() so tool executors can attribute
    graph nodes to the correct conversation."""
    global _current_conversation_id
    _current_conversation_id = conv_id
    log.info(f"[GRAPH] set_current_conversation_id → {conv_id[:8] if conv_id else 'None'}...")
    if conv_id and conv_id not in _graph_store:
        # Try loading from disk first (survives agent restart)
        loaded = _graph_load_from_disk(conv_id)
        if loaded:
            _graph_store[conv_id] = loaded
            log.info(f"[GRAPH] Restored {len(loaded['nodes'])} nodes from disk for {conv_id[:8]}")
        else:
            _graph_store[conv_id] = {
                "nodes": [], "edges": [], "version": 0, "_node_keys": {},
            }


def get_graph_data(conv_id: str) -> dict:
    """JSON-serializable snapshot matching Clyde's GraphData shape."""
    store = _graph_store.get(conv_id)
    if not store:
        # Try loading from disk (e.g. agent just restarted)
        loaded = _graph_load_from_disk(conv_id)
        if loaded:
            _graph_store[conv_id] = loaded
            store = loaded
            log.info(f"[GRAPH] Late-loaded {len(loaded['nodes'])} nodes from disk for {conv_id[:8]}")
        else:
            log.info(f"[GRAPH] get_graph_data({conv_id[:8]}...) → no store found. Known keys: {[k[:8] for k in _graph_store.keys()]}")
            return {"nodes": [], "edges": [], "version": 0}
    log.info(f"[GRAPH] get_graph_data({conv_id[:8]}...) → {len(store['nodes'])} nodes, {len(store['edges'])} edges")
    return {
        "nodes": list(store["nodes"]),
        "edges": list(store["edges"]),
        "version": store["version"],
    }


def put_graph_data(conv_id: str, data: dict) -> dict:
    """Accept a full graph push from Clyde (re-seeding after agent restart).
    Only accepts if the agent's current store for this conversation is empty
    or has fewer nodes — Clyde is the source of truth for persistence."""
    incoming_nodes = data.get("nodes", [])
    if not incoming_nodes:
        return {"ok": True, "action": "ignored", "reason": "empty payload"}

    store = _graph_store.get(conv_id)
    if store and len(store["nodes"]) >= len(incoming_nodes):
        return {"ok": True, "action": "ignored", "reason": "agent already has equal or more nodes"}

    # Rebuild dedup keys from incoming nodes
    node_keys = {}
    for n in incoming_nodes:
        key = f"{n.get('type', '')}::{n.get('title', '')}"
        node_keys[key] = n["id"]

    _graph_store[conv_id] = {
        "nodes": incoming_nodes,
        "edges": data.get("edges", []),
        "version": data.get("version", 0),
        "_node_keys": node_keys,
    }
    _graph_save_to_disk(conv_id)
    log.info(f"[GRAPH] Re-seeded {len(incoming_nodes)} nodes for {conv_id[:8]} from Clyde")
    return {"ok": True, "action": "seeded", "nodes": len(incoming_nodes)}


def clear_graph(conv_id: str):
    if conv_id in _graph_store:
        _graph_store[conv_id] = {
            "nodes": [], "edges": [], "version": 0, "_node_keys": {},
        }


def _graph_add_node(
    node_type: str,
    title: str,
    body: str = "",
    source_tool: str = "",
    metadata: dict | None = None,
    dedup_key: str | None = None,
) -> str | None:
    conv_id = _current_conversation_id
    if not conv_id:
        log.warning(f"[GRAPH] _graph_add_node({node_type}, {title[:40]}) skipped — no conversation ID")
        return None
    log.info(f"[GRAPH] _graph_add_node({node_type}, {title[:40]}) → conv={conv_id[:8]}...")
    store = _graph_store.setdefault(conv_id, {
        "nodes": [], "edges": [], "version": 0, "_node_keys": {},
    })
    key = dedup_key or f"{node_type}::{title}"
    if key in store["_node_keys"]:
        return store["_node_keys"][key]
    node_id = f"{node_type}_{len(store['nodes']) + 1}_{int(time.time() * 1000) % 1000000}"
    now = time.time()
    node = {
        "id": node_id,
        "type": node_type,
        "title": (title or "")[:200],
        "body": (body or "")[:2000],
        "status": "ready",
        "created_at": now,
        "updated_at": now,
        "source_tool": source_tool,
        "metadata": {k: str(v) for k, v in (metadata or {}).items()},
    }
    store["nodes"].append(node)
    store["_node_keys"][key] = node_id
    store["version"] += 1
    _graph_save_to_disk(conv_id)
    return node_id


def _graph_add_edge(from_id: str | None, to_id: str | None, kind: str, note: str = ""):
    if not from_id or not to_id:
        return
    conv_id = _current_conversation_id
    if not conv_id:
        return
    store = _graph_store.setdefault(conv_id, {
        "nodes": [], "edges": [], "version": 0, "_node_keys": {},
    })
    edge_id = f"{from_id}->{to_id}:{kind}"
    for e in store["edges"]:
        if e.get("id") == edge_id:
            return
    store["edges"].append({
        "id": edge_id,
        "from_id": from_id,
        "to_id": to_id,
        "kind": kind,
        "note": note,
        "created_at": time.time(),
    })
    store["version"] += 1
    _graph_save_to_disk(conv_id)


def _graph_update_node(
    node_id: str,
    body: str | None = None,
    metadata_updates: dict | None = None,
    status: str | None = None,
) -> bool:
    """Update an existing graph node's body, metadata, or status.

    Used by the bulk-organizer skill to maintain a persistent state machine
    in the asset graph. Phase nodes get status updates, task nodes get
    progress counts, etc.

    - body: replaces the node body (truncated to 2000 chars)
    - metadata_updates: merged into existing metadata (does NOT replace)
    - status: replaces the node status field
    Returns True if the node was found and updated, False otherwise.
    """
    conv_id = _current_conversation_id
    if not conv_id:
        return False
    store = _graph_store.get(conv_id)
    if not store:
        return False
    for node in store["nodes"]:
        if node["id"] == node_id:
            if body is not None:
                node["body"] = body[:2000]
            if metadata_updates:
                existing = node.get("metadata", {})
                existing.update({k: str(v) for k, v in metadata_updates.items()})
                node["metadata"] = existing
            if status is not None:
                node["status"] = status
            node["updated_at"] = time.time()
            store["version"] += 1
            _graph_save_to_disk(conv_id)
            log.info(f"[GRAPH] updated node {node_id} (body={'yes' if body else 'no'}, "
                     f"meta={list(metadata_updates.keys()) if metadata_updates else '[]'}, "
                     f"status={status})")
            return True
    log.warning(f"[GRAPH] update_node: node {node_id} not found")
    return False


# In-memory fact store — survives within a turn and is injected into
# compaction summaries so facts aren't lost when context is compressed.
_recorded_facts: list[dict] = []

# P18: track every successfully fetched URL for hallucination check
_fetched_sources: list[str] = []

def get_fetched_sources() -> list[str]:
    return list(_fetched_sources)

def clear_fetched_sources():
    _fetched_sources.clear()

# Research budget — limits total search/fetch calls to force transition to writing
_research_call_count: int = 0
_RESEARCH_BUDGET: int = 12  # Max web_search + web_fetch calls before forcing writing phase

def get_research_call_count() -> int:
    return _research_call_count

def increment_research_calls() -> tuple[int, int]:
    """Increment and return (current_count, budget). Called by web_search/web_fetch."""
    global _research_call_count
    _research_call_count += 1
    return _research_call_count, _RESEARCH_BUDGET

def get_recorded_facts() -> list[dict]:
    """Return all recorded facts. Called by conversation.py during compaction."""
    return list(_recorded_facts)

def clear_recorded_facts():
    """Clear all recorded facts. Called after they've been injected into a summary."""
    _recorded_facts.clear()

def _exec_record_fact(args: dict) -> str:
    """Record a key finding from research that must survive context compaction."""
    fact = args.get("fact", "").strip()
    source = args.get("source", "").strip()
    category = args.get("category", "general").strip()
    confidence = args.get("confidence", "medium").strip().lower()
    if not fact:
        return "ERROR: Must provide a 'fact' string."
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    # Warn if no source on research tasks
    source_warning = ""
    if not source:
        source_warning = "\n⚠️  No source URL provided. For academic research, ALWAYS include the source URL."
    entry = {
        "fact": fact,
        "source": source,
        "category": category,
        "confidence": confidence,
        "timestamp": time.time(),
    }
    _recorded_facts.append(entry)
    count = len(_recorded_facts)
    # Graph: add a "fact" node; if we have a source URL, add/link a
    # `source` node with a `sourced_from` edge so post-compaction
    # recovery + verification can walk from facts back to their origins.
    try:
        fact_node_id = _graph_add_node(
            "fact",
            title=fact[:140],
            body=fact,
            source_tool="record_fact",
            metadata={
                "node_role": "fact",
                "category": category,
                "confidence": confidence,
                "source": source,
            },
            dedup_key=f"fact::{fact[:200]}",
        )
        if source and fact_node_id:
            url_node_id = _graph_add_node(
                "url",
                title=source[:120],
                body=source,
                source_tool="record_fact",
                metadata={
                    "node_role": "source",
                    "source_type": "record_fact",
                },
                dedup_key=f"source::{source}",
            )
            # Both edge kinds for backward-compat: old UI renders `cites`,
            # new graph_read traverses `sourced_from`.
            _graph_add_edge(fact_node_id, url_node_id, "sourced_from")
            _graph_add_edge(fact_node_id, url_node_id, "cites")
    except Exception:
        pass  # Graph emission must never break a tool
    return (
        f"Fact #{count} recorded: \"{fact[:100]}{'...' if len(fact) > 100 else ''}\"\n"
        f"Source: {source or '(none)'} | Category: {category} | Confidence: {confidence}\n"
        f"Total facts recorded: {count}\n"
        f"These facts will survive context compaction."
        f"{source_warning}"
    )


# ─── Research Outline ───

_research_outline: dict | None = None

def get_research_outline() -> dict | None:
    """Return the active research outline. Called during compaction."""
    return _research_outline

def _exec_research_outline(args: dict) -> str:
    """Create a structured research outline that tracks paper progress."""
    global _research_outline
    title = args.get("title", "").strip()
    sections = args.get("sections", [])
    target_words = args.get("target_words", 5000)

    if not title:
        return "ERROR: Must provide a 'title' for the research paper."
    if not sections:
        return "ERROR: Must provide at least one section in 'sections' array."

    outline_sections = []
    words_per_section = target_words // len(sections)
    for i, sec in enumerate(sections):
        if isinstance(sec, str):
            sec = {"title": sec, "target_words": words_per_section}
        outline_sections.append({
            "id": f"sec_{i+1}",
            "title": sec.get("title", f"Section {i+1}"),
            "target_words": sec.get("target_words", words_per_section),
            "key_questions": sec.get("key_questions", []),
            "status": "pending",  # pending → researched → drafted → revised
            "facts_collected": 0,
            "words_written": 0,
            "citations_used": 0,
        })

    _research_outline = {
        "title": title,
        "target_words": target_words,
        "sections": outline_sections,
        "thesis": args.get("thesis", ""),
        "created_at": time.time(),
    }

    section_list = []
    for s in outline_sections:
        section_list.append(
            f"  {s['id']}: {s['title']} (~{s['target_words']} words) [{s['status']}]"
        )

    return (
        f"Research outline created: \"{title}\"\n"
        f"Target: {target_words} words across {len(outline_sections)} sections\n"
        f"Thesis: {_research_outline['thesis'] or '(to be determined during research)'}\n\n"
        f"Sections:\n" + "\n".join(section_list) + "\n\n"
        f"Now research each section thoroughly before writing.\n"
        f"Use record_fact for every finding. Aim for 3+ sources per section.\n"
        f"Call ask_user for approval, then begin Phase 2: Deep Research."
    )


# ─── Draft Section ───

_draft_sections: dict = {}  # section_id → content

def get_draft_sections() -> dict:
    """Return all drafted sections. Called during compaction."""
    return dict(_draft_sections)

def _exec_draft_section(args: dict) -> str:
    """Write one section of the research paper. Tracks word count and citations."""
    global _research_outline, _draft_sections, _recorded_facts

    # P20: empty-content guard (BEFORE P17) — prevents wasted turns from empty drafts
    _p20_content_check = args.get("content", "")
    if not isinstance(_p20_content_check, str) or not _p20_content_check.strip():
        return (
            "ERROR: draft_section called with empty content. "
            "You must provide the full section text (minimum 500 words) "
            "in the 'content' argument. Do not call draft_section with empty "
            "content. If you are not ready to write, call record_fact or web_search "
            "instead."
        )

    # P17: fact gate — require >=4 facts recorded before ANY section draft accepted
    _P17_MIN_FACTS = 4
    if len(_recorded_facts) < _P17_MIN_FACTS:
        _p21_need = _P17_MIN_FACTS - len(_recorded_facts)
        _p21_recent_urls = []
        try:
            # Surface recent web_search URLs to help model pick facts
            from_global = globals()
            _search_cache = from_global.get("_last_search_results", [])
            for r in _search_cache[:3]:
                if isinstance(r, dict) and r.get("url"):
                    _p21_recent_urls.append(r["url"])
        except Exception:
            pass
        _p21_url_hint = ""
        if _p21_recent_urls:
            _p21_url_hint = (
                " Recent sources to cite: " + ", ".join(_p21_recent_urls) + "."
            )
        return (
            f"ERROR: Cannot draft yet. You have {len(_recorded_facts)} of "
            f"{_P17_MIN_FACTS} required facts. You need {_p21_need} more.\n\n"
            f"REQUIRED NEXT ACTION: call record_fact (not draft_section, not web_search, "
            f"not prose). Extract a concrete factual claim from a web_search result "
            f"you already have and pass it to record_fact with fact, category, confidence, "
            f"and source fields.{_p21_url_hint}\n\n"
            f"After you reach {_P17_MIN_FACTS} facts, draft_section will be allowed."
        )

    section_id = args.get("section_id", "").strip()
    content = args.get("content", "").strip()

    # Strip thinking / tool-call markup that sometimes leaks into the
    # `content` arg on the mlx-vlm route (the inline <think> splitter
    # missed the close tag). Without this, the saved draft shows the
    # user literal `<think>` blocks + `<function=…>` XML.
    _thinking_pats = [
        (r'<think>.*?</think>', re.DOTALL),
        (r'<think>.*', re.DOTALL),                 # unclosed fallback
        (r'<thinking>.*?</thinking>', re.DOTALL),
        (r'<\|channel>thought.*?(?:<channel\|>|<\|channel>)', re.DOTALL),
        (r'<\|think\|>.*?<\|/think\|>', re.DOTALL),
        (r'<tool_call>.*?</tool_call>', re.DOTALL),
        (r'<function=\w+\s*>.*?</function>', re.DOTALL),
        (r'</?tool_call>|</?function>|</?parameter[^>]*>|</?export>', 0),
    ]
    for pat, flags in _thinking_pats:
        content = re.sub(pat, '', content, flags=flags)
    content = content.strip()

    if not section_id:
        return "ERROR: Must provide 'section_id' (e.g. 'sec_1')."
    if not content:
        return "ERROR: Must provide 'content' — the full text of this section."

    # ─── Argument-quality gate (Patch 1) ───
    # Reject placeholder/fragment content before it pollutes the draft store.
    # The model degenerates into `"content": "..."`, `"content": "\"#"`, `"content": "\"The"`, etc.
    # Force it to retry with real prose instead of silently recording a 1-word section.
    _stripped = content.strip().strip('"').strip("'").strip()
    _junk_literals = {"...", ".", "..", "#", "##", "###", "", "the", "The", "THE",
                      "content", "section", "text", "...content...", "todo", "TODO"}
    _word_count_check = len(_stripped.split())
    _section_title = section_id
    if _research_outline:
        for _s in _research_outline["sections"]:
            if _s["id"] == section_id:
                _section_title = _s.get("title", section_id)
                _section_target = _s.get("target_words", 500)
                break
        else:
            _section_target = 500
    else:
        _section_target = 500

    # P26 (iter10): relaxed from 300ch/50w to 200ch/35w
    if (
        len(_stripped) < 200
        or _stripped in _junk_literals
        or _word_count_check < 35
    ):
        _example = (
            'draft_section({"section_id": "' + section_id + '", "content": "'
            'Solid-state batteries represent a fundamental shift in energy storage '
            'technology, replacing the liquid electrolyte of conventional lithium-ion '
            'cells with a solid ionic conductor. This architectural change promises '
            'higher energy densities — QuantumScape has demonstrated cells exceeding '
            '400 Wh/kg in lab conditions [1], nearly double commercial lithium-ion. '
            'The history of solid-state research dates to the 1970s work on '
            'lithium-iodine pacemaker cells [2], but only in the past decade have '
            'advances in sulfide and oxide electrolytes made high-power applications '
            'practical. Toyota, Samsung SDI, and a wave of startups now race toward '
            'automotive-scale production [3]... (continue for ~' +
            str(_section_target) + ' words total)"})'
        )
        return (
            f"ERROR: draft_section rejected — content is too short or appears to be a placeholder.\n"
            f"  section_id: {section_id}\n"
            f"  section title: '{_section_title}'\n"
            f"  received content: {_stripped[:100]!r} ({len(_stripped)} chars, {_word_count_check} words)\n"
            f"  minimum required: 300 chars AND 50 words of real prose.\n"
            f"  target for this section: ~{_section_target} words.\n\n"
            f"REQUIRED: retry draft_section with the FULL body text for '{_section_title}'. "
            f"Write complete paragraphs — a proper academic section of ~{_section_target} words with "
            f"inline citations [1], [2] etc. Do NOT pass placeholder text like '...', '#', or 'The'. "
            f"Do NOT pass a content fragment. Write the entire section body now.\n\n"
            f"EXAMPLE of a valid call (truncated for brevity — your content should be "
            f"~{_section_target} words, not ~100):\n"
            f"  {_example}\n\n"
            f"Your job right now: emit ONE tool_call with the complete section prose inside "
            f"the content field. Do not narrate. Do not explain. Do not call any other tool. "
            f"Do not pass a short fragment. Write the section body as a single string."
        )

    # Count words and citations
    words = len(content.split())
    import re as _re
    citations = _re.findall(r'\[(\d+)\]', content)
    unique_citations = sorted(set(int(c) for c in citations))

    # Store the draft
    # P16: enforce min 500 words + at least 1 inline citation
    _p16_wc = len(content.split())
    _p16_cit = re.findall(r"\[\d+\]", content)
    _P16_MIN_WORDS = 300  # P25 (iter10): relaxed to 300 to enable accumulation
    if _p16_wc < _P16_MIN_WORDS:
        return (
            f"ERROR: Section draft too short: {_p16_wc} words "
            f"(minimum {_P16_MIN_WORDS}). Expand with more technical detail, citations, "
            f"and depth. Re-call draft_section for section {section_id} with richer content."
        )
    if len(_p16_cit) < 1:
        # P27 (iter10): auto-repair — long content without citations gets [1] appended
        # rather than discarded. Prevents losing salvaged prose.
        if _p16_wc >= 300 and _recorded_facts:
            content = content.rstrip() + " [1]"
            _p16_cit = re.findall(r"\[\d+\]", content)
            log_msg = f"P27: auto-repaired 0-citation draft ({_p16_wc}w) with [1]"
            try:
                import logging as _logging
                _logging.getLogger("tools").warning(log_msg)
            except Exception:
                pass
        else:
            return (
                f"ERROR: Section has 0 inline citations. Every section MUST cite "
                f"at least one source in [N] format where N matches a recorded fact. "
                f"Re-call draft_section for section {section_id} with at least 1 inline citation."
            )

    # P24 (iter10): accumulation — if section already drafted, APPEND
    if section_id in _draft_sections:
        _prev = _draft_sections[section_id]
        _prev_content = _prev.get("content", "")
        _combined_content = _prev_content.rstrip() + "\n\n" + content.lstrip()
        _combined_words = len(_combined_content.split())
        _combined_cites = sorted(set(
            list(_prev.get("citations", [])) + list(unique_citations)
        ))
        _draft_sections[section_id] = {
            "content": _combined_content,
            "words": _combined_words,
            "citations": _combined_cites,
            "drafted_at": time.time(),
            "append_count": _prev.get("append_count", 1) + 1,
        }
        # Override return-side values so status message is accurate
        words = _combined_words
        unique_citations = _combined_cites
    else:
        _draft_sections[section_id] = {
            "content": content,
            "words": words,
            "citations": unique_citations,
            "drafted_at": time.time(),
            "append_count": 1,
        }

    # Update outline if it exists
    section_title = section_id
    if _research_outline:
        for sec in _research_outline["sections"]:
            if sec["id"] == section_id:
                sec["status"] = "drafted"
                sec["words_written"] = words
                sec["citations_used"] = len(unique_citations)
                section_title = sec["title"]
                break

    # Progress summary
    total_words = sum(d["words"] for d in _draft_sections.values())
    total_sections = len(_draft_sections)
    target = _research_outline["target_words"] if _research_outline else 5000

    result = (
        f"Section '{section_title}' drafted: {words} words, {len(unique_citations)} citations\n"
        f"Citations used: {unique_citations if unique_citations else 'none'}\n\n"
        f"Progress: {total_sections} sections drafted, {total_words}/{target} words total "
        f"({int(total_words/target*100)}%)\n"
    )

    if words < 200:
        result += "\n⚠️  This section is very short. Academic sections should be 400-800 words minimum."
    if not unique_citations:
        result += "\n⚠️  No citations in this section. Every section should cite at least 2 sources."

    # Check remaining sections
    if _research_outline:
        remaining = [s for s in _research_outline["sections"] if s["status"] != "drafted"]
        if remaining:
            result += f"\n\nNext section: {remaining[0]['id']} — {remaining[0]['title']}"
        else:
            result += (
                f"\n\n✅ ALL SECTIONS DRAFTED. Total: {total_words} words.\n"
                f"Now call self_grade to evaluate the paper quality."
            )

    return result


# ─── Self Grade ───

def _exec_self_grade(args: dict) -> str:
    # P28 (iter11): block self_grade until draft corpus is substantial.
    # Prevents early-termination pattern observed in iter10 where the model
    # calls self_grade at 2200w and treats the paper as "done" instead of
    # expanding via draft_section accumulation.
    global _draft_sections, _research_outline, _recorded_facts
    _p28_total = sum(d.get("words", 0) for d in _draft_sections.values())
    _p28_append_total = sum(d.get("append_count", 1) for d in _draft_sections.values())
    _P28_MIN_WORDS = 4000  # P39b lowered from 4500 after iter24 stuck at 4242w (target 5000, accept 4000)
    _P28_MIN_APPENDS = len(_draft_sections) + 5  # each section + 5 expansion calls
    if _p28_total < _P28_MIN_WORDS and _p28_append_total < _P28_MIN_APPENDS:
        # Find shortest section to help the model know where to accumulate
        _p28_shortest = None
        _p28_shortest_w = 1_000_000
        for _sid, _d in _draft_sections.items():
            if _d.get("words", 0) < _p28_shortest_w:
                _p28_shortest = _sid
                _p28_shortest_w = _d.get("words", 0)
        _p28_need = _P28_MIN_WORDS - _p28_total
        _p28_target = _p28_shortest or "sec_1"
        return (
            f"ERROR: self_grade blocked. Current paper: {_p28_total} words across "
            f"{len(_draft_sections)} sections. Minimum before grading: {_P28_MIN_WORDS} words.\n\n"
            f"REQUIRED NEXT ACTION: call draft_section with section_id='{_p28_target}' "
            f"(currently {_p28_shortest_w}w, the shortest) and ADDITIONAL content "
            f"(300+ words of new material). The tool now APPENDS — your new content "
            f"adds to the existing section, it does NOT replace it.\n\n"
            f"You need approximately {_p28_need} more words. That's roughly "
            f"{max(1, _p28_need // 400)} more draft_section calls of 400 words each, "
            f"or {max(1, _p28_need // 300)} more calls of 300 words each. "
            f"You may expand any section — pick the shortest ones first.\n\n"
            f"Do NOT call self_grade again until total words >= {_P28_MIN_WORDS}."
        )

    """Evaluate the research paper against an academic rubric and produce a score."""

    _model_paper_text = args.get("paper_text", "").strip()
    paper_text = ""

    # P14: ALWAYS assemble paper_text from _draft_sections — model-provided
    # paper_text is ignored for grading (model consistently truncates/summarizes).
    if _draft_sections:
        if _research_outline:
            ordered = []
            for sec in _research_outline["sections"]:
                if sec["id"] in _draft_sections:
                    ordered.append(_draft_sections[sec["id"]]["content"])
            paper_text = "\n\n".join(ordered)
        else:
            paper_text = "\n\n".join(d["content"] for d in _draft_sections.values())
        if _research_outline and _research_outline.get("title"):
            paper_text = f"# {_research_outline['title']}\n\n" + paper_text

    if not paper_text:
        paper_text = _model_paper_text

    if not paper_text:
        return "ERROR: No paper text to grade. Either provide 'paper_text' or draft sections first."

    import re as _re

    # ─── Rubric scoring ───
    scores = {}
    feedback = []

    # 1. Word count (20 points)
    words = len(paper_text.split())
    target = _research_outline["target_words"] if _research_outline else 5000
    word_pct = min(words / target, 1.0)
    scores["word_count"] = int(word_pct * 20)
    if words < target * 0.7:
        feedback.append(f"WORD COUNT: {words}/{target} words — significantly short. Add more depth to each section.")
    elif words < target * 0.9:
        feedback.append(f"WORD COUNT: {words}/{target} words — slightly short. Expand analysis in weaker sections.")
    else:
        feedback.append(f"WORD COUNT: {words}/{target} words — meets target.")

    # 2. Citation count and diversity (20 points)
    inline_citations = _re.findall(r'\[(\d+)\]', paper_text)
    unique_citations = sorted(set(int(c) for c in inline_citations))
    citation_score = min(len(unique_citations) / 15, 1.0)  # 15 citations = full marks
    scores["citations"] = int(citation_score * 20)
    if len(unique_citations) < 5:
        feedback.append(f"CITATIONS: Only {len(unique_citations)} unique sources — needs 15+ for academic quality.")
    elif len(unique_citations) < 10:
        feedback.append(f"CITATIONS: {len(unique_citations)} sources — good but aim for 15+ for thorough coverage.")
    else:
        feedback.append(f"CITATIONS: {len(unique_citations)} sources — strong citation coverage.")

    # 3. Section structure (15 points)
    headings = _re.findall(r'^#{1,3}\s+.+', paper_text, _re.MULTILINE)
    section_score = min(len(headings) / 8, 1.0)  # 8 headings = full marks
    scores["structure"] = int(section_score * 15)
    if len(headings) < 4:
        feedback.append(f"STRUCTURE: Only {len(headings)} section headings — needs 8+ for proper paper structure.")
    else:
        feedback.append(f"STRUCTURE: {len(headings)} sections — well-structured.")

    # 4. Depth indicators (15 points) — looks for technical content, numbers, analysis
    depth_signals = 0
    # Numbers/statistics
    numbers = _re.findall(r'\d+[\.,]?\d*\s*(?:%|GWh|kWh|billion|million|°C|mAh|Wh/kg|km|mile)', paper_text, _re.IGNORECASE)
    depth_signals += min(len(numbers), 10)
    # Technical terms
    tech_terms = ['electrolyte', 'cathode', 'anode', 'dendrite', 'conductivity', 'interface',
                  'manufacturing', 'scalability', 'energy density', 'cycle life', 'solid-state']
    found_terms = [t for t in tech_terms if t.lower() in paper_text.lower()]
    depth_signals += len(found_terms)
    # Comparative language
    comparative = len(_re.findall(r'(?:compared to|versus|unlike|in contrast|however|whereas)', paper_text, _re.IGNORECASE))
    depth_signals += min(comparative, 5)
    depth_score = min(depth_signals / 20, 1.0)
    scores["depth"] = int(depth_score * 15)
    feedback.append(f"DEPTH: {len(numbers)} data points, {len(found_terms)}/{len(tech_terms)} key terms, {comparative} comparative analyses.")

    # 5. Academic tone (10 points)
    # Check for informal language
    informal = _re.findall(r'\b(?:really|very|super|awesome|cool|stuff|things|a lot|basically|pretty much)\b', paper_text, _re.IGNORECASE)
    tone_penalty = min(len(informal) * 2, 10)
    scores["tone"] = max(10 - tone_penalty, 0)
    if informal:
        feedback.append(f"TONE: Found {len(informal)} informal terms ({', '.join(informal[:5])}). Use formal academic language.")
    else:
        feedback.append("TONE: Appropriate academic register throughout.")

    # 6. Bibliography check (10 points)
    has_bibliography = bool(_re.search(r'(?:bibliography|references|works cited|sources)', paper_text, _re.IGNORECASE))
    urls_in_text = _re.findall(r'https?://[^\s\)\"]+', paper_text)
    unique_urls = list(set(urls_in_text))
    biblio_score = 0
    if has_bibliography:
        biblio_score += 5
    if len(unique_urls) >= 10:
        biblio_score += 5
    elif len(unique_urls) >= 5:
        biblio_score += 3
    scores["bibliography"] = biblio_score
    if not has_bibliography:
        feedback.append("BIBLIOGRAPHY: Missing bibliography section — REQUIRED.")
    else:
        feedback.append(f"BIBLIOGRAPHY: Present with {len(unique_urls)} URLs.")

    # 7. Counterarguments / balance (10 points)
    counter_signals = len(_re.findall(r'(?:however|on the other hand|critics|challenges|limitations|drawbacks|concerns|counterargument|despite|although)', paper_text, _re.IGNORECASE))
    balance_score = min(counter_signals / 5, 1.0)
    scores["balance"] = int(balance_score * 10)
    if counter_signals < 2:
        feedback.append("BALANCE: Paper needs more counterarguments and discussion of limitations.")
    else:
        feedback.append(f"BALANCE: {counter_signals} balancing statements — shows multiple perspectives.")

    # Total score
    total = sum(scores.values())

    # Build report
    report = f"# Self-Grade Report\n\n"
    report += f"## Score: {total}/100\n\n"
    report += "## Breakdown:\n"
    for category, score in scores.items():
        max_score = {"word_count": 20, "citations": 20, "structure": 15, "depth": 15,
                     "tone": 10, "bibliography": 10, "balance": 10}[category]
        report += f"  {category}: {score}/{max_score}\n"
    report += f"\n## Feedback:\n"
    for fb in feedback:
        report += f"  - {fb}\n"

    # Improvement guidance
    report += f"\n## Action Items:\n"
    if total < 70:
        weakest = sorted(scores.items(), key=lambda x: x[1])[:3]
        for cat, score in weakest:
            if cat == "word_count" and score < 15:
                report += "  - EXPAND: Add more depth and detail to each section. Target 500+ words per section.\n"
            elif cat == "citations" and score < 15:
                report += "  - CITE: Do additional web_search queries and add more inline citations.\n"
            elif cat == "depth" and score < 10:
                report += "  - DEEPEN: Add specific numbers, data points, and technical analysis.\n"
            elif cat == "bibliography" and score < 8:
                report += "  - BIBLIOGRAPHY: Add a ## Bibliography section with all source URLs.\n"
            elif cat == "balance" and score < 7:
                report += "  - BALANCE: Add discussion of limitations, challenges, and counterarguments.\n"
        report += f"\n  Score is {total}/100 — below 70 threshold. IMPROVE the weakest areas and re-grade.\n"
    else:
        report += f"  Score is {total}/100 — meets quality threshold.\n"

    # Auto-save the paper as a document
    # Build bibliography from recorded facts
    biblio_lines = []
    seen_sources = set()
    for fact in _recorded_facts:
        src = fact.get("source", "").strip()
        if src and src not in seen_sources:
            seen_sources.add(src)
            biblio_lines.append(f"[{len(biblio_lines) + 1}] {src}")

    # P38 (iter24): citation-preserving bibliography merge. iter23 observed
    # that P37's regex-strip replaced the model's bibliography (which went to
    # [25]) with an 8-URL merged list, orphaning all inline [9]..[25] markers.
    # New behavior: parse the model's existing bibliography FIRST to preserve
    # its numbering, then APPEND any additional fact/fetched URLs as new
    # numbered entries so inline citations remain resolvable.
    import re as _re_bib
    _bib_header_pat = _re_bib.compile(
        r'\n+#{1,3}\s*(?:Bibliography|References|Works\s+Cited|Sources)\b[^\n]*\n',
        _re_bib.IGNORECASE,
    )
    _bib_match = _bib_header_pat.search(paper_text)
    body_text = paper_text
    existing_entries = []  # list of (num:int, url:str) preserving model order
    seen_urls_bib = set()
    if _bib_match:
        body_text = paper_text[:_bib_match.start()].rstrip()
        bib_body = paper_text[_bib_match.end():]
        for _m in _re_bib.finditer(r'\[(\d+)\]\s*(https?://[^\s\)\]\"]+)', bib_body):
            _n = int(_m.group(1))
            _u = _m.group(2).rstrip('.,;')
            if _u not in seen_urls_bib:
                seen_urls_bib.add(_u)
                existing_entries.append((_n, _u))
        # Also pick up plain URLs in bib body that weren't numbered
        for _u in _re_bib.findall(r'https?://[^\s\)\]\"]+', bib_body):
            _u = _u.rstrip('.,;')
            if _u not in seen_urls_bib:
                seen_urls_bib.add(_u)
                # assign next available number after max existing
                _maxn = max([n for n, _ in existing_entries], default=0)
                existing_entries.append((_maxn + 1, _u))
    _maxn = max([n for n, _ in existing_entries], default=0)
    # Gather fact URLs + fetched URLs + body-cited URLs not already in biblio
    _new_urls = []
    for _fact in _recorded_facts:
        _src = (_fact.get("source") or "").strip().rstrip('.,;')
        if _src and _src.startswith("http") and _src not in seen_urls_bib and _src not in _new_urls:
            _new_urls.append(_src)
    # P40: _fetched_sources is the actual global populated by _exec_web_fetch.
    # The earlier P37/P38 reference to "_fetched_urls" was a dead lookup and
    # caused iter24b to ship only 2 URLs despite 5+ fetches.
    try:
        for _u in globals().get("_fetched_sources", []) or []:
            _u = (_u or "").strip().rstrip('.,;')
            if _u and _u not in seen_urls_bib and _u not in _new_urls:
                _new_urls.append(_u)
    except Exception:
        pass
    try:
        for _u in globals().get("_fetched_urls", []) or []:
            _u = (_u or "").strip().rstrip('.,;')
            if _u and _u not in seen_urls_bib and _u not in _new_urls:
                _new_urls.append(_u)
    except Exception:
        pass
    for _u in _re_bib.findall(r'https?://[^\s\)\]\"]+', body_text):
        _u = _u.rstrip('.,;')
        if _u not in seen_urls_bib and _u not in _new_urls:
            _new_urls.append(_u)
    # Assign continuing numbers to new URLs
    _final_entries = list(existing_entries)
    for _u in _new_urls:
        _maxn += 1
        _final_entries.append((_maxn, _u))
    # Render assembled
    assembled = body_text
    if _final_entries:
        _final_entries.sort(key=lambda x: x[0])
        _lines = [f"[{n}] {u}" for n, u in _final_entries]
        assembled += "\n\n## Bibliography\n\n" + "\n".join(_lines) + "\n"

    # Auto-save via file_sidecar
    try:
        from file_sidecar import create_docx
        title = _research_outline.get("title", "Research Paper") if _research_outline else "Research Paper"
        safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in title)[:80]
        # create_docx takes a dict ({title, content, filename}) — not positional args
        filepath = create_docx({
            "title": title,
            "content": assembled,
            "filename": safe_title,
        })
        report += f"\n\n✅ PAPER SAVED: {filepath}\n"
        report += f"The paper has been automatically saved as a Word document.\n"
        report += f"Words: {words} | Citations: {len(unique_citations)} | Score: {total}/100\n"
        try:
            import logging as _lg
            _lg.getLogger("tools").warning(f"PAPER SAVED: {filepath}")
        except Exception:
            pass
    except Exception as e:
        import traceback as _tb
        report += f"\n\n⚠️ Auto-save failed: {type(e).__name__}: {e}\n"
        report += "Call create_document manually to save the paper.\n"
        try:
            import logging as _lg
            _lg.getLogger("tools").error(f"Auto-save failed: {e}\n{_tb.format_exc()}")
        except Exception:
            pass

    return report


_WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}

_COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]


def _format_weather(data: dict, location_name: str | None = None) -> str:
    """Format weather data dict into a human-readable summary."""
    loc = location_name or f"{data.get('latitude', '?'):.2f}, {data.get('longitude', '?'):.2f}"
    lines = [
        f"📍 {loc}",
        f"🌡️ {data['temperature']:.1f}{data['temperatureUnit']} (feels like {data['feelsLike']:.1f}{data['temperatureUnit']})",
        f"☁️ {data['condition']}",
        f"💧 Humidity: {data['humidity']:.0f}%",
        f"💨 Wind: {data['windSpeed']:.0f} {data['windSpeedUnit']} {data['windDirection']}",
        f"☀️ UV Index: {data['uvIndex']}",
        f"👁️ Visibility: {data['visibility']:.0f} {data['visibilityUnit']}",
        f"☁️ Cloud Cover: {data['cloudCover']:.0f}%",
        f"📊 Pressure: {data['pressure']:.0f} {data['pressureUnit']}",
        f"🕐 Observed: {data['timestamp']}",
    ]
    return "\n".join(lines)


def _fetch_weather_for_coords(lat: float, lon: float) -> dict:
    """Fetch current weather from Open-Meteo for given coordinates."""
    import urllib.request, json as _json
    fields = ",".join([
        "temperature_2m", "apparent_temperature", "relative_humidity_2m",
        "wind_speed_10m", "wind_direction_10m", "weather_code",
        "surface_pressure", "cloud_cover", "uv_index",
        "dew_point_2m", "visibility",
    ])
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}&current={fields}&timezone=auto")
    req = urllib.request.Request(url, headers={"User-Agent": "Clyde/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        r = _json.loads(resp.read())
    c = r["current"]
    u = r["current_units"]
    deg = c.get("wind_direction_10m", 0)
    compass = _COMPASS[min(int((deg + 11.25) % 360 / 22.5), 15)]
    return {
        "temperature": c["temperature_2m"],
        "temperatureUnit": u["temperature_2m"],
        "feelsLike": c["apparent_temperature"],
        "condition": _WMO_CODES.get(c["weather_code"], f"Code {c['weather_code']}"),
        "humidity": c["relative_humidity_2m"],
        "windSpeed": c["wind_speed_10m"],
        "windSpeedUnit": u["wind_speed_10m"],
        "windDirection": compass,
        "uvIndex": int(c.get("uv_index", 0)),
        "visibility": (c.get("visibility") or 16000) / 1000,
        "visibilityUnit": "km",
        "pressure": c["surface_pressure"],
        "pressureUnit": u["surface_pressure"],
        "cloudCover": c["cloud_cover"],
        "latitude": lat,
        "longitude": lon,
        "timestamp": c["time"],
    }


def _geocode(location: str) -> tuple[float, float, str] | None:
    """Geocode a location name via Open-Meteo. Returns (lat, lon, display_name) or None."""
    import urllib.request, urllib.parse, json as _json
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(location)}&count=1&language=en"
    req = urllib.request.Request(url, headers={"User-Agent": "Clyde/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        r = _json.loads(resp.read())
    results = r.get("results", [])
    if not results:
        return None
    hit = results[0]
    name = hit.get("name", location)
    country = hit.get("country", "")
    admin = hit.get("admin1", "")
    display = ", ".join(filter(None, [name, admin, country]))
    return hit["latitude"], hit["longitude"], display


def _exec_get_weather(args: dict) -> str:
    """Get weather — local (GPS) or any city via Open-Meteo."""
    import json as _json
    from pathlib import Path

    location = args.get("location", "").strip()

    if not location:
        # Local weather — read from Clyde's cached GPS-based file
        weather_file = Path.home() / "Library" / "Application Support" / "Clyde" / "weather.json"
        if not weather_file.exists():
            return "Weather data not available yet. Clyde may still be fetching location. Try again in a few seconds."
        try:
            data = _json.loads(weather_file.read_text())
            return _format_weather(data, "Your location (via GPS)")
        except Exception as e:
            return f"Error reading local weather data: {e}"
    else:
        # Remote weather — geocode + fetch from Open-Meteo
        try:
            geo = _geocode(location)
            if geo is None:
                return f"Could not find location: {location}"
            lat, lon, display_name = geo
            data = _fetch_weather_for_coords(lat, lon)
            return _format_weather(data, display_name)
        except Exception as e:
            return f"Error fetching weather for {location}: {e}"


def _exec_web_search(args: dict) -> str:
    """Search the web using DuckDuckGo HTML."""
    import urllib.request, urllib.parse, html as html_mod
    query = args.get("query", "")
    if not query:
        return "ERROR: No query provided."
    max_results = min(args.get("max_results", 8), 20)
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Clyde/1.0"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            page = resp.read().decode("utf-8", errors="replace")

        # Parse results from DDG HTML
        results = []
        # DDG HTML format: <a class="result__a" href="...">title</a>
        # and <a class="result__snippet">snippet</a>
        import re
        # Extract result links and titles
        links = re.findall(
            r'<a[^>]+class="result__a"[^>]+href="([^"]*)"[^>]*>(.*?)</a>',
            page, re.DOTALL
        )
        snippets = re.findall(
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            page, re.DOTALL
        )

        for i, (href, title) in enumerate(links[:max_results]):
            # DDG redirects through their URL, extract actual URL
            actual_url = href
            if "uddg=" in href:
                match = re.search(r'uddg=([^&]+)', href)
                if match:
                    actual_url = urllib.parse.unquote(match.group(1))

            # Clean HTML tags from title and snippet
            clean_title = re.sub(r'<[^>]+>', '', title).strip()
            clean_snippet = ""
            if i < len(snippets):
                clean_snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
                clean_snippet = html_mod.unescape(clean_snippet)

            clean_title = html_mod.unescape(clean_title)
            results.append(f"{i+1}. {clean_title}\n   URL: {actual_url}\n   {clean_snippet}")

        if not results:
            return f"No results found for: {query}"
        result_text = "\n\n".join(results)

        # Graph: emit a "query" node + "url" nodes for each result
        try:
            query_node_id = _graph_add_node(
                "query",
                title=query[:120],
                body=f"web_search: {query}",
                source_tool="web_search",
                metadata={"result_count": len(links[:max_results])},
                dedup_key=f"query::{query}",
            )
            for i, (href, title) in enumerate(links[:max_results]):
                actual_url = href
                if "uddg=" in href:
                    m = re.search(r'uddg=([^&]+)', href)
                    if m:
                        actual_url = urllib.parse.unquote(m.group(1))
                clean_title = re.sub(r'<[^>]+>', '', title).strip() or actual_url
                clean_title = html_mod.unescape(clean_title)
                url_node_id = _graph_add_node(
                    "url",
                    title=clean_title[:140],
                    body=actual_url,
                    source_tool="web_search",
                    metadata={"rank": i + 1, "url": actual_url},
                    dedup_key=f"url::{actual_url}",
                )
                _graph_add_edge(query_node_id, url_node_id, "found")
        except Exception:
            pass

        # Research budget tracking
        count, budget = increment_research_calls()
        remaining = budget - count
        if remaining <= 0:
            result_text += (
                "\n\n⚠️ RESEARCH BUDGET EXHAUSTED. You have used all your research calls. "
                "STOP searching and fetching. Use record_fact for any remaining findings, "
                "then move IMMEDIATELY to Phase 4: call draft_section for each section of your outline. "
                "Use your recorded facts to write the paper NOW."
            )
        elif remaining <= 3:
            result_text += (
                f"\n\n⚠️ Research budget: {remaining} calls remaining out of {budget}. "
                "Start wrapping up research. Record key facts and prepare to start writing with draft_section."
            )
        return result_text
    except Exception as e:
        return f"ERROR: Web search failed: {e}"


def _exec_web_fetch(args: dict) -> str:
    """Fetch a web page. Uses Firecrawl if available, falls back to urllib."""
    url = args.get("url", "")
    global _fetched_sources
    if url and url not in _fetched_sources:
        _fetched_sources.append(url)
    if not url:
        return "ERROR: No URL provided."
    try:
        _graph_add_node(
            "url",
            title=url[:140],
            body=url,
            source_tool="web_fetch",
            metadata={
                "node_role": "source",
                "source_type": "web_fetch",
                "url": url,
                "fetched": True,
            },
            dedup_key=f"source::{url}",
        )
    except Exception:
        pass
    max_chars = min(args.get("max_chars", 3000), 8000)
    result_text = None

    # Try Firecrawl first (localhost:3002)
    try:
        import json as _json
        req_data = _json.dumps({"url": url, "formats": ["markdown"]}).encode()
        req = urllib.request.Request(
            "http://localhost:3002/v1/scrape",
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read())
            content = data.get("data", {}).get("markdown", "")
            if content:
                if len(content) > max_chars:
                    content = content[:max_chars] + "\n\n[content truncated]"
                result_text = f"Source: {url}\n\n{content}"
    except Exception:
        pass  # Firecrawl not available, fall back

    # Fallback: urllib with basic HTML stripping
    if result_text is None:
        import urllib.request
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Clyde/1.0"
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")

            import re
            # Remove script/style blocks
            text = re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
            # Remove HTML tags
            text = re.sub(r'<[^>]+>', ' ', text)
            # Clean whitespace
            text = re.sub(r'\s+', ' ', text).strip()
            # Unescape HTML entities
            import html as html_mod
            text = html_mod.unescape(text)

            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n[content truncated]"
            result_text = f"Source: {url}\n\n{text}"
        except Exception as e:
            return f"ERROR: Failed to fetch {url}: {e}"

    # Research budget tracking
    count, budget = increment_research_calls()
    remaining = budget - count
    if remaining <= 0:
        result_text += (
            "\n\n⚠️ RESEARCH BUDGET EXHAUSTED. You have used all your research calls. "
            "STOP searching and fetching. Use record_fact for any remaining findings, "
            "then move IMMEDIATELY to Phase 4: call draft_section for each section of your outline. "
            "Use your recorded facts to write the paper NOW."
        )
    elif remaining <= 3:
        result_text += (
            f"\n\n⚠️ Research budget: {remaining} calls remaining out of {budget}. "
            "Start wrapping up research. Record key facts and prepare to start writing with draft_section."
        )
    return result_text


# ─── Bulk Organizer Tools ───

# Track which state_dirs have been permission-approved this session.
# Resets with reset_session_permissions().
_organize_approved_dirs: set[str] = set()

def _check_organize_permission(state_dir: str) -> str | None:
    """
    Check if the user has approved the organizer working on this state_dir.
    Returns None if approved, or a PERMISSION_DENIED string if not yet approved.
    This ensures the user always sees an explicit consent card before the
    organizer reads or modifies their data, even if the path is technically
    within allowed directories.
    """
    if not state_dir:
        return None
    resolved = str(_pathlib.Path(state_dir).expanduser().resolve())
    if resolved in _organize_approved_dirs:
        return None
    # Not yet approved — trigger the permission flow
    display = resolved.replace(str(_pathlib.Path.home()), "~")
    return (
        f"PERMISSION_DENIED: Cannot access {resolved}. "
        f"This path is outside the allowed directories."
    )

def _grant_organize_dir(state_dir: str):
    """Mark a state_dir as approved for this organize session."""
    resolved = str(_pathlib.Path(state_dir).expanduser().resolve())
    _organize_approved_dirs.add(resolved)

def _exec_organize_progress(args: dict) -> str:
    """Compact status + exact next action. Called every turn start."""
    state_dir = args.get("state_dir", "")

    # ── Find graph nodes ──
    conv_id = _current_conversation_id
    task_node = None
    category_nodes = []
    if conv_id and conv_id in _graph_store:
        for n in _graph_store[conv_id]["nodes"]:
            meta = n.get("metadata", {})
            if n["type"] == "fact" and meta.get("node_role") == "organize_task":
                task_node = n
            elif n["type"] == "fact" and meta.get("node_role") == "organize_category":
                category_nodes.append(n)

    # ── No graph — check disk ──
    if not task_node:
        if state_dir:
            sp = _pathlib.Path(state_dir).expanduser() / "state.json"
            if sp.exists():
                try:
                    s = _json.loads(sp.read_text(encoding="utf-8"))
                    total = len(s.get("items", []))
                    phase = s.get("phase", "ingest")
                    classified = sum(1 for i in s.get("items", []) if i.get("status") == "classified")
                    return (
                        f"RECOVERY|phase={phase}|total={total}|classified={classified}|state_dir={state_dir}\n"
                        f"NEXT: Call organize_update_graph action=create_task to rebuild graph, then continue."
                    )
                except Exception as e:
                    return f"ERROR: {e}"
        return "NO_JOB: Start new job. Use bash to run the parser script, then create_task."

    # ── Build compact status ──
    meta = task_node.get("metadata", {})
    phase = meta.get("phase", "unknown")
    total = int(meta.get("total_items", 0))
    classified = int(meta.get("classified", 0))
    uncertain = int(meta.get("uncertain", 0))
    sd = meta.get("state_dir", state_dir)
    cat_keys = ",".join(cn.get("metadata", {}).get("category_key", "?") for cn in category_nodes) if category_nodes else "none"
    task_id = task_node.get("id", "")

    status = f"phase={phase}|{classified}/{total}|uncertain={uncertain}|cats={len(category_nodes)}:{cat_keys}|state_dir={sd}|task_id={task_id}"

    # ── Determine exact next action ──
    if phase == "ingest":
        # Check if state.json exists and has items
        sp = _pathlib.Path(sd).expanduser() / "state.json" if sd else None
        if sp and sp.exists():
            try:
                s = _json.loads(sp.read_text(encoding="utf-8"))
                item_count = len(s.get("items", []))
                if item_count > 0:
                    return f"{status}\nINGEST_DONE: {item_count} items parsed.\nNEXT: Call organize_update_graph action=update_task node_id={task_id} phase=taxonomy total_items={item_count}. Then call organize_batch_read start=0 count=30 to sample items for taxonomy."
            except Exception:
                pass
        return f"{status}\nNEXT: Run parser script via bash to create state.json. Parser: python3 \"$HOME/Library/Application Support/Clyde/agent/skills/bulk-organizer/scripts/parse_bookmarks.py\" <input_file> {sd}"

    elif phase == "taxonomy":
        if not category_nodes:
            return f"{status}\nNEXT: Call organize_batch_read start=0 count=30 to sample items. Then propose categories to user with ask_user."
        else:
            return f"{status}\nTAXONOMY_READY: {len(category_nodes)} categories defined.\nNEXT: Call organize_update_graph action=update_task node_id={task_id} phase=classify. Then call organize_batch_read to start classifying."

    elif phase == "classify":
        next_start = int(meta.get("next_batch_start", classified))
        remaining = total - classified
        batch_size = min(15, remaining)
        if remaining <= 0:
            return f"{status}\nCLASSIFY_DONE: All items classified.\nNEXT: Call organize_update_graph action=update_task node_id={task_id} phase=review. Then present summary to user."
        return (f"{status}\nNEXT: Call organize_batch_read state_dir={sd} start={next_start} count={batch_size}. "
                f"Then IMMEDIATELY call organize_batch_write with classifications for those items. "
                f"Do NOT read more batches until you have written the current batch. "
                f"Pattern: read → classify → write → read next.")

    elif phase == "review":
        return f"{status}\nNEXT: Present classification summary to user via ask_user. Include category distribution and uncertain items."

    elif phase == "execute":
        return f"{status}\nNEXT: Run generate_plan.py to produce the reorganized output. Parser: python3 \"$HOME/Library/Application Support/Clyde/agent/skills/bulk-organizer/scripts/generate_plan.py\" {sd}"

    elif phase == "done":
        return f"{status}\nDONE: Job complete."

    return f"{status}\nNEXT: Unknown phase '{phase}'. Check state."


def _exec_organize_batch_read(args: dict) -> str:
    """Read a slice of items from state.json. Never loads the full file into context."""
    state_dir = args.get("state_dir", "")
    start = int(args.get("start", 0))
    count = int(args.get("count", 15))

    if not state_dir:
        return "ERROR: state_dir required"

    # Check organize permission (triggers user consent card on first access)
    perm_error = _check_organize_permission(state_dir)
    if perm_error:
        return perm_error

    sp = _pathlib.Path(state_dir).expanduser() / "state.json"
    if not sp.exists():
        return f"ERROR: No state.json at {state_dir}"

    try:
        state = _json.loads(sp.read_text(encoding="utf-8"))
        items = state.get("items", [])
        total = len(items)

        if start >= total:
            return f"END: start={start} >= total={total}. All items read."

        end = min(start + count, total)
        batch = items[start:end]

        # Compact output: only fields the model needs for classification
        lines = [f"BATCH: items {start}-{end-1} of {total} (showing {len(batch)})"]
        cats = state.get("taxonomy", {}).get("categories", {})
        if cats:
            lines.append(f"CATEGORIES: {','.join(cats.keys())}")

        for i, item in enumerate(batch):
            idx = start + i
            cls = item.get("classification") or {}
            cat = cls.get("category", "") if isinstance(cls, dict) else ""
            if cat:
                # Already classified — ultra-compact, just show idx+cat
                lines.append(f"  {idx}: [{cat}]")
            else:
                # Unclassified — show title + truncated url (enough to classify)
                orig = item.get("original", {})
                title = (orig.get("title") or item.get("title") or "untitled")[:60]
                url = (orig.get("url") or item.get("url") or "")[:60]
                line = f"  {idx}: {title} | {url}"
                # If enriched metadata available, append it
                enriched = item.get("enriched", {})
                if enriched and enriched.get("source") == "firecrawl":
                    enr_title = enriched.get("og_title") or enriched.get("title") or ""
                    enr_desc = enriched.get("og_description") or enriched.get("description") or ""
                    if enr_title and enr_title != title:
                        line += f" [enriched: {enr_title[:60]}]"
                    if enr_desc:
                        line += f" [desc: {enr_desc[:80]}]"
                lines.append(line)

        # If in classify phase, remind model to WRITE before reading more
        current_phase = state.get("phase", "")
        if current_phase == "classify":
            unclassified_in_batch = sum(
                1 for item in batch
                if not (item.get("classification") or {}).get("category")
            )
            if unclassified_in_batch > 0:
                lines.append(f"\nACTION REQUIRED: Classify these {unclassified_in_batch} items and call organize_batch_write NOW.")
                lines.append(f'Format: organize_batch_write state_dir="{state_dir}" classifications=\'[{{"idx":N,"category":"cat","confidence":"high"}}]\'')
                lines.append("Do NOT call organize_batch_read again until you have written this batch.")

        return "\n".join(lines)
    except Exception as e:
        return f"ERROR: {e}"


def _exec_organize_batch_write(args: dict) -> str:
    """Write classification results for a batch back to state.json.

    classifications: JSON string like [{"idx":0,"category":"dev","confidence":"high"}, ...]
    """
    state_dir = args.get("state_dir", "")
    classifications_raw = args.get("classifications", "")

    if not state_dir or not classifications_raw:
        return "ERROR: state_dir and classifications required"

    # Check organize permission
    perm_error = _check_organize_permission(state_dir)
    if perm_error:
        return perm_error

    sp = _pathlib.Path(state_dir).expanduser() / "state.json"
    if not sp.exists():
        return f"ERROR: No state.json at {state_dir}"

    try:
        classifications = _json.loads(classifications_raw)
        state = _json.loads(sp.read_text(encoding="utf-8"))
        items = state.get("items", [])

        classified_count = 0
        uncertain_count = 0
        for c in classifications:
            idx = int(c.get("idx", -1))
            if 0 <= idx < len(items):
                items[idx]["classification"] = {
                    "category": c.get("category", ""),
                    "confidence": c.get("confidence", "medium"),
                }
                items[idx]["status"] = "classified"
                classified_count += 1
                if c.get("confidence", "medium") == "low":
                    uncertain_count += 1

        state["items"] = items

        # Update progress tracking
        total_classified = sum(1 for i in items if i.get("status") == "classified")
        total_uncertain = sum(
            1 for i in items
            if isinstance(i.get("classification"), dict)
            and i["classification"].get("confidence") == "low"
        )

        state["phase"] = "classify"
        state["classified"] = total_classified
        state["uncertain"] = total_uncertain

        # Atomic write
        tmp = sp.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(state, indent=2), encoding="utf-8")
        tmp.rename(sp)

        # ── Auto-populate taxonomy.json from classifications ──
        # Build taxonomy with proper hierarchy fields (parent, subcategories,
        # item_count, examples) from the actual categories seen in items.
        taxonomy_path = sp.parent / "taxonomy.json"
        try:
            existing_tax = _json.loads(taxonomy_path.read_text(encoding="utf-8")) if taxonomy_path.exists() else {}
            cats = existing_tax.get("categories", {})

            # Count items per category and collect examples
            cat_counts: dict[str, int] = {}
            cat_examples: dict[str, list[str]] = {}
            for i in items:
                cls = i.get("classification")
                if cls and isinstance(cls, dict):
                    cat_key = cls.get("category", "")
                    if cat_key:
                        cat_counts[cat_key] = cat_counts.get(cat_key, 0) + 1
                        if len(cat_examples.get(cat_key, [])) < 3:
                            orig = i.get("original", {})
                            example = orig.get("url", orig.get("title", ""))[:60]
                            if example:
                                cat_examples.setdefault(cat_key, []).append(example)

            for cat_key, count in cat_counts.items():
                if cat_key not in cats:
                    display_name = cat_key.replace("_", " ").replace("-", " & ").title()
                    cats[cat_key] = {
                        "name": display_name,
                        "description": "",
                        "parent": None,
                        "subcategories": [],
                        "item_count": count,
                        "examples": cat_examples.get(cat_key, []),
                    }
                else:
                    # Update counts and examples on existing entries
                    cats[cat_key]["item_count"] = count
                    if not cats[cat_key].get("examples"):
                        cats[cat_key]["examples"] = cat_examples.get(cat_key, [])
                    # Ensure hierarchy fields exist
                    cats[cat_key].setdefault("parent", None)
                    cats[cat_key].setdefault("subcategories", [])

            # ── Auto-infer hierarchy from slash-delimited names ──
            # e.g. "Tech/Linux" → parent="Tech", child="Tech/Linux"
            all_keys = set(cats.keys())
            for key in list(all_keys):
                if "/" in key:
                    parts = key.split("/", 1)
                    parent_key = parts[0]
                    # Only link if parent exists as a standalone category
                    if parent_key in cats and parent_key != key:
                        cats[key]["parent"] = parent_key
                        if key not in cats[parent_key].get("subcategories", []):
                            cats[parent_key].setdefault("subcategories", []).append(key)

            existing_tax["categories"] = cats
            existing_tax["version"] = existing_tax.get("version", 0) + 1
            taxonomy_path.write_text(_json.dumps(existing_tax, indent=2), encoding="utf-8")
        except Exception as tax_err:
            log.warning(f"  Failed to update taxonomy.json: {tax_err}")

        remaining = len(items) - total_classified
        next_start = max(int(c.get("idx", 0)) for c in classifications) + 1 if classifications else 0

        # ── Auto-update graph node so the model doesn't waste a tool call ──
        conv_id = _current_conversation_id
        task_node_id = ""
        if conv_id and conv_id in _graph_store:
            for n in _graph_store[conv_id]["nodes"]:
                meta = n.get("metadata", {})
                if n["type"] == "fact" and meta.get("node_role") == "organize_task":
                    task_node_id = n["id"]
                    meta["classified"] = str(total_classified)
                    meta["uncertain"] = str(total_uncertain)
                    meta["next_batch_start"] = str(next_start)
                    n["metadata"] = meta
                    log.info(f"  Auto-updated graph task node: classified={total_classified}, next={next_start}")
                    break

            # ── Auto-create category nodes for any new categories ──
            # Also update item_count on existing category nodes
            cat_counts: dict[str, int] = {}
            for it in items:
                cls = it.get("classification")
                if cls and isinstance(cls, dict):
                    ck = cls.get("category", "")
                    if ck:
                        cat_counts[ck] = cat_counts.get(ck, 0) + 1

            existing_cats = {}
            for n in _graph_store[conv_id]["nodes"]:
                if n.get("metadata", {}).get("node_role") == "organize_category":
                    existing_cats[n["metadata"].get("category_key", "")] = n

            for ck, count in cat_counts.items():
                if ck in existing_cats:
                    # Update item count
                    existing_cats[ck]["metadata"]["item_count"] = str(count)
                else:
                    # Create new category node, connected to task
                    display = ck.replace("_", " ").replace("-", " & ").title()
                    new_id = _graph_add_node(
                        node_type="fact",
                        title=display,
                        body=f"Items categorized as {display}",
                        source_tool="organize_batch_write",
                        metadata={
                            "node_role": "organize_category",
                            "category_key": ck,
                            "item_count": str(count),
                            "confidence": "high",
                        },
                        dedup_key=f"organize_cat::{ck}",
                    )
                    if new_id and task_node_id:
                        _graph_add_edge(task_node_id, new_id, "has_category")
                        log.info(f"  Auto-created category node: {display} ({count} items)")
                    # Also connect category to the source file (items.json)
                    # so bookmarks in graph are visually linked to their data file
                    if new_id:
                        task_meta = {}
                        for n in _graph_store[conv_id]["nodes"]:
                            if n["id"] == task_node_id:
                                task_meta = n.get("metadata", {})
                                break
                        sd_path = task_meta.get("state_dir", "")
                        if sd_path:
                            sd = _pathlib.Path(sd_path).expanduser()
                            for fname in ["items.json", "state.json"]:
                                for n in _graph_store[conv_id]["nodes"]:
                                    fpath = str(sd / fname)
                                    if n["type"] == "file" and (n.get("body", "") == fpath or n.get("title", "") == fname):
                                        _graph_add_edge(new_id, n["id"], "extracted_from", note=f"{count} items from {fname}")
                                        break

            _graph_save_to_disk(conv_id)

        result = f"OK {total_classified}/{len(items)} classified, {remaining} left."

        # ── Category explosion guard ──
        # Count leaf categories (those with no subcategories)
        try:
            tax_data = _json.loads(taxonomy_path.read_text(encoding="utf-8")) if taxonomy_path.exists() else {}
            all_cats = tax_data.get("categories", {})
            leaf_cats = [k for k, v in all_cats.items()
                         if not v.get("subcategories")]
            if len(leaf_cats) > 40:
                result += (
                    f"\n⚠️ CATEGORY_EXPLOSION: {len(leaf_cats)} leaf categories "
                    f"(limit: 40). You MUST consolidate before continuing. "
                    f"Merge the smallest categories into parent groups. "
                    f"Target: 15-20 top-level folders with 2-8 subfolders each."
                )
        except Exception:
            pass

        # ── Auto-continue directive for classify phase ──
        if remaining > 0:
            batch_size = min(15, remaining)
            result += f" NEXT: batch_read start={next_start} count={batch_size}"
        else:
            result += " DONE: All classified. Move to review."

        return result
    except Exception as e:
        return f"ERROR: {e}"


def _find_organize_task_node() -> str:
    """Find the organize_task node ID in the current conversation's graph."""
    conv_id = _current_conversation_id
    if not conv_id or conv_id not in _graph_store:
        return ""
    for n in _graph_store[conv_id].get("nodes", []):
        if n.get("metadata", {}).get("node_role") == "organize_task":
            return n["id"]
    return ""


def _exec_organize_update_graph(args: dict) -> str:
    """Update the bulk-organizer graph nodes to reflect current progress.

    This tool updates existing graph nodes (task, phase, category) and
    creates new ones (batch records, new categories). It's the model's
    primary mechanism for keeping the graph state machine in sync with
    disk state after each batch or phase transition.
    """
    action = args.get("action", "")

    if action == "update_task":
        node_id = args.get("node_id", "")
        if not node_id:
            return "ERROR: node_id required for update_task"
        meta_updates = {}
        for key in ["phase", "total_items", "classified", "uncertain",
                     "categories", "next_batch_start", "state_dir"]:
            if key in args:
                meta_updates[key] = args[key]
        body = args.get("body")
        ok = _graph_update_node(node_id, body=body, metadata_updates=meta_updates if meta_updates else None)
        return f"OK: task node updated" if ok else f"ERROR: node {node_id} not found"

    elif action == "update_phase":
        node_id = args.get("node_id", "")
        new_status = args.get("status", "")
        body = args.get("body")
        if not node_id:
            return "ERROR: node_id required"
        meta = {"status": new_status} if new_status else None
        ok = _graph_update_node(node_id, body=body, metadata_updates=meta)
        return f"OK: phase node updated" if ok else f"ERROR: node {node_id} not found"

    elif action == "create_phase":
        title = args.get("title", "")
        phase_name = args.get("phase_name", "")
        status = args.get("status", "pending")
        task_node_id = args.get("task_node_id", "")
        node_id = _graph_add_node(
            node_type="section",
            title=title,
            body=args.get("body", ""),
            source_tool="organize_update_graph",
            metadata={"node_role": "organize_phase", "phase_name": phase_name, "status": status},
        )
        if node_id and task_node_id:
            _graph_add_edge(task_node_id, node_id, "has_phase", note=phase_name)
        return f"OK: phase node created (id={node_id})"

    elif action == "create_task":
        title = args.get("title", "")
        body = args.get("body", "")
        state_dir = args.get("state_dir", "")
        total_items = args.get("total_items", "0")
        # Store the full workflow directive in the body so it survives compaction
        if not body:
            body = (
                f"Organize {total_items} items from {state_dir}. "
                "Phases: ingest → taxonomy → classify → review → execute. "
                "Classify all items using batch_read/batch_write loop. "
                "After all classified, review with ask_user, then generate_plan.py."
            )
        node_id = _graph_add_node(
            node_type="fact",
            title=title or f"Organize {total_items} items",
            body=body,
            source_tool="organize_update_graph",
            metadata={
                "node_role": "organize_task",
                "phase": "ingest",
                "total_items": str(total_items),
                "classified": "0",
                "uncertain": "0",
                "categories": "0",
                "state_dir": state_dir,
            },
        )
        # Link the task node to the source file (items.json / state.json)
        # so it doesn't float disconnected in the asset graph
        if node_id and state_dir:
            conv_id = _current_conversation_id
            if conv_id and conv_id in _graph_store:
                sd = _pathlib.Path(state_dir).expanduser()
                source_files = ["state.json", "items.json"]
                for fname in source_files:
                    fpath = str(sd / fname)
                    for n in _graph_store[conv_id]["nodes"]:
                        if n["type"] == "file" and (n.get("body", "") == fpath or n.get("title", "") == fname):
                            _graph_add_edge(node_id, n["id"], "source_file", note=f"Input data: {fname}")
                            log.info(f"  Linked task node to source file: {fname}")
                            break
        return f"OK: task node created (id={node_id})"

    elif action == "create_category":
        name = args.get("name", "")
        description = args.get("description", "")
        category_key = args.get("category_key", "")
        task_node_id = args.get("task_node_id", "")

        # Auto-find task node if not provided
        if not task_node_id:
            task_node_id = _find_organize_task_node()

        # Better display name: "Development Tools" not "Category: dev"
        display_name = name or category_key.replace("_", " ").replace("-", " & ").title()
        node_id = _graph_add_node(
            node_type="fact",
            title=display_name,
            body=description or f"Items categorized as {display_name}",
            source_tool="organize_update_graph",
            metadata={
                "node_role": "organize_category",
                "category_key": category_key,
                "item_count": "0",
                "confidence": "high",
            },
            dedup_key=f"organize_cat::{category_key}",
        )
        if node_id and task_node_id:
            _graph_add_edge(task_node_id, node_id, "has_category")
            # Connect category to source file for full graph connectivity
            for n in _graph_store.get(_current_conversation_id, {}).get("nodes", []):
                if n["id"] == task_node_id:
                    sd_path = n.get("metadata", {}).get("state_dir", "")
                    if sd_path:
                        sd = _pathlib.Path(sd_path).expanduser()
                        for fname in ["items.json", "state.json"]:
                            fpath = str(sd / fname)
                            for fn in _graph_store[_current_conversation_id]["nodes"]:
                                if fn["type"] == "file" and (fn.get("body", "") == fpath or fn.get("title", "") == fname):
                                    _graph_add_edge(node_id, fn["id"], "extracted_from", note=f"Category from {fname}")
                                    break
                    break
        return f"OK: category node created (id={node_id})"

    elif action == "update_category":
        node_id = args.get("node_id", "")
        meta_updates = {}
        for key in ["item_count", "confidence"]:
            if key in args:
                meta_updates[key] = args[key]
        body = args.get("body")
        ok = _graph_update_node(node_id, body=body, metadata_updates=meta_updates if meta_updates else None)
        return f"OK: category updated" if ok else f"ERROR: node {node_id} not found"

    elif action == "set_parent":
        # Link a child category node to a parent category node.
        # Creates a "parent_of" edge from parent → child in the graph.
        child_node_id = args.get("child_node_id", "")
        parent_node_id = args.get("parent_node_id", "")
        child_key = args.get("child_category_key", "")
        parent_key = args.get("parent_category_key", "")

        conv_id = _current_conversation_id
        if not conv_id or conv_id not in _graph_store:
            return "ERROR: no graph store for current conversation"

        nodes = _graph_store[conv_id]["nodes"]

        # Resolve node IDs from category keys if not provided directly
        if not parent_node_id and parent_key:
            for n in nodes:
                if (n.get("metadata", {}).get("node_role") == "organize_category"
                        and n.get("metadata", {}).get("category_key") == parent_key):
                    parent_node_id = n["id"]
                    break
        if not child_node_id and child_key:
            for n in nodes:
                if (n.get("metadata", {}).get("node_role") == "organize_category"
                        and n.get("metadata", {}).get("category_key") == child_key):
                    child_node_id = n["id"]
                    break

        if not parent_node_id or not child_node_id:
            return f"ERROR: could not resolve nodes. parent={parent_node_id} child={child_node_id}"

        _graph_add_edge(parent_node_id, child_node_id, "parent_of",
                        note=f"{parent_key} → {child_key}")

        # Update child node metadata with parent reference
        for n in nodes:
            if n["id"] == child_node_id:
                n["metadata"]["parent_category"] = parent_key
                break

        log.info(f"[GRAPH] set_parent: {parent_key} → {child_key}")
        return f"OK: parent_of edge created ({parent_key} → {child_key})"

    elif action == "set_parent_bulk":
        # Bulk version: set parent for multiple children at once.
        # mappings: JSON string like [{"parent":"Tech","child":"Tech/Linux"}, ...]
        mappings_raw = args.get("mappings", "")
        if not mappings_raw:
            return "ERROR: mappings required (JSON array of {parent, child})"

        conv_id = _current_conversation_id
        if not conv_id or conv_id not in _graph_store:
            return "ERROR: no graph store for current conversation"

        try:
            mappings = _json.loads(mappings_raw) if isinstance(mappings_raw, str) else mappings_raw
        except Exception as e:
            return f"ERROR: invalid mappings JSON: {e}"

        nodes = _graph_store[conv_id]["nodes"]
        # Build category_key → node_id index
        key_to_id: dict[str, str] = {}
        for n in nodes:
            meta = n.get("metadata", {})
            if meta.get("node_role") == "organize_category":
                ck = meta.get("category_key", "")
                if ck:
                    key_to_id[ck] = n["id"]

        created = 0
        errors = []
        for m in mappings:
            parent_key = m.get("parent", "")
            child_key = m.get("child", "")
            pid = key_to_id.get(parent_key)
            cid = key_to_id.get(child_key)
            if pid and cid:
                _graph_add_edge(pid, cid, "parent_of", note=f"{parent_key} → {child_key}")
                # Update child metadata
                for n in nodes:
                    if n["id"] == cid:
                        n["metadata"]["parent_category"] = parent_key
                        break
                created += 1
            else:
                errors.append(f"{parent_key}→{child_key}: parent={'found' if pid else 'MISSING'}, child={'found' if cid else 'MISSING'}")

        _graph_save_to_disk(conv_id)
        result = f"OK: {created} parent_of edges created"
        if errors:
            result += f". Errors: {'; '.join(errors[:5])}"
        return result

    elif action == "create_batch":
        title = args.get("title", "")
        body = args.get("body", "")
        batch_num = args.get("batch_num", "")
        batch_size = args.get("batch_size", "")
        classified = args.get("classified", "")
        uncertain = args.get("uncertain", "")
        phase_node_id = args.get("phase_node_id", "")
        node_id = _graph_add_node(
            node_type="query",
            title=title,
            body=body,
            source_tool="organize_update_graph",
            metadata={
                "node_role": "organize_batch",
                "batch_num": str(batch_num),
                "batch_size": str(batch_size),
                "classified": str(classified),
                "uncertain": str(uncertain),
            },
        )
        if node_id and phase_node_id:
            _graph_add_edge(phase_node_id, node_id, "has_batch")
        return f"OK: batch node created (id={node_id})"

    else:
        return f"ERROR: Unknown action '{action}'. Valid: create_task, create_phase, update_task, update_phase, create_category, update_category, set_parent, set_parent_bulk, create_batch"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DEEP RESEARCH — via local-deep-research (LDR)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_LDR_VENV = _pathlib.Path.home() / "Library" / "Application Support" / "Clyde" / "services" / "local-deep-research" / ".venv"
_LDR_SETTINGS = _pathlib.Path.home() / "Library" / "Application Support" / "Clyde" / "services" / "local-deep-research" / "settings.toml"


def _exec_deep_research(args: dict) -> str:
    """Run a deep research query via local-deep-research.

    Spawns LDR in a subprocess using Clyde's bundled venv so it uses the
    same model server (llama.cpp / mlx-vlm on port 8810/8814).
    Returns a synthesized report with citations.
    """
    query = args.get("query", "")
    mode = args.get("mode", "quick")  # quick, detailed, report

    if not query:
        return "ERROR: query required"

    if not _LDR_VENV.exists():
        return "ERROR: local-deep-research not installed. Expected venv at: " + str(_LDR_VENV)

    python = str(_LDR_VENV / "bin" / "python")

    # Build LDR invocation script — env vars override settings.toml
    func = "quick_summary" if mode == "quick" else "quick_query"
    escaped_query = query.replace("\\", "\\\\").replace("'", "\\'")
    script = (
        f"import os\\n"
        f"os.environ['SETTINGS_FILE_FOR_DYNACONF'] = '{_LDR_SETTINGS}'\\n"
        f"os.environ['LDR_LLM__PROVIDER'] = 'llamacpp'\\n"
        f"os.environ['LDR_LLM__LLAMACPP__BASE_URL'] = 'http://localhost:8810/v1'\\n"
        f"from local_deep_research.api import {func}\\n"
        f"result = {func}('clyde', 'clyde', '{escaped_query}')\\n"
        f"print(result)"
    )

    import subprocess
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)

    try:
        r = subprocess.run(
            [python, "-c", script.replace("\\n", "\n")],
            capture_output=True, text=True,
            timeout=300,  # 5 min max for research
            env=env,
            cwd=str(_LDR_VENV.parent),
        )
    except subprocess.TimeoutExpired:
        return "ERROR: Research timed out after 5 minutes. Try a more specific query."
    except Exception as e:
        return f"ERROR: deep_research failed: {e}"

    if r.returncode != 0:
        stderr_tail = (r.stderr or "")[-500:]
        return f"ERROR: LDR exited {r.returncode}\\n{stderr_tail}"

    result = (r.stdout or "").strip()
    if not result:
        return "ERROR: LDR returned empty result"

    # Create graph node for research result
    conv_id = _current_conversation_id
    if conv_id:
        node_id = _graph_add_node(
            node_type="fact",
            title=f"Research: {query[:80]}",
            body=result[:500],
            source_tool="deep_research",
            metadata={
                "node_role": "research_report",
                "query": query[:200],
                "mode": mode,
                "full_length": str(len(result)),
            },
        )
        # Link to request node
        if conv_id in _graph_store:
            for n in _graph_store[conv_id]["nodes"]:
                if n.get("metadata", {}).get("node_role") in ("request", "organize_request"):
                    _graph_add_edge(node_id, n["id"], "researched_for")
                    break

    # Truncate if too long for context
    if len(result) > 8000:
        result = result[:8000] + f"\\n\\n[... truncated, full report is {len(result)} chars]"

    return result


# ORGANIZE SKILL v3 — Compound tools (replace v2 batch_read/write)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _exec_organize_ingest(args: dict) -> str:
    """Parse source file, dedup, sample, create graph nodes. Returns samples for taxonomy."""
    source_path = args.get("source_path", "")
    item_type = args.get("item_type", "bookmarks")

    if not source_path:
        return "ERROR: source_path required (path to bookmarks HTML, directory, etc.)"

    resolved = _resolve_path(source_path)
    if not resolved.exists():
        return f"ERROR: Source not found: {resolved}"

    # Determine state_dir (sibling of source or ~/organize-state/<name>)
    state_dir = _pathlib.Path.home() / "organize-state" / resolved.stem
    state_dir.mkdir(parents=True, exist_ok=True)

    # Parse based on item_type
    if item_type == "bookmarks":
        # Use existing parse_bookmarks script
        result = _exec_parse_bookmarks({
            "html_path": str(resolved),
            "state_dir": str(state_dir),
        })
        if "ERROR" in result:
            return result
    elif item_type == "files":
        # Walk directory, create state.json with file entries
        items = []
        for root, dirs, files in os.walk(str(resolved)):
            for fname in files:
                if fname.startswith("."):
                    continue
                fpath = _pathlib.Path(root) / fname
                items.append({
                    "title": fname,
                    "url": str(fpath),
                    "original": {"title": fname, "url": str(fpath), "path": str(fpath)},
                    "status": "unclassified",
                })
        state = {"items": items, "phase": "ingest", "source": str(resolved)}
        sp = state_dir / "state.json"
        sp.write_text(_json.dumps(state, indent=2), encoding="utf-8")
    else:
        return f"ERROR: Unsupported item_type '{item_type}'. Use: bookmarks, files"

    # Load state to get item count
    sp = state_dir / "state.json"
    if not sp.exists():
        return f"ERROR: Parser didn't create state.json at {state_dir}"
    state = _json.loads(sp.read_text(encoding="utf-8"))
    items = state.get("items", [])
    total = len(items)

    if total == 0:
        return "ERROR: No items parsed from source."

    # ── Sample 5 batches from different positions ──
    sample_positions = [0, total // 5, 2 * total // 5, 3 * total // 5, 4 * total // 5]
    sample_size = min(15, total)
    samples = []
    for pos in sample_positions:
        end = min(pos + sample_size, total)
        for i in range(pos, end):
            orig = items[i].get("original", {})
            title = (orig.get("title") or items[i].get("title") or "untitled")[:60]
            url = (orig.get("url") or items[i].get("url") or "")[:60]
            samples.append(f"  {i}: {title} | {url}")
        if len(samples) >= 75:
            break

    # ── Create graph nodes ──
    conv_id = _current_conversation_id
    source_node_id = ""
    if conv_id:
        source_node_id = _graph_add_node(
            node_type="fact",
            title=f"Source: {resolved.name}",
            body=f"{total} items parsed from {item_type}",
            source_tool="organize_ingest",
            metadata={
                "node_role": "organize_source",
                "path": str(resolved),
                "item_type": item_type,
                "item_count": str(total),
                "state_dir": str(state_dir),
            },
        )
        # Link source to request node
        if conv_id in _graph_store:
            for n in _graph_store[conv_id]["nodes"]:
                if n.get("metadata", {}).get("node_role") in ("organize_request", "request"):
                    _graph_add_edge(source_node_id, n["id"], "sourced_from")
                    break

    return (
        f"INGESTED: {total} items from {item_type} ({resolved.name})\n"
        f"Source node: {source_node_id}\n"
        f"State dir: {state_dir}\n\n"
        f"Samples ({len(samples)} items from 5 positions):\n"
        + "\n".join(samples) + "\n\n"
        f"YOUR TASK: Propose 8-15 categories based on these samples.\n"
        f"Call organize_propose_taxonomy with your categories."
    )


def _exec_organize_propose_taxonomy(args: dict) -> str:
    """Create taxonomy, ask user for approval, create graph nodes."""
    categories_raw = args.get("categories", "")
    state_dir_raw = args.get("state_dir", "")

    if not categories_raw:
        return "ERROR: categories required (JSON array of {key, name, description})"

    # Find state_dir from args or graph
    state_dir = None
    if state_dir_raw:
        state_dir = _pathlib.Path(state_dir_raw).expanduser()
    else:
        conv_id = _current_conversation_id
        if conv_id and conv_id in _graph_store:
            for n in _graph_store[conv_id]["nodes"]:
                sd = n.get("metadata", {}).get("state_dir")
                if sd and n.get("metadata", {}).get("node_role") == "organize_source":
                    state_dir = _pathlib.Path(sd)
                    break
    if not state_dir or not (state_dir / "state.json").exists():
        return "ERROR: Can't find state_dir. Pass state_dir explicitly or run organize_ingest first."

    try:
        categories = _json.loads(categories_raw)
    except Exception as e:
        return f"ERROR: Invalid JSON for categories: {e}"

    if not isinstance(categories, list) or len(categories) == 0:
        return "ERROR: categories must be a non-empty JSON array"

    # Build taxonomy display for ask_user
    cat_display = []
    for c in categories:
        key = c.get("key", "")
        name = c.get("name", key)
        desc = c.get("description", "")
        cat_display.append(f"  {key}: {name} — {desc}")

    question = (
        f"Here's my proposed folder structure for your items ({len(categories)} categories):\n\n"
        + "\n".join(cat_display)
        + "\n\nDoes this look good? You can:\n"
        "- Say 'yes' or 'looks good' to approve\n"
        "- Suggest changes (merge, rename, add, remove categories)\n"
        "- Say 'no' to reject and I'll propose again"
    )

    # ── Block for user answer (no timeout) ──
    import uuid as _uuid
    import threading
    question_id = f"q_{_uuid.uuid4().hex[:8]}"

    # Access the runtime's question event mechanism
    # We need to get the current runtime to use its event/answer mechanism
    conv_id = _current_conversation_id
    runtime = None
    try:
        import agent as _agent_mod
        if conv_id and hasattr(_agent_mod, '_runtimes'):
            runtime = _agent_mod._runtimes.get(conv_id)
        if not runtime and hasattr(_agent_mod, '_runtime'):
            runtime = _agent_mod._runtime
    except Exception:
        pass

    user_answer = "yes"  # default if can't get runtime
    if runtime and hasattr(runtime, '_question_event'):
        # Emit question event via the runtime's emit callback
        if hasattr(runtime, '_current_emit') and runtime._current_emit:
            runtime._current_emit("question",
                                   id=question_id,
                                   question=question,
                                   choices=["Looks good!", "Merge some categories", "Start over"],
                                   allow_other=True)

        runtime._pending_question_id = question_id
        runtime._question_event.clear()
        log.info(f"  organize_propose_taxonomy: blocking for user approval (no timeout)")

        # Wait with KV keepalive
        answered = False
        while not answered:
            answered = runtime._question_event.wait(timeout=60.0)
            if not answered:
                try:
                    import httpx
                    httpx.get(f"{runtime.backend_url}/v1/models", timeout=2.0)
                except Exception:
                    pass

        if answered and runtime._pending_answer:
            user_answer = runtime._pending_answer
        else:
            user_answer = "approved (no response)"

        runtime._pending_question_id = None
        runtime._pending_answer = None
    else:
        log.warning("  organize_propose_taxonomy: no runtime found, auto-approving")

    # ── Create graph nodes ──
    taxonomy_node_id = ""
    decision_node_id = ""
    category_node_ids = {}

    if conv_id:
        # Taxonomy node
        taxonomy_node_id = _graph_add_node(
            node_type="fact",
            title=f"Taxonomy ({len(categories)} categories)",
            body=", ".join(c.get("name", c.get("key", "")) for c in categories),
            source_tool="organize_propose_taxonomy",
            metadata={
                "node_role": "organize_taxonomy",
                "category_count": str(len(categories)),
                "version": "1",
                "approved": "true",
            },
        )

        # Link taxonomy to request
        if conv_id in _graph_store:
            for n in _graph_store[conv_id]["nodes"]:
                if n.get("metadata", {}).get("node_role") in ("organize_request", "request"):
                    _graph_add_edge(n["id"], taxonomy_node_id, "has_taxonomy")
                    break

        # Category nodes
        for c in categories:
            key = c.get("key", "")
            name = c.get("name", key)
            desc = c.get("description", "")
            cat_id = _graph_add_node(
                node_type="fact",
                title=name,
                body=desc,
                source_tool="organize_propose_taxonomy",
                metadata={
                    "node_role": "organize_category",
                    "category_key": key,
                    "item_count": "0",
                },
            )
            category_node_ids[key] = cat_id
            _graph_add_edge(taxonomy_node_id, cat_id, "has_category")

        # Decision node
        decision_node_id = _graph_add_node(
            node_type="fact",
            title=f"User decision: taxonomy",
            body=user_answer[:500],
            source_tool="organize_propose_taxonomy",
            metadata={
                "node_role": "organize_decision",
                "question": "taxonomy approval",
                "answer": user_answer[:200],
            },
        )
        _graph_add_edge(taxonomy_node_id, decision_node_id, "decided_by")

    # Save taxonomy to disk
    taxonomy_data = {"categories": {c["key"]: c for c in categories}, "version": 1}
    tax_path = state_dir / "taxonomy.json"
    tax_path.write_text(_json.dumps(taxonomy_data, indent=2), encoding="utf-8")

    # Update state phase
    sp = state_dir / "state.json"
    if sp.exists():
        state = _json.loads(sp.read_text(encoding="utf-8"))
        state["phase"] = "classify"
        sp.write_text(_json.dumps(state, indent=2), encoding="utf-8")

    # Build response
    cat_list = "\n".join(f"  {c['key']} [{category_node_ids.get(c['key'], '?')}]: {c.get('name', c['key'])}"
                         for c in categories)

    return (
        f"TAXONOMY {'APPROVED' if 'yes' in user_answer.lower() or 'good' in user_answer.lower() or 'approved' in user_answer.lower() else 'USER SAID: ' + user_answer[:100]}\n"
        f"Categories ({len(categories)}):\n{cat_list}\n\n"
        f"YOUR TASK: Call organize_classify_batch to start classifying.\n"
        f"Pass state_dir=\"{state_dir}\""
    )


def _exec_organize_classify_batch(args: dict) -> str:
    """Read batch + write previous classifications + update graph. Atomic read-write-progress."""
    state_dir_raw = args.get("state_dir", "")
    classifications_raw = args.get("classifications", "")
    start_raw = args.get("start", "")

    # Find state_dir
    state_dir = None
    if state_dir_raw:
        state_dir = _pathlib.Path(state_dir_raw).expanduser()
    else:
        conv_id = _current_conversation_id
        if conv_id and conv_id in _graph_store:
            for n in _graph_store[conv_id]["nodes"]:
                if n.get("metadata", {}).get("node_role") == "organize_source":
                    state_dir = _pathlib.Path(n["metadata"]["state_dir"])
                    break
    if not state_dir:
        return "ERROR: state_dir required"

    perm_error = _check_organize_permission(str(state_dir))
    if perm_error:
        return perm_error

    sp = state_dir / "state.json"
    if not sp.exists():
        return f"ERROR: No state.json at {state_dir}"

    state = _json.loads(sp.read_text(encoding="utf-8"))
    items = state.get("items", [])
    total = len(items)

    # Load taxonomy for category validation
    tax_path = state_dir / "taxonomy.json"
    valid_categories = set()
    if tax_path.exists():
        tax = _json.loads(tax_path.read_text(encoding="utf-8"))
        valid_categories = set(tax.get("categories", {}).keys())

    conv_id = _current_conversation_id
    wrote_count = 0
    wrote_summary = {}

    # ── STEP 1: Write classifications if provided ──
    if classifications_raw:
        try:
            classifications = _json.loads(classifications_raw)
        except Exception as e:
            return f"ERROR: Invalid JSON for classifications: {e}"

        for c in classifications:
            idx = int(c.get("idx", -1))
            cat = c.get("category", "")
            conf = c.get("confidence", "medium")
            if 0 <= idx < total and cat:
                if valid_categories and cat not in valid_categories:
                    log.warning(f"  classify: unknown category '{cat}' for idx {idx}, accepting anyway")
                items[idx]["classification"] = {"category": cat, "confidence": conf}
                items[idx]["status"] = "classified"
                wrote_count += 1
                wrote_summary[cat] = wrote_summary.get(cat, 0) + 1

        # Save state
        total_classified = sum(1 for i in items if i.get("status") == "classified")
        state["items"] = items
        state["classified"] = total_classified
        state["phase"] = "classify"

        tmp = sp.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(state, indent=2), encoding="utf-8")
        tmp.rename(sp)

        # Update taxonomy counts
        if tax_path.exists():
            _update_taxonomy_counts(items, tax_path)

        # ── Create PROGRESS node in graph ──
        if conv_id and wrote_count > 0:
            batch_start = min(int(c.get("idx", 0)) for c in classifications)
            batch_end = max(int(c.get("idx", 0)) for c in classifications)
            progress_id = _graph_add_node(
                node_type="fact",
                title=f"Batch {batch_start}-{batch_end} classified",
                body=f"{wrote_count} items: " + ", ".join(f"{k}:{v}" for k, v in wrote_summary.items()),
                source_tool="organize_classify_batch",
                metadata={
                    "node_role": "organize_progress",
                    "batch_start": str(batch_start),
                    "batch_end": str(batch_end),
                    "classified_count": str(wrote_count),
                    "total_classified": str(total_classified),
                    "total": str(total),
                },
            )
            # Link progress to request
            if conv_id in _graph_store:
                for n in _graph_store[conv_id]["nodes"]:
                    if n.get("metadata", {}).get("node_role") in ("organize_request", "request"):
                        _graph_add_edge(n["id"], progress_id, "batch_progress")
                        break
                # Link progress to categories used
                for cat_key in wrote_summary:
                    for n in _graph_store[conv_id]["nodes"]:
                        if (n.get("metadata", {}).get("node_role") == "organize_category"
                                and n.get("metadata", {}).get("category_key") == cat_key):
                            _graph_add_edge(progress_id, n["id"], "classified_into")
                            # Update category item_count
                            cat_total = sum(1 for i in items
                                            if (i.get("classification") or {}).get("category") == cat_key)
                            n["metadata"]["item_count"] = str(cat_total)
                            break

    # ── STEP 2: Determine next batch to read ──
    total_classified = sum(1 for i in items if i.get("status") == "classified")
    remaining = total - total_classified

    if remaining <= 0:
        # All done!
        return (
            f"{'WROTE: ' + str(wrote_count) + ' items. ' if wrote_count else ''}"
            f"CLASSIFY COMPLETE: All {total} items classified!\n\n"
            f"Category distribution:\n"
            + _category_distribution(items)
            + f"\n\nYOUR TASK: Call organize_export to generate the output file."
        )

    # Find next unclassified batch
    if start_raw:
        next_start = int(start_raw)
    else:
        # Find first unclassified item
        next_start = 0
        for i, item in enumerate(items):
            if item.get("status") != "classified":
                next_start = i
                break

    batch_size = min(15, remaining)
    end = min(next_start + batch_size, total)
    batch = items[next_start:end]

    # Build batch display
    lines = [f"BATCH {next_start}-{end-1} of {total} (showing {len(batch)}):"]
    if valid_categories:
        lines.append(f"CATEGORIES: {', '.join(sorted(valid_categories))}")
    for i, item in enumerate(batch):
        idx = next_start + i
        cls = item.get("classification") or {}
        if cls.get("category"):
            lines.append(f"  {idx}: [{cls['category']}] (already classified)")
        else:
            orig = item.get("original", {})
            title = (orig.get("title") or item.get("title") or "untitled")[:60]
            url = (orig.get("url") or item.get("url") or "")[:60]
            lines.append(f"  {idx}: {title} | {url}")

    wrote_msg = f"WROTE: {wrote_count} items ({', '.join(f'{k}:{v}' for k, v in wrote_summary.items())})\n" if wrote_count else ""
    progress_msg = f"PROGRESS: {total_classified}/{total} ({100*total_classified//total}%) — {remaining} remaining\n\n"

    return (
        wrote_msg
        + progress_msg
        + "\n".join(lines) + "\n\n"
        f"YOUR TASK: Classify each unclassified item above.\n"
        f"Call organize_classify_batch state_dir=\"{state_dir}\" with classifications JSON.\n"
        f'Format: classifications=\'[{{"idx":{next_start},"category":"cat_key","confidence":"high"}}]\''
    )


def _exec_organize_export(args: dict) -> str:
    """Generate output file from classifications, create artifact graph node."""
    state_dir_raw = args.get("state_dir", "")
    output_format = args.get("format", "netscape_html")
    output_path_raw = args.get("output_path", "")

    state_dir = _pathlib.Path(state_dir_raw).expanduser() if state_dir_raw else None
    if not state_dir:
        conv_id = _current_conversation_id
        if conv_id and conv_id in _graph_store:
            for n in _graph_store[conv_id]["nodes"]:
                if n.get("metadata", {}).get("node_role") == "organize_source":
                    state_dir = _pathlib.Path(n["metadata"]["state_dir"])
                    break
    if not state_dir or not (state_dir / "state.json").exists():
        return "ERROR: state_dir required or run organize_ingest first"

    state = _json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    items = state.get("items", [])

    # Default output path
    if not output_path_raw:
        output_path_raw = str(_pathlib.Path.home() / "Desktop" / f"organized_{state_dir.name}.html")
    output_path = _pathlib.Path(output_path_raw).expanduser()

    # Group by category
    by_cat: dict[str, list] = {}
    uncategorized = []
    for item in items:
        cls = item.get("classification") or {}
        cat = cls.get("category", "")
        if cat:
            by_cat.setdefault(cat, []).append(item)
        else:
            uncategorized.append(item)

    # Load taxonomy for display names
    tax_path = state_dir / "taxonomy.json"
    cat_names = {}
    if tax_path.exists():
        tax = _json.loads(tax_path.read_text(encoding="utf-8"))
        for k, v in tax.get("categories", {}).items():
            cat_names[k] = v.get("name", k)

    if output_format == "netscape_html":
        lines = [
            '<!DOCTYPE NETSCAPE-Bookmark-file-1>',
            '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
            '<TITLE>Bookmarks</TITLE>',
            '<H1>Bookmarks</H1>',
            '<DL><p>',
        ]
        for cat_key in sorted(by_cat.keys()):
            display = cat_names.get(cat_key, cat_key.replace("_", " ").title())
            lines.append(f'    <DT><H3 PERSONAL_TOOLBAR_FOLDER="false">{display}</H3>')
            lines.append('    <DL><p>')
            for item in by_cat[cat_key]:
                orig = item.get("original", {})
                title = orig.get("title", item.get("title", ""))
                url = orig.get("url", item.get("url", ""))
                lines.append(f'        <DT><A HREF="{url}">{title}</A>')
            lines.append('    </DL><p>')
        if uncategorized:
            lines.append('    <DT><H3>Uncategorized</H3>')
            lines.append('    <DL><p>')
            for item in uncategorized:
                orig = item.get("original", {})
                title = orig.get("title", "")
                url = orig.get("url", "")
                lines.append(f'        <DT><A HREF="{url}">{title}</A>')
            lines.append('    </DL><p>')
        lines.append('</DL><p>')
        output_path.write_text("\n".join(lines), encoding="utf-8")
    elif output_format == "json":
        export = {cat: [{"title": i.get("original", {}).get("title", ""),
                         "url": i.get("original", {}).get("url", "")}
                        for i in items_list]
                  for cat, items_list in by_cat.items()}
        output_path.write_text(_json.dumps(export, indent=2), encoding="utf-8")
    else:
        return f"ERROR: Unknown format '{output_format}'. Use: netscape_html, json"

    # ── Create artifact graph node ──
    conv_id = _current_conversation_id
    if conv_id:
        artifact_id = _graph_add_node(
            node_type="fact",
            title=f"Export: {output_path.name}",
            body=f"{len(items)} items in {len(by_cat)} categories",
            source_tool="organize_export",
            metadata={
                "node_role": "organize_artifact",
                "path": str(output_path),
                "format": output_format,
                "item_count": str(len(items)),
                "category_count": str(len(by_cat)),
            },
        )
        # Link to request
        if conv_id in _graph_store:
            for n in _graph_store[conv_id]["nodes"]:
                if n.get("metadata", {}).get("node_role") in ("organize_request", "request"):
                    _graph_add_edge(n["id"], artifact_id, "produced")
                    break
            # Link to each category
            for n in _graph_store[conv_id]["nodes"]:
                if n.get("metadata", {}).get("node_role") == "organize_category":
                    _graph_add_edge(artifact_id, n["id"], "exported_from")

    dist = _category_distribution(items)
    return (
        f"EXPORTED: {len(items)} items to {output_path}\n"
        f"Format: {output_format}\n"
        f"Distribution:\n{dist}\n\n"
        f"File ready for user review."
    )


def _category_distribution(items: list) -> str:
    """Helper: build category distribution string from items."""
    counts: dict[str, int] = {}
    for i in items:
        cat = (i.get("classification") or {}).get("category", "")
        if cat:
            counts[cat] = counts.get(cat, 0) + 1
    lines = [f"  {k}: {v} items" for k, v in sorted(counts.items(), key=lambda x: -x[1])]
    return "\n".join(lines) if lines else "  (no classifications)"


def _update_taxonomy_counts(items: list, tax_path: _pathlib.Path) -> None:
    """Helper: update item counts in taxonomy.json from state items."""
    try:
        tax = _json.loads(tax_path.read_text(encoding="utf-8"))
        cats = tax.get("categories", {})
        for k in cats:
            cats[k]["item_count"] = sum(
                1 for i in items
                if (i.get("classification") or {}).get("category") == k
            )
        tax_path.write_text(_json.dumps(tax, indent=2), encoding="utf-8")
    except Exception:
        pass


# ─── MVP Tool Specs (aligned with claw-code mvp_tool_specs) ───

def mvp_tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="bash",
            description="Execute a shell command in the user's home directory. Returns stdout and stderr.",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "minimum": 1, "description": "Timeout in seconds (default 30, max 120)"},
                },
                "required": ["command"],
                "additionalProperties": False
            },
            execute=_exec_bash,
        ),
        ToolSpec(
            name="read_file",
            description="Read a text file and return its contents with line numbers. If path is a directory, lists its entries. Reads up to 2000 lines by default — do NOT set limit unless you specifically need a narrow range of a huge file. IMPORTANT: Always use absolute paths (starting with ~ or /Users/). Never use bare filenames or relative paths — they will fail.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path (e.g. ~/Desktop/file.txt or $HOME/file.txt). Never use bare filenames."},
                    "offset": {"type": "integer", "minimum": 0, "description": "Starting line number (0-based). Only use for very large files when you need a specific section."},
                    "limit": {"type": "integer", "minimum": 50, "description": "Max lines to read. Default 2000 — do NOT set this unless reading a specific range of a very large file. Minimum 50."},
                },
                "required": ["path"],
                "additionalProperties": False
            },
            execute=_exec_read_file,
        ),
        ToolSpec(
            name="write_file",
            description="Write a text file. Creates parent directories if needed.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path (e.g. ~/Desktop/file.txt). Never use bare filenames."},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
                "additionalProperties": False
            },
            execute=_exec_write_file,
        ),
        ToolSpec(
            name="edit_file",
            description="Find and replace text in a file. old_string must match exactly once unless replace_all is true.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "description": "Exact string to find"},
                    "new_string": {"type": "string", "description": "Replacement string"},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)"},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False
            },
            execute=_exec_edit_file,
        ),
        ToolSpec(
            name="glob_search",
            description=(
                "Find files by glob pattern. Returns matching file paths. "
                "IMPORTANT: the search prunes common noise dirs (Library, .Trash, "
                ".cache, .git, node_modules, .venv, __pycache__, DerivedData, "
                "build, dist, target, Caches, Logs) for speed. To search INSIDE "
                "one of those, pass it as `path` — e.g. for app data like Zen, "
                "Firefox, Chrome, Slack, Discord, Notes, etc., use "
                "path='~/Library/Application Support' and a narrow pattern. "
                "For browser bookmarks / history / cookies / profiles, the "
                "typical locations are: Zen → ~/Library/Application Support/zen, "
                "Firefox → ~/Library/Application Support/Firefox, Chrome → "
                "~/Library/Application Support/Google/Chrome, Safari → "
                "~/Library/Safari. Results capped at 200; wall-clock limited to 8s."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py', 'bookmarks*', 'zen/**')"},
                    "path": {"type": "string", "description": "Base directory (default ~). For app data use ~/Library/Application Support or a narrower subdir."},
                },
                "required": ["pattern"],
                "additionalProperties": False
            },
            execute=_exec_glob_search,
        ),
        ToolSpec(
            name="bookmark_locator",
            description=(
                "Find browser bookmark files on this Mac in ONE call. Checks the "
                "canonical macOS paths for Safari (Bookmarks.plist), Chromium-family "
                "(Chrome, Edge, Brave, Arc, Vivaldi, Opera — JSON Bookmarks file under "
                "each profile), and Firefox-family (Firefox, Zen, LibreWolf, Waterfox — "
                "places.sqlite under each profile). Returns only the paths that EXIST. "
                "Use this BEFORE glob/grep when the user mentions bookmarks — saves "
                "many tool calls and avoids dead-end searches."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            },
            execute=_exec_bookmark_locator,
        ),
        ToolSpec(
            name="bookmark_extract_all",
            description=(
                "Find every browser bookmark file on this Mac, copy each to a "
                "fresh working directory under /tmp, AND convert them all to "
                "Netscape HTML (the format `parse_bookmarks.py` expects). One "
                "call, no bash, no path typos, no SQLite locking issues. "
                "Skips Safari if Full Disk Access isn't granted (and tells you "
                "so). Returns the working directory path and a per-browser "
                "summary. Use this for 'organize my bookmarks' tasks instead "
                "of stringing together bookmark_locator + bash cp + bash "
                "python3 — it's one round-trip vs ten."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "out_dir": {
                        "type": "string",
                        "description": "Optional working dir; defaults to /tmp/clyde-bookmarks-<timestamp>.",
                    },
                },
                "additionalProperties": False
            },
            execute=_exec_bookmark_extract_all,
        ),
        ToolSpec(
            name="graph_read",
            description=(
                "Read the asset graph (agent's external working memory). "
                "Returns REQUEST (original user requirements), PHASE "
                "(progress), ARTIFACT (outputs), SOURCE (URLs/files "
                "consulted), and FACT nodes. Always call this FIRST after "
                "compaction to re-orient. Call before `verify` to check "
                "whether every requirement is satisfied."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "enum": ["request", "phase", "artifact", "source", "fact"],
                        "description": "Filter to a single node role. Omit for all."
                    },
                    "request_id": {
                        "type": "string",
                        "description": "Scope the output to one request tree.",
                    },
                },
                "additionalProperties": False,
            },
            execute=_exec_graph_read,
        ),
        ToolSpec(
            name="graph_write",
            description=(
                "Write to the asset graph. Actions: "
                "`create_request` (root task + requirements), "
                "`create_phase` (work phase under a request), "
                "`update_phase` (status / progress / next_batch_start), "
                "`create_artifact` (output, link to requirement via "
                "`satisfies`), `create_source` (URL/file you consulted), "
                "`link_fact_to_source` (edge fact→source), `verify` "
                "(check every requirement is satisfied)."
            ),
            input_schema={
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "create_request", "create_phase", "update_phase",
                            "create_artifact", "create_source",
                            "link_fact_to_source", "verify",
                        ],
                    },
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "requirements": {
                        "type": "object",
                        "description": "create_request only — key/value pairs of what the user needs.",
                    },
                    "skill": {"type": "string"},
                    "source_file": {"type": "string"},
                    "request_id": {"type": "string"},
                    "node_id": {"type": "string"},
                    "phase_name": {"type": "string"},
                    "phase_id": {"type": "string"},
                    "depends_on": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "active", "done", "failed"]},
                    "progress_current": {"type": "string"},
                    "progress_total": {"type": "string"},
                    "next_batch_start": {"type": "string"},
                    "state_dir": {"type": "string"},
                    "batch_size": {"type": "string"},
                    "total": {"type": "string"},
                    "artifact_type": {"type": "string"},
                    "satisfies": {"type": "string"},
                    "count": {"type": "string"},
                    "url": {"type": "string"},
                    "path": {"type": "string"},
                    "source_type": {"type": "string"},
                    "fact_id": {"type": "string"},
                    "source_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            execute=_exec_graph_write,
        ),
        ToolSpec(
            name="parse_bookmarks",
            description=(
                "Parse a Netscape-HTML bookmark file into the bulk-organizer "
                "state. Do NOT shell out to python3 for this — call this "
                "tool directly. Handles path quoting, PYTHONHOME, and "
                "interpreter selection so it never fails with 'cannot "
                "find platform-independent libraries' or shell-quoting "
                "errors. Returns `OK: Parsed N bookmarks (...)` + state_dir "
                "+ the next-step hint (organize_progress / organize_batch_*)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "html_path": {
                        "type": "string",
                        "description": "Path to a Netscape-format bookmark HTML file (absolute or ~-prefixed).",
                    },
                    "state_dir": {
                        "type": "string",
                        "description": "Optional; defaults to the HTML file's parent directory.",
                    },
                },
                "required": ["html_path"],
                "additionalProperties": False,
            },
            execute=_exec_parse_bookmarks,
        ),
        ToolSpec(
            name="grep_search",
            description="Search file contents with a regex pattern. Returns matching lines with file paths and line numbers.",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory to search in"},
                    "-i": {"type": "boolean", "description": "Case insensitive search"},
                    "-C": {"type": "integer", "minimum": 0, "description": "Context lines before and after"},
                    "head_limit": {"type": "integer", "minimum": 1, "description": "Max result lines (default 50)"},
                },
                "required": ["pattern"],
                "additionalProperties": False
            },
            execute=_exec_grep_search,
        ),
        ToolSpec(
            name="memory_read",
            description="Read a specific memory topic file by filename. Memory files contain persistent knowledge across sessions.",
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Memory filename (e.g. 'user_role.md')"},
                },
                "required": ["filename"],
                "additionalProperties": False
            },
            execute=_exec_memory_read,
        ),
        ToolSpec(
            name="memory_write",
            description="Save a new memory. Writes topic file then updates index. Types: user, feedback, project, reference. Do NOT store code patterns, git history, debug solutions, or anything derivable from source.",
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Filename like 'user_role.md'"},
                    "name": {"type": "string", "description": "Short name"},
                    "description": {"type": "string", "description": "One-line description (max ~100 chars, used for relevance matching)"},
                    "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"]},
                    "content": {"type": "string", "description": "Memory content. For feedback/project: include Why + How to apply."},
                },
                "required": ["filename", "name", "description", "type", "content"],
                "additionalProperties": False
            },
            execute=_exec_memory_write,
        ),
        ToolSpec(
            name="memory_update",
            description="Update an existing memory file's content, preserving frontmatter metadata.",
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "content": {"type": "string", "description": "New content body"},
                },
                "required": ["filename", "content"],
                "additionalProperties": False
            },
            execute=_exec_memory_update,
        ),
        ToolSpec(
            name="memory_delete",
            description="Delete a memory file and remove its entry from the index.",
            input_schema={
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                },
                "required": ["filename"],
                "additionalProperties": False
            },
            execute=_exec_memory_delete,
        ),
        ToolSpec(
            name="memory_search",
            description="Search across all memory topic files for a query string. Returns matching lines with filenames.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False
            },
            execute=_exec_memory_search,
        ),
        ToolSpec(
            name="memory_list",
            description="List all memory files with their types and descriptions.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            execute=_exec_memory_list,
        ),
        ToolSpec(
            name="transcript_search",
            description="Search past conversation transcripts. Transcripts are never loaded fully — only grep'd.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False
            },
            execute=_exec_transcript_search,
        ),
        ToolSpec(
            name="get_weather",
            description="Get current weather conditions. Without a location, returns the user's local weather (GPS). With a location, fetches weather for any city worldwide via Open-Meteo. Use for ALL weather questions.",
            input_schema={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City or place name (e.g. 'Tokyo', 'London, UK', 'New York'). Omit for user's current location."
                    },
                },
                "additionalProperties": False
            },
            execute=_exec_get_weather,
        ),
        ToolSpec(
            name="web_search",
            description="Search the web using DuckDuckGo. Returns titles, URLs, and snippets. Use this when the user asks about current events, recent news, or anything you don't know.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Max results (default 8)"},
                },
                "required": ["query"],
                "additionalProperties": False
            },
            execute=_exec_web_search,
        ),
        ToolSpec(
            name="web_fetch",
            description="Fetch and extract text content from a URL. Uses Firecrawl if available, otherwise basic HTTP fetch with HTML stripping. Use after web_search to read a specific page.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "max_chars": {"type": "integer", "minimum": 1000, "maximum": 20000, "description": "Max chars to return (default 8000)"},
                },
                "required": ["url"],
                "additionalProperties": False
            },
            execute=_exec_web_fetch,
        ),
        ToolSpec(
            name="deep_research",
            description="Run autonomous deep research on any topic. Uses 20+ search engines (DuckDuckGo, Wikipedia, arXiv, academic sources), synthesizes findings into a report with citations. Powered by local-deep-research. Use for questions needing multi-source investigation — NOT for simple lookups (use web_search for those).",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Research question or topic"},
                    "mode": {
                        "type": "string",
                        "enum": ["quick", "detailed"],
                        "description": "quick = fast summary (1-2 min), detailed = thorough report (3-5 min)",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            execute=_exec_deep_research,
        ),
        ToolSpec(
            name="create_document",
            description="Create a Word document (.docx). Use when the user asks you to write a report, letter, memo, article, or any document they want to save. Content supports markdown-ish formatting: # headings, - bullets, 1. numbered lists, --- page breaks.",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Document title"},
                    "content": {"type": "string", "description": "Document body. Use # for headings, - for bullets, 1. for numbered lists, --- for page breaks."},
                    "filename": {"type": "string", "description": "Output filename stem (optional, defaults to title)"},
                },
                "required": ["title", "content"],
                "additionalProperties": False
            },
            execute=_exec_create_document,
        ),
        ToolSpec(
            name="create_spreadsheet",
            description="Create an Excel spreadsheet (.xlsx). Use when the user asks for tabular data, budgets, trackers, or any structured data in a spreadsheet format.",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Workbook name"},
                    "sheets": {
                        "type": "array",
                        "description": "List of sheets, each with name, headers, and rows",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Sheet tab name"},
                                "headers": {"type": "array", "items": {"type": "string"}, "description": "Column headers"},
                                "rows": {"type": "array", "items": {"type": "array"}, "description": "Data rows (array of arrays)"},
                            },
                            "required": ["name", "headers", "rows"]
                        }
                    },
                    "filename": {"type": "string", "description": "Output filename stem (optional)"},
                },
                "required": ["title", "sheets"],
                "additionalProperties": False
            },
            execute=_exec_create_spreadsheet,
        ),
        ToolSpec(
            name="create_presentation",
            description="Create a PowerPoint presentation (.pptx). Use when the user asks for a slide deck, pitch deck, or presentation.",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Presentation title"},
                    "slides": {
                        "type": "array",
                        "description": "List of slides",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "Slide title"},
                                "content": {"type": "string", "description": "Slide body text (use \\n for line breaks, - for bullets)"},
                                "layout": {"type": "string", "enum": ["title", "content", "section", "blank"], "description": "Slide layout (default: content)"},
                            },
                            "required": ["title", "content"]
                        }
                    },
                    "filename": {"type": "string", "description": "Output filename stem (optional)"},
                },
                "required": ["title", "slides"],
                "additionalProperties": False
            },
            execute=_exec_create_presentation,
        ),
        ToolSpec(
            name="create_pdf",
            description="Create a PDF document. Use when the user specifically asks for PDF output. Content supports # headings and plain text.",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Document title"},
                    "content": {"type": "string", "description": "Document body text"},
                    "filename": {"type": "string", "description": "Output filename stem (optional)"},
                },
                "required": ["title", "content"],
                "additionalProperties": False
            },
            execute=_exec_create_pdf,
        ),
        ToolSpec(
            name="ask_user",
            description=(
                "Ask the user a multiple-choice question and wait for their answer. "
                "Use this when you need clarification, want the user to choose between options, "
                "or need to confirm how to proceed. You can provide 2-6 choices. "
                "The user can also type a custom answer if allow_other is true. "
                "The tool returns the user's selected choice or custom text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user"
                    },
                    "choices": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 6,
                        "description": "List of choices (2-6 options)"
                    },
                    "allow_other": {
                        "type": "boolean",
                        "description": "Whether to show an 'Other' option with a free-text field (default true)"
                    },
                },
                "required": ["question", "choices"],
                "additionalProperties": False
            },
            execute=_exec_ask_user,
        ),
        # ─── Planning & Research Tools ───
        ToolSpec(
            name="task_plan",
            description=(
                "Create a multi-step execution plan. Call this FIRST for any multi-step task. "
                "Provide a goal and an ordered list of steps. Each step gets a step_id. "
                "After creating the plan, call ask_user for approval, then execute steps "
                "one at a time using task_update to track progress."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "What the overall task aims to achieve"
                    },
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 15,
                        "description": "Ordered list of step descriptions"
                    },
                    "output_file": {
                        "type": "string",
                        "description": "Path to the output file if the task produces a single artifact (optional)"
                    },
                },
                "required": ["goal", "steps"],
                "additionalProperties": False
            },
            execute=_exec_task_plan,
        ),
        ToolSpec(
            name="task_update",
            description=(
                "Update the status of a plan step. Call this to mark a step as "
                "in_progress (before starting), done (after completing), or failed. "
                "Always mark a step in_progress BEFORE doing its work, and done AFTER."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "step_id": {
                        "type": "string",
                        "description": "The step ID to update (e.g. 'step_1', 'step_2')"
                    },
                    "status": {
                        "type": "string",
                        "enum": ["in_progress", "done", "failed"],
                        "description": "New status for the step"
                    },
                },
                "required": ["step_id", "status"],
                "additionalProperties": False
            },
            execute=_exec_task_update,
        ),
        ToolSpec(
            name="record_fact",
            description=(
                "Record a key finding from web research or file analysis. "
                "Facts survive context compaction — raw web content does NOT. "
                "ALWAYS call this after web_search/web_fetch to preserve important findings. "
                "Include the source URL, category, and confidence level."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "The key finding or fact to record"
                    },
                    "source": {
                        "type": "string",
                        "description": "Source URL where this fact was found. REQUIRED for academic research."
                    },
                    "category": {
                        "type": "string",
                        "description": "Category for organizing facts (e.g. 'history', 'technology', 'market', 'manufacturing')"
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "Confidence level: high (multiple sources confirm), medium (single reliable source), low (unverified or opinion)"
                    },
                },
                "required": ["fact", "source"],
                "additionalProperties": False
            },
            execute=_exec_record_fact,
        ),
        ToolSpec(
            name="research_outline",
            description=(
                "Create a structured research outline for a multi-section paper. "
                "ALWAYS use this as the first step when writing a research paper. "
                "Defines sections, target word counts, and key questions for each section. "
                "The outline tracks research and drafting progress."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Title of the research paper"
                    },
                    "thesis": {
                        "type": "string",
                        "description": "Central thesis or argument of the paper"
                    },
                    "sections": {
                        "type": "array",
                        "description": "List of section titles (strings) or objects with title, target_words, key_questions",
                        "items": {"type": "string"}
                    },
                    "target_words": {
                        "type": "integer",
                        "description": "Total target word count for the paper (default 5000)"
                    },
                },
                "required": ["title", "sections"],
                "additionalProperties": False
            },
            execute=_exec_research_outline,
        ),
        ToolSpec(
            name="draft_section",
            description=(
                "Write one section of the research paper. Call this once per section. "
                "Content should include inline citations [1], [2] etc. "
                "Tracks word count, citation count, and overall paper progress. "
                "After all sections are drafted, call self_grade."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "section_id": {
                        "type": "string",
                        "description": "Section ID from research_outline (e.g. 'sec_1', 'sec_2')"
                    },
                    "content": {
                        "type": "string",
                        "description": "Full markdown text of this section with inline [N] citations"
                    },
                },
                "required": ["section_id", "content"],
                "additionalProperties": False
            },
            execute=_exec_draft_section,
        ),
        ToolSpec(
            name="self_grade",
            description=(
                "Evaluate the research paper against an academic rubric. "
                "Scores: word count, citations, structure, depth, tone, bibliography, balance. "
                "If score < 70, provides specific improvement guidance. "
                "Call this after all sections are drafted. If it fails, improve and re-grade."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "paper_text": {
                        "type": "string",
                        "description": "Full paper text to grade (optional — if omitted, assembles from drafted sections)"
                    },
                },
                "additionalProperties": False
            },
            execute=_exec_self_grade,
        ),
        # ─── Bulk Organizer Tools ───
        # ── Organize Skill v3: 4 compound tools ──
        ToolSpec(
            name="organize_ingest",
            description="Parse a source file (bookmarks, directory, etc.) into items. Creates SOURCE graph node. Returns samples for taxonomy.",
            input_schema={
                "type": "object",
                "properties": {
                    "source_path": {"type": "string", "description": "Path to source file (bookmarks HTML) or directory"},
                    "item_type": {"type": "string", "enum": ["bookmarks", "files"], "description": "Type of items to organize"},
                },
                "required": ["source_path"],
                "additionalProperties": False,
            },
            execute=_exec_organize_ingest,
        ),
        ToolSpec(
            name="organize_propose_taxonomy",
            description="Propose categories for classification. Asks user for approval (blocks until answered). Creates TAXONOMY + CATEGORY + DECISION graph nodes.",
            input_schema={
                "type": "object",
                "properties": {
                    "categories": {
                        "type": "string",
                        "description": 'JSON array: [{"key":"dev_tools","name":"Development & Tech","description":"Linux, coding tools"}, ...]',
                    },
                    "state_dir": {"type": "string", "description": "Path to state dir (auto-detected from graph if omitted)"},
                },
                "required": ["categories"],
                "additionalProperties": False,
            },
            execute=_exec_organize_propose_taxonomy,
        ),
        ToolSpec(
            name="organize_classify_batch",
            description="Classify items. If called with classifications: writes them + reads next batch. If called without: reads first/next batch. Creates PROGRESS graph nodes linked to CATEGORY nodes.",
            input_schema={
                "type": "object",
                "properties": {
                    "state_dir": {"type": "string", "description": "Path to state dir (auto-detected from graph if omitted)"},
                    "classifications": {
                        "type": "string",
                        "description": 'JSON array: [{"idx":0,"category":"dev_tools","confidence":"high"}, ...]. Omit to read first batch.',
                    },
                    "start": {"type": "string", "description": "Override start index (auto-detected if omitted)"},
                },
                "additionalProperties": False,
            },
            execute=_exec_organize_classify_batch,
        ),
        ToolSpec(
            name="organize_export",
            description="Generate organized output file from classifications. Creates ARTIFACT graph node.",
            input_schema={
                "type": "object",
                "properties": {
                    "state_dir": {"type": "string", "description": "Path to state dir (auto-detected from graph if omitted)"},
                    "format": {"type": "string", "enum": ["netscape_html", "json"], "description": "Output format (default: netscape_html)"},
                    "output_path": {"type": "string", "description": "Output file path (default: ~/Desktop/organized_<name>.html)"},
                },
                "additionalProperties": False,
            },
            execute=_exec_organize_export,
        ),
        # ─── Meta: model-driven skill switch (Phase B) ───
        ToolSpec(
            name="switch_skill",
            description=(
                "Switch the active skill for this conversation. Use when the "
                "user's request fits a different skill than the one currently "
                "active (e.g. they asked a quick lookup but now want a full "
                "research paper, or vice versa). Pass skill_name from this "
                "list: general, quick_research, research, code, memory, "
                "organizer, icloud_macos. Provide a one-line reason. The "
                "switch takes effect on the next LLM iteration — your tool "
                "surface and system instructions update automatically."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "enum": [
                            "general", "quick_research", "research",
                            "code", "memory", "organizer", "icloud_macos",
                        ],
                        "description": "The skill to switch to.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this skill fits the user's request better.",
                    },
                },
                "required": ["skill_name", "reason"],
                "additionalProperties": False,
            },
            execute=_exec_switch_skill,
        ),
        # ─── iCloud / macOS Integration Tools ───
        # All write/send tools expect the model to call ask_user FIRST.
        # The macos_bridge module executes AppleScript via osascript.
        *_macos_tool_specs(),
    ]


def _macos_tool_specs() -> list[ToolSpec]:
    """iCloud / macOS integration tools — Messages, Calendar, Contacts, Mail, Notes, Reminders."""
    from macos_bridge import (
        messages_send, messages_read, messages_list_chats,
        calendar_create_event, calendar_list_events, calendar_list_calendars,
        contacts_search, contacts_create,
        mail_send, mail_read, mail_search,
        notes_create, notes_search,
        reminders_create,
    )

    def _wrap(fn):
        """Wrap a macos_bridge function so it accepts a dict of args."""
        def executor(args: dict) -> str:
            return fn(**args)
        return executor

    return [
        # ── Messages ──
        ToolSpec(
            name="messages_send",
            description=(
                "Send an iMessage or SMS via Messages.app, optionally with a file attachment. "
                "IMPORTANT: You MUST call ask_user first to show the user a preview "
                "of the message and get explicit confirmation before calling this tool. "
                "Never send a message without user approval. "
                "For attachments, pass the absolute file path on the user's Mac."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "recipient": {"type": "string", "description": "Phone number, email, or contact name"},
                    "text": {"type": "string", "description": "Message text to send"},
                    "attachment": {"type": "string", "description": "Absolute path to a file to attach (optional, iMessage only)"},
                },
                "required": ["recipient", "text"],
                "additionalProperties": False,
            },
            execute=_wrap(messages_send),
        ),
        ToolSpec(
            name="messages_read",
            description=(
                "Read recent messages from a conversation in Messages.app. "
                "Returns the last N messages with sender, date, and text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "contact": {"type": "string", "description": "Contact name, phone number, or email to find the conversation"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Number of recent messages to retrieve (default 20)"},
                },
                "required": ["contact"],
                "additionalProperties": False,
            },
            execute=_wrap(messages_read),
        ),
        ToolSpec(
            name="messages_list_chats",
            description=(
                "List recent conversations in Messages.app with the last message preview and date."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Number of conversations to list (default 15)"},
                },
                "additionalProperties": False,
            },
            execute=_wrap(messages_list_chats),
        ),
        # ── Calendar ──
        ToolSpec(
            name="calendar_create_event",
            description=(
                "Create a new event in Calendar.app. "
                "IMPORTANT: You MUST call ask_user first to show the event details "
                "and get explicit confirmation. If the user hasn't specified which "
                "calendar, call calendar_list_calendars first, then ask_user to let "
                "them choose."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Event title"},
                    "start_date": {"type": "string", "description": "Start date/time (ISO 8601, natural language like 'tomorrow at 2pm', or 'April 20, 2026 at 3:00 PM')"},
                    "end_date": {"type": "string", "description": "End date/time (defaults to 1 hour after start if omitted)"},
                    "calendar_name": {"type": "string", "description": "Calendar name (e.g. 'Personal', 'Work'). If omitted, uses first available."},
                    "location": {"type": "string", "description": "Event location"},
                    "notes": {"type": "string", "description": "Event notes/description"},
                    "all_day": {"type": "boolean", "description": "Whether this is an all-day event"},
                },
                "required": ["title", "start_date"],
                "additionalProperties": False,
            },
            execute=_wrap(calendar_create_event),
        ),
        ToolSpec(
            name="calendar_list_events",
            description=(
                "List upcoming events from Calendar.app. "
                "Returns events within the next N days from all or a specific calendar."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "days_ahead": {"type": "integer", "minimum": 1, "maximum": 90, "description": "How many days ahead to look (default 7)"},
                    "calendar_name": {"type": "string", "description": "Filter by calendar name (omit for all calendars)"},
                },
                "additionalProperties": False,
            },
            execute=_wrap(calendar_list_events),
        ),
        ToolSpec(
            name="calendar_list_calendars",
            description="List all available calendars in Calendar.app (name and account). Use this to help the user choose which calendar to create events in.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            execute=_wrap(calendar_list_calendars),
        ),
        # ── Contacts ──
        ToolSpec(
            name="contacts_search",
            description=(
                "Search Contacts.app by name, email, or phone number. "
                "Returns matching contacts with their phone numbers, emails, and company. "
                "Use this to resolve a contact before sending messages or emails."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Name, email, or phone number to search for"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            execute=_wrap(contacts_search),
        ),
        ToolSpec(
            name="contacts_create",
            description=(
                "Create a new contact in Contacts.app. "
                "IMPORTANT: Call ask_user first to confirm the contact details."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "phone": {"type": "string", "description": "Phone number"},
                    "email": {"type": "string", "description": "Email address"},
                    "company": {"type": "string", "description": "Company/organization"},
                },
                "required": ["first_name"],
                "additionalProperties": False,
            },
            execute=_wrap(contacts_create),
        ),
        # ── Mail ──
        ToolSpec(
            name="mail_send",
            description=(
                "Send an email via Mail.app. "
                "IMPORTANT: You MUST call ask_user first to show the full email preview "
                "(to, subject, body) and get explicit confirmation. "
                "Never send an email without user approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {"type": "string", "description": "Email body text"},
                    "cc": {"type": "string", "description": "CC addresses (comma-separated)"},
                    "attachment_path": {"type": "string", "description": "Absolute path to file to attach"},
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
            execute=_wrap(mail_send),
        ),
        ToolSpec(
            name="mail_read",
            description="Read recent emails from a mailbox in Mail.app. Returns sender, subject, date, and a snippet of each message.",
            input_schema={
                "type": "object",
                "properties": {
                    "mailbox": {"type": "string", "description": "Mailbox name (default 'INBOX')"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Number of emails to retrieve (default 10)"},
                    "account": {"type": "string", "description": "Mail account name (omit for default)"},
                },
                "additionalProperties": False,
            },
            execute=_wrap(mail_read),
        ),
        ToolSpec(
            name="mail_search",
            description="Search emails in Mail.app by keyword (searches subject lines). Returns matching emails with sender, subject, date, and mailbox.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword to search for in email subjects"},
                    "mailbox": {"type": "string", "description": "Limit search to specific mailbox (omit to search all)"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Max results (default 10)"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            execute=_wrap(mail_search),
        ),
        # ── Notes ──
        ToolSpec(
            name="notes_create",
            description=(
                "Create a new note in Notes.app. "
                "IMPORTANT: Call ask_user first to confirm the note title and content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title"},
                    "body": {"type": "string", "description": "Note body content (plain text, newlines supported)"},
                    "folder": {"type": "string", "description": "Notes folder (omit for default)"},
                },
                "required": ["title", "body"],
                "additionalProperties": False,
            },
            execute=_wrap(notes_create),
        ),
        ToolSpec(
            name="notes_search",
            description="Search notes in Notes.app by keyword. Returns matching notes with title, modification date, and a snippet.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword to search for"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Max results (default 10)"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            execute=_wrap(notes_search),
        ),
        # ── Reminders ──
        ToolSpec(
            name="reminders_create",
            description=(
                "Create a reminder in Reminders.app. "
                "IMPORTANT: Call ask_user first to confirm the reminder details. "
                "If the user hasn't specified which list, ask them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Reminder title"},
                    "due_date": {"type": "string", "description": "Due date (ISO 8601, 'tomorrow', 'April 20, 2026'). Omit for no due date."},
                    "list_name": {"type": "string", "description": "Reminder list name (omit for default list)"},
                    "notes": {"type": "string", "description": "Additional notes"},
                    "priority": {"type": "string", "enum": ["none", "low", "medium", "high"], "description": "Priority level (default 'none')"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            execute=_wrap(reminders_create),
        ),
    ]


# ─── Tool Registry ───

log = logging.getLogger("tools")

# ─── Error Classification ───

# Patterns that indicate transient failures worth retrying
_RETRYABLE_PATTERNS = [
    r"timed?\s*out",
    r"connection\s*(refused|reset|aborted)",
    r"dns.*fail|name.*resolution|nodename.*not.*provided",
    r"temporary\s*failure",
    r"errno\s*(110|111|104)",  # ETIMEDOUT, ECONNREFUSED, ECONNRESET
    r"503|502|429",            # Service Unavailable, Bad Gateway, Rate Limited
    r"resource\s*temporarily\s*unavailable",
    r"broken\s*pipe",
    r"network\s*(is\s*)?unreachable",
]
_RETRYABLE_RE = re.compile("|".join(_RETRYABLE_PATTERNS), re.IGNORECASE)

# Tools that are safe to retry (idempotent / read-only)
_RETRYABLE_TOOLS = {
    "bash",          # might not be idempotent, but transient failures are common
    "read_file", "glob_search", "grep_search",
    "memory_read", "memory_search", "memory_list",
    "transcript_search",
    "web_search", "web_fetch",
}

# Hints to help the model self-correct after a failure
_ERROR_HINTS = {
    "file not found":      "Check the path exists. Use glob_search to find the correct filename.",
    "permission denied":   "The file/directory is not accessible. Try a different path.",
    "no such file":        "The path does not exist. Use glob_search to locate the file.",
    "invalid regex":       "The regex pattern is malformed. Simplify or escape special characters.",
    "json":                "The arguments may have invalid JSON. Check quoting and escaping.",
    "timed out":           "The operation took too long. Try a simpler command or smaller scope.",
    "connection refused":  "The target service is not running. Check if the server is up.",
    "dns":                 "DNS resolution failed. Check network connectivity or the URL.",
}


def _is_retryable(error_msg: str, tool_name: str) -> bool:
    """Classify whether a tool error is transient and worth retrying."""
    if tool_name not in _RETRYABLE_TOOLS:
        return False
    return bool(_RETRYABLE_RE.search(error_msg))


def _enhance_error(tool_name: str, error_msg: str) -> str:
    """Append a helpful hint to error messages so the model can self-correct."""
    error_lower = error_msg.lower()
    for pattern, hint in _ERROR_HINTS.items():
        if pattern in error_lower:
            return f"{error_msg}\n\nHint: {hint}"
    return error_msg


class ToolRegistry:
    """Aligned with claw-code GlobalToolRegistry. Includes retry + error classification."""

    MAX_RETRIES = 2
    RETRY_BACKOFF = 1.0  # seconds, multiplied by attempt number

    def __init__(self):
        self._specs = {s.name: s for s in mvp_tool_specs()}

    def definitions(self, skill=None) -> list[dict]:
        """Return OpenAI function-calling tool definitions, optionally
        filtered to the active skill's tool_allowlist.

        Callers pass a ``skills.Skill`` object (or None for unfiltered).
        Unknown tool names in the allowlist are silently ignored, so a
        skill definition never blocks the agent from starting."""
        all_defs = [s.to_openai_tool() for s in self._specs.values()]
        if skill is None:
            return all_defs
        allowed = set(getattr(skill, "tool_allowlist", []) or [])
        if not allowed:
            return all_defs
        return [d for d in all_defs
                if ((d or {}).get("function") or {}).get("name") in allowed]

    def execute(self, name: str, args: dict) -> str:
        """Execute a tool by name with automatic retry for transient failures."""
        spec = self._specs.get(name)
        if spec is None:
            return f"ERROR: Unknown tool '{name}'. Available: {list(self._specs.keys())}"

        last_error = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                result = spec.execute(args)
            except Exception as e:
                result = f"ERROR executing {name}: {e}"

            is_error = result.startswith("ERROR")

            if not is_error:
                if attempt > 0:
                    log.info(f"  Tool {name} succeeded on retry #{attempt}")
                return result

            # Error path — decide whether to retry
            if attempt < self.MAX_RETRIES and _is_retryable(result, name):
                wait = self.RETRY_BACKOFF * (attempt + 1)
                log.warning(f"  Tool {name} failed (attempt {attempt + 1}), retrying in {wait}s: {result[:120]}")
                time.sleep(wait)
                last_error = result
                continue

            # Not retryable or out of retries — enhance the error and return
            enhanced = _enhance_error(name, result)
            if attempt > 0:
                enhanced += f"\n\n(Failed after {attempt + 1} attempts)"
            return enhanced

        # Should not reach here, but just in case
        return _enhance_error(name, last_error or "ERROR: Unknown failure")

    def names(self) -> list[str]:
        return list(self._specs.keys())
