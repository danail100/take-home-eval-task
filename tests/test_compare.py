"""Focused unit tests for evalkit.compare edge cases and comparability checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalkit.cli import main as cli_main
from evalkit.compare import (
    ComparabilityError,
    RunData,
    check_comparability,
    classify_efficiency,
    compare,
    task_stats,
)
from evalkit.report import markdown_to_html, render_markdown
from evalkit.telemetry import RUN_ID, SEED, TASK_ID, TRIAL
from evalkit.traceview import ChatView, TrialTrace

ROOT = Path(__file__).resolve().parent.parent


def _make_trace(
    task_id: str = "t1",
    trial: int = 0,
    status: str = "ok",
    latency_s: float = 1.0,
    input_tokens: int = 1000,
    output_tokens: int = 200,
) -> TrialTrace:
    return TrialTrace(
        run_id="r",
        task_id=task_id,
        trial=trial,
        seed=0,
        trace_id=f"trace-{task_id}-{trial}",
        status=status,
        final_answer="done",
        latency_s=latency_s,
        chats=[
            ChatView(
                model="scripted-small",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_s=latency_s,
                finish_reasons=["end_turn"],
            )
        ],
    )


def _make_run(
    run_id: str = "r",
    *,
    config_hash: str = "cfg1",
    code: dict | None = None,
    task_hashes: dict[str, str] | None = None,
    grades: list[dict] | None = None,
    traces: dict[tuple[str, int], TrialTrace] | None = None,
    trials: int = 1,
) -> RunData:
    task_hashes = task_hashes if task_hashes is not None else {"t1": "h1"}
    traces = traces if traces is not None else {("t1", 0): _make_trace()}
    grades = grades if grades is not None else [
        {"task_id": "t1", "trial": 0, "grader": "outcome", "dimension": "correctness", "passed": True, "reason": "ok"}
    ]
    return RunData(
        run_dir=Path(f"/tmp/{run_id}"),
        manifest={
            "run_id": run_id,
            "fixtures_hash": "fix1",
            "trials": trials,
            "provider": "scripted",
            "assistant": {
                "config_hash": config_hash,
                "config": {},
                "code": code if code is not None else {"git_sha": "abc123", "dirty": False},
            },
        },
        grades_meta={
            "grades_id": "g1",
            "created_at": "2026-01-01T00:00:00Z",
            "grader_set_hash": "gs1",
            "graders": [{"name": "outcome", "version": "1"}],
            "task_definition_hashes": task_hashes,
        },
        grades=grades,
        traces=traces,
    )


def _write_run_dir(
    base: Path,
    run_id: str,
    *,
    task_hashes: dict[str, str],
    latency_s: float = 1.0,
    input_tokens: int = 1000,
    output_tokens: int = 200,
    config_hash: str = "cfg",
) -> Path:
    rd = base / run_id
    (rd / "grades").mkdir(parents=True)
    manifest = {
        "run_id": run_id,
        "created_at": "2026-01-01T00:00:00Z",
        "fixtures_hash": "fix1",
        "trials": 1,
        "provider": "scripted",
        "assistant": {
            "model": "scripted-small",
            "config_hash": config_hash,
            "config": {},
            "code": {"git_sha": "abc123", "dirty": False},
        },
    }
    (rd / "manifest.json").write_text(json.dumps(manifest))
    meta = {
        "grades_id": "g1",
        "created_at": "2026-01-01T00:00:00Z",
        "grader_set_hash": "gs1",
        "graders": [{"name": "outcome", "version": "1"}],
        "task_definition_hashes": task_hashes,
        "task_definition_set_hash": "ts1",
    }
    (rd / "grades" / "g1.meta.json").write_text(json.dumps(meta))

    spans = []
    grade_rows = []
    dur_ns = int(latency_s * 1e9)
    for i, tid in enumerate(sorted(task_hashes)):
        trace_id = f"{i + 1:032x}"
        spans.append(
            {
                "traceId": trace_id,
                "spanId": f"{i + 1:016x}",
                "startTimeUnixNano": 0,
                "endTimeUnixNano": dur_ns,
                "status": {"code": "STATUS_CODE_OK"},
                "attributes": {
                    RUN_ID: run_id,
                    TASK_ID: tid,
                    TRIAL: 0,
                    SEED: 0,
                },
            }
        )
        spans.append(
            {
                "traceId": trace_id,
                "spanId": f"{i + 100:016x}",
                "startTimeUnixNano": 0,
                "endTimeUnixNano": dur_ns,
                "status": {"code": "STATUS_CODE_OK"},
                "attributes": {
                    "gen_ai.operation.name": "invoke_agent",
                    "assistant.run.status": "ok",
                    "gen_ai.output.messages": json.dumps([{"parts": [{"type": "text", "content": "ok"}]}]),
                },
            }
        )
        spans.append(
            {
                "traceId": trace_id,
                "spanId": f"{i + 200:016x}",
                "startTimeUnixNano": 0,
                "endTimeUnixNano": dur_ns,
                "status": {"code": "STATUS_CODE_OK"},
                "attributes": {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "scripted-small",
                    "gen_ai.response.model": "scripted-small",
                    "gen_ai.usage.input_tokens": input_tokens,
                    "gen_ai.usage.output_tokens": output_tokens,
                    "gen_ai.response.finish_reasons": ["end_turn"],
                },
            }
        )
        grade_rows.append(
            {
                "task_id": tid,
                "trial": 0,
                "grader": "outcome",
                "dimension": "correctness",
                "passed": True,
                "reason": "ok",
            }
        )

    (rd / "spans.jsonl").write_text("\n".join(json.dumps(s) for s in spans) + "\n")
    (rd / "grades" / "g1.jsonl").write_text("\n".join(json.dumps(g) for g in grade_rows) + "\n")
    return rd


# --------------------------------------------------------------------------------------
# 1. task_stats quality_pass checks
# --------------------------------------------------------------------------------------


def test_task_stats_fails_quality_when_trace_status_not_ok():
    run = _make_run(
        traces={("t1", 0): _make_trace(status="max_steps")},
        grades=[
            {"task_id": "t1", "trial": 0, "grader": "outcome", "dimension": "correctness", "passed": True, "reason": "ok"}
        ],
    )
    outcome = task_stats(run, {"t1"})["t1"].trials[0]
    assert outcome.quality_pass is False
    assert ("harness", "agent status is 'max_steps'") in outcome.failures


def test_task_stats_fails_quality_when_no_quality_graders_applied():
    run = _make_run(
        traces={("t1", 0): _make_trace(status="ok")},
        grades=[
            {"task_id": "t1", "trial": 0, "grader": "budget", "dimension": "efficiency", "passed": True, "reason": "ok"}
        ],
    )
    outcome = task_stats(run, {"t1"})["t1"].trials[0]
    assert outcome.quality_pass is False
    assert outcome.efficiency_pass is True
    assert ("harness", "no quality graders applied") in outcome.failures


def test_task_stats_passes_quality_when_status_ok_and_quality_graders_pass():
    run = _make_run()
    outcome = task_stats(run, {"t1"})["t1"].trials[0]
    assert outcome.quality_pass is True
    assert outcome.failures == []


# --------------------------------------------------------------------------------------
# 2. Empty comparable guard
# --------------------------------------------------------------------------------------


def test_compare_raises_comparability_error_when_no_comparable_tasks(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    a = _write_run_dir(tmp_path, "a", task_hashes={"t1": "h1"}, config_hash="cfg-a")
    b = _write_run_dir(tmp_path, "b", task_hashes={"t2": "h2"}, config_hash="cfg-b")

    with pytest.raises(ComparabilityError, match="no comparable tasks in common"):
        compare(a, b, tasks_dir=tasks_dir, allow_mismatch=True)

    rc = cli_main(["compare", str(a), str(b), "--tasks", str(tasks_dir), "--allow-mismatch"])
    assert rc == 2


# --------------------------------------------------------------------------------------
# 3. A/A check vs code changes
# --------------------------------------------------------------------------------------


def test_check_comparability_aa_clean_same_sha():
    a = _make_run("a", config_hash="same", code={"git_sha": "deadbeef", "dirty": False})
    b = _make_run("b", config_hash="same", code={"git_sha": "deadbeef", "dirty": False})
    checks, _ = check_comparability(a, b)
    cfg = next(c for c in checks if c.name == "assistant config")
    assert cfg.status == "warn"
    assert "A/A run" in cfg.detail


def test_check_comparability_same_config_different_git_sha():
    a = _make_run("a", config_hash="same", code={"git_sha": "aaa111", "dirty": False})
    b = _make_run("b", config_hash="same", code={"git_sha": "bbb222", "dirty": False})
    checks, _ = check_comparability(a, b)
    cfg = next(c for c in checks if c.name == "assistant config")
    assert cfg.status == "info"
    assert "code version differs" in cfg.detail
    assert "A/A" not in cfg.detail


def test_check_comparability_same_config_dirty_tree():
    a = _make_run("a", config_hash="same", code={"git_sha": "deadbeef", "dirty": False})
    b = _make_run("b", config_hash="same", code={"git_sha": "deadbeef", "dirty": True})
    checks, _ = check_comparability(a, b)
    cfg = next(c for c in checks if c.name == "assistant config")
    assert cfg.status == "warn"
    assert "dirty" in cfg.detail
    assert "A/A" not in cfg.detail


# --------------------------------------------------------------------------------------
# 4. Symmetric efficiency classification
# --------------------------------------------------------------------------------------


def test_compare_classifies_latency_improvement_at_same_cost_as_cheaper(tmp_path):
    a = _write_run_dir(tmp_path, "a", task_hashes={"t1": "h1"}, latency_s=2.0, config_hash="cfg-a")
    b = _write_run_dir(tmp_path, "b", task_hashes={"t1": "h1"}, latency_s=1.0, config_hash="cfg-b")

    c = compare(a, b, tasks_dir=ROOT / "tasks")
    assert c.tasks[0].efficiency == "cheaper"
    assert c.efficiency == "cheaper"
    assert c.recommendation == "SHIP"


def test_compare_classifies_cost_rise_at_same_latency_as_costlier(tmp_path):
    a = _write_run_dir(tmp_path, "a", task_hashes={"t1": "h1"}, latency_s=1.0, input_tokens=1000, config_hash="cfg-a")
    b = _write_run_dir(tmp_path, "b", task_hashes={"t1": "h1"}, latency_s=1.0, input_tokens=2000, config_hash="cfg-b")

    c = compare(a, b, tasks_dir=ROOT / "tasks")
    assert c.tasks[0].efficiency == "costlier"
    assert c.efficiency == "costlier"
    assert c.recommendation == "REVIEW"
    assert any("costlier" in r for r in c.reasons)


def test_compare_classifies_cheaper_cost_but_slower_latency_as_mixed(tmp_path):
    a = _write_run_dir(tmp_path, "a", task_hashes={"t1": "h1"}, latency_s=1.0, input_tokens=2000, config_hash="cfg-a")
    b = _write_run_dir(tmp_path, "b", task_hashes={"t1": "h1"}, latency_s=1.5, input_tokens=1000, config_hash="cfg-b")

    c = compare(a, b, tasks_dir=ROOT / "tasks")
    assert c.tasks[0].cost_ratio < 0.9 and c.tasks[0].latency_ratio > 1.1
    assert c.tasks[0].efficiency == "mixed"
    assert c.efficiency == "mixed"
    assert c.recommendation == "REVIEW"
    assert any("p50 latency rose and mean cost fell" in r for r in c.reasons)


def test_compare_classifies_costlier_but_faster_as_mixed(tmp_path):
    a = _write_run_dir(tmp_path, "a", task_hashes={"t1": "h1"}, latency_s=2.0, input_tokens=1000, config_hash="cfg-a")
    b = _write_run_dir(tmp_path, "b", task_hashes={"t1": "h1"}, latency_s=1.0, input_tokens=1300, config_hash="cfg-b")

    c = compare(a, b, tasks_dir=ROOT / "tasks")
    assert c.tasks[0].efficiency == "mixed"
    assert c.efficiency == "mixed"
    assert c.recommendation == "REVIEW"
    assert any("mean cost rose and p50 latency fell" in r for r in c.reasons)


@pytest.mark.parametrize(
    "cost_r, lat_r, expected",
    [
        (1.0, 1.0, "same"),
        (1.05, 0.95, "same"),  # both within tolerance
        (1.3, 1.0, "costlier"),
        (1.0, 1.3, "costlier"),
        (1.3, 1.3, "costlier"),
        (0.7, 1.0, "cheaper"),
        (1.0, 0.5, "cheaper"),
        (0.7, 0.5, "cheaper"),
        (1.3, 0.5, "mixed"),
        (0.7, 1.5, "mixed"),
        (float("inf"), 0.5, "mixed"),
    ],
)
def test_classify_efficiency(cost_r, lat_r, expected):
    assert classify_efficiency(cost_r, lat_r, 0.10) == expected


def test_classify_efficiency_uses_custom_same_label():
    assert classify_efficiency(1.0, 1.0, 0.10, same="unchanged") == "unchanged"
    assert classify_efficiency(1.3, 0.5, 0.10, same="unchanged") == "mixed"


def test_mixed_efficiency_does_not_override_block(tmp_path):
    a = _write_run_dir(tmp_path, "a", task_hashes={"t1": "h1"}, latency_s=2.0, input_tokens=1000, config_hash="cfg-a")
    b = _write_run_dir(tmp_path, "b", task_hashes={"t1": "h1"}, latency_s=1.0, input_tokens=1300, config_hash="cfg-b")
    grades = b / "grades" / "g1.jsonl"
    grades.write_text(grades.read_text().replace('"passed": true', '"passed": false'))

    c = compare(a, b, tasks_dir=ROOT / "tasks")
    assert c.efficiency == "mixed"
    assert c.quality == "regressed"
    assert c.recommendation == "BLOCK"
    assert not any("mixed" in r for r in c.reasons)


def test_report_grid_has_mixed_column(tmp_path):
    a = _write_run_dir(tmp_path, "a", task_hashes={"t1": "h1"}, latency_s=2.0, input_tokens=1000, config_hash="cfg-a")
    b = _write_run_dir(tmp_path, "b", task_hashes={"t1": "h1"}, latency_s=1.0, input_tokens=1300, config_hash="cfg-b")

    c = compare(a, b, tasks_dir=ROOT / "tasks")
    md = render_markdown(c)
    assert "| quality \\ efficiency | cheaper | same | costlier | mixed |" in md
    assert "| **same** | · | · | · | `t1` |" in md
    assert "<th>mixed</th>" in markdown_to_html(md, "t")
