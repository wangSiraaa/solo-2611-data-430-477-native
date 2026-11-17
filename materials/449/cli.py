"""Command line entry: plan / run / explain for the finite join language.

Examples (run from this directory)::

    python3 cli.py plan    --case family1_benchmark
    python3 cli.py run     --case family1_benchmark
    python3 cli.py explain --case probe_bk_ck_must_not_rotate
    python3 cli.py run     --case family1_benchmark --strategy local

``plan``   prints the certified candidate space and the cost-based choice
           without executing the joins.
``run``    executes the chosen plan for real and reports every join's actual
           input/output counts, plus occurrence provenance for row auditing.
``explain`` shows derived properties, accepted and rejected transformations
           (with reasons), and the two strategies' cost bounds.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from cases import ALL_CASES
from common import Schema, render
from executor import execute
from oracle import sorted_bag, sqlite_original
from reorder import plan as make_plan

HERE = Path(__file__).resolve().parent
SCHEMA = Schema.load(HERE / "schemas.json")
CASES = {factory()["name"]: factory for factory in ALL_CASES}


def _occurrence_provenance(rows):
    """Collapse each output row to its (A,B,C) occurrence-id combination."""
    combos = []
    for row in rows:
        combo = {
            alias: row.get(f"{alias}.occurrence_id")
            for alias in SCHEMA.aliases()
        }
        combos.append(combo)
    return combos


def _join_counts(metrics, prefix=""):
    lines = []
    for metric in metrics:
        if "filter" in metric:
            lines.append(f"{prefix}Filter: {metric['in_rows']} -> "
                         f"{metric['out_rows']} (estimated)")
            continue
        lines.append(
            f"{prefix}{metric['join'].upper()} JOIN "
            f"({metric['on_predicates']} ON predicate(s)): "
            f"left={metric['left_rows']} right={metric['right_rows']} "
            f"build={metric['build_rows']} -> out={metric['out_rows']}")
        lines.extend(_join_counts(metric["nested_joins_below"], prefix + "    "))
    return lines


def cmd_plan(args):
    case = CASES[args.case]()
    report = make_plan(case["plan"], schema=SCHEMA, stats=case["stats"],
                       strategy=args.strategy)
    payload = {
        "case": args.case,
        "strategy": args.strategy,
        "candidate_count": len(report["candidates"]),
        "chosen_changed": report["changed"],
        "baseline_estimate": report["baseline_estimate"],
        "chosen_estimate": report["chosen_estimate"],
        "candidates": [
            {
                "proof": candidate["proof"],
                "score": candidate["score"],
                "max_intermediate": candidate["estimate"]["max_intermediate"],
                "total_cost": candidate["estimate"]["total_cost"],
                "tree": render(candidate["plan"]),
            }
            for candidate in report["candidates"]
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_run(args):
    case = CASES[args.case]()
    report = make_plan(case["plan"], schema=SCHEMA, stats=case["stats"],
                       strategy=args.strategy)
    metrics = []
    chosen_rows = execute(report["chosen"], case["tables"],
                          metrics=metrics, schema=SCHEMA)
    original_metrics = []
    original_rows = execute(case["plan"], case["tables"],
                            metrics=original_metrics, schema=SCHEMA)

    oracle_sqlite = sqlite_original(case["plan"], case["tables"], SCHEMA)
    actual_max = max((m["out_rows"] for m in metrics), default=0)
    payload = {
        "case": args.case,
        "chosen_tree": render(report["chosen"]),
        "actual_join_counts": _join_counts(metrics),
        "actual_max_intermediate": actual_max,
        "output_row_count": len(chosen_rows),
        "matches_original_bag": sorted_bag(chosen_rows) == sorted_bag(original_rows),
        "matches_sqlite_oracle": sorted_bag(chosen_rows) == oracle_sqlite,
        "original_first_join": original_metrics[0] if original_metrics else None,
        "occurrence_provenance": _occurrence_provenance(chosen_rows),
    }
    if args.show_rows:
        payload["rows"] = chosen_rows
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_explain(args):
    case = CASES[args.case]()
    report = make_plan(case["plan"], schema=SCHEMA, stats=case["stats"],
                       strategy="bounded")
    local_report = make_plan(case["plan"], schema=SCHEMA,
                             stats=case["stats"], strategy="local")
    lines = [
        f"case: {args.case}",
        "original tree:",
        render(report["original"]),
        "",
        "rejections / certifications:",
    ]
    for item in report["rejections"]:
        verdict = "REFUSED" if item["rejected"] else "CERTIFIED"
        lines.append(f"  - {item['transform']}: {verdict}")
        lines.append(f"      {item['reason']}")
    lines += [
        "",
        "strategy bounds (estimated from published stats only):",
        "  S1 local rules:    "
        f"max_intermediate={local_report['chosen_estimate']['max_intermediate']}, "
        f"total={local_report['chosen_estimate']['total_cost']}, "
        f"candidates={len(local_report['candidates'])}",
        "  S2 bounded enum:   "
        f"max_intermediate={report['chosen_estimate']['max_intermediate']}, "
        f"total={report['chosen_estimate']['total_cost']}, "
        f"candidates={len(report['candidates'])}",
        "",
        "chosen tree:",
        render(report["chosen"]),
    ]
    print("\n".join(lines))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    choices = list(CASES)

    for name, handler in (("plan", cmd_plan), ("run", cmd_run),
                          ("explain", cmd_explain)):
        sp = sub.add_parser(name)
        sp.add_argument("--case", required=True, choices=choices)
        if name != "explain":
            sp.add_argument("--strategy", choices=("local", "bounded"),
                            default="bounded")
        if name == "run":
            sp.add_argument("--show-rows", action="store_true")
        sp.set_defaults(func=handler)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
