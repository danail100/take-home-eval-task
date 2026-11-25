"""Assistant configuration: the part that changes week to week (prompt, model, tools)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class AssistantConfig:
    name: str
    system_prompt: str
    # model id per provider, e.g. {"scripted": "scripted-small", "anthropic": "claude-haiku-5-5"}
    models: dict[str, str]
    tools: list[str] = field(default_factory=lambda: ["search_docs", "run_sql", "lookup_ticket"])
    max_steps: int = 6
    max_tokens: int = 1024

    def model_for(self, provider: str) -> str:
        if provider not in self.models:
            raise ValueError(f"config {self.name!r} defines no model for provider {provider!r}")
        return self.models[provider]

    def to_dict(self) -> dict:
        return asdict(self)

    def config_hash(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def load_config(path: str | Path) -> AssistantConfig:
    raw = yaml.safe_load(Path(path).read_text())
    return AssistantConfig(**raw)
