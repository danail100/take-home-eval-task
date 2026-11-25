"""Evaluation task definitions.

Each task has two hashes:
  * inference_hash: covers only what the agent sees (id + input). If it changes, the
    stored traces are stale and inference has to be re-run.
  * definition_hash: covers the whole task, expectations included. If only this one
    changes, the stored traces can be re-graded without new inference, but runs graded
    against different definitions are not comparable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Task:
    id: str
    input: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)
    trajectory: dict[str, Any] = field(default_factory=dict)
    rubric: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def safety_critical(self) -> bool:
        return "safety" in self.tags

    @property
    def inference_hash(self) -> str:
        return _hash({"id": self.id, "input": self.input})

    @property
    def definition_hash(self) -> str:
        return _hash(self.raw)


def load_tasks(tasks_dir: str | Path) -> dict[str, Task]:
    tasks: dict[str, Task] = {}
    for path in sorted(Path(tasks_dir).glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        task = Task(**raw, raw=raw)
        if task.id in tasks:
            raise ValueError(f"duplicate task id {task.id!r} in {path}")
        tasks[task.id] = task
    if not tasks:
        raise ValueError(f"no tasks found in {tasks_dir}")
    return tasks


def task_set_fingerprint(tasks: dict[str, Task]) -> dict[str, Any]:
    inference = {tid: t.inference_hash for tid, t in sorted(tasks.items())}
    definition = {tid: t.definition_hash for tid, t in sorted(tasks.items())}
    return {
        "inference_hashes": inference,
        "definition_hashes": definition,
        "inference_set_hash": _hash(inference),
        "definition_set_hash": _hash(definition),
    }


def hash_directory(path: str | Path) -> str:
    """Content hash of every file under `path`, used to fingerprint the fixtures."""
    h = hashlib.sha256()
    root = Path(path)
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]
