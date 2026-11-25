"""Rebuild per-trial views from stored OTel spans, and derive metrics from them.

This is the only place that interprets span attributes. Graders and the comparison step
work on `TrialTrace` and never touch raw spans. To read traces from Langfuse or another
backend instead of `spans.jsonl`, write a different loader that yields the same view.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evalkit import pricing
from evalkit.telemetry import RUN_ID, SEED, TASK_ID, TRIAL


@dataclass
class ToolCallView:
    name: str
    call_id: str
    arguments: dict[str, Any] | None
    result: str | None
    is_error: bool
    duration_s: float


@dataclass
class ChatView:
    model: str
    input_tokens: int
    output_tokens: int
    duration_s: float
    finish_reasons: list[str]


@dataclass
class TrialTrace:
    run_id: str
    task_id: str
    trial: int
    seed: int
    trace_id: str
    status: str
    final_answer: str
    latency_s: float
    chats: list[ChatView] = field(default_factory=list)
    tools: list[ToolCallView] = field(default_factory=list)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.chats)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.chats)

    @property
    def tool_call_count(self) -> int:
        return len(self.tools)

    @property
    def llm_calls(self) -> int:
        return len(self.chats)

    def cost_usd(self) -> float:
        return sum(pricing.cost_usd(c.model, c.input_tokens, c.output_tokens) for c in self.chats)

    def metrics(self) -> dict[str, float]:
        return {
            "cost_usd": self.cost_usd(),
            "latency_s": self.latency_s,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "tool_calls": self.tool_call_count,
            "llm_calls": self.llm_calls,
        }


def _dur(span: dict) -> float:
    return (span["endTimeUnixNano"] - span["startTimeUnixNano"]) / 1e9


def _text_from_messages(raw: str | None) -> str:
    if not raw:
        return ""
    msgs = json.loads(raw)
    return "".join(
        p.get("content", "") for m in msgs for p in m.get("parts", []) if p.get("type") == "text"
    )


def build_trial(spans: list[dict]) -> TrialTrace:
    """Build a TrialTrace from all the spans of a single trace."""
    by_op: dict[str, list[dict]] = defaultdict(list)
    root = None
    for s in sorted(spans, key=lambda s: s["startTimeUnixNano"]):
        attrs = s["attributes"]
        if TASK_ID in attrs:
            root = s
        by_op[attrs.get("gen_ai.operation.name", "")].append(s)
    if root is None:
        raise ValueError("trace has no eval.trial root span")
    agent = by_op["invoke_agent"][0] if by_op["invoke_agent"] else None
    ra = root["attributes"]
    aa = agent["attributes"] if agent else {}

    tools = []
    for s in by_op["execute_tool"]:
        a = s["attributes"]
        args = a.get("gen_ai.tool.call.arguments")
        tools.append(
            ToolCallView(
                name=a["gen_ai.tool.name"],
                call_id=a.get("gen_ai.tool.call.id", ""),
                arguments=json.loads(args) if args else None,
                result=a.get("gen_ai.tool.call.result"),
                is_error=s["status"]["code"] == "STATUS_CODE_ERROR",
                duration_s=_dur(s),
            )
        )
    chats = [
        ChatView(
            model=s["attributes"].get("gen_ai.response.model") or s["attributes"]["gen_ai.request.model"],
            input_tokens=int(s["attributes"].get("gen_ai.usage.input_tokens", 0)),
            output_tokens=int(s["attributes"].get("gen_ai.usage.output_tokens", 0)),
            duration_s=_dur(s),
            finish_reasons=list(s["attributes"].get("gen_ai.response.finish_reasons", [])),
        )
        for s in by_op["chat"]
    ]
    return TrialTrace(
        run_id=ra[RUN_ID],
        task_id=ra[TASK_ID],
        trial=int(ra[TRIAL]),
        seed=int(ra.get(SEED, 0)),
        trace_id=root["traceId"],
        status=aa.get("assistant.run.status", "error" if agent is None else "unknown"),
        final_answer=_text_from_messages(aa.get("gen_ai.output.messages")),
        latency_s=_dur(agent) if agent else _dur(root),
        chats=chats,
        tools=tools,
    )


def load_traces(run_dir: str | Path) -> dict[tuple[str, int], TrialTrace]:
    spans_by_trace: dict[str, list[dict]] = defaultdict(list)
    with open(Path(run_dir) / "spans.jsonl") as f:
        for line in f:
            if line.strip():
                s = json.loads(line)
                spans_by_trace[s["traceId"]].append(s)
    trials = (build_trial(spans) for spans in spans_by_trace.values())
    return {(t.task_id, t.trial): t for t in trials}
