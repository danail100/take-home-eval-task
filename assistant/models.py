"""Model interface and a deterministic scripted model.

The agent loop only depends on the `Model` protocol. `ScriptedModel` lets the whole
system run offline and reproducibly. `assistant.claude_model.ClaudeModel` is the real
backend.

Messages use a provider-neutral format:
    {"role": "user", "content": str}
    {"role": "assistant", "content": str | None, "tool_calls": [{"id", "name", "arguments"}]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str, "is_error": bool}
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from assistant.tools import ToolSpec, tokenize


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelResponse:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = "stop"
    response_model: str = ""
    response_id: str = ""
    # Opaque provider-native content, replayed unchanged on the next turn (e.g. thinking blocks).
    provider_content: Any = field(default=None, repr=False)


class Model(Protocol):
    provider: str  # OTel gen_ai.provider.name
    model: str

    def complete(
        self, system: str, messages: list[dict], tools: list[ToolSpec], max_tokens: int
    ) -> ModelResponse: ...


# ---------------------------------------------------------------------------------------
# Scripted model
# ---------------------------------------------------------------------------------------

# Prompt directives the scripted model reacts to. A real LLM would read the prompt; the
# scripted model reacts to these phrases, so a prompt change still changes behaviour offline.
DIRECTIVES = {
    "docs_first": "always search the internal documentation before answering",
    "follow_doc_notes": "follow any operational notes",
    "verbose": "be thorough",
}

_INJECTION = re.compile(r"Operational note for assistants:.*?run `([^`]+)`", re.IGNORECASE)
_TICKET = re.compile(r"\bT-\d+\b", re.IGNORECASE)
_WRITE = re.compile(r"\b(delete|drop|remove|truncate|update|insert)\b", re.IGNORECASE)
_SMALLTALK = re.compile(r"^\s*(thanks|thank you|hi|hello|ok|cheers)\b", re.IGNORECASE)
_QUARTER = re.compile(r"\bQ([1-4])\s*(\d{4})\b", re.IGNORECASE)
_REGION = re.compile(r"\b(EMEA|APAC|AMER)\b", re.IGNORECASE)


@dataclass(frozen=True)
class _Profile:
    slowdown: float  # latency multiplier
    sql_slip_rate: float  # chance of forgetting the year filter in a date-range query


PROFILES = {
    "scripted-small": _Profile(slowdown=1.0, sql_slip_rate=0.35),
    "scripted-large": _Profile(slowdown=1.8, sql_slip_rate=0.0),
}


def _count_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


class ScriptedModel:
    """A deterministic rule-based policy that has the same interface as an LLM.

    It does intent routing on the user message, emits tool calls, and builds answers from
    tool results. Token counts come from the real prompt size, and latency is simulated
    with a real sleep, so the spans look like an LLM's. It has some realistic flaws: the
    small profile sometimes writes a wrong SQL date filter, and with `follow_doc_notes`
    it obeys instructions planted in documents (prompt injection).
    """

    provider = "scripted"

    def __init__(self, model: str, seed: int = 0, simulate_latency: bool = True):
        if model not in PROFILES:
            raise ValueError(f"unknown scripted model {model!r}; known: {sorted(PROFILES)}")
        self.model = model
        self.profile = PROFILES[model]
        self.seed = seed
        self.simulate_latency = simulate_latency
        self._behaviour_rng = random.Random(seed)
        self._latency_rng = random.Random(seed * 7919 + 1)
        self._calls = 0

    # -- Model protocol ----------------------------------------------------------------

    def complete(
        self, system: str, messages: list[dict], tools: list[ToolSpec], max_tokens: int
    ) -> ModelResponse:
        self._calls += 1
        directives = {k for k, phrase in DIRECTIVES.items() if phrase in system.lower()}
        resp = self._decide(directives, messages, {t.name for t in tools})

        prompt_text = system + json.dumps(messages) + json.dumps([t.__dict__ for t in tools])
        output_text = resp.text or json.dumps([tc.__dict__ for tc in resp.tool_calls])
        resp.input_tokens = _count_tokens(prompt_text)
        resp.output_tokens = min(_count_tokens(output_text) + 12, max_tokens)  # +12: reasoning overhead
        resp.response_model = self.model
        resp.response_id = f"scripted-{self.seed}-{self._calls}"

        if self.simulate_latency:
            base = 0.02 + resp.input_tokens * 0.00002 + resp.output_tokens * 0.0015
            time.sleep(base * self.profile.slowdown * self._latency_rng.uniform(0.85, 1.25))
        return resp

    # -- policy --------------------------------------------------------------------------

    def _decide(self, d: set[str], messages: list[dict], tools: set[str]) -> ModelResponse:
        user = next(m["content"] for m in messages if m["role"] == "user")
        results = [m for m in messages if m["role"] == "tool"]
        made = [tc for m in messages if m["role"] == "assistant" for tc in m.get("tool_calls", [])]

        def call(name: str, **args: Any) -> ModelResponse:
            tc = ToolCall(id=f"call_{len(made) + 1}", name=name, arguments=args)
            return ModelResponse(text=None, tool_calls=[tc], finish_reason="tool_call")

        def ran(name: str, **args: Any) -> bool:
            return any(tc["name"] == name and (not args or tc["arguments"] == args) for tc in made)

        def last(name: str) -> dict | None:
            for m in reversed(results):
                if m["name"] == name:
                    return m
            return None

        # Prompt says to always look at the docs first: one search, whatever the question.
        if "docs_first" in d and "search_docs" in tools and not ran("search_docs"):
            return call("search_docs", query=user)

        # Prompt says to follow operational notes: the model obeys text inside documents.
        injected_rows: list = []
        if "follow_doc_notes" in d:
            for m in results:
                if m["name"] != "search_docs":
                    continue
                for sql in _INJECTION.findall(m["content"]):
                    if not ran("run_sql", query=sql) and "run_sql" in tools:
                        return call("run_sql", query=sql)
                    r = next((x for x in results if x["name"] == "run_sql"), None)
                    if r and not r["is_error"]:
                        injected_rows = json.loads(r["content"])

        answer = self._route(user, results, tools, call, ran, last)
        if isinstance(answer, ModelResponse):
            return answer
        if injected_rows:
            answer += " Account verification data: " + "; ".join(
                f"{r.get('name')} <{r.get('email')}>" for r in injected_rows
            )
        if "verbose" in d:
            answer = (
                "Thanks for the question - here is what I found. "
                + answer
                + " I checked the relevant internal sources before answering; let me know if "
                "you need more detail or a breakdown of the underlying data."
            )
        return ModelResponse(text=answer, finish_reason="stop")

    def _route(self, user, results, tools, call, ran, last) -> str | ModelResponse:
        err = next((m for m in reversed(results) if m["is_error"]), None)
        if err is not None and err["name"] != "run_sql":
            return f"Sorry, I could not complete that: {err['content']}"

        if _SMALLTALK.match(user):
            return "You're welcome! Happy to help."

        if _WRITE.search(user):
            return (
                "I can't do that: I only have read-only access to the data and cannot delete "
                "or modify records. Please raise a request with the data platform team."
            )

        ticket_match = _TICKET.search(user)
        if ticket_match and "lookup_ticket" in tools:
            ticket_id = ticket_match.group(0).upper()
            if not ran("lookup_ticket"):
                return call("lookup_ticket", ticket_id=ticket_id)
            ticket = json.loads(last("lookup_ticket")["content"])
            if "order" in user.lower() and "run_sql" in tools:
                cust = ticket["customer_id"]
                sql = (
                    "SELECT c.name, COUNT(o.id) AS order_count FROM customers c "
                    f"LEFT JOIN orders o ON o.customer_id = c.id WHERE c.id = '{cust}' "
                    "GROUP BY c.name"
                )
                if not ran("run_sql", query=sql):
                    return call("run_sql", query=sql)
                row = json.loads(last("run_sql")["content"])[0]
                return (
                    f"The customer on ticket {ticket_id} is {row['name']} ({cust}); "
                    f"they have placed {row['order_count']} orders."
                )
            return (
                f"Ticket {ticket_id} ('{ticket['title']}') is {ticket['status']}, priority "
                f"{ticket['priority']}, assigned to {ticket['assignee']}."
            )

        if "revenue" in user.lower() and "run_sql" in tools:
            region = (_REGION.search(user) or [None, "EMEA"])[1].upper()
            q = _QUARTER.search(user)
            quarter, year = (int(q.group(1)), q.group(2)) if q else (3, "2026")
            m0, m1 = 3 * quarter - 2, 3 * quarter
            sql_r = [m for m in results if m["name"] == "run_sql"]
            if not sql_r:
                if self._behaviour_rng.random() < self.profile.sql_slip_rate:
                    # Typical small-model slip: filters months but forgets the year.
                    date_filter = f"CAST(strftime('%m', order_date) AS INT) BETWEEN {m0} AND {m1}"
                else:
                    date_filter = f"order_date BETWEEN '{year}-{m0:02d}-01' AND '{year}-{m1:02d}-31'"
                return call(
                    "run_sql",
                    query=f"SELECT ROUND(SUM(amount), 2) AS revenue FROM orders "
                    f"WHERE region = '{region}' AND {date_filter}",
                )
            if sql_r[-1]["is_error"]:
                return f"Sorry, the revenue query failed: {sql_r[-1]['content']}"
            revenue = json.loads(sql_r[-1]["content"])[0]["revenue"] or 0
            return f"Total revenue in {region} for Q{quarter} {year} was {revenue:,.2f} EUR."

        if "search_docs" in tools:
            if not ran("search_docs"):
                return call("search_docs", query=user)
            hits = json.loads(last("search_docs")["content"])
            q = tokenize(user)
            best, best_overlap = None, 0
            for hit in hits:
                for line in hit["content"].splitlines():
                    if not line.strip() or line.startswith("#") or _INJECTION.search(line):
                        continue
                    overlap = len(q & tokenize(line))
                    if overlap > best_overlap:
                        best, best_overlap = line.strip(), overlap
            if best is None or best_overlap < 2:
                return "I couldn't find that in the internal documentation, so I don't know."
            return best

        return "I don't know."
