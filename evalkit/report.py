"""Render a Comparison as Markdown, plus a static HTML file with no dependencies."""

from __future__ import annotations

import difflib
import html
import re
from collections import defaultdict
from pathlib import Path

import yaml

from evalkit.compare import Comparison

_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}
_REC = {
    "SHIP": "🟢 SHIP: no quality regression and no efficiency loss",
    "REVIEW": "🟡 REVIEW: quality held, but there is an efficiency trade-off",
    "BLOCK": "🔴 BLOCK: quality regressed",
}


def _money(x: float) -> str:
    return f"${x:.5f}" if x == x else "-"


def _ratio(b: float, a: float, lower_is_better: bool = True) -> str:
    if not a or a != a or b != b:
        return "-"
    r = b / a
    if abs(r - 1) < 0.005:
        return "="
    arrow = "▲" if r > 1 else "▼"
    good = (r < 1) == lower_is_better
    return f"{arrow} x{r:.2f} {'🟢' if good else '🔴'}"


def _pp(b: float, a: float) -> str:
    d = (b - a) * 100
    if abs(d) < 0.05:
        return "="
    return f"{'▲' if d > 0 else '▼'} {d:+.1f} pp {'🟢' if d > 0 else '🔴'}"


def _config_lines(cfg: dict) -> list[str]:
    """Config as YAML, with the system prompt as a block so the diff works line by line."""
    rest = {k: v for k, v in cfg.items() if k != "system_prompt"}
    lines = yaml.safe_dump(rest, sort_keys=True, width=100).splitlines()
    lines.append("system_prompt: |")
    lines += [f"  {line}" for line in cfg.get("system_prompt", "").splitlines()]
    return lines


def _config_diff(c: Comparison) -> str:
    ya = _config_lines(c.a.manifest["assistant"]["config"])
    yb = _config_lines(c.b.manifest["assistant"]["config"])
    diff = difflib.unified_diff(ya, yb, fromfile=c.a.run_id, tofile=c.b.run_id, lineterm="", n=2)
    return "\n".join(diff) or "(no differences)"


def _failures(stats) -> list[str]:
    """Group failures by grader. If the reason differs across trials (e.g. latency values), show the first."""
    grouped: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for t in stats.trials:
        for grader, reason in t.failures:
            grouped[grader].append((t.trial, reason))
    out = []
    for g, items in grouped.items():
        trials = ",".join(str(k) for k, _ in items)
        reasons = {r for _, r in items}
        note = "" if len(reasons) == 1 else " (values vary by trial)"
        out.append(f"`{g}` ({len(items)}/{len(stats.trials)} trials: #{trials}): {items[0][1]}{note}")
    return out


def render_markdown(c: Comparison) -> str:
    A, B = c.a.run_id, c.b.run_id
    aa, ab = c.agg_a, c.agg_b
    L: list[str] = []
    L += [f"# Eval report: `{A}` (A) vs `{B}` (B)", ""]
    L += [f"## {_REC[c.recommendation]}", ""]
    L += [f"- **Quality:** {c.quality}", f"- **Efficiency:** {c.efficiency}"]
    L += [f"- {r}" for r in c.reasons]
    L += [""]

    L += ["## Comparability checks", "", "| check | status | detail |", "|---|---|---|"]
    L += [f"| {ch.name} | {_ICON[ch.status]} {ch.status} | {ch.detail} |" for ch in c.checks]
    L += [""]

    L += ["## What changed (assistant config diff)", "", "```diff", _config_diff(c), "```", ""]

    L += ["## Summary", "", f"| metric | A `{A}` | B `{B}` | B vs A |", "|---|---|---|---|"]
    rows = [
        ("quality pass rate (all trials)", "quality_pass_rate", "pct"),
        ("pass^k (tasks passing every trial)", "pass_all_rate", "pct"),
        ("within budget (all trials)", "budget_pass_rate", "pct"),
        ("mean cost / request", "mean_cost_usd", "money"),
        ("cost / successful request", "cost_per_success_usd", "money"),
        ("total cost of run", "total_cost_usd", "money"),
        ("latency p50", "latency_p50_s", "s"),
        ("latency p95", "latency_p95_s", "s"),
        ("mean tokens / request", "mean_total_tokens", "n"),
        ("mean tool calls / request", "mean_tool_calls", "n"),
        ("mean LLM calls / request", "mean_llm_calls", "n"),
    ]
    for label, key, kind in rows:
        va, vb = aa[key], ab[key]
        if kind == "pct":
            L.append(f"| {label} | {va:.0%} | {vb:.0%} | {_pp(vb, va)} |")
        elif kind == "money":
            L.append(f"| {label} | {_money(va)} | {_money(vb)} | {_ratio(vb, va)} |")
        elif kind == "s":
            L.append(f"| {label} | {va:.3f}s | {vb:.3f}s | {_ratio(vb, va)} |")
        else:
            L.append(f"| {label} | {va:.1f} | {vb:.1f} | {_ratio(vb, va)} |")
    L += [""]

    L += [
        "## Quality × efficiency, per task",
        "",
        "Each task goes in one cell. Quality is the per-task pass rate, B vs A. Efficiency is the "
        f"median cost and latency, with ±{c.thresholds['efficiency_tolerance']:.0%} counted as the same; "
        "mixed means one of them rose beyond that and the other fell beyond it.",
        "",
        "| quality \\ efficiency | cheaper | same | costlier | mixed |",
        "|---|---|---|---|---|",
    ]
    for q in ("better", "same", "worse"):
        cells = []
        for e in ("cheaper", "same", "costlier", "mixed"):
            ids = [f"`{t.task_id}`" for t in c.tasks if t.quality == q and t.efficiency == e]
            cells.append(", ".join(ids) or "·")
        L.append(f"| **{q}** | " + " | ".join(cells) + " |")
    L += [""]

    L += [
        "## Per task",
        "",
        "| task | tags | pass A | pass B | cost A | cost B | Δcost | p50 lat A | p50 lat B | tools A | tools B | budget A | budget B |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in c.tasks:
        L.append(
            f"| `{t.task_id}`{' 🛡️' if t.safety else ''} | {', '.join(t.tags)} | {_pass(t.a)} | {_pass(t.b)} "
            f"| {_money(t.a.cost)} | {_money(t.b.cost)} | {_ratio(t.b.cost, t.a.cost)} "
            f"| {t.a.latency:.3f}s | {t.b.latency:.3f}s | {t.a.tool_calls:.0f} | {t.b.tool_calls:.0f} "
            f"| {t.a.budget_rate:.0%} | {t.b.budget_rate:.0%} |"
        )
    L += ["", "🛡️ = safety-critical task. A regression on one of these always blocks.", ""]

    L += ["## Failures", ""]
    any_fail = False
    for t in c.tasks:
        for label, s in (("A", t.a), ("B", t.b)):
            fl = _failures(s)
            if fl:
                any_fail = True
                L.append(f"**`{t.task_id}` - {label}**")
                L += [f"- {f}" for f in fl]
                sample = next((x.answer for x in s.trials if not x.quality_pass), None)
                if sample:
                    L.append(f"- sample failing answer: _{sample[:300]}_")
                L.append("")
    if not any_fail:
        L += ["No failures.", ""]

    L += ["## Runs", "", "| | A | B |", "|---|---|---|"]
    for label, fn in [
        ("run id", lambda r: r.manifest["run_id"]),
        ("created", lambda r: r.manifest["created_at"]),
        ("provider / model", lambda r: f"{r.manifest['provider']} / {r.manifest['assistant']['model']}"),
        ("config hash", lambda r: r.manifest["assistant"]["config_hash"]),
        ("code version", lambda r: f"{r.manifest['assistant']['code']['git_sha'] or 'n/a'}{' (dirty)' if r.manifest['assistant']['code']['dirty'] else ''}"),
        ("trials", lambda r: r.manifest["trials"]),
        ("task set", lambda r: r.grades_meta["task_definition_set_hash"]),
        ("fixtures", lambda r: r.manifest["fixtures_hash"]),
        ("grading", lambda r: r.grades_meta["grades_id"]),
    ]:
        L.append(f"| {label} | {fn(c.a)} | {fn(c.b)} |")
    L += [""]
    return "\n".join(L)


def _pass(s) -> str:
    mark = "✅" if s.pass_all else "❌" if s.pass_rate == 0 else "⚠️"
    return f"{mark} {sum(t.quality_pass for t in s.trials)}/{len(s.trials)}"


# --------------------------------------------------------------------------------------
# Minimal Markdown -> HTML (headings, tables, code fences, lists, paragraphs, inline code/bold)
# --------------------------------------------------------------------------------------


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])_([^_]+)_(?![\w*])", r"<em>\1</em>", text)
    return text


def markdown_to_html(md: str, title: str) -> str:
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            lang = line[3:].strip()
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            rendered = []
            for b in block:
                cls = "add" if b.startswith("+") else "del" if b.startswith("-") else ""
                rendered.append(f'<span class="{cls}">{html.escape(b)}</span>' if lang == "diff" else html.escape(b))
            out.append("<pre>" + "\n".join(rendered) + "</pre>")
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h{level}>{_inline(line[level:].strip())}</h{level}>")
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            i -= 1
            head, body = rows[0], [r for r in rows[2:]]
            t = "<div class='tw'><table><thead><tr>" + "".join(f"<th>{_inline(h)}</th>" for h in head) + "</tr></thead><tbody>"
            t += "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
            out.append(t + "</tbody></table></div>")
        elif line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(f"<li>{_inline(lines[i][2:])}</li>")
                i += 1
            i -= 1
            out.append("<ul>" + "".join(items) + "</ul>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
        i += 1
    css = """
:root{--bg:#fff;--fg:#1d1f23;--muted:#5f6670;--line:#e2e5e9;--code:#f4f5f7;--add:#1a7f37;--del:#cf222e}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e6e8eb;--muted:#9aa1ab;--line:#2c3036;--code:#1f2227;--add:#3fb950;--del:#f85149}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,sans-serif;max-width:1200px;margin:0 auto;padding:24px 16px}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid var(--line);padding-bottom:4px}
.tw{overflow-x:auto}table{border-collapse:collapse;font-size:13px;margin:8px 0}th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
th{background:var(--code)}code,pre{background:var(--code);border-radius:4px;font-family:ui-monospace,monospace;font-size:12.5px}
code{padding:1px 4px}pre{padding:12px;overflow-x:auto}.add{color:var(--add)}.del{color:var(--del)}p,li{color:var(--fg)}
"""
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{css}</style></head><body>{''.join(out)}</body></html>"
    )


def write_report(c: Comparison, out_dir: str | Path = "reports") -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{c.a.run_id}__vs__{c.b.run_id}"
    md = render_markdown(c)
    md_path, html_path = out_dir / f"{stem}.md", out_dir / f"{stem}.html"
    md_path.write_text(md)
    html_path.write_text(markdown_to_html(md, f"Eval report {c.a.run_id} vs {c.b.run_id}"))
    return md_path, html_path
