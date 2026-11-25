from pathlib import Path

from evalkit.graders.base import QUALITY_DIMENSIONS, Grader, GradeResult, grader_set_hash
from evalkit.graders.budget import BudgetGrader
from evalkit.graders.llm_judge import JudgeGrader
from evalkit.graders.outcome import OutcomeGrader
from evalkit.graders.trajectory import TrajectoryGrader

DEFAULT_GRADERS = ["outcome", "trajectory", "judge", "budget"]


def build_graders(
    names: list[str], judge_backend: str = "auto", judge_cache: str | Path | None = None
) -> list[Grader]:
    factories = {
        "outcome": OutcomeGrader,
        "trajectory": TrajectoryGrader,
        "budget": BudgetGrader,
        "judge": lambda: JudgeGrader(backend=judge_backend, cache_path=judge_cache),
    }
    unknown = set(names) - set(factories)
    if unknown:
        raise ValueError(f"unknown graders {sorted(unknown)}; available: {sorted(factories)}")
    return [factories[n]() for n in names]


__all__ = [
    "QUALITY_DIMENSIONS", "Grader", "GradeResult", "grader_set_hash", "build_graders",
    "DEFAULT_GRADERS", "BudgetGrader", "JudgeGrader", "OutcomeGrader", "TrajectoryGrader",
]
