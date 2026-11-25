"""The agent loop: model -> tools -> model ... -> final answer, instrumented with OTel."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from opentelemetry.trace import SpanKind, Status, StatusCode

from assistant import telemetry as t
from assistant.config import AssistantConfig, load_config
from assistant.models import Model, ScriptedModel
from assistant.tools import Toolbox, ToolError


@dataclass
class AgentResult:
    answer: str
    status: str  # ok | max_steps | error
    steps: int


class Agent:
    def __init__(
        self,
        config: AssistantConfig,
        model: Model,
        toolbox: Toolbox,
        capture_content: bool | None = None,
    ):
        self.config = config
        self.model = model
        self.toolbox = toolbox
        self.tool_specs = toolbox.specs(config.tools)
        self.capture_content = (
            t.capture_content_default() if capture_content is None else capture_content
        )

    def run(self, user_message: str) -> AgentResult:
        cfg = self.config
        with t.tracer.start_as_current_span(
            f"invoke_agent {cfg.name}", kind=SpanKind.INTERNAL
        ) as span:
            span.set_attributes(
                {
                    t.OP: "invoke_agent",
                    t.AGENT_NAME: "helpdesk-assistant",
                    t.AGENT_ID: cfg.name,
                    t.PROVIDER: self.model.provider,
                    t.REQ_MODEL: self.model.model,
                    t.CONFIG_NAME: cfg.name,
                    t.CONFIG_HASH: cfg.config_hash(),
                }
            )
            messages: list[dict] = [{"role": "user", "content": user_message}]
            if self.capture_content:
                span.set_attribute(t.IN_MSGS, t.to_otel_messages(messages))

            result = self._loop(messages)

            span.set_attribute(t.RUN_STATUS, result.status)
            span.set_attribute(t.RUN_STEPS, result.steps)
            if self.capture_content:
                span.set_attribute(
                    t.OUT_MSGS,
                    t.to_otel_messages([{"role": "assistant", "content": result.answer}]),
                )
            if result.status == "error":
                span.set_status(Status(StatusCode.ERROR, result.answer))
                span.set_attribute(t.ERROR_TYPE, "agent_error")
            return result

    def _loop(self, messages: list[dict]) -> AgentResult:
        for step in range(1, self.config.max_steps + 1):
            try:
                resp = self._chat(messages)
            except Exception as e:  # provider failure: record it and stop
                return AgentResult(answer=f"model error: {e}", status="error", steps=step)

            msg = {
                "role": "assistant",
                "content": resp.text,
                "tool_calls": [tc.__dict__ for tc in resp.tool_calls],
            }
            if resp.provider_content is not None:
                msg["provider_content"] = resp.provider_content
            messages.append(msg)
            if not resp.tool_calls:
                return AgentResult(answer=resp.text or "", status="ok", steps=step)

            for tc in resp.tool_calls:
                content, is_error = self._execute_tool(tc.id, tc.name, tc.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": content,
                        "is_error": is_error,
                    }
                )
        return AgentResult(
            answer="I could not finish within the step limit.",
            status="max_steps",
            steps=self.config.max_steps,
        )

    def _chat(self, messages: list[dict]):
        m = self.model
        with t.tracer.start_as_current_span(f"chat {m.model}", kind=SpanKind.CLIENT) as span:
            span.set_attributes(
                {
                    t.OP: "chat",
                    t.PROVIDER: m.provider,
                    t.REQ_MODEL: m.model,
                    t.REQ_MAX_TOKENS: self.config.max_tokens,
                }
            )
            if self.capture_content:
                span.set_attribute(t.SYSTEM, json.dumps([{"type": "text", "content": self.config.system_prompt}]))
                span.set_attribute(t.IN_MSGS, t.to_otel_messages(messages))
                span.set_attribute(
                    t.TOOL_DEFS, json.dumps([s.__dict__ for s in self.tool_specs])
                )
            try:
                resp = m.complete(
                    self.config.system_prompt, messages, self.tool_specs, self.config.max_tokens
                )
            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.set_attribute(t.ERROR_TYPE, type(e).__name__)
                raise
            span.set_attributes(
                {
                    t.RESP_MODEL: resp.response_model,
                    t.RESP_ID: resp.response_id,
                    t.FINISH: [resp.finish_reason],
                    t.IN_TOK: resp.input_tokens,
                    t.OUT_TOK: resp.output_tokens,
                }
            )
            if self.capture_content:
                span.set_attribute(
                    t.OUT_MSGS,
                    t.to_otel_messages(
                        [
                            {
                                "role": "assistant",
                                "content": resp.text,
                                "tool_calls": [tc.__dict__ for tc in resp.tool_calls],
                            }
                        ]
                    ),
                )
            return resp

    def _execute_tool(self, call_id: str, name: str, arguments: dict) -> tuple[str, bool]:
        with t.tracer.start_as_current_span(f"execute_tool {name}", kind=SpanKind.INTERNAL) as span:
            span.set_attributes(
                {t.OP: "execute_tool", t.TOOL_NAME: name, t.TOOL_TYPE: "function", t.TOOL_CALL_ID: call_id}
            )
            if self.capture_content:
                span.set_attribute(t.TOOL_ARGS, json.dumps(arguments))
            try:
                content, is_error = self.toolbox.call(name, arguments), False
            except ToolError as e:
                content, is_error = json.dumps({"error": str(e)}), True
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.set_attribute(t.ERROR_TYPE, "tool_error")
            if self.capture_content:
                span.set_attribute(t.TOOL_RESULT, content)
            return content, is_error


def build_agent(
    config_path: str | Path,
    fixtures_dir: str | Path,
    provider: str = "scripted",
    seed: int = 0,
    capture_content: bool | None = None,
) -> Agent:
    """Construct an agent from a config file. This is the only entry point the eval framework uses."""
    config = load_config(config_path)
    model_id = config.model_for(provider)
    model: Model
    if provider == "scripted":
        model = ScriptedModel(model_id, seed=seed)
    elif provider == "anthropic":
        from assistant.claude_model import ClaudeModel

        model = ClaudeModel(model_id)
    else:
        raise ValueError(f"unknown provider {provider!r}")
    return Agent(config, model, Toolbox(fixtures_dir), capture_content=capture_content)
