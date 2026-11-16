"""Command-line entry point: plan, collect, merge, estimate, inspect.

Collection never touches unselected page bodies.  Verification against the
full source is a separate ``verify.py`` path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from artifact import (
    ArtifactError,
    inspect_sample,
    load,
    load_with_report,
    save,
)
from collect import collect as collect_part
from design import build_plan, design_summary, validate_plan
from estimator import EstimateError, estimate as estimate_sample
from merge import independent_samples, merge as merge_parts
from source import Source, SourceError
from util import read_json_file


def _read_json(path):
    try:
        return read_json_file(path)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read {path!r}: {exc}")


def _parse_design(values):
    kind = values.get("design")
    seed = values.get("seed")
    if kind == "wor":
        if values.get("k") is None:
            raise SystemExit("--k is required for --design wor")
        return {"type": "wor", "k": values["k"], "seed": seed}
    if kind == "bernoulli":
        if values.get("p") is None:
            raise SystemExit("--p is required for --design bernoulli")
        return {"type": "bernoulli", "p": values["p"], "seed": seed}
    raise SystemExit("--design must be 'wor' or 'bernoulli'")


def _print(obj):
    sys.stdout.write(json.dumps(obj, sort_keys=True, indent=2) + "\n")


def cmd_plan(args):
    source = Source(args.source)
    catalog = source.catalog()  # catalog only; no page body is read
    design = _parse_design(vars(args))
    plan = build_plan(catalog, args.job, design)
    errors = validate_plan(plan)
    if errors:
        raise SystemExit("internal plan error: " + "; ".join(errors))
    if args.out:
        save(plan, args.out)
    _print(
        {
            "plan_sha256": plan["plan_sha256"],
            "job_id": plan["job_id"],
            "source": plan["source"],
            "design": design_summary(plan),
            "selected": plan["selected"],
            "inclusion_probability": plan["inclusion_probabilities"],
            "M": plan["population"]["M"],
            "N": plan["population"]["N"],
            "page_reads": source.reads,
            "out": args.out,
        }
    )


def cmd_collect(args):
    plan = load(args.plan)
    if args.page:
        pages = args.page
    else:
        pages = None
    # The allow-list is a second guard in addition to collect.py's own check:
    # this Source instance physically cannot read an unselected page.
    source = Source(args.source, allowed_ids=plan["selected"])
    part = collect_part(plan, source, args.worker, pages=pages)
    if args.out:
        save(part, args.out)
    _print(
        {
            "worker_id": part["worker_id"],
            "requested": part["requested"],
            "completed": part["completed"],
            "reads": part["reads"],
            "part_sha256": part["part_sha256"],
            "out": args.out,
        }
    )


def _expand_parts(values):
    parts = []
    for value in values:
        if os.path.isdir(value):
            for name in sorted(os.listdir(value)):
                if name.endswith(".json") and not name.startswith("."):
                    parts.append(os.path.join(value, name))
        else:
            parts.append(value)
    return parts


def cmd_merge(args):
    plan = load(args.plan)
    paths = _expand_parts(args.part)
    if not paths:
        raise SystemExit("no part files were given")
    parts = []
    for path in paths:
        obj, problems = load_with_report(path)
        if obj is None:
            raise SystemExit(f"cannot read part {path!r}: {'; '.join(problems)}")
        parts.append(obj)
    sample = merge_parts(plan, parts)
    if args.out:
        save(sample, args.out)
    _print(
        {
            "job_id": sample["job"]["job_id"],
            "status": sample["status"],
            "coverage": sample["coverage"],
            "page_status": sample["page_status"],
            "duplicates": sample["duplicates"],
            "conflicts": [
                {"page_id": c["page_id"], "deliveries": len(c["deliveries"])}
                for c in sample["conflicts"]
            ],
            "issues": sample["issues"],
            "workers": [
                {
                    "worker_id": w["worker_id"],
                    "accepted": w["accepted"],
                    "completed": w.get("completed"),
                }
                for w in sample["workers"]
            ],
            "sample_sha256": sample["sample_sha256"],
            "out": args.out,
        }
    )


def cmd_estimate(args):
    sample = load(args.sample)
    report = estimate_sample(sample)
    _print(report)


def cmd_inspect(args):
    reports = [inspect_sample(path) for path in args.artifact]
    if len(reports) == 1:
        _print(reports[0])
    else:
        _print(reports)


def cmd_list(args):
    samples = [load(path) for path in args.sample]
    listing = independent_samples(samples)
    _print(
        {
            "entries": [
                {
                    "job_id": entry["job"]["job_id"],
                    "plan_sha256": entry["job"]["plan_sha256"],
                    "status": entry["status"],
                    "source_name": entry["job"]["source_name"],
                }
                for entry in listing["entries"]
            ],
            "note": listing["note"],
        }
    )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="freeze a design and selection from the catalog")
    p.add_argument("--source", required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--design", choices=["wor", "bernoulli"], required=True)
    p.add_argument("--k", type=int)
    p.add_argument("--p")
    p.add_argument("--seed", required=True)
    p.add_argument("--out")
    p.set_defaults(func=cmd_plan)

    c = sub.add_parser("collect", help="read selected whole pages as one worker")
    c.add_argument("--source", required=True)
    c.add_argument("--plan", required=True)
    c.add_argument("--worker", required=True)
    c.add_argument("--page", action="append", help="restrict this worker to these pages")
    c.add_argument("--out")
    c.set_defaults(func=cmd_collect)

    m = sub.add_parser("merge", help="merge worker parts for one frozen job")
    m.add_argument("--plan", required=True)
    m.add_argument("--part", action="append", required=True)
    m.add_argument("--out")
    m.set_defaults(func=cmd_merge)

    e = sub.add_parser("estimate", help="HT total and design-based variance")
    e.add_argument("--sample", required=True)
    e.set_defaults(func=cmd_estimate)

    i = sub.add_parser("inspect", help="re-open artifacts and check design/coverage")
    i.add_argument("--artifact", action="append", required=True)
    i.set_defaults(func=cmd_inspect)

    l = sub.add_parser("list", help="keep per-job samples as an independent list")
    l.add_argument("--sample", action="append", required=True)
    l.set_defaults(func=cmd_list)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ArtifactError, EstimateError, SourceError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
