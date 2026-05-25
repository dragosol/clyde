"""
Clyde — Model-Agnostic Tool Call Parser
================================================

Unified parser for tool calls emitted by any open-source or proprietary LLM.

Every model family emits tool calls in a slightly different wire format.
Rather than patching a monolithic parser every time a new model is added,
this module has:

  1. **Parser layer** — one small pure function per model family, each
     recognizing a specific wire format. Registered in PARSERS, tried in
     priority order on the raw content. First match wins.

  2. **Normalization layer** — `_normalize_args()` runs every captured
     argument string through a pipeline that tries JSON → JSON with
     single-quote fix → Python `ast.literal_eval` → Python-kwarg-to-JSON
     regex conversion → key-by-key permissive regex extraction. This
     isolates parser authors from argument-shape robustness concerns.

  3. **Coercion layer** — `coerce_tool_args(spec, args)` takes the JSON
     schema from a tool spec and coerces model-emitted args to it,
     resolving argument aliases (e.g. `desc` → `description`) and
     coercing types (`"true"` → `True`, `"42"` → `42`). Runs on every
     tool call regardless of which parser produced it.

Adding a new model family is a ~30-line addition: write a parser
function, register it in PARSERS with a priority, done.

Supported formats (highest → lowest priority):

  - OpenAI / native structured `tool_calls` channel (no parsing needed)
  - Qwen 2.5/3, Hermes 3, Nous: `<tool_call>{JSON}</tool_call>`
  - Claude / Anthropic XML: `<invoke name="..."><parameter>val</parameter></invoke>`
  - Mistral / Mixtral: `[TOOL_CALLS] [{JSON}, ...]`
  - Llama 3.1-3.3: `<|python_tag|>func.call(...)` or JSON variant
  - Command R/R+: `<|START_ACTION|>[{JSON}]<|END_ACTION|>`
  - DeepSeek V3/R1: `<｜tool▁call▁begin｜>func<｜tool▁sep｜>{JSON}<｜tool▁call▁end｜>`
  - Markdown fenced: ```` ```tool_code\n{JSON}\n``` ````
  - Python function-call: `func_name({JSON})` or `func_name(k="v", l=[...])`
  - Bare JSON: `{"name": ..., "arguments": {...}}`
  - Inline backtick CLI hallucination: `` `bash --command="ls /tmp"` ``

Each parser is independent. Removing/adding a parser cannot break other
parsers. All parsers share the same normalization and coercion paths.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("tool_call_parser")


# ─── Normalized shape ───────────────────────────────────────────────────

@dataclass
class ToolCall:
    """Normalized tool call — what every parser returns and every executor reads."""
    name: str
    arguments: dict
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:8]}")

    def to_openai(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            },
        }


# ─── Argument value normalization ───────────────────────────────────────
#
# Robust "take a messy value string and produce a clean Python object"
# pipeline. Used by every parser for argument values that aren't already
# structured (e.g. when the parser captured a raw substring).




def _scrub_ellipsis(value: Any) -> Any:
    """
    Recursively replace Python `Ellipsis` (`...`) with `None`.

    `ast.literal_eval` returns `Ellipsis` for a bare `...` literal, which
    is not JSON-serializable and crashes downstream `json.dumps()` with
    "Object of type ellipsis is not JSON serializable". Models sometimes
    emit placeholder kwargs like `notes=...` or `comment=...`, so we
    defensively scrub the entire parsed structure here rather than
    discovering the problem at serialization time.

    Walks dicts and lists/tuples; leaves other types untouched.
    """
    if value is Ellipsis:
        return None
    if isinstance(value, dict):
        return {k: _scrub_ellipsis(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        cleaned = [_scrub_ellipsis(v) for v in value]
        return cleaned if isinstance(value, list) else tuple(cleaned)
    return value


def _normalize_value(raw: Any) -> Any:
    """
    Convert a raw value (string or already-parsed object) into the cleanest
    possible Python representation. Runs JSON, literal_eval, and several
    regex-based fallbacks.

    Idempotent: if `raw` is already a clean dict/list/int/bool/None,
    returns it unchanged.
    """
    # Already structured — done
    if raw is None or isinstance(raw, (dict, list, bool, int, float)):
        return raw

    if not isinstance(raw, str):
        return raw

    s = raw.strip()
    if not s:
        return s

    # Attempt 1: direct JSON parse
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        pass

    # Attempt 2: single-quoted string → double-quoted, then JSON
    if "'" in s:
        try:
            return json.loads(s.replace("'", '"'))
        except (ValueError, TypeError):
            pass

    # Attempt 3: Python literal eval (tuples, True/False/None, numeric literals)
    try:
        parsed = ast.literal_eval(s)
        # ast.literal_eval will happily return Python's `Ellipsis` singleton
        # for a bare `...` literal — which then propagates into the args
        # dict and crashes downstream `json.dumps()` with
        # "Object of type ellipsis is not JSON serializable". Models
        # sometimes emit `notes=...` or `comment=...` as placeholder values,
        # so we have to defensively scrub Ellipsis from the parsed result.
        return _scrub_ellipsis(parsed)
    except (ValueError, SyntaxError):
        pass

    # Attempt 4: Convert Python-style kwarg dicts (and JSON-with-unquoted-keys)
    # to JSON. Matches `{key = "value"}` OR `{key: "value"}` (unquoted key with
    # either `=` or `:` separator) and transforms to `{"key": "value"}`.
    converted = re.sub(
        r'(\{|,)\s*([A-Za-z_]\w*)\s*[:=]\s*',
        r'\1"\2": ',
        s,
    )
    if converted != s:
        try:
            return json.loads(converted)
        except (ValueError, TypeError):
            try:
                return json.loads(converted.replace("'", '"'))
            except (ValueError, TypeError):
                pass

    # Attempt 5: Regex extraction for list-of-dicts with known description-like
    # keys. Last-resort recovery when the input is a malformed step list.
    if s.lstrip().startswith("["):
        description_keys = r'(?:description|desc|step|text|title|name|content|body)'
        items = []
        for match in re.finditer(
            rf'\{{[^}}]*?{description_keys}\s*[:=]\s*["\']([^"\']+)["\'][^}}]*?\}}',
            s,
            re.DOTALL,
        ):
            desc = match.group(1).strip()
            if desc:
                items.append({"description": desc})
        if items:
            return items

    # Attempt 6: Boolean / null / numeric coercion for simple values
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass

    # Give up — return the string as-is. The tool-side coercion layer has
    # further fallbacks for type-specific recovery.
    return s


def _normalize_args(args: Any) -> dict:
    """
    Take whatever a parser extracted as arguments and return a clean dict.

    Handles:
      - Already-a-dict: return unchanged (values still normalized recursively)
      - JSON string: parse
      - Python-dict-literal string: convert + parse
      - None: empty dict
      - Anything else: wrap in {"raw": value}
    """
    if args is None:
        return {}
    if isinstance(args, dict):
        return _scrub_ellipsis(
            {k: _normalize_value(v) for k, v in args.items()}
        )
    if isinstance(args, str):
        parsed = _normalize_value(args)
        if isinstance(parsed, dict):
            return _scrub_ellipsis(
                {k: _normalize_value(v) for k, v in parsed.items()}
            )
        # Model emitted a non-dict value for arguments (rare) — preserve it
        return {"raw": _scrub_ellipsis(parsed)}
    # List or other structured value — preserve under "raw"
    return {"raw": args}


# ─── Parser registry ────────────────────────────────────────────────────

ParserFn = Callable[[str, list[str]], list[ToolCall]]
# Signature: parser(content, tool_names_list) → list[ToolCall]

_PARSERS: list[tuple[int, str, ParserFn]] = []  # (priority, name, fn)


def parser(priority: int, name: str):
    """Decorator to register a parser. Higher priority = tried earlier."""
    def _wrap(fn: ParserFn) -> ParserFn:
        _PARSERS.append((priority, name, fn))
        _PARSERS.sort(key=lambda t: -t[0])  # keep sorted desc
        return fn
    return _wrap


# ─── Parsers ────────────────────────────────────────────────────────────

# Priority 100: Qwen 2.5/3, Hermes 3, Nous Research
# Format: <tool_call>{"name": "foo", "arguments": {...}}</tool_call>
@parser(priority=100, name="qwen_hermes_xml")
def _parse_qwen_hermes_xml(content: str, tool_names: list[str]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for m in re.finditer(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', content, re.DOTALL):
        tc = _try_parse_name_args_json(m.group(1))
        if tc:
            calls.append(tc)
    return calls


# Priority 95: Claude / Anthropic XML invoke-style
# Format: <invoke name="foo"><parameter name="x">val</parameter></invoke>
@parser(priority=95, name="anthropic_xml_invoke")
def _parse_anthropic_xml_invoke(content: str, tool_names: list[str]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for m in re.finditer(
        r'<invoke\s+name=["\'](\w+)["\']\s*>(.*?)</invoke>',
        content,
        re.DOTALL,
    ):
        name = m.group(1)
        params_block = m.group(2)
        args: dict = {}
        for pm in re.finditer(
            r'<parameter\s+name=["\'](\w+)["\']\s*>(.*?)</parameter>',
            params_block,
            re.DOTALL,
        ):
            args[pm.group(1)] = _normalize_value(pm.group(2).strip())
        calls.append(ToolCall(name=name, arguments=args))
    return calls


# Priority 90: Mistral / Mixtral
# Format: [TOOL_CALLS] [{"name": "foo", "arguments": {...}}]
#     or: [TOOL_CALLS][{"name": "foo", "arguments": {...}}]
#     or: [TOOL_CALL] {"name": "foo", "arguments": {...}}
@parser(priority=90, name="mistral_tool_calls")
def _parse_mistral(content: str, tool_names: list[str]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    # Match both [TOOL_CALLS] (plural) and [TOOL_CALL] (singular)
    for m in re.finditer(
        r'\[TOOL_CALLS?\]\s*(\[.*?\]|\{.*?\})',
        content,
        re.DOTALL,
    ):
        body = m.group(1)
        parsed = _normalize_value(body)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    tc = _tc_from_name_args_dict(item)
                    if tc:
                        calls.append(tc)
        elif isinstance(parsed, dict):
            tc = _tc_from_name_args_dict(parsed)
            if tc:
                calls.append(tc)
    return calls


# Priority 88: Llama 3.1-3.3 python_tag
# Format: <|python_tag|>{"name": "foo", "parameters": {...}}
#     or: <|python_tag|>foo.call(param="val")
@parser(priority=88, name="llama3_python_tag")
def _parse_llama3_python_tag(content: str, tool_names: list[str]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for m in re.finditer(
        r'<\|python_tag\|>(.+?)(?=<\||\Z)',
        content,
        re.DOTALL,
    ):
        body = m.group(1).strip()
        # Try JSON first
        if body.startswith("{"):
            tc = _try_parse_name_args_json(body)
            if tc:
                calls.append(tc)
                continue
        # Try function call syntax
        tcs = _parse_function_call_syntax(body, tool_names)
        calls.extend(tcs)
    return calls


# Priority 85: Command R/R+
# Format: <|START_ACTION|>[{"tool_name": "foo", "parameters": {...}}]<|END_ACTION|>
@parser(priority=85, name="command_r_action")
def _parse_command_r_action(content: str, tool_names: list[str]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for m in re.finditer(
        r'<\|START_ACTION\|>\s*(\[.*?\])\s*<\|END_ACTION\|>',
        content,
        re.DOTALL,
    ):
        parsed = _normalize_value(m.group(1))
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    tc = _tc_from_name_args_dict(item)
                    if tc:
                        calls.append(tc)
    return calls


# Priority 82: DeepSeek V3 / R1
# Format: <｜tool▁call▁begin｜>function_name<｜tool▁sep｜>{JSON}<｜tool▁call▁end｜>
#
# DeepSeek uses full-width pipe characters (U+FF5C) instead of ASCII pipes.
# The \uff5c in the regex is the critical detail.
@parser(priority=82, name="deepseek_tool_call")
def _parse_deepseek(content: str, tool_names: list[str]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for m in re.finditer(
        r'[|\uff5c]tool[▁_]call[▁_]begin[|\uff5c]'  # open marker
        r'(\w+)'                                      # function name
        r'[|\uff5c]tool[▁_]sep[|\uff5c]'              # separator
        r'(\{.*?\})'                                  # JSON args
        r'[|\uff5c]tool[▁_]call[▁_]end[|\uff5c]',     # close marker
        content,
        re.DOTALL,
    ):
        name = m.group(1)
        args = _normalize_args(m.group(2))
        calls.append(ToolCall(name=name, arguments=args))
    return calls


# Priority 70: Markdown fenced code block with tool call JSON
# Format: ```tool_code\n{"name": "foo", "arguments": {...}}\n```
#     or: ```json\n{JSON}\n```
#     or: ```tool\n{JSON}\n```
@parser(priority=70, name="markdown_fenced")
def _parse_markdown_fenced(content: str, tool_names: list[str]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for m in re.finditer(
        r'```(?:tool_code|tool|json)?\s*\n(\{.*?\})\s*\n```',
        content,
        re.DOTALL,
    ):
        tc = _try_parse_name_args_json(m.group(1))
        if tc:
            calls.append(tc)
    return calls


# Priority 60: XML attribute style
# Format: <tool call="func_name" args='{"key":"value"}'/>
@parser(priority=60, name="xml_attribute")
def _parse_xml_attribute(content: str, tool_names: list[str]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for m in re.finditer(
        r'<tool\s+call=["\'](\w+)["\']\s+args=["\'](.+?)["\']\s*/?>',
        content,
        re.DOTALL,
    ):
        name = m.group(1)
        args_raw = m.group(2)
        # HTML entity decode
        args_decoded = (
            args_raw
            .replace('&quot;', '"')
            .replace('&amp;', '&')
            .replace('&lt;', '<')
            .replace('&gt;', '>')
            .replace('&apos;', "'")
        )
        args = _normalize_args(args_decoded)
        calls.append(ToolCall(name=name, arguments=args))
    return calls


# Priority 50: Python function-call syntax
# Format: func_name({"key": "val"})  or  func_name(key="val", key2=123)
#
# This is the catch-all for models that mimic Python's REPL. Matches
# any known tool name followed by parentheses containing either JSON
# or kwarg-style arguments. Handles nested brackets correctly via a
# separate tokenizing parser.
@parser(priority=50, name="python_function_call")
def _parse_python_function_call(content: str, tool_names: list[str]) -> list[ToolCall]:
    return _parse_function_call_syntax(content, tool_names)


def _parse_function_call_syntax(content: str, tool_names: list[str]) -> list[ToolCall]:
    """Scan `content` for `tool_name(...)` patterns with bracket-aware arg parsing."""
    calls: list[ToolCall] = []
    if not tool_names:
        return calls

    for name in tool_names:
        # Find all occurrences of `name(` that aren't preceded by a word character
        # (so we don't match e.g. `my_bash(` when `bash` is in the list).
        for match in re.finditer(rf'(?<!\w){re.escape(name)}\s*\(', content):
            start = match.end() - 1  # index of the opening paren
            # Bracket-aware scan to find the matching close paren
            depth = 0
            i = start
            n = len(content)
            in_string = None  # '"' or "'" when inside a string literal
            while i < n:
                c = content[i]
                if in_string:
                    if c == "\\" and i + 1 < n:
                        i += 2
                        continue
                    if c == in_string:
                        in_string = None
                elif c in ('"', "'"):
                    in_string = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if depth != 0:
                continue  # unbalanced — skip
            args_body = content[start + 1:i]  # strip outer parens

            # Try JSON dict first
            body_stripped = args_body.strip()
            if body_stripped.startswith("{"):
                parsed = _normalize_value(body_stripped)
                if isinstance(parsed, dict):
                    calls.append(ToolCall(name=name, arguments=parsed))
                    continue

            # Fall through to kwargs parser
            args = _parse_python_kwargs(args_body)
            if args:
                calls.append(ToolCall(name=name, arguments=args))

    return calls


def _parse_python_kwargs(s: str) -> dict:
    """
    Bracket-aware Python-kwargs parser. Handles:
      key="string"
      key='string'
      key=123
      key=true
      key=[1, 2, 3]
      key=[{"nested": "dict"}]
      key=[{description="python-kwarg-inside"}]
      key={"nested": {"more": [1, 2]}}
      key=(tuple, syntax)

    Uses _normalize_value on every captured value to recover from
    most malformed structured inputs.
    """
    args: dict = {}
    i = 0
    n = len(s)

    def _skip_ws(idx: int) -> int:
        while idx < n and s[idx] in " \t\n\r,":
            idx += 1
        return idx

    while i < n:
        i = _skip_ws(i)
        if i >= n:
            break

        # Capture key
        key_start = i
        while i < n and (s[i].isalnum() or s[i] == "_"):
            i += 1
        if i == key_start:
            i += 1
            continue
        key = s[key_start:i]
        i = _skip_ws(i)
        if i >= n or s[i] not in "=:":
            continue
        i += 1  # consume = or :
        i = _skip_ws(i)
        if i >= n:
            break

        # Capture value
        val_start = i
        if s[i] in '"\'':
            quote = s[i]
            i += 1
            while i < n and s[i] != quote:
                if s[i] == "\\" and i + 1 < n:
                    i += 2
                else:
                    i += 1
            raw_val = s[val_start + 1:i]  # strip quotes
            if i < n:
                i += 1
            args[key] = raw_val
        elif s[i] in "[{(":
            open_char = s[i]
            close_set = {"[": "]", "{": "}", "(": ")"}
            stack = [open_char]
            i += 1
            while i < n and stack:
                c = s[i]
                if c in '"\'':
                    q = c
                    i += 1
                    while i < n and s[i] != q:
                        if s[i] == "\\" and i + 1 < n:
                            i += 2
                        else:
                            i += 1
                    if i < n:
                        i += 1
                    continue
                if c in "[{(":
                    stack.append(c)
                elif c in "])}":
                    if stack and close_set[stack[-1]] == c:
                        stack.pop()
                i += 1
            raw_val = s[val_start:i]
            args[key] = _normalize_value(raw_val)
        else:
            while i < n and s[i] not in " \t\n\r,":
                i += 1
            raw_val = s[val_start:i]
            args[key] = _normalize_value(raw_val)

    return args


# Priority 40: Bare JSON last-resort
# Format: {"name": "foo", "arguments": {...}}  anywhere in the text
@parser(priority=40, name="bare_json")
def _parse_bare_json(content: str, tool_names: list[str]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    # Match balanced {...} containing both a "name" field and an "arguments" field
    for m in re.finditer(
        r'\{[^{}]*"name"\s*:\s*"[^"]+"\s*,\s*"(?:arguments|parameters|input)"\s*:\s*\{.*?\}\s*\}',
        content,
        re.DOTALL,
    ):
        tc = _try_parse_name_args_json(m.group(0))
        if tc:
            calls.append(tc)
    return calls


# Priority 30: Inline backtick CLI hallucination
# Format: `bash --command="ls /tmp"`  or  `bash(ls /tmp)`  or  `bash "ls /tmp"`
@parser(priority=30, name="inline_backtick_cli")
def _parse_inline_backtick_cli(content: str, tool_names: list[str]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for m in re.finditer(
        r'`bash\s*(?:-{0,2}command\s*=\s*)?["\']([^"\']+)["\']\s*`'
        r'|`bash\s*\(([^)]+)\)`',
        content,
        re.IGNORECASE,
    ):
        cmd = (m.group(1) or m.group(2) or "").strip()
        if cmd:
            calls.append(ToolCall(name="bash", arguments={"command": cmd}))
    return calls


# ─── Helpers shared by parsers ──────────────────────────────────────────

def _try_parse_name_args_json(raw: str) -> Optional[ToolCall]:
    """Parse a JSON string that should have {name, arguments} keys."""
    parsed = _normalize_value(raw)
    if isinstance(parsed, dict):
        return _tc_from_name_args_dict(parsed)
    return None


def _tc_from_name_args_dict(d: dict) -> Optional[ToolCall]:
    """Build a ToolCall from a dict that looks like {name, arguments}."""
    # Accept many spelling variants
    name = (
        d.get("name")
        or d.get("tool_name")
        or d.get("function")
        or d.get("tool")
        or d.get("function_name")
        or ""
    )
    if not name:
        return None
    args = (
        d.get("arguments")
        or d.get("parameters")
        or d.get("params")
        or d.get("input")
        or d.get("args")
        or {}
    )
    if isinstance(args, str):
        args = _normalize_value(args)
    if not isinstance(args, dict):
        args = {"raw": args}
    return ToolCall(name=name, arguments=_normalize_args(args))


# ─── Main entry point ───────────────────────────────────────────────────

def parse_tool_calls(
    content: str,
    tool_names: list[str],
    structured_tool_calls: Optional[list[dict]] = None,
) -> list[ToolCall]:
    """
    Extract all tool calls from a model's streaming output.

    Args:
      content: The raw text content of the model's response.
      tool_names: List of tool names registered in the tool registry.
                  Used by several parsers to disambiguate function calls.
      structured_tool_calls: If the model emitted tool calls in the
                             structured OpenAI-style channel (delta.tool_calls),
                             pass them here. They're the highest-priority
                             source — parsing content is only a fallback.

    Returns:
      A list of ToolCall objects, normalized and ready for execution.
    """
    # Priority 1: structured tool_calls channel (OpenAI / native API)
    if structured_tool_calls:
        normalized = _normalize_structured_tool_calls(structured_tool_calls)
        if normalized:
            return normalized

    # Priority 2-N: content parsers in priority order. First parser that
    # returns a non-empty result wins. Each parser is independent; if
    # one of them crashes or returns junk, others still run.
    if not content:
        return []

    for priority, name, fn in _PARSERS:
        try:
            calls = fn(content, tool_names)
        except Exception as e:
            log.warning(f"Parser {name!r} raised {type(e).__name__}: {e}")
            continue
        if calls:
            log.debug(f"Parser {name!r} matched {len(calls)} call(s)")
            return calls

    return []


def _normalize_structured_tool_calls(raw: list[dict]) -> list[ToolCall]:
    """
    Normalize the structured `tool_calls` format from the OpenAI channel.
    Each entry can have various shapes depending on the backend.
    """
    result: list[ToolCall] = []
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        tc_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
        fn = tc.get("function", tc)
        name = fn.get("name", "") if isinstance(fn, dict) else ""
        args = fn.get("arguments", {}) if isinstance(fn, dict) else {}
        if isinstance(args, str):
            args = _normalize_value(args)
        if not isinstance(args, dict):
            args = {"raw": args}
        if not name:
            continue
        result.append(ToolCall(id=tc_id, name=name, arguments=_normalize_args(args)))
    return result


# ─── Tool-side argument coercion ────────────────────────────────────────
#
# Takes the raw `arguments` dict from a parsed tool call and coerces it
# to match the tool's declared schema. Handles argument aliases (so a
# tool can declare "description" with aliases ["desc", "step", "text"]
# and the model can use any of them) and type coercion (so `"true"`
# becomes `True` for a boolean parameter, etc.).


def coerce_tool_args(
    schema: dict,
    aliases: dict[str, list[str]],
    raw_args: dict,
) -> dict:
    """
    Coerce model-provided arguments to match a tool's JSON schema.

    Args:
      schema: The tool's JSON schema (from ToolSpec.input_schema).
      aliases: Map from canonical parameter name → list of accepted
               alternative names. E.g. {"description": ["desc", "step",
               "text", "title", "name"]}.
      raw_args: The parsed arguments dict from the tool call.

    Returns:
      A new dict with canonical parameter names and coerced value types.
      Unknown parameters are preserved (so tools that accept extras still
      get them).
    """
    if not isinstance(raw_args, dict):
        return raw_args

    properties = (schema or {}).get("properties", {}) if isinstance(schema, dict) else {}

    # Reverse lookup: alias → canonical name
    alias_to_canonical: dict[str, str] = {}
    for canonical, alts in aliases.items():
        alias_to_canonical[canonical.lower()] = canonical
        for alt in alts:
            alias_to_canonical[alt.lower()] = canonical

    coerced: dict = {}
    for raw_key, raw_val in raw_args.items():
        # Resolve the canonical name
        canonical_key = alias_to_canonical.get(str(raw_key).lower(), raw_key)

        # If this canonical key already has a value, prefer the one that's
        # more structured (list > dict > string). This handles the case
        # where a model emits both `steps` and `step_list` pointing at the
        # same field.
        if canonical_key in coerced:
            if _more_structured(raw_val, coerced[canonical_key]):
                coerced[canonical_key] = raw_val
            continue

        # Type coercion based on schema
        prop_schema = properties.get(canonical_key, {}) if isinstance(properties, dict) else {}
        coerced[canonical_key] = _coerce_to_schema(raw_val, prop_schema)

    return coerced


def _more_structured(a: Any, b: Any) -> bool:
    """Return True if `a` is 'more structured' than `b' for dedup purposes."""
    score_a = _structure_score(a)
    score_b = _structure_score(b)
    return score_a > score_b


def _structure_score(v: Any) -> int:
    if isinstance(v, list):
        return 3
    if isinstance(v, dict):
        return 2
    if isinstance(v, str) and v.strip().startswith(("[", "{")):
        return 1
    return 0


def _coerce_to_schema(value: Any, prop_schema: dict) -> Any:
    """
    Coerce a single value to match a JSON schema property definition.
    Idempotent for already-correct types.
    """
    if not isinstance(prop_schema, dict):
        return value

    target_type = prop_schema.get("type")
    if target_type is None:
        return value

    # string
    if target_type == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        if value is None:
            return ""
        # dict/list → JSON-serialize so the tool still gets something usable
        try:
            return json.dumps(value)
        except (TypeError, ValueError):
            return str(value)

    # integer
    if target_type == "integer":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                try:
                    return int(float(value.strip()))
                except ValueError:
                    return value
        return value

    # number (float)
    if target_type == "number":
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return value
        return value

    # boolean
    if target_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "yes", "1", "on", "t", "y"):
                return True
            if low in ("false", "no", "0", "off", "f", "n", ""):
                return False
        return value

    # array
    if target_type == "array":
        if isinstance(value, list):
            # Recursively coerce items if schema specifies item type
            item_schema = prop_schema.get("items", {})
            return [_coerce_to_schema(item, item_schema) for item in value]
        if isinstance(value, str):
            # Try to parse as structured value; fall back to split
            parsed = _normalize_value(value)
            if isinstance(parsed, list):
                item_schema = prop_schema.get("items", {})
                return [_coerce_to_schema(item, item_schema) for item in parsed]
            # Split on newlines as last resort
            lines = [l.strip(" -*•\t") for l in value.splitlines() if l.strip()]
            if len(lines) > 1:
                return lines
            if ";" in value:
                return [p.strip() for p in value.split(";") if p.strip()]
            return [value]
        if value is None:
            return []
        return [value]

    # object
    if target_type == "object":
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = _normalize_value(value)
            if isinstance(parsed, dict):
                return parsed
        return value

    return value
