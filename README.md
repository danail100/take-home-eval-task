# Agent evalkit

An evaluation framework for a tool-calling LLM assistant. It tells you whether a change to prompts, tools or models is an improvement, and shows **correctness and efficiency (cost, latency, tokens, tool calls) side by side**.

- **Design doc:** [docs/DESIGN.md](docs/DESIGN.md)
- **What's next / open questions:** [docs/NEXT.md](docs/NEXT.md)
- **Sample report (no need to run anything):** [docs/sample-report/baseline__vs__candidate.md](docs/sample-report/baseline__vs__candidate.md). The raw runs it was built from (OTel spans, manifests, grades) are in [docs/sample-report/runs/](docs/sample-report/runs/).

## Quick start

Requires Python ≥ 3.11.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python -m evalkit demo      # infer + grade + compare baseline vs candidate, offline, ~5s
python -m pytest            # 15 tests
```

`demo` prints a summary and writes `reports/<A>__vs__<B>.md` and `.html`:

```
recommendation: BLOCK  (quality regressed, efficiency costlier)
  - SAFETY regression on `injection_vpn_note`: pass rate 100% -> 0%
  - improvement on `revenue_q3_emea`: pass rate 67% -> 100%
  - mean cost per request x5.34, p50 latency x3.58
```

No API key is needed. By default the assistant runs on a deterministic **scripted model** (see "What's real and what's simulated" below).

## What's in the box

```
assistant/      the "production" harness: agent loop, 3 fake tools, model adapters, OTel spans
                (depends on opentelemetry-api only and never imports evalkit)
configs/        the two assistant configurations under evaluation: baseline.yaml, candidate.yaml
tasks/          8 eval tasks (YAML)
fixtures/       hermetic data for the tools: docs, SQLite seed, tickets
evalkit/        the eval framework: infer -> grade -> compare/report
tests/          grader unit tests, pipeline/comparability tests, Claude adapter test (offline)
```

**Toy agent and tools:** `search_docs` (keyword search over policy docs), `run_sql` (read-only SQLite), and `lookup_ticket`.

**Tasks (8):** ticket lookup, text-to-SQL revenue, policy retrieval, multi-hop (ticket → customer → orders), unanswerable, prompt injection in a document, destructive request, and small talk (over-tooling).

**Graders (4 kinds):**
- `outcome`: deterministic checks on the answer (contains / regex / numeric).
- `trajectory`: checks on tool use, from the spans.
- `judge`: an LLM-as-judge with a rubric.
- `budget`: efficiency limits, reported separately from quality.

**Two variants:** `baseline` (a lean prompt on a small model) vs `candidate` (a "thorough" prompt on a big model: always search the docs, follow operational notes, verbose answers).

## The three stages

Inference is the only expensive step, so it runs once. Everything after it reads the stored traces.

```bash
# 1. infer: run a config on every task × N trials and store OTel spans + a manifest
python -m evalkit infer configs/baseline.yaml  --run-id base  --trials 3
python -m evalkit infer configs/candidate.yaml --run-id cand  --trials 3
#    -> runs/base/manifest.json, runs/base/spans.jsonl

# 2. grade: pure functions over the stored spans; no agent calls; can be re-run with other graders
python -m evalkit grade runs/base
python -m evalkit grade runs/cand
python -m evalkit grade runs/cand --graders outcome,trajectory   # a new grading, no new inference

# 3. compare: comparability checks first, then a paired per-task comparison and the report
python -m evalkit compare runs/base runs/cand            # --gate: exit 1 on BLOCK (for CI)
```

`compare` refuses to compare runs graded with different graders, judge, task definitions or fixtures. `--allow-mismatch` overrides this and compares only the tasks that are identical in both runs. `grade` refuses to run if a task's *input* changed since inference, because the traces would be stale. It allows re-grading when only the *expectations* changed.

## Running on Claude

```bash
pip install -e ".[claude]"
export ANTHROPIC_API_KEY=...
python -m evalkit demo --provider anthropic --judge claude
```

The configs map `baseline` to `claude-haiku-5-5` and `candidate` to `claude-sonnet-5-5`. The judge uses `claude-opus-5-5`. Judge verdicts are cached in `.cache/judge.jsonl`. A run of 8 tasks × 3 trials × 2 variants makes roughly 100–150 model calls.

## Sending traces to Langfuse (or any OTLP backend)

The spans follow the OpenTelemetry GenAI semantic conventions (`invoke_agent` / `chat` / `execute_tool`, `gen_ai.usage.*`, `gen_ai.tool.*`), so any OTLP backend can receive them unchanged:

```bash
pip install -e ".[otlp]"
export OTEL_EXPORTER_OTLP_ENDPOINT=https://<your-langfuse-host>/api/public/otel
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(public_key:secret_key)>"
python -m evalkit infer configs/baseline.yaml
```

## What's real and what's simulated

- **Real:**
  - the agent loop
  - the OTel instrumentation and stored spans
  - the tools (over fixture data)
  - grading, comparability checks, pricing from tokens, and the report
  - the Claude adapter (tested offline with a fake client)
- **Simulated:** `ScriptedModel`, a rule-based policy that mimics an LLM:
  - It reacts to a few phrases in the system prompt, so changing the prompt changes its behaviour.
  - It counts tokens from the real prompt size and sleeps to simulate latency, so the timings are real wall-clock numbers.
  - It has two planted flaws: the small model sometimes drops the year from a date filter, and the "follow operational notes" prompt makes it obey an injected instruction.
  - It shows that the framework catches these kinds of differences. It says nothing about how a real LLM behaves.
- **Prices:** the scripted models have made-up prices (small $1/$5, large $3/$15 per million tokens). The Claude prices are list prices as of the date in `evalkit/pricing.py`.
