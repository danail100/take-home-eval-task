# What I'd build next, and what I'm unsure about

## Next, in priority order

1. **Run it on real models and calibrate the judge.** Run `--provider anthropic` across the task set. Hand-label about 50 answers per rubric and measure judge–human agreement. Only then let the judge gate releases. The judge should come from a different model family or tier than the candidate, to limit self-preference.
2. **A noise floor and honest statistics.** Run A/A comparisons (the same config twice) to measure run-to-run variance per task. Add paired bootstrap confidence intervals for pass rate and cost. Run more trials on tasks that turn out flaky (adaptive N). Replace "any drop blocks" with "a drop beyond the noise floor blocks; any safety drop blocks".
3. **A bigger, sliced dataset sourced from production.** Export production traces to Langfuse over the same OTel conventions. Sample failures, thumbs-down responses and expensive outliers into candidate tasks for review. Report per tag (sql, docs, multi-hop) so a regression in one capability isn't averaged away.
4. **A CI integration.** A GitHub Action on PRs that touch `configs/` or `assistant/`: run infer on the changed config, compare it against the last `main` baseline (cached, not re-run), post the report as a PR comment, and gate on BLOCK. Put a hard cost cap on each eval run.
5. **Richer telemetry.** Cache read/write tokens (cost is wrong without them once prompt caching is on), time to first token via streaming, retries and rate-limit waits, and tool latency recorded separately from model latency. Also judge cost, so the eval itself has a known price.
6. **More realistic tools.** Record-and-replay of real tool responses instead of hand-written fixtures. Checks on side effects (e.g. the DB state after the run), with that state written into spans so grading stays offline.
7. **Multi-turn tasks.** A simulated user for clarifying questions and follow-ups, with conversation-level graders.

## What I'm unsure about

- **The release rule.** Is "any per-task drop blocks" right, or should some tasks be allowed to trade off against each other? I lean towards strict for safety tasks, and a noise-adjusted rule weighted by product priority for the rest. That's a product decision as much as an engineering one.
- **Budgets per task vs global.** Per-task budgets make "correct but too slow" explicit, but someone has to set them, and they go stale when models get faster or cheaper. Relative budgets (e.g. "≤1.2× the current production cost") may age better than absolute ones.
- **Latency in evals vs production.** Eval latency depends on concurrency, rate limits and provider load at that moment. Two runs an hour apart can differ for reasons unrelated to the change. Median-of-N and running A and B interleaved help, but eval latency is still only a proxy for production.
- **How far the infer/grade split holds.** It works as long as everything a grader needs is in the spans. Graders that need a live environment (side effects, external state) break it, unless that state is snapshotted into the trace.
- **GenAI semantic conventions are still in development.** Attribute names have changed between releases (e.g. `gen_ai.system` → `gen_ai.provider.name`, and how message content is stored). Keeping all attribute parsing in `traceview.py` limits the damage, but the version needs pinning.
- **Content capture and PII.** Graders need the answers and tool arguments, but recording full content in production traces has privacy and retention costs. Evals on synthetic fixtures avoid that. Tasks sampled from production would not.
- **How much the scripted model proves.** It shows the pipeline end to end and that the report surfaces the right differences. Whether these tasks and graders catch the regressions *real* prompt changes cause can only be learned on real models.
