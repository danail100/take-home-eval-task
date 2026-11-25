"""Offline test of the Claude adapter, using a fake client (no network, no API key)."""

from types import SimpleNamespace as NS

import pytest

pytest.importorskip("anthropic")

from assistant.agent import Agent  # noqa: E402
from assistant.claude_model import ClaudeModel  # noqa: E402
from assistant.config import load_config  # noqa: E402
from assistant.tools import Toolbox  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class FakeMessages:
    def __init__(self):
        self.requests = []

    def create(self, **kw):
        self.requests.append(kw)
        usage = NS(input_tokens=500, output_tokens=40)
        if len(self.requests) == 1:
            content = [
                NS(type="thinking", thinking="", signature="sig"),
                NS(type="tool_use", id="toolu_1", name="lookup_ticket", input={"ticket_id": "T-1050"}),
            ]
            return NS(content=content, stop_reason="tool_use", usage=usage, model="claude-haiku-5-5", id="msg_1")
        content = [NS(type="text", text="T-1050 is waiting on customer, assigned to Ravi Patel.")]
        return NS(content=content, stop_reason="end_turn", usage=usage, model="claude-haiku-5-5", id="msg_2")


def test_claude_adapter_tool_loop_and_block_replay():
    fake = NS(messages=FakeMessages())
    cfg = load_config(ROOT / "configs/baseline.yaml")
    agent = Agent(cfg, ClaudeModel("claude-haiku-5-5", client=fake), Toolbox(ROOT / "fixtures", simulate_latency=False))
    result = agent.run("Status of T-1050?")
    assert result.status == "ok" and "Ravi Patel" in result.answer

    second = fake.messages.requests[1]["messages"]
    # The assistant turn is replayed with its original blocks (thinking included).
    assert second[1]["role"] == "assistant" and second[1]["content"][0].type == "thinking"
    # The tool result goes back as a user turn with a tool_result block.
    assert second[2]["role"] == "user" and second[2]["content"][0]["type"] == "tool_result"
    assert second[2]["content"][0]["tool_use_id"] == "toolu_1"
    assert fake.messages.requests[0]["system"] == cfg.system_prompt
