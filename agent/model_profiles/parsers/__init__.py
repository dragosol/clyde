"""Tool-call parsers for various model families.

A parser takes the accumulated assistant text (everything the model has emitted
for this turn so far, concatenated) and returns either:
  - None: nothing detected yet / incomplete frame
  - list[dict]: completed tool-call dicts in the Clyde canonical shape:
        {"id": str, "name": str, "arguments_json": str, "index": int}

Parsers must be pure; they receive the full accumulated text so they can
re-detect anchors without keeping state across calls.
"""
