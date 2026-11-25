"""Stage 1: inference. Run one assistant config on every task for N trials and store the spans.

This is the only expensive stage, the one that calls models. Everything after it
(grading, comparing, reporting) reads the files written here:

    runs/<run_id>/manifest.json   what was run: config, code version, task and fixture hashes
    runs/<run_id>/spans.jsonl     every OTel span emitted, one JSON object per line
"""

from __future__ import annotations

import json
import subprocess
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry.trace import Status, StatusCode

from assistant import build_agent, load_config
from evalkit import telemetry
from evalkit.tasks import Task, hash_directory, load_tasks, task_set_fingerprint


def trial_seed(task_id: str, trial: int) -> int:
    return zlib.crc32(f"{task_id}:{trial}".encode())


def code_version() -> dict:
    """Git version of the assistant code, so a result can be traced back to a commit."""

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()

    try:
        sha = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--", "assistant", "configs"))
        return {"git_sha": sha, "dirty": dirty}
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"git_sha": None, "dirty": None}


def _run_trial(run_id, task: Task, trial, config_path, fixtures_dir, provider) -> None:
    seed = trial_seed(task.id, trial)
    with telemetry.eval_tracer.start_as_current_span(
        f"eval.trial {task.id}",
        attributes={
            telemetry.RUN_ID: run_id,
            telemetry.TASK_ID: task.id,
            telemetry.TRIAL: trial,
            telemetry.SEED: seed,
        },
    ) as span:
        try:
            agent = build_agent(
                config_path, fixtures_dir, provider=provider, seed=seed, capture_content=True
            )
            agent.run(task.input)
        except Exception as e:  # harness crash: still produce a trace so it gets graded as a failure
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))


def infer(
    config_path: str | Path,
    tasks_dir: str | Path = "tasks",
    fixtures_dir: str | Path = "fixtures",
    runs_dir: str | Path = "runs",
    trials: int = 3,
    provider: str = "scripted",
    run_id: str | None = None,
    concurrency: int = 8,
) -> Path:
    config = load_config(config_path)
    tasks = load_tasks(tasks_dir)
    run_id = run_id or f"{config.name}-{provider}-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir = Path(runs_dir) / run_id
    if run_dir.exists():
        raise FileExistsError(f"{run_dir} already exists")
    run_dir.mkdir(parents=True)

    collector = telemetry.setup_tracing()
    collector.drain()

    jobs = [(t, k) for t in tasks.values() for k in range(trials)]
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(_run_trial, run_id, t, k, config_path, fixtures_dir, provider) for t, k in jobs
        ]
        for f in futures:
            f.result()
    telemetry.flush()
    spans = sorted(collector.drain(), key=lambda s: s.start_time)

    with open(run_dir / "spans.jsonl", "w") as f:
        for s in spans:
            f.write(json.dumps(telemetry.span_to_dict(s)) + "\n")

    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "assistant": {
            "config_path": str(config_path),
            "config": config.to_dict(),
            "config_hash": config.config_hash(),
            "model": config.model_for(provider),
            "code": code_version(),
        },
        "tasks": task_set_fingerprint(tasks),
        "fixtures_hash": hash_directory(fixtures_dir),
        "trials": trials,
        "seeds": {t.id: [trial_seed(t.id, k) for k in range(trials)] for t in tasks.values()},
        "telemetry": {"format": "otlp-json-flat-attributes", "semconv": "gen_ai (OTel GenAI)"},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return run_dir
