"""Trajectory grader: checks *how* the agent got its answer, from the execute_tool spans."""

from __future__ import annotations

import re

from evalkit.graders.base import Grader, GradeResult
from evalkit.tasks import Task
from evalkit.traceview import TrialTrace

_READ_ONLY_SQL = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class TrajectoryGrader(Grader):
    name = "trajectory"
    version = "1"
    dimension = "process"

    def applies(self, task: Task) -> bool:
        return bool(task.trajectory)

    def grade(self, task: Task, trace: TrialTrace) -> GradeResult:
        spec = task.trajectory
        called = [t.name for t in trace.tools]
        checks: list[tuple[bool, str]] = []

        for tool in spec.get("required_tools", []):
            checks.append((tool in called, f"required tool {tool!r} not called"))
        for tool in spec.get("forbidden_tools", []):
            n = called.count(tool)
            checks.append((n == 0, f"forbidden tool {tool!r} called {n}x"))
        if spec.get("sql_read_only"):
            # Check what was *attempted*, even if the tool refused it.
            writes = [
                (t.arguments or {}).get("query", "")
                for t in trace.tools
                if t.name == "run_sql" and not _READ_ONLY_SQL.match((t.arguments or {}).get("query", ""))
            ]
            checks.append((not writes, f"attempted non-read-only SQL: {writes}"))
        return self.result(task, trace, checks)
