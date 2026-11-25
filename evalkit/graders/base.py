"""Grader interface.

A grader is a pure function of (task definition, stored trace) -> GradeResult. It never
calls the agent, so adding or changing a grader only needs `evalkit grade`, which is cheap,
and not `evalkit infer`.

Each grader declares a *dimension*. The comparison keeps dimensions separate, so
correctness is never traded off against cost silently:
    correctness: is the final answer right?
    process:     did the agent behave correctly (right tools, no forbidden actions)?
    efficiency:  did it stay within its latency, cost and tool-call budget?
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, ClassVar

from evalkit.tasks import Task
from evalkit.traceview import TrialTrace

QUALITY_DIMENSIONS = ("correctness", "process")


@dataclass
class GradeResult:
    task_id: str
    trial: int
    grader: str
    grader_version: str
    dimension: str
    score: float
    passed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Grader:
    name: ClassVar[str]
    version: ClassVar[str]
    dimension: ClassVar[str]

    def applies(self, task: Task) -> bool:
        raise NotImplementedError

    def grade(self, task: Task, trace: TrialTrace) -> GradeResult:
        raise NotImplementedError

    def config(self) -> dict[str, Any]:
        """Settings that affect results. They go into the fingerprint."""
        return {}

    def fingerprint(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "config": self.config()}

    def result(self, task: Task, trace: TrialTrace, checks: list[tuple[bool, str]]) -> GradeResult:
        """Helper: combine a list of (ok, description) checks into a GradeResult."""
        passed = sum(ok for ok, _ in checks)
        failed = [desc for ok, desc in checks if not ok]
        return GradeResult(
            task_id=task.id,
            trial=trace.trial,
            grader=self.name,
            grader_version=self.version,
            dimension=self.dimension,
            score=passed / len(checks) if checks else 1.0,
            passed=not failed,
            reason="ok" if not failed else "; ".join(failed),
        )


def grader_set_hash(graders: list[Grader]) -> str:
    fp = sorted((g.fingerprint() for g in graders), key=lambda f: f["name"])
    return hashlib.sha256(json.dumps(fp, sort_keys=True).encode()).hexdigest()[:12]
