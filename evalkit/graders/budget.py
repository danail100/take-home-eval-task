"""Budget grader: per-task efficiency limits (tool calls, tokens, latency, cost).

Its results go in the `efficiency` dimension, which is reported separately from quality. An answer
can be correct and still over budget. That is the "correct but 2x more expensive" case.
"""

from __future__ import annotations

from evalkit.graders.base import Grader, GradeResult
from evalkit.tasks import Task
from evalkit.traceview import TrialTrace

_LIMITS = {
    "max_tool_calls": ("tool_calls", "{:.0f}"),
    "max_total_tokens": ("total_tokens", "{:.0f}"),
    "max_latency_s": ("latency_s", "{:.3f}s"),
    "max_cost_usd": ("cost_usd", "${:.5f}"),
}


class BudgetGrader(Grader):
    name = "budget"
    version = "1"
    dimension = "efficiency"

    def applies(self, task: Task) -> bool:
        return bool(task.budget)

    def grade(self, task: Task, trace: TrialTrace) -> GradeResult:
        m = trace.metrics()
        checks = []
        for key, limit in task.budget.items():
            metric, fmt = _LIMITS[key]
            value = m[metric]
            checks.append(
                (value <= limit, f"{metric} {fmt.format(value)} > {fmt.format(limit)}")
            )
        return self.result(task, trace, checks)
