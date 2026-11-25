"""Unit tests for the Agent loop and tool execution guardrails."""

import json
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from assistant import telemetry as t
from assistant.agent import Agent
from assistant.config import AssistantConfig
from assistant.models import ModelResponse, ToolCall
from assistant.tools import Toolbox, ToolSpec

ROOT = Path(__file__).resolve().parent.parent


class _DisallowedToolModel:
    """Fake model that attempts to call a disabled tool, then finishes."""

    provider = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.seen_messages: list[list[dict]] = []

    def complete(
        self, system: str, messages: list[dict], tools: list[ToolSpec], max_tokens: int
    ) -> ModelResponse:
        self.seen_messages.append(list(messages))
        if len(self.seen_messages) == 1:
            return ModelResponse(
                text=None,
                tool_calls=[ToolCall(id="call_1", name="run_sql", arguments={"query": "SELECT 1"})],
                finish_reason="tool_calls",
                response_model=self.model,
                response_id="resp_1",
            )
        return ModelResponse(
            text="I cannot run SQL.",
            finish_reason="stop",
            response_model=self.model,
            response_id="resp_2",
        )


def test_disabled_tool_is_rejected_and_not_executed(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(t, "tracer", provider.get_tracer("assistant", "0.1.0"))

    cfg = AssistantConfig(
        name="no-sql",
        system_prompt="You are a helpful assistant.",
        models={"fake": "fake-model"},
        tools=["search_docs", "lookup_ticket"],
    )
    toolbox = Toolbox(ROOT / "fixtures", simulate_latency=False)

    executed = False

    def _fail_if_called(query: str):
        nonlocal executed
        executed = True
        raise AssertionError("disabled tool should never be executed")

    monkeypatch.setattr(toolbox, "run_sql", _fail_if_called)
    toolbox._impls["run_sql"] = _fail_if_called

    model = _DisallowedToolModel()
    agent = Agent(cfg, model, toolbox, capture_content=True)
    result = agent.run("Run a query for me.")

    assert result.status == "ok"
    assert not executed

    # Verify the model received an error tool result for the disabled tool.
    tool_msgs = [m for m in model.seen_messages[1] if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["is_error"] is True
    assert json.loads(tool_msgs[0]["content"]) == {"error": "tool not enabled: run_sql"}

    # Verify the execute_tool span was recorded as an error.
    tool_spans = [
        s for s in exporter.get_finished_spans() if s.attributes.get(t.OP) == "execute_tool"
    ]
    assert len(tool_spans) == 1
    span = tool_spans[0]
    assert span.attributes[t.TOOL_NAME] == "run_sql"
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes[t.ERROR_TYPE] == "tool_error"
    assert json.loads(span.attributes[t.TOOL_RESULT]) == {"error": "tool not enabled: run_sql"}

