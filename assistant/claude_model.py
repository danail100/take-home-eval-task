"""Claude backend for the Model protocol (Anthropic Messages API, manual tool loop).

The agent loop is our own code, not the SDK tool runner, so that it is provider-neutral
and instrumented the same way for every backend. This adapter turns the neutral messages
into Messages API calls.

Thinking/content blocks are passed back to the API unchanged: each assistant message
keeps the raw response content under `provider_content`, as the API requires.
"""

from __future__ import annotations

import anthropic

from assistant.models import ModelResponse, ToolCall
from assistant.tools import ToolSpec


class ClaudeModel:
    provider = "anthropic"

    def __init__(self, model: str, client: anthropic.Anthropic | None = None):
        self.model = model
        self.client = client or anthropic.Anthropic()

    def _to_api_messages(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                content = m.get("provider_content")
                if content is None:  # message not produced by this backend: rebuild it
                    content = []
                    if m.get("content"):
                        content.append({"type": "text", "text": m["content"]})
                    for tc in m.get("tool_calls", []):
                        content.append(
                            {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]}
                        )
                out.append({"role": "assistant", "content": content})
            elif m["role"] == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": m["content"],
                    "is_error": m["is_error"],
                }
                # All results for one assistant turn go back in a single user message.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    def complete(
        self, system: str, messages: list[dict], tools: list[ToolSpec], max_tokens: int
    ) -> ModelResponse:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            tools=[
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ],
            messages=self._to_api_messages(messages),
        )
        text = "".join(b.text for b in response.content if b.type == "text") or None
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]
        if response.stop_reason == "refusal":
            text = text or "[refused]"
        return ModelResponse(
            text=text,
            tool_calls=calls,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            finish_reason=response.stop_reason or "",
            response_model=response.model,
            response_id=response.id,
            provider_content=response.content,
        )
