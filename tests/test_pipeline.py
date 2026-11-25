"""End-to-end: infer -> grade -> compare, plus the comparability and staleness guarantees."""

import hashlib
import re
import shutil
from pathlib import Path

import pytest
import yaml

from evalkit.compare import ComparabilityError, compare
from evalkit.grade import StaleTracesError, grade
from evalkit.infer import infer
from evalkit.report import write_report
from evalkit.traceview import load_traces

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    d = tmp_path_factory.mktemp("runs")
    kw = dict(tasks_dir=ROOT / "tasks", fixtures_dir=ROOT / "fixtures", runs_dir=d, trials=2)
    a = infer(ROOT / "configs/baseline.yaml", run_id="a", **kw)
    b = infer(ROOT / "configs/candidate.yaml", run_id="b", **kw)
    for r in (a, b):
        grade(r, ROOT / "tasks", judge_backend="mock")
    return a, b


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_spans_follow_genai_conventions(runs):
    a, _ = runs
    traces = load_traces(a)
    assert len(traces) == 8 * 2
    t = traces[("multi_hop_ticket_orders", 0)]
    assert [x.name for x in t.tools] == ["lookup_ticket", "run_sql"]
    assert t.llm_calls == 3 and t.input_tokens > 0 and t.cost_usd() > 0
    assert "4 orders" in t.final_answer


def test_demo_detects_safety_regression_and_cost_increase(runs, tmp_path):
    c = compare(*runs, tasks_dir=ROOT / "tasks")
    assert c.recommendation == "BLOCK"
    worse = {t.task_id for t in c.tasks if t.quality == "worse"}
    assert worse == {"injection_vpn_note"}
    assert c.efficiency == "costlier"
    same_but_costlier = {t.task_id for t in c.tasks if t.quality == "same" and t.efficiency == "costlier"}
    assert "ticket_status" in same_but_costlier
    md, html = write_report(c, tmp_path)
    assert "BLOCK" in md.read_text() and html.read_text().startswith("<!doctype html>")


def test_regrading_does_not_rerun_inference(runs, tmp_path):
    a = shutil.copytree(runs[0], tmp_path / "a")
    before = _sha(a / "spans.jsonl")
    grade(a, ROOT / "tasks", grader_names=["outcome", "budget"], judge_backend="mock")
    assert _sha(a / "spans.jsonl") == before
    assert len(list((a / "grades").glob("*.jsonl"))) == 2


def test_compare_refuses_different_grader_sets(tmp_path, runs):
    a, b = (shutil.copytree(r, tmp_path / r.name) for r in runs)
    shutil.rmtree(b / "grades")
    grade(b, ROOT / "tasks", grader_names=["outcome"], judge_backend="mock")
    with pytest.raises(ComparabilityError, match="grader set"):
        compare(a, b, tasks_dir=ROOT / "tasks")


def test_compare_refuses_changed_task_definitions_unless_allowed(tmp_path, runs):
    tasks = shutil.copytree(ROOT / "tasks", tmp_path / "tasks")
    p = tasks / "ticket_status.yaml"
    spec = yaml.safe_load(p.read_text())
    spec["expected"]["contains"].append("Bulk export")  # expectation changed; input did not
    p.write_text(yaml.safe_dump(spec))

    a = shutil.copytree(runs[0], tmp_path / "a")
    b = shutil.copytree(runs[1], tmp_path / "b")
    shutil.rmtree(b / "grades")
    grade(b, tasks, judge_backend="mock")  # re-grade with the new expectation: allowed, no inference needed
    with pytest.raises(ComparabilityError, match="task definitions"):
        compare(a, b, tasks_dir=tasks)
    c = compare(a, b, tasks_dir=tasks, allow_mismatch=True)
    assert "ticket_status" not in {t.task_id for t in c.tasks}


def test_grade_refuses_stale_traces_when_task_input_changes(tmp_path, runs):
    tasks = shutil.copytree(ROOT / "tasks", tmp_path / "tasks")
    p = tasks / "ticket_status.yaml"
    p.write_text(p.read_text().replace("T-1050", "T-1043"))
    with pytest.raises(StaleTracesError):
        grade(runs[0], tasks, judge_backend="mock")


def test_assistant_never_imports_evalkit():
    for f in (ROOT / "assistant").rglob("*.py"):
        assert not re.search(r"^\s*(from|import)\s+evalkit", f.read_text(), re.M), f
