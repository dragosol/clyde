"""
agent/model_profiles/registry.py
────────────────────────────────
ModelProfileRegistry — loads YAML profiles once at boot, compiles their
parsers, and exposes `resolve(model_id)` with fuzzy family fallback.

Zero hot-path reflection: every lookup is a dict get on pre-built state.
"""
from __future__ import annotations

import logging
import os
import typing as t
from pathlib import Path

import yaml

from model_profiles import ModelProfile, PhaseProfile, resolve_parser

log = logging.getLogger("model_profiles.registry")


class ModelProfileRegistry:
    """Holds every known ModelProfile keyed by id, with family fallback."""

    def __init__(self, profile_dir: Path | None = None) -> None:
        if profile_dir is None:
            profile_dir = Path(os.path.expanduser("~/.clyde/model_profiles"))
        self.profile_dir = Path(profile_dir)
        self._by_id: dict[str, ModelProfile] = {}
        self._by_family: dict[str, ModelProfile] = {}  # last-loaded wins
        self._load()

    def _load(self) -> None:
        if not self.profile_dir.exists():
            log.warning("profile dir %s missing; no profiles loaded", self.profile_dir)
            return
        count = 0
        for path in sorted(self.profile_dir.glob("*.yaml")):
            try:
                profile = self._load_one(path)
            except Exception as exc:
                log.error("skipping %s: %r", path.name, exc)
                continue
            self._by_id[profile.id] = profile
            self._by_family[profile.family] = profile
            count += 1
        log.info("loaded %d model profiles from %s", count, self.profile_dir)

    def _load_one(self, path: Path) -> ModelProfile:
        data = yaml.safe_load(path.read_text()) or {}
        if "id" not in data or "family" not in data:
            raise ValueError(f"{path}: missing required keys id/family")

        phase_overrides_raw = data.get("phase_overrides") or {}
        phase_overrides = {
            name: PhaseProfile(
                temperature=po.get("temperature"),
                top_p=po.get("top_p"),
                top_k=po.get("top_k"),
                repetition_penalty=po.get("repetition_penalty"),
                max_tokens_floor=po.get("max_tokens_floor"),
            )
            for name, po in phase_overrides_raw.items()
        }

        parser_path = data.get("tool_call_parser")
        parser = resolve_parser(parser_path) if parser_path else None

        return ModelProfile(
            id=data["id"],
            family=data["family"],
            context_window=int(data.get("context_window", 131_072)),
            thinking_directive=data.get("thinking_directive", "not_supported"),
            thinking_directive_payload=data.get("thinking_directive_payload") or {},
            thinking_mode_default=data.get("thinking_mode_default", "on"),
            tool_schema_strategy=data.get("tool_schema_strategy", "openai"),
            tool_call_parser=parser,
            tool_call_parser_path=parser_path,
            chat_template_quirks=data.get("chat_template_quirks") or {},
            sampling_defaults=data.get("sampling_defaults") or {},
            phase_overrides=phase_overrides,
            notes=data.get("notes", ""),
        )

    # ─── public API ───

    def resolve(self, model_id: str, family_hint: str | None = None) -> ModelProfile:
        """Return the profile for model_id, with family fallback and a loud default."""
        if profile := self._by_id.get(model_id):
            return profile
        if family_hint and (profile := self._by_family.get(family_hint)):
            log.warning("model_id %r not found; fell back to family %r", model_id, family_hint)
            return profile
        # Last-resort: a minimal "unknown" profile so the stack doesn't crash
        log.error("no profile for %r (family_hint=%r); using unknown fallback", model_id, family_hint)
        return ModelProfile(id=model_id, family=family_hint or "unknown")

    def all(self) -> list[ModelProfile]:
        return list(self._by_id.values())

    def reload(self) -> None:
        """Hot-reload profiles from disk. Safe to call from Clyde's Settings pane."""
        self._by_id.clear()
        self._by_family.clear()
        self._load()
