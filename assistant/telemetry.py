"""OpenTelemetry instrumentation for the assistant (GenAI semantic conventions).

The assistant depends only on `opentelemetry-api`. Whoever hosts it (the production
service, or the eval framework) configures the SDK and exporters. With no SDK configured,
every call here is a no-op.

Span layout per request:
    invoke_agent {agent}            gen_ai.operation.name=invoke_agent
      chat {model}                  gen_ai.operation.name=chat   (one per model call)
      execute_tool {tool}           gen_ai.operation.name=execute_tool (one per tool call)
      ...

Message content (prompts, answers, tool arguments and results) is opt-in, as in the
conventions. Set OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true to enable it.
"""

from __future__ import annotations

import json
import os
from typing import Any

from opentelemetry import trace

tracer = trace.get_tracer("assistant", "0.1.0")

# Attribute names (from the OTel GenAI semantic conventions).
OP = "gen_ai.operation.name"
PROVIDER = "gen_ai.provider.name"
AGENT_NAME = "gen_ai.agent.name"
AGENT_ID = "gen_ai.agent.id"
REQ_MODEL = "gen_ai.request.model"
REQ_MAX_TOKENS = "gen_ai.request.max_tokens"
RESP_MODEL = "gen_ai.response.model"
RESP_ID = "gen_ai.response.id"
FINISH = "gen_ai.response.finish_reasons"
IN_TOK = "gen_ai.usage.input_tokens"
OUT_TOK = "gen_ai.usage.output_tokens"
SYSTEM = "gen_ai.system_instructions"
IN_MSGS = "gen_ai.input.messages"
OUT_MSGS = "gen_ai.output.messages"
TOOL_DEFS = "gen_ai.tool.definitions"
TOOL_NAME = "gen_ai.tool.name"
TOOL_TYPE = "gen_ai.tool.type"
TOOL_CALL_ID = "gen_ai.tool.call.id"
TOOL_ARGS = "gen_ai.tool.call.arguments"
TOOL_RESULT = "gen_ai.tool.call.result"
ERROR_TYPE = "error.type"

# Assistant-specific attributes. These are not part of the conventions, so they get their own namespace.
CONFIG_NAME = "assistant.config.name"
CONFIG_HASH = "assistant.config.hash"
RUN_STATUS = "assistant.run.status"
RUN_STEPS = "assistant.run.steps"


def capture_content_default() -> bool:
    return os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "").lower() in {
        "1", "true", "yes",
    }


def to_otel_messages(messages: list[dict]) -> str:
    """Convert neutral messages to the GenAI conventions' `parts` structure (as JSON)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "parts": [{"type": "text", "content": m["content"]}]})
        elif m["role"] == "assistant":
            parts: list[dict[str, Any]] = []
            if m.get("content"):
                parts.append({"type": "text", "content": m["content"]})
            for tc in m.get("tool_calls", []):
                parts.append(
                    {"type": "tool_call", "id": tc["id"], "name": tc["name"], "arguments": tc["arguments"]}
                )
            out.append({"role": "assistant", "parts": parts})
        elif m["role"] == "tool":
            out.append(
                {
                    "role": "tool",
                    "parts": [
                        {"type": "tool_call_response", "id": m["tool_call_id"], "response": m["content"]}
                    ],
                }
            )
    return json.dumps(out)
