"""evalkit: an evaluation framework for the assistant.

Pipeline:  infer (expensive, runs the agent)  ->  grade (cheap, pure functions over traces)
           ->  compare/report (any two graded runs, after comparability checks).

evalkit treats `assistant` as a black box. It calls `assistant.build_agent()` and observes
behaviour only through the OpenTelemetry spans the assistant emits.
"""
