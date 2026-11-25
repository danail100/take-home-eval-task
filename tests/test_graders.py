"""Grader unit tests on hand-built traces. Graders need no agent and no spans file."""

from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from evalkit.graders import BudgetGrader, JudgeGrader, OutcomeGrader, TrajectoryGrader
from evalkit.graders.llm_judge import _SCHEMA
from evalkit.tasks import Task, load_tasks
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


def test_budget_unknown_key_raises_value_error(tmp_path):
    t = task(budget={"max_cost": 0.01})
    with pytest.raises(ValueError, match=r"unknown budget limit.*max_cost.*allowed:.*max_cost_usd"):
        BudgetGrader().grade(t, trace())

    (tmp_path / "bad.yaml").write_text("id: bad\ninput: hi\nbudget:\n  max_cost: 0.01\n")
    with pytest.raises(ValueError, match=r"unknown budget limit.*max_cost.*allowed:.*max_cost_usd"):
        load_tasks(tmp_path)


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
    assert grader_set_hash(mock, {}) != grader_set_hash(real, {})
    assert grader_set_hash(mock, {}) == grader_set_hash([OutcomeGrader(), JudgeGrader(backend="mock")], {})


class _FakeJudgeMessages:
    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.requests = []
        self._lock = threading.Lock()

    def create(self, **kw):
        with self._lock:
            self.requests.append(kw)
            verdict = self._verdicts[len(self.requests) - 1]
        return NS(content=[NS(type="text", text=json.dumps(verdict))])


def test_claude_judge_verdict_parsing_normalization_clamping_and_threshold(tmp_path):
    fake = NS(
        messages=_FakeJudgeMessages(
            [
                {"score": 4, "reason": "meets rubric"},
                {"score": 3, "reason": "partial answer"},
                {"score": 0, "reason": "way below range"},
                {"score": 9, "reason": "way above range"},
            ]
        )
    )
    t = task(rubric={"criteria": "must be accurate"})
    j = JudgeGrader(backend="claude", model="claude-opus-5-5", cache_path=tmp_path / "judge.jsonl", client=fake)

    r4 = j.grade(t, trace("answer 4"))
    assert r4.passed is True
    assert r4.score == pytest.approx(3 / 4)
    assert "[claude:claude-opus-5-5] 4/5 - meets rubric" == r4.reason

    r3 = j.grade(t, trace("answer 3"))
    assert r3.passed is False
    assert r3.score == pytest.approx(2 / 4)
    assert "3/5 - partial answer" in r3.reason

    r0 = j.grade(t, trace("answer 0"))
    assert r0.passed is False
    assert r0.score == pytest.approx(0.0)
    assert "1/5 - way below range" in r0.reason

    r9 = j.grade(t, trace("answer 9"))
    assert r9.passed is True
    assert r9.score == pytest.approx(1.0)
    assert "5/5 - way above range" in r9.reason


def test_claude_judge_request_payload(tmp_path):
    fake = NS(messages=_FakeJudgeMessages([{"score": 5, "reason": "exact match"}]))
    t = Task(
        id="t1",
        input="What is the daily meal allowance?",
        rubric={"criteria": "Must state 60 EUR per day."},
        raw={},
    )
    j = JudgeGrader(backend="claude", model="claude-opus-5-5", cache_path=tmp_path / "judge.jsonl", client=fake)
    j.grade(t, trace("Employees may claim up to 60 EUR per day."))

    assert len(fake.messages.requests) == 1
    req = fake.messages.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["max_tokens"] == 1024
    assert len(req["messages"]) == 1
    assert req["messages"][0]["role"] == "user"
    prompt = req["messages"][0]["content"]
    assert "What is the daily meal allowance?" in prompt
    assert "Must state 60 EUR per day." in prompt
    assert "Employees may claim up to 60 EUR per day." in prompt
    assert req["output_config"] == {"format": {"type": "json_schema", "schema": _SCHEMA}}


def test_claude_judge_cache_prevents_duplicate_api_calls(tmp_path):
    cache_path = tmp_path / "judge.jsonl"
    fake1 = NS(messages=_FakeJudgeMessages([{"score": 5, "reason": "good"}]))
    t = task(rubric={"criteria": "be concise"})

    j1 = JudgeGrader(backend="claude", model="claude-opus-5-5", cache_path=cache_path, client=fake1)
    r1 = j1.grade(t, trace("short answer"))
    r2 = j1.grade(t, trace("short answer"))
    assert r1.score == r2.score == 1.0
    assert len(fake1.messages.requests) == 1

    # A new JudgeGrader pointing at the same cache file must reuse the persisted verdict.
    fake2 = NS(messages=_FakeJudgeMessages([{"score": 1, "reason": "should not be called"}]))
    j2 = JudgeGrader(backend="claude", model="claude-opus-5-5", cache_path=cache_path, client=fake2)
    r3 = j2.grade(t, trace("short answer"))
    assert r3.score == 1.0 and r3.passed is True
    assert len(fake2.messages.requests) == 0


def test_claude_judge_thread_safe_lazy_init_and_concurrent_grades(tmp_path, monkeypatch):
    n_threads = 4
    init_count = 0
    init_lock = threading.Lock()
    in_flight_barrier = threading.Barrier(n_threads, timeout=2.0)

    class _ConcurrentMessages:
        def create(self, **kw):
            # All threads must enter create() concurrently, proving self._lock is not held across the API call.
            in_flight_barrier.wait()
            return NS(content=[NS(type="text", text=json.dumps({"score": 5, "reason": "ok"}))])

    class _FakeAnthropicClient:
        def __init__(self):
            nonlocal init_count
            time.sleep(0.02)
            with init_lock:
                init_count += 1
            self.messages = _ConcurrentMessages()

    monkeypatch.setitem(sys.modules, "anthropic", NS(Anthropic=_FakeAnthropicClient))

    t = task(rubric={"criteria": "be helpful"})
    cache_path = tmp_path / "judge.jsonl"
    j = JudgeGrader(backend="claude", model="claude-opus-5-5", cache_path=cache_path)

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        results = list(pool.map(lambda i: j.grade(t, trace(f"answer {i}")), range(n_threads)))

    assert init_count == 1
    assert all(r.passed and r.score == 1.0 for r in results)
    lines = cache_path.read_text().splitlines()
    assert len(lines) == n_threads
    assert all("key" in json.loads(line) for line in lines)


def test_task_definition_hash_excludes_rubric_mock():
    raw_base = {"id": "t", "input": "q", "rubric": {"criteria": "must be accurate", "mock": {"pass_if_any": ["ok"]}}}
    raw_mock_changed = {
        "id": "t",
        "input": "q",
        "rubric": {"criteria": "must be accurate", "mock": {"pass_if_any": ["yes", "fine"], "fail_if_any": ["no"]}},
    }
    raw_no_mock = {"id": "t", "input": "q", "rubric": {"criteria": "must be accurate"}}
    raw_criteria_changed = {
        "id": "t",
        "input": "q",
        "rubric": {"criteria": "must cite the source", "mock": {"pass_if_any": ["ok"]}},
    }

    t_base = Task(**raw_base, raw=raw_base)
    t_mock_changed = Task(**raw_mock_changed, raw=raw_mock_changed)
    t_no_mock = Task(**raw_no_mock, raw=raw_no_mock)
    t_criteria_changed = Task(**raw_criteria_changed, raw=raw_criteria_changed)

    assert t_base.definition_hash == t_mock_changed.definition_hash == t_no_mock.definition_hash
    assert t_base.definition_hash != t_criteria_changed.definition_hash
    # Ensure accessing definition_hash did not mutate raw.
    assert "mock" in t_base.raw["rubric"]


def test_claude_judge_cache_ignores_rubric_mock_changes(tmp_path):
    cache_path = tmp_path / "judge.jsonl"
    fake = NS(
        messages=_FakeJudgeMessages(
            [
                {"score": 5, "reason": "first call"},
                {"score": 3, "reason": "criteria changed"},
            ]
        )
    )
    j = JudgeGrader(backend="claude", model="claude-opus-5-5", cache_path=cache_path, client=fake)

    t1 = task(rubric={"criteria": "be concise", "mock": {"pass_if_any": ["short"]}})
    t2 = task(rubric={"criteria": "be concise", "mock": {"pass_if_any": ["brief"], "fail_if_any": ["bad"]}})
    t3 = task(rubric={"criteria": "be thorough", "mock": {"pass_if_any": ["brief"], "fail_if_any": ["bad"]}})

    r1 = j.grade(t1, trace("short answer"))
    r2 = j.grade(t2, trace("short answer"))
    assert r1.score == r2.score == 1.0
    assert len(fake.messages.requests) == 1

    r3 = j.grade(t3, trace("short answer"))
    assert r3.score == pytest.approx(2 / 4)
    assert len(fake.messages.requests) == 2


def test_judge_fingerprint_tracks_mock_phrases_only_for_mock_backend():
    tasks = load_tasks(Path(__file__).resolve().parent.parent / "tasks")
    edited = {**tasks}
    t = tasks["policy_meal_allowance"]
    raw = {**t.raw, "rubric": {**t.rubric, "mock": {"pass_if_any": ["70 EUR"], "fail_if_any": []}}}
    edited[t.id] = Task(**raw, raw=raw)
    assert edited[t.id].definition_hash == t.definition_hash
    mock, real = JudgeGrader(backend="mock"), JudgeGrader(backend="claude")
    assert mock.fingerprint(tasks) != mock.fingerprint(edited)
    assert real.fingerprint(tasks) == real.fingerprint(edited) == real.fingerprint({})
