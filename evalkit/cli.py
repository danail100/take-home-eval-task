"""evalkit CLI: infer | grade | compare | demo."""

from __future__ import annotations

import argparse
import sys

from evalkit.compare import ComparabilityError, compare
from evalkit.grade import grade
from evalkit.graders import DEFAULT_GRADERS
from evalkit.infer import infer
from evalkit.report import write_report


def _summary(c) -> str:
    a, b = c.agg_a, c.agg_b
    lines = [
        f"recommendation: {c.recommendation}  (quality {c.quality}, efficiency {c.efficiency})",
        *[f"  - {r}" for r in c.reasons],
        f"  {'':24s} {'A':>12s} {'B':>12s}",
        f"  {'quality pass rate':24s} {a['quality_pass_rate']:>12.0%} {b['quality_pass_rate']:>12.0%}",
        f"  {'within budget':24s} {a['budget_pass_rate']:>12.0%} {b['budget_pass_rate']:>12.0%}",
        f"  {'mean cost / request':24s} {a['mean_cost_usd']:>12.5f} {b['mean_cost_usd']:>12.5f}",
        f"  {'latency p50 (s)':24s} {a['latency_p50_s']:>12.3f} {b['latency_p50_s']:>12.3f}",
        f"  {'mean tool calls':24s} {a['mean_tool_calls']:>12.2f} {b['mean_tool_calls']:>12.2f}",
    ]
    return "\n".join(lines)


def _do_compare(args, run_a, run_b) -> int:
    try:
        c = compare(run_a, run_b, tasks_dir=args.tasks, allow_mismatch=args.allow_mismatch)
    except ComparabilityError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    md, html = write_report(c, args.out)
    print(_summary(c))
    print(f"report: {md}\n        {html}")
    return {"SHIP": 0, "REVIEW": 0, "BLOCK": 1}[c.recommendation] if args.gate else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evalkit", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--tasks", default="tasks")
        sp.add_argument("--fixtures", default="fixtures")
        sp.add_argument("--runs-dir", default="runs")

    sp = sub.add_parser("infer", help="stage 1: run a config on all tasks and store OTel spans")
    common(sp)
    sp.add_argument("config")
    sp.add_argument("--trials", type=int, default=3)
    sp.add_argument("--provider", default="scripted", choices=["scripted", "anthropic"])
    sp.add_argument("--run-id")
    sp.add_argument("--concurrency", type=int, default=8)

    sp = sub.add_parser("grade", help="stage 2: grade stored traces (no inference)")
    common(sp)
    sp.add_argument("run_dir")
    sp.add_argument("--graders", default=",".join(DEFAULT_GRADERS))
    sp.add_argument("--judge", default="auto", choices=["auto", "mock", "claude"])

    sp = sub.add_parser("compare", help="stage 3: compare two graded runs and write a report")
    common(sp)
    sp.add_argument("run_a")
    sp.add_argument("run_b")
    sp.add_argument("--allow-mismatch", action="store_true")
    sp.add_argument("--out", default="reports")
    sp.add_argument("--gate", action="store_true", help="exit 1 on BLOCK (for CI)")

    sp = sub.add_parser("demo", help="infer + grade + compare baseline vs candidate")
    common(sp)
    sp.add_argument("--a", default="configs/baseline.yaml")
    sp.add_argument("--b", default="configs/candidate.yaml")
    sp.add_argument("--trials", type=int, default=3)
    sp.add_argument("--provider", default="scripted", choices=["scripted", "anthropic"])
    sp.add_argument("--judge", default="auto", choices=["auto", "mock", "claude"])
    sp.add_argument("--allow-mismatch", action="store_true")
    sp.add_argument("--out", default="reports")
    sp.add_argument("--gate", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "infer":
        rd = infer(args.config, args.tasks, args.fixtures, args.runs_dir, args.trials, args.provider, args.run_id, args.concurrency)
        print(f"wrote {rd}")
    elif args.cmd == "grade":
        g = grade(args.run_dir, args.tasks, args.graders.split(","), args.judge)
        print(f"wrote {g}")
    elif args.cmd == "compare":
        return _do_compare(args, args.run_a, args.run_b)
    elif args.cmd == "demo":
        dirs = []
        for cfg in (args.a, args.b):
            print(f"[infer] {cfg} ({args.provider}, {args.trials} trials)")
            rd = infer(cfg, args.tasks, args.fixtures, args.runs_dir, args.trials, args.provider)
            print(f"[grade] {rd}")
            grade(rd, args.tasks, DEFAULT_GRADERS, args.judge)
            dirs.append(rd)
        print("[compare]")
        return _do_compare(args, *dirs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
