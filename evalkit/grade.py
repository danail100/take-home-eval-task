"""Stage 2: grading. Apply a set of graders to the stored traces of a run.

This stage never runs the agent. Its output goes to
    runs/<run_id>/grades/<grader_set_hash>-<task_definition_hash>.jsonl   (one GradeResult per row)
    runs/<run_id>/grades/<...>.meta.json                                   (what was used to grade)
so the same run can be graded many times with different graders or updated expectations.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from evalkit.graders import DEFAULT_GRADERS, build_graders, grader_set_hash
from evalkit.tasks import load_tasks, task_set_fingerprint
from evalkit.traceview import load_traces


class StaleTracesError(RuntimeError):
    """The task inputs changed after inference, so the traces no longer match the tasks."""


def grade(
    run_dir: str | Path,
    tasks_dir: str | Path = "tasks",
    grader_names: list[str] | None = None,
    judge_backend: str = "auto",
) -> Path:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    all_tasks = load_tasks(tasks_dir)

    # Grade only tasks that took part in inference, and only if their input is unchanged.
    ran = manifest["tasks"]["inference_hashes"]
    tasks = {}
    for tid, inf_hash in ran.items():
        if tid not in all_tasks:
            print(f"warning: task {tid!r} was run but no longer exists; skipping")
            continue
        if all_tasks[tid].inference_hash != inf_hash:
            raise StaleTracesError(
                f"task {tid!r} input changed since inference ({inf_hash} -> "
                f"{all_tasks[tid].inference_hash}); re-run `evalkit infer`"
            )
        tasks[tid] = all_tasks[tid]

    graders = build_graders(grader_names or DEFAULT_GRADERS, judge_backend=judge_backend)
    traces = load_traces(run_dir)
    fp = task_set_fingerprint(tasks)

    jobs = [(g, tasks[t.task_id], t) for t in traces.values() if t.task_id in tasks for g in graders]
    jobs = [(g, task, t) for g, task, t in jobs if g.applies(task)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda j: j[0].grade(j[1], j[2]), jobs))
    results.sort(key=lambda r: (r.task_id, r.trial, r.grader))

    missing = [
        (tid, k) for tid in tasks for k in range(manifest["trials"]) if (tid, k) not in traces
    ]
    if missing:
        print(f"warning: {len(missing)} trials have no trace: {missing}")

    gs_hash = grader_set_hash(graders)
    out_id = f"{gs_hash}-{fp['definition_set_hash']}"
    grades_dir = run_dir / "grades"
    grades_dir.mkdir(exist_ok=True)
    with open(grades_dir / f"{out_id}.jsonl", "w") as f:
        for r in results:
            f.write(json.dumps(r.to_dict()) + "\n")
    meta = {
        "grades_id": out_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "grader_set_hash": gs_hash,
        "graders": [g.fingerprint() for g in graders],
        "task_definition_hashes": fp["definition_hashes"],
        "task_definition_set_hash": fp["definition_set_hash"],
        "missing_trials": missing,
    }
    (grades_dir / f"{out_id}.meta.json").write_text(json.dumps(meta, indent=2))
    return grades_dir / f"{out_id}.jsonl"
