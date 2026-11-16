"""Re-run the three-page bias probe.

The original probe showed that treating sampled rows as i.i.d. draws gives a
biased design expectation (37.5 vs a true total of 40), while a whole-page
Horvitz-Thompson total is unbiased.  This version reproduces that result and
then runs the real plan/collect/merge/estimate chain once, without using the
oracle inside collection.

Run ``python3 verify.py`` for the full acceptance harness.
"""
from __future__ import annotations

import json
import tempfile
from fractions import Fraction

from artifact import inspect_sample, load, save
from collect import collect as collect_part
from design import build_plan
from estimator import estimate
from merge import merge as merge_parts
from source import Source
from util import fraction_text, page_total
from verify import oracle_read_all


def main():
    # --- bias reproduction (this uses the oracle: it enumerates all pairs) ---
    catalog, pages, oracle_reads = oracle_read_all("seed")
    ids = sorted(pages, key=lambda value: value.encode("utf-8"))
    truth = sum(page_total(rows) for rows in pages.values())
    pairs = [(ids[i], ids[j]) for i in range(len(ids)) for j in range(i + 1, len(ids))]
    naive, ht = [], []
    for pair in pairs:
        rows = [row for pid in pair for row in pages[pid]]
        naive.append(Fraction(sum(row["value"] for row in rows), len(rows)) * catalog["rows"])
        ht.append(sum(page_total(pages[pid]) for pid in pair) * Fraction(len(ids), len(pair)))
    naive_expectation = sum(naive, Fraction(0)) / len(pairs)
    ht_expectation = sum(ht, Fraction(0)) / len(pairs)

    # --- one real chain run: two workers, merged out of order ---
    plan = build_plan(catalog, "probe-job", {"type": "wor", "k": 2, "seed": "probe"})
    selected = plan["selected"]
    job_source = Source("seed")
    part_first = collect_part(plan, job_source, "probe-a", pages=selected[:1])
    part_second = collect_part(plan, job_source, "probe-b", pages=selected[1:])
    merged = merge_parts(plan, [part_second, part_first])
    report = estimate(merged)
    with tempfile.TemporaryDirectory() as directory:
        path = f"{directory}/sample.json"
        save(merged, path)
        reopened = load(path)
        inspection = inspect_sample(path)

    print(
        json.dumps(
            {
                "seed_bias": {
                    "source": catalog["source"],
                    "page_totals": {pid: page_total(pages[pid]) for pid in ids},
                    "true_total": truth,
                    "all_pairs": pairs,
                    "naive_row_mean_estimates": [fraction_text(v) for v in naive],
                    "naive_design_expectation": fraction_text(naive_expectation),
                    "naive_bias": fraction_text(naive_expectation - truth),
                    "ht_estimates": [fraction_text(v) for v in ht],
                    "ht_design_expectation": fraction_text(ht_expectation),
                    "ht_bias": fraction_text(ht_expectation - truth),
                    "oracle_reads": oracle_reads,
                    "note": "Oracle enumeration only; this is a design check, not this run's sample quality.",
                },
                "chain_run": {
                    "selected": selected,
                    "job_page_calls": job_source.reads_log(),
                    "merge_status": merged["status"],
                    "coverage": merged["coverage"],
                    "workers": [
                        {"worker_id": w["worker_id"], "completed": w["completed"]}
                        for w in merged["workers"]
                    ],
                    "estimate_total": report["total"],
                    "estimate_variance": report["variance_estimate"],
                    "reopened_estimable": inspection["estimable_as_complete"],
                    "reopened_audit_complete": inspection["audit"]["complete"],
                },
                "note": "The realized chain estimate need not equal the true total; only its design expectation is checked above.",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
