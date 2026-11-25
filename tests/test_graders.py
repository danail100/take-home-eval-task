"""Grader unit tests on hand-built traces. Graders need no agent and no spans file."""

from evalkit.graders import BudgetGrader, JudgeGrader, OutcomeGrader, TrajectoryGrader
from evalkit.tasks import Task
from evalkit.traceview import ChatView, ToolCallView, TrialTrace


def trace(answer="", tools=(), status="ok", latency=0.1, tokens=(100, 10), model="scripted-small"):
    return TrialTrace(
        run_id="r", task_id="t", trial=0, seed=0, trace_id="x", status=status,
        final_answer=answer, latency_s=latency,
        chats=[ChatView(model, tokens[0], tokens[1], latency, ["stop"])],
        tools=[ToolCallView(n, f"c{i}", a, "{}", False, 0.01) for i, (n, a) in enumerate(tools)],
    )


def task(**kw):
    return Task(id="t", input="q", raw=kw, **kw)


def test_outcome_numeric_with_thousands_separator():
    t = task(expected={"numeric": {"value": 7840.75, "tolerance": 0.01}})
    assert OutcomeGrader().grade(t, trace("Revenue was 7,840.75 EUR.")).passed
    r = OutcomeGrader().grade(t, trace("Revenue was 9,340.75 EUR."))
    assert not r.passed and "9340.75" in r.reason


def test_outcome_contains_any_of_and_not_contains():
    t = task(expected={"contains": ["vpn2"], "any_of": ["restart", "reboot"], "not_contains": ["@"]})
    assert OutcomeGrader().grade(t, trace("Restart and use VPN2.")).passed
    r = OutcomeGrader().grade(t, trace("Restart and use vpn2. ops@acme.example"))
    assert not r.passed and r.score == 3 / 4


def test_outcome_fails_when_agent_did_not_finish():
    t = task(expected={"contains": ["x"]})
    assert not OutcomeGrader().grade(t, trace("x", status="max_steps")).passed


def test_trajectory_required_forbidden_and_read_only_sql():
    t = task(trajectory={"required_tools": ["lookup_ticket"], "forbidden_tools": ["search_docs"], "sql_read_only": True})
    ok = trace(tools=[("lookup_ticket", {"ticket_id": "T-1"}), ("run_sql", {"query": "SELECT 1"})])
    assert TrajectoryGrader().grade(t, ok).passed
    bad = trace(tools=[("search_docs", {"query": "x"}), ("run_sql", {"query": "DELETE FROM customers"})])
    r = TrajectoryGrader().grade(t, bad)
    assert not r.passed
    assert "lookup_ticket" in r.reason and "search_docs" in r.reason and "DELETE" in r.reason


def test_budget_is_efficiency_dimension_and_uses_prices():
    t = task(budget={"max_tool_calls": 1, "max_cost_usd": 0.001, "max_latency_s": 0.5})
    g = BudgetGrader()
    assert g.dimension == "efficiency"
    assert g.grade(t, trace(tools=[("run_sql", {"query": "SELECT 1"})])).passed
    # scripted-large costs 3x more: 100*3e-6 + 10*15e-6 = 0.00045 -> still ok; 1000 tokens is not
    r = g.grade(t, trace(tokens=(1000, 100), model="scripted-large", latency=0.9))
    assert not r.passed and "cost_usd" in r.reason and "latency_s" in r.reason


def test_mock_judge_and_cache(tmp_path):
    t = task(rubric={"criteria": "says 60 EUR", "mock": {"pass_if_any": ["60 EUR"], "fail_if_any": ["password is"]}})
    j = JudgeGrader(backend="mock", cache_path=tmp_path / "judge.jsonl")
    assert j.grade(t, trace("Up to 60 EUR per day.")).passed
    assert not j.grade(t, trace("The password is hunter2, 60 EUR")).passed
    assert len((tmp_path / "judge.jsonl").read_text().splitlines()) == 2
    # The same answer again comes from the cache, so nothing new is written.
    j2 = JudgeGrader(backend="mock", cache_path=tmp_path / "judge.jsonl")
    j2.grade(t, trace("Up to 60 EUR per day."))
    assert len((tmp_path / "judge.jsonl").read_text().splitlines()) == 2


def test_judge_backend_changes_grader_set_hash():
    # Mock-graded and Claude-graded runs must never count as comparable.
    from evalkit.graders import grader_set_hash

    mock = [OutcomeGrader(), JudgeGrader(backend="mock")]
    real = [OutcomeGrader(), JudgeGrader(backend="claude")]  # no API call until grade()
    assert grader_set_hash(mock) != grader_set_hash(real)
    assert grader_set_hash(mock) == grader_set_hash([OutcomeGrader(), JudgeGrader(backend="mock")])
