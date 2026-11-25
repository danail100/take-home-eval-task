"""Fake tools for the toy assistant.

They stand in for the real integrations (search, the analytics SQL warehouse and the
ticketing system). Their data comes from a fixtures directory, so an evaluation run is
hermetic: every run reads the same data, and the eval framework can hash that data.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "do", "does", "did", "i", "my", "me", "we",
    "our", "you", "your", "to", "of", "for", "in", "on", "at", "and", "or", "what", "whats",
    "how", "when", "who", "which", "can", "could", "should", "with", "it", "its", "be", "by",
    "there", "this", "that", "much", "many", "per", "s", "please", "tell", "about", "if",
}


def tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolError(Exception):
    """Raised by a tool for an expected failure, which is reported back to the model."""


class Toolbox:
    """The tool registry. `call` returns a JSON string, or raises ToolError."""

    def __init__(self, fixtures_dir: str | Path, simulate_latency: bool = True):
        self.fixtures_dir = Path(fixtures_dir)
        self.simulate_latency = simulate_latency
        self._docs = {
            p.stem: p.read_text() for p in sorted((self.fixtures_dir / "docs").glob("*.md"))
        }
        self._tickets = json.loads((self.fixtures_dir / "tickets.json").read_text())
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.executescript((self.fixtures_dir / "db.sql").read_text())
        self._db.execute("PRAGMA query_only = ON")  # defence in depth: the tool is read-only

        self._impls: dict[str, Callable[..., Any]] = {
            "search_docs": self.search_docs,
            "run_sql": self.run_sql,
            "lookup_ticket": self.lookup_ticket,
        }

    SPECS = {
        "search_docs": ToolSpec(
            "search_docs",
            "Full-text search over internal policy and how-to documents. Returns the best "
            "matching documents.",
            {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
        ),
        "run_sql": ToolSpec(
            "run_sql",
            "Run a read-only SQLite query against the analytics DB. Tables: "
            "customers(id, name, email, region), "
            "orders(id, customer_id, region, amount, order_date ISO text).",
            {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "A SELECT statement"}},
                "required": ["query"],
            },
        ),
        "lookup_ticket": ToolSpec(
            "lookup_ticket",
            "Fetch a support ticket by id (e.g. T-1042). Returns status, priority, assignee, "
            "customer_id.",
            {
                "type": "object",
                "properties": {"ticket_id": {"type": "string"}},
                "required": ["ticket_id"],
            },
        ),
    }

    def specs(self, enabled: list[str]) -> list[ToolSpec]:
        return [self.SPECS[n] for n in enabled]

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self._impls:
            raise ToolError(f"unknown tool: {name}")
        try:
            result = self._impls[name](**arguments)
        except TypeError as e:
            raise ToolError(f"bad arguments for {name}: {e}") from e
        return json.dumps(result)

    def _sleep(self, seconds: float) -> None:
        if self.simulate_latency:
            time.sleep(seconds)

    # --- tool implementations -------------------------------------------------------

    def search_docs(self, query: str) -> list[dict[str, Any]]:
        self._sleep(0.015)
        q = tokenize(query)
        scored = []
        for doc_id, text in self._docs.items():
            overlap = len(q & tokenize(text))
            if overlap >= 2:
                scored.append((overlap, doc_id, text))
        scored.sort(reverse=True)
        return [{"doc_id": d, "score": s, "content": t} for s, d, t in scored[:2]]

    def run_sql(self, query: str) -> list[dict[str, Any]]:
        self._sleep(0.008)
        if not re.match(r"^\s*(select|with)\b", query, re.IGNORECASE):
            raise ToolError("only read-only SELECT queries are allowed")
        try:
            cur = self._db.execute(query)
        except sqlite3.Error as e:
            raise ToolError(f"SQL error: {e}") from e
        cols = [c[0] for c in cur.description or []]
        return [dict(zip(cols, row)) for row in cur.fetchmany(50)]

    def lookup_ticket(self, ticket_id: str) -> dict[str, Any]:
        self._sleep(0.004)
        ticket = self._tickets.get(ticket_id.strip().upper())
        if ticket is None:
            raise ToolError(f"ticket {ticket_id} not found")
        return ticket
