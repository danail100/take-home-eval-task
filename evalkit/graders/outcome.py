"""Deterministic outcome grader: string, regex and numeric checks on the final answer."""

from __future__ import annotations

import re

from evalkit.graders.base import Grader, GradeResult
from evalkit.tasks import Task
from evalkit.traceview import TrialTrace

_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def extract_numbers(text: str) -> list[float]:
    out = []
    for m in _NUMBER.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


class OutcomeGrader(Grader):
    name = "outcome"
    version = "1"
    dimension = "correctness"

    def applies(self, task: Task) -> bool:
        return bool(task.expected)

    def grade(self, task: Task, trace: TrialTrace) -> GradeResult:
        exp = task.expected
        answer = trace.final_answer
        low = answer.lower()
        checks: list[tuple[bool, str]] = [(trace.status == "ok", f"agent status is {trace.status!r}")]

        for s in exp.get("contains", []):
            checks.append((s.lower() in low, f"missing {s!r}"))
        if exp.get("any_of"):
            opts = exp["any_of"]
            checks.append((any(o.lower() in low for o in opts), f"none of {opts!r} present"))
        for s in exp.get("not_contains", []):
            checks.append((s.lower() not in low, f"contains forbidden {s!r}"))
        if "regex" in exp:
            checks.append((re.search(exp["regex"], answer) is not None, f"no match for /{exp['regex']}/"))
        if "numeric" in exp:
            target, tol = float(exp["numeric"]["value"]), float(exp["numeric"].get("tolerance", 0))
            nums = extract_numbers(answer)
            checks.append(
                (any(abs(n - target) <= tol for n in nums), f"expected {target}±{tol}, found {nums}")
            )
        return self.result(task, trace, checks)
