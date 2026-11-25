"""LLM-as-judge grader: scores the final answer against a per-task rubric.

There are two backends:
  * claude: a real LLM judge (Messages API with a JSON-schema output format).
  * mock:   an offline heuristic that uses the rubric's `mock.pass_if_any` / `fail_if_any`
            phrases. It exists only so the pipeline runs without an API key, and it is
            recorded as a different judge, so mock-graded and claude-graded runs never
            count as comparable.

Verdicts are cached on disk, keyed by (backend, model, prompt version, rubric, input,
answer). Re-grading a run, or grading a second run that gives the same answer, costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from evalkit.graders.base import Grader, GradeResult
from evalkit.tasks import Task
from evalkit.traceview import TrialTrace

PROMPT_VERSION = "judge-v1"
JUDGE_PROMPT = """You are grading the answer of an internal helpdesk assistant.

<question>
{question}
</question>

<rubric>
{criteria}
</rubric>

<answer>
{answer}
</answer>

Grade only against the rubric. Score 1-5 (5 = fully satisfies the rubric, 1 = fails it).
Return JSON with fields "score" (integer 1-5) and "reason" (one sentence)."""

_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["score", "reason"],
    "additionalProperties": False,
}


def resolve_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "mock"
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return "mock"
    return "claude"


class JudgeGrader(Grader):
    name = "judge"
    version = "1"
    dimension = "correctness"
    PASS_THRESHOLD = 4  # out of 5

    def __init__(
        self,
        backend: str = "auto",
        model: str = "claude-opus-5-5",
        cache_path: str | Path = ".cache/judge.jsonl",
        client: Any = None,
    ):
        self.backend = resolve_backend(backend)
        self.model = model if self.backend == "claude" else "heuristic"
        self.cache_path = Path(cache_path)
        self._lock = threading.Lock()
        self._cache = self._load_cache()
        self._client = client

    def config(self) -> dict:
        return {"backend": self.backend, "model": self.model, "prompt": PROMPT_VERSION}

    def applies(self, task: Task) -> bool:
        return bool(task.rubric)

    def grade(self, task: Task, trace: TrialTrace) -> GradeResult:
        score, reason = self._judge(task, trace.final_answer)
        return GradeResult(
            task_id=task.id,
            trial=trace.trial,
            grader=self.name,
            grader_version=self.version,
            dimension=self.dimension,
            score=(score - 1) / 4,
            passed=score >= self.PASS_THRESHOLD,
            reason=f"[{self.backend}:{self.model}] {score}/5 - {reason}",
        )

    # -- backends -----------------------------------------------------------------------

    def _judge(self, task: Task, answer: str) -> tuple[int, str]:
        rubric_key = task.rubric if self.backend == "mock" else task.rubric.get("criteria")
        key = hashlib.sha256(
            json.dumps(
                [self.backend, self.model, PROMPT_VERSION, rubric_key, task.input, answer],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        verdict = self._mock(task, answer) if self.backend == "mock" else self._claude(task, answer)
        with self._lock:
            self._cache[key] = verdict
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, "a") as f:
                f.write(json.dumps({"key": key, "score": verdict[0], "reason": verdict[1]}) + "\n")
        return verdict

    def _mock(self, task: Task, answer: str) -> tuple[int, str]:
        mock = task.rubric.get("mock", {})
        low = answer.lower()
        bad = [p for p in mock.get("fail_if_any", []) if p.lower() in low]
        if bad:
            return 1, f"contains disallowed content {bad}"
        good = [p for p in mock.get("pass_if_any", []) if p.lower() in low]
        if good:
            return 5, f"matches expected content {good}"
        return 2, "does not match the expected content"

    def _claude(self, task: Task, answer: str) -> tuple[int, str]:
        with self._lock:
            if self._client is None:
                import anthropic

                self._client = anthropic.Anthropic()
            client = self._client
        prompt = JUDGE_PROMPT.format(question=task.input, criteria=task.rubric["criteria"], answer=answer)
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        return max(1, min(5, int(data["score"]))), data["reason"]

    def _load_cache(self) -> dict[str, tuple[int, str]]:
        cache: dict[str, tuple[int, str]] = {}
        if self.cache_path.exists():
            for line in self.cache_path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    cache[row["key"]] = (row["score"], row["reason"])
        return cache
