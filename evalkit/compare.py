"""Stage 3: compare two graded runs (A = current/baseline, B = candidate).

1. Comparability checks. The two runs must have been graded with the same graders and the
   same task definitions, on the same fixtures. Otherwise a difference could come from the
   eval changing rather than the assistant changing. Failed checks raise
   ComparabilityError unless allow_mismatch=True, in which case they become warnings and
   only the tasks with identical definitions are compared.
2. A paired, per-task comparison on two separate axes, quality and efficiency, plus an
   overall recommendation.

Cost is computed here from token counts, with one price table for both runs.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evalkit import pricing
from evalkit.graders import QUALITY_DIMENSIONS
from evalkit.tasks import load_tasks
from evalkit.traceview import TrialTrace, load_traces


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


@dataclass
class RunData:
    run_dir: Path
    manifest: dict
    grades_meta: dict
    grades: list[dict]
    traces: dict[tuple[str, int], TrialTrace]

    @property
    def run_id(self) -> str:
        return self.manifest["run_id"]


def _grade_ids(run_dir: Path) -> list[tuple[str, str]]:
    """(created_at, grades_id) for every grading of a run, oldest first."""
    out = []
    for p in (run_dir / "grades").glob("*.meta.json"):
        meta = json.loads(p.read_text())
        out.append((meta["created_at"], meta["grades_id"]))
    return sorted(out)


def load_run(run_dir: str | Path, grades_id: str | None = None) -> RunData:
    run_dir = Path(run_dir)
    ids = _grade_ids(run_dir)
    if not ids:
        raise FileNotFoundError(f"{run_dir} has not been graded; run `evalkit grade {run_dir}`")
    grades_id = grades_id or ids[-1][1]
    meta = json.loads((run_dir / "grades" / f"{grades_id}.meta.json").read_text())
    with open(run_dir / "grades" / f"{grades_id}.jsonl") as f:
        grades = [json.loads(line) for line in f if line.strip()]
    return RunData(
        run_dir=run_dir,
        manifest=json.loads((run_dir / "manifest.json").read_text()),
        grades_meta=meta,
        grades=grades,
        traces=load_traces(run_dir),
    )


def load_pair(run_a: str | Path, run_b: str | Path) -> tuple[RunData, RunData]:
    """Load two runs, preferring the most recent grading the two have in common."""
    ids_a = {g for _, g in _grade_ids(Path(run_a))}
    common = [g for _, g in _grade_ids(Path(run_b)) if g in ids_a]
    gid = common[-1] if common else None
    return load_run(run_a, gid), load_run(run_b, gid)


# --------------------------------------------------------------------------------------
# Comparability
# --------------------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info
    detail: str


class ComparabilityError(RuntimeError):
    def __init__(self, checks: list[Check], *, hint: str = "(pass --allow-mismatch to compare anyway)"):
        self.checks = checks
        failed = "\n".join(f"  - {c.name}: {c.detail}" for c in checks if c.status == "fail")
        msg = f"runs are not comparable:\n{failed}"
        if hint:
            msg += f"\n{hint}"
        super().__init__(msg)


def check_comparability(a: RunData, b: RunData) -> tuple[list[Check], set[str]]:
    """Return the checks and the set of task ids that can be compared safely."""
    checks: list[Check] = []

    ga, gb = a.grades_meta, b.grades_meta
    if ga["grader_set_hash"] == gb["grader_set_hash"]:
        names = ", ".join(f"{g['name']}@{g['version']}" for g in ga["graders"])
        checks.append(Check("grader set", "ok", f"identical ({ga['grader_set_hash']}: {names})"))
    else:
        fa = {g["name"]: g for g in ga["graders"]}
        fb = {g["name"]: g for g in gb["graders"]}
        diffs = [n for n in sorted(set(fa) | set(fb)) if fa.get(n) != fb.get(n)]
        checks.append(Check("grader set", "fail", f"graders differ: {diffs} (A={fa}, B={fb})"))

    da, db = ga["task_definition_hashes"], gb["task_definition_hashes"]
    same = {t for t in set(da) & set(db) if da[t] == db[t]}
    changed = sorted(t for t in set(da) & set(db) if da[t] != db[t])
    only = sorted(set(da) ^ set(db))
    if not changed and not only:
        checks.append(Check("task definitions", "ok", f"{len(same)} tasks, identical definitions"))
    else:
        checks.append(
            Check(
                "task definitions",
                "fail",
                f"changed: {changed or '-'}; in one run only: {only or '-'}; comparable: {len(same)}",
            )
        )

    fa_, fb_ = a.manifest["fixtures_hash"], b.manifest["fixtures_hash"]
    checks.append(
        Check("fixtures", "ok" if fa_ == fb_ else "fail", f"A={fa_} B={fb_}" if fa_ != fb_ else f"identical ({fa_})")
    )

    ta, tb = a.manifest["trials"], b.manifest["trials"]
    checks.append(Check("trials", "ok" if ta == tb else "warn", f"A={ta} B={tb}"))

    pa, pb = a.manifest["provider"], b.manifest["provider"]
    checks.append(Check("provider", "ok" if pa == pb else "warn", f"A={pa} B={pb}"))

    ha, hb = a.manifest["assistant"]["config_hash"], b.manifest["assistant"]["config_hash"]
    if ha == hb:
        ca = a.manifest["assistant"].get("code") or {}
        cb = b.manifest["assistant"].get("code") or {}
        sha_a, sha_b = ca.get("git_sha"), cb.get("git_sha")
        dirty_a, dirty_b = ca.get("dirty"), cb.get("dirty")
        if sha_a != sha_b:
            checks.append(
                Check(
                    "assistant config",
                    "info",
                    f"config identical ({ha}) but code version differs (A={sha_a}, B={sha_b})",
                )
            )
        elif dirty_a or dirty_b:
            checks.append(
                Check(
                    "assistant config",
                    "warn",
                    f"config identical ({ha}) but working tree was dirty (A={dirty_a}, B={dirty_b}), so code may differ",
                )
            )
        else:
            checks.append(
                Check("assistant config", "warn", f"identical ({ha}): this is an A/A run, so differences are noise")
            )
    else:
        checks.append(Check("assistant config", "info", f"A={ha} B={hb} (expected to differ; see diff below)"))

    checks.append(Check("pricing", "ok", f"both runs priced with table {pricing.PRICE_TABLE_VERSION}"))
    return checks, same


# --------------------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------------------


@dataclass
class TrialOutcome:
    trial: int
    quality_pass: bool
    efficiency_pass: bool
    failures: list[tuple[str, str]]  # (grader, reason)
    metrics: dict[str, float]
    answer: str


@dataclass
class TaskStats:
    task_id: str
    trials: list[TrialOutcome]

    def _med(self, key: str) -> float:
        vals = [t.metrics[key] for t in self.trials if t.metrics]
        return statistics.median(vals) if vals else float("nan")

    @property
    def pass_rate(self) -> float:
        return sum(t.quality_pass for t in self.trials) / len(self.trials)

    @property
    def pass_all(self) -> bool:  # pass^k
        return all(t.quality_pass for t in self.trials)

    @property
    def budget_rate(self) -> float:
        return sum(t.efficiency_pass for t in self.trials) / len(self.trials)

    @property
    def flaky(self) -> bool:
        return 0 < self.pass_rate < 1

    cost = property(lambda self: self._med("cost_usd"))
    latency = property(lambda self: self._med("latency_s"))
    tokens = property(lambda self: self._med("total_tokens"))
    tool_calls = property(lambda self: self._med("tool_calls"))


def task_stats(run: RunData, task_ids: set[str]) -> dict[str, TaskStats]:
    by_trial: dict[tuple[str, int], list[dict]] = {}
    for g in run.grades:
        by_trial.setdefault((g["task_id"], g["trial"]), []).append(g)
    out = {}
    for tid in sorted(task_ids):
        outcomes = []
        for k in range(run.manifest["trials"]):
            trace = run.traces.get((tid, k))
            grades = by_trial.get((tid, k), [])
            if trace is None:  # missing trace counts as a failure
                outcomes.append(TrialOutcome(k, False, False, [("harness", "no trace")], {}, ""))
                continue
            q = [g for g in grades if g["dimension"] in QUALITY_DIMENSIONS]
            e = [g for g in grades if g["dimension"] == "efficiency"]
            failures = [(g["grader"], g["reason"]) for g in grades if not g["passed"]]
            if trace.status != "ok":
                failures.append(("harness", f"agent status is {trace.status!r}"))
            if not q:
                failures.append(("harness", "no quality graders applied"))
            outcomes.append(
                TrialOutcome(
                    trial=k,
                    quality_pass=trace.status == "ok" and bool(q) and all(g["passed"] for g in q),
                    efficiency_pass=all(g["passed"] for g in e),
                    failures=failures,
                    metrics=trace.metrics(),
                    answer=trace.final_answer,
                )
            )
        out[tid] = TaskStats(tid, outcomes)
    return out


def _pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return s[idx]


def aggregate(stats: dict[str, TaskStats]) -> dict[str, float]:
    trials = [t for s in stats.values() for t in s.trials]
    m = [t.metrics for t in trials if t.metrics]
    costs = [x["cost_usd"] for x in m]
    passed = sum(t.quality_pass for t in trials)
    return {
        "quality_pass_rate": passed / len(trials),
        "pass_all_rate": sum(s.pass_all for s in stats.values()) / len(stats),
        "budget_pass_rate": sum(t.efficiency_pass for t in trials) / len(trials),
        "total_cost_usd": sum(costs),
        "mean_cost_usd": statistics.mean(costs) if costs else float("nan"),
        "cost_per_success_usd": sum(costs) / passed if passed else float("inf"),
        "latency_p50_s": _pct([x["latency_s"] for x in m], 50),
        "latency_p95_s": _pct([x["latency_s"] for x in m], 95),
        "mean_total_tokens": statistics.mean(x["total_tokens"] for x in m) if m else float("nan"),
        "mean_tool_calls": statistics.mean(x["tool_calls"] for x in m) if m else float("nan"),
        "mean_llm_calls": statistics.mean(x["llm_calls"] for x in m) if m else float("nan"),
    }


@dataclass
class TaskDelta:
    task_id: str
    tags: list[str]
    safety: bool
    a: TaskStats
    b: TaskStats
    quality: str  # better | same | worse
    efficiency: str  # cheaper | same | costlier | mixed
    cost_ratio: float
    latency_ratio: float


@dataclass
class Comparison:
    a: RunData
    b: RunData
    checks: list[Check]
    tasks: list[TaskDelta]
    agg_a: dict[str, float]
    agg_b: dict[str, float]
    quality: str
    efficiency: str
    recommendation: str
    reasons: list[str] = field(default_factory=list)
    thresholds: dict[str, Any] = field(default_factory=dict)


def _ratio(b: float, a: float) -> float:
    return b / a if a else float("inf") if b else 1.0


def classify_efficiency(cost_ratio: float, latency_ratio: float, tol: float, same: str = "same") -> str:
    """Classify B/A cost and latency ratios as costlier, cheaper, mixed, or `same`.

    A metric "rises" when its ratio is above 1 + tol and "falls" when it is below 1 - tol.
    mixed means one metric rose and the other fell, so neither costlier nor cheaper is honest.
    """
    ratios = (cost_ratio, latency_ratio)
    rose = any(r > 1 + tol for r in ratios)
    fell = any(r < 1 - tol for r in ratios)
    if rose and fell:
        return "mixed"
    if rose:
        return "costlier"
    if fell:
        return "cheaper"
    return same


def compare(
    run_a: str | Path,
    run_b: str | Path,
    tasks_dir: str | Path = "tasks",
    allow_mismatch: bool = False,
    efficiency_tolerance: float = 0.10,
) -> Comparison:
    a, b = load_pair(run_a, run_b)
    checks, comparable = check_comparability(a, b)
    if any(c.status == "fail" for c in checks) and not allow_mismatch:
        raise ComparabilityError(checks)
    for c in checks:
        if c.status == "fail":
            c.status = "warn"
            c.detail += " [overridden with --allow-mismatch]"
    if not comparable:
        checks.append(Check("comparable tasks", "fail", "no comparable tasks in common between runs"))
        raise ComparabilityError(checks, hint="")

    defs = load_tasks(tasks_dir)
    sa, sb = task_stats(a, comparable), task_stats(b, comparable)
    tol = efficiency_tolerance

    deltas = []
    for tid in sorted(comparable):
        ta, tb = sa[tid], sb[tid]
        d = tb.pass_rate - ta.pass_rate
        quality = "better" if d > 1e-9 else "worse" if d < -1e-9 else "same"
        cr, lr = _ratio(tb.cost, ta.cost), _ratio(tb.latency, ta.latency)
        eff = classify_efficiency(cr, lr, tol)
        task = defs.get(tid)
        tags = task.tags if task else []
        deltas.append(TaskDelta(tid, tags, "safety" in tags, ta, tb, quality, eff, cr, lr))

    agg_a, agg_b = aggregate(sa), aggregate(sb)
    worse = [t for t in deltas if t.quality == "worse"]
    better = [t for t in deltas if t.quality == "better"]
    quality = "regressed" if worse else "improved" if better else "unchanged"

    cost_r = _ratio(agg_b["mean_cost_usd"], agg_a["mean_cost_usd"])
    lat_r = _ratio(agg_b["latency_p50_s"], agg_a["latency_p50_s"])
    efficiency = classify_efficiency(cost_r, lat_r, tol, same="unchanged")

    reasons = []
    for t in worse:
        kind = "SAFETY regression" if t.safety else "regression"
        reasons.append(f"{kind} on `{t.task_id}`: pass rate {t.a.pass_rate:.0%} -> {t.b.pass_rate:.0%}")
    for t in better:
        reasons.append(f"improvement on `{t.task_id}`: pass rate {t.a.pass_rate:.0%} -> {t.b.pass_rate:.0%}")
    reasons.append(f"mean cost per request x{cost_r:.2f}, p50 latency x{lat_r:.2f}")

    if quality == "regressed":
        rec = "BLOCK"
    elif efficiency == "costlier":
        rec = "REVIEW"
        reasons.append(
            "quality "
            + ("improved" if quality == "improved" else "did not improve")
            + " but the change is costlier, so a human needs to decide if it is worth it"
        )
    elif efficiency == "mixed":
        rec = "REVIEW"
        rose, fell = ("mean cost", "p50 latency") if cost_r > lat_r else ("p50 latency", "mean cost")
        reasons.append(
            f"efficiency is mixed: {rose} rose and {fell} fell beyond ±{tol:.0%}, "
            "so a human needs to decide if the trade-off is worth it"
        )
    else:
        rec = "SHIP"

    return Comparison(
        a=a, b=b, checks=checks, tasks=deltas, agg_a=agg_a, agg_b=agg_b,
        quality=quality, efficiency=efficiency, recommendation=rec, reasons=reasons,
        thresholds={"efficiency_tolerance": tol, "quality": "any drop in per-task pass rate"},
    )
