"""Toy internal helpdesk assistant: the artifact that would ship to production.

This package must never import from `evalkit`. Its only observability contract is the
OpenTelemetry spans it emits (see assistant/telemetry.py).
"""

from assistant.agent import Agent, AgentResult, build_agent
from assistant.config import AssistantConfig, load_config

__all__ = ["Agent", "AgentResult", "AssistantConfig", "build_agent", "load_config"]
