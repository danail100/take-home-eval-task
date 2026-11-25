"""evalkit CLI: infer | grade | compare | demo."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evalkit.compare import ComparabilityError, compare
from evalkit.grade import grade
from evalkit.graders import DEFAULT_GRADERS
from evalkit.graders.llm_judge import CACHE_ENV_VAR, DEFAULT_CACHE_PATH, resolve_cache_path
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


def _check_file(parser, path: str, what: str) -> None:
    """Exit with a usage error if ``path`` is not an existing file; suggest configs/<name>.yaml."""
    if Path(path).is_file():
        return
    if Path(path).is_dir():
        msg = f"{what} is a directory, not a file: {path!r}"
    else:
        msg = f"{what} not found: {path!r}"
    for cand in (Path("configs") / f"{path}.yaml", Path("configs") / f"{path}.yml"):
        if cand.is_file():
            msg += f" (did you mean {cand.as_posix()!r}?)"
            break
    parser.error(msg)


def _check_dir(parser, path: str, what: str) -> None:
    """Exit with a usage error if ``path`` is not an existing directory."""
    if not Path(path).is_dir():
        parser.error(f"{what} not found or not a directory: {path!r}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evalkit", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def judge_cache(sp):
        sp.add_argument(
            "--judge-cache", metavar="PATH",
            help=f"file for cached judge verdicts (default: ${CACHE_ENV_VAR} if set, "
                 f"else {DEFAULT_CACHE_PATH} in the current directory)",
        )

    def common(sp):
        sp.add_argument("--tasks", default="tasks", metavar="DIR",
                        help="directory of task definitions (default: %(default)s)")
        sp.add_argument("--fixtures", default="fixtures", metavar="DIR",
                        help="directory of tool fixtures used during inference (default: %(default)s)")
        sp.add_argument("--runs-dir", default="runs", metavar="DIR",
                        help="directory where new runs are written (default: %(default)s)")

    def trials_provider(sp):
        sp.add_argument("--trials", type=int, default=3, metavar="N",
                        help="trials per task (default: %(default)s)")
        sp.add_argument("--provider", default="scripted", choices=["scripted", "anthropic"],
                        help="model backend: offline scripted models or the Anthropic API "
                             "(default: %(default)s)")

    def judge(sp):
        sp.add_argument("--judge", default="auto", choices=["auto", "mock", "claude"],
                        help="LLM-judge backend; auto picks claude if ANTHROPIC_API_KEY is set "
                             "and the anthropic package is installed, else mock "
                             "(default: %(default)s)")

    def compare_opts(sp):
        sp.add_argument("--allow-mismatch", action="store_true",
                        help="compare only the tasks identical in both runs instead of refusing "
                             "when graders, judge, tasks or fixtures differ")
        sp.add_argument("--out", default="reports", metavar="DIR",
                        help="directory for the Markdown and HTML report (default: %(default)s)")
        sp.add_argument("--gate", action="store_true", help="exit 1 on BLOCK (for CI)")

    sp = sub.add_parser("infer", help="stage 1: run a config on all tasks and store OTel spans")
    common(sp)
    sp.add_argument("config", help="path to the agent config YAML, e.g. configs/baseline.yaml")
    trials_provider(sp)
    sp.add_argument("--run-id", help="name of the run directory under --runs-dir "
                                     "(default: <config name>-<provider>-<timestamp>)")
    sp.add_argument("--concurrency", type=int, default=8, metavar="N",
                    help="max trials run in parallel (default: %(default)s)")

    sp = sub.add_parser("grade", help="stage 2: grade stored traces (no inference)")
    common(sp)
    sp.add_argument("run_dir", help="run directory written by infer, e.g. runs/base")
    sp.add_argument("--graders", default=",".join(DEFAULT_GRADERS), metavar="LIST",
                    help="comma-separated graders to run (default: %(default)s)")
    judge(sp)
    judge_cache(sp)

    sp = sub.add_parser("compare", help="stage 3: compare two graded runs and write a report")
    common(sp)
    sp.add_argument("run_a", help="graded run directory for variant A (the reference)")
    sp.add_argument("run_b", help="graded run directory for variant B")
    compare_opts(sp)

    sp = sub.add_parser("demo", help="infer + grade + compare baseline vs candidate")
    common(sp)
    sp.add_argument("--config-a", "--a", dest="a", metavar="CONFIG_A", default="configs/baseline.yaml",
                    help="config file path for variant A (default: %(default)s)")
    sp.add_argument("--config-b", "--b", dest="b", metavar="CONFIG_B", default="configs/candidate.yaml",
                    help="config file path for variant B (default: %(default)s)")
    trials_provider(sp)
    judge(sp)
    judge_cache(sp)
    compare_opts(sp)

    args = p.parse_args(argv)
    sp = sub.choices[args.cmd]

    _check_dir(sp, args.tasks, "tasks dir")
    if args.cmd == "infer":
        _check_file(sp, args.config, "config file")
        _check_dir(sp, args.fixtures, "fixtures dir")
    elif args.cmd == "grade":
        _check_dir(sp, args.run_dir, "run dir")
    elif args.cmd == "compare":
        _check_dir(sp, args.run_a, "run dir A")
        _check_dir(sp, args.run_b, "run dir B")
    elif args.cmd == "demo":
        _check_file(sp, args.a, "config file for --config-a")
        _check_file(sp, args.b, "config file for --config-b")
        _check_dir(sp, args.fixtures, "fixtures dir")

    if args.cmd == "infer":
        rd = infer(args.config, args.tasks, args.fixtures, args.runs_dir, args.trials, args.provider, args.run_id, args.concurrency)
        print(f"wrote {rd}")
    elif args.cmd == "grade":
        names = args.graders.split(",")
        cache = resolve_cache_path(args.judge_cache)
        g = grade(args.run_dir, args.tasks, names, args.judge, cache)
        print(f"wrote {g}")
        if "judge" in names:
            print(f"judge cache: {cache}")
    elif args.cmd == "compare":
        return _do_compare(args, args.run_a, args.run_b)
    elif args.cmd == "demo":
        cache = resolve_cache_path(args.judge_cache)
        dirs = []
        for cfg in (args.a, args.b):
            print(f"[infer] {cfg} ({args.provider}, {args.trials} trials)")
            rd = infer(cfg, args.tasks, args.fixtures, args.runs_dir, args.trials, args.provider)
            print(f"[grade] {rd} (judge cache: {cache})")
            grade(rd, args.tasks, DEFAULT_GRADERS, args.judge, cache)
            dirs.append(rd)
        print("[compare]")
        return _do_compare(args, *dirs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
