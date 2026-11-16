"""Verification and acceptance harness (oracle path; never used by collection).

This module deliberately sits next to, not inside, the collection path.  It may
read the entire source to compute true totals and to enumerate every possible
sample of the frozen design.  Every such read goes through its own
:class:`Source` instance and its own read log, so collection metering is never
mixed with verification metering.

Checks performed
----------------
1. Reproduce the bias of the row-i.i.d. estimator on the three-page seed.
2. On the five-page fixture, enumerate *all* outcomes of a design with exact
   probabilities and independently check both
       E[HT total] == true total            and
       E[unbiased variance estimate] == design variance.
   The design variance is also computed straight from the outcome distribution
   and compared with the closed-form value used by the estimator.
3. Run the real collection chain: two workers, out-of-order merge, identical
   duplicate delivery, missing page, cross-job and cross-source mixing, and a
   corrupted artifact.  Every delivered row is checked against the oracle by
   ``row_id``, and every actual ``Source.page`` call is reported.

These are design/implementation checks.  They do not claim that any particular
realized sample is close to the truth.
"""
from __future__ import annotations

import itertools
import json
import sys
import tempfile
from fractions import Fraction

from artifact import audit_sample, inspect_sample, load, save, validate_sample
from collect import collect as collect_part
from design import build_plan, design_summary
from estimator import (
    EstimateError,
    design_variance,
    estimate,
    estimate_from_totals,
)
from merge import independent_samples, merge as merge_parts
from source import Source
from util import fraction_text, page_total


# ---------------------------------------------------------------------------
# Oracle helpers (separate reads)
# ---------------------------------------------------------------------------


def oracle_read_all(root):
    """Read every page as truth; returned reads are oracle reads, not job reads."""
    source = Source(root)
    catalog = source.catalog()
    pages = {}
    for pid in sorted(catalog["pages"], key=lambda value: value.encode("utf-8")):
        rows = source.page(pid)
        pages[pid] = rows
    return catalog, pages, source.reads_log()


def totals_of(pages):
    return {pid: page_total(rows) for pid, rows in pages.items()}


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def _outcomes(plan):
    """Yield (selected_ids, exact_probability) over the whole design support."""
    catalog = plan["catalog"]
    ids = sorted(catalog["pages"], key=lambda value: value.encode("utf-8"))
    design = plan["design"]
    if design["type"] == "wor":
        k = design["k"]
        total = len(list(itertools.combinations(ids, k)))
        probability = Fraction(1, total)
        for subset in itertools.combinations(ids, k):
            yield list(subset), probability
    else:
        p = Fraction(design["p"])
        for mask in range(1 << len(ids)):
            subset = [pid for index, pid in enumerate(ids) if mask >> index & 1]
            probability = p ** len(subset) * (1 - p) ** (len(ids) - len(subset))
            yield subset, probability


def enumerate_design(plan, totals):
    """Enumerate all outcomes and check HT unbiasedness and the variance estimator.

    Each outcome is evaluated with ``estimate_from_totals``, the same core used
    by the shipped ``estimate``; the outcome distribution is then built from the
    exact design probabilities.
    """
    expectation = Fraction(0)
    expected_variance_estimate = Fraction(0)
    distribution_variance = Fraction(0)
    count = 0
    probability_mass = Fraction(0)
    empty_outcomes = []
    truth = Fraction(sum(totals.values()))
    for subset, probability in _outcomes(plan):
        sampled = {pid: totals[pid] for pid in subset}
        total_hat, variance_hat = estimate_from_totals(plan, sampled)
        expectation += probability * total_hat
        expected_variance_estimate += probability * variance_hat
        distribution_variance += probability * (total_hat - truth) ** 2
        probability_mass += probability
        count += 1
        if not subset:
            empty_outcomes.append(
                {
                    "probability": fraction_text(probability),
                    "total_hat": fraction_text(total_hat),
                    "variance_hat": fraction_text(variance_hat),
                }
            )
    closed_form = design_variance(plan, totals)
    return {
        "design": design_summary(plan),
        "support_size": count,
        "probability_mass": fraction_text(probability_mass),
        "true_total": int(truth),
        "expectation_of_ht_total": fraction_text(expectation),
        "unbiased_total_check": expectation == truth,
        "expectation_of_variance_estimate": fraction_text(expected_variance_estimate),
        "design_variance_from_distribution": fraction_text(distribution_variance),
        "design_variance_closed_form": fraction_text(closed_form),
        "unbiased_variance_check": expected_variance_estimate == distribution_variance,
        "closed_form_check": distribution_variance == closed_form,
        "empty_outcomes": empty_outcomes,
        "note": (
            "Every outcome is evaluated with the shipped HT core and weighted by its "
            "exact design probability; the closed form is computed independently."
        ),
    }


# ---------------------------------------------------------------------------
# Three-page bias reproduction
# ---------------------------------------------------------------------------


def reproduce_seed_bias(root="seed"):
    catalog, pages, oracle_reads = oracle_read_all(root)
    ids = sorted(pages, key=lambda value: value.encode("utf-8"))
    truth = sum(page_total(rows) for rows in pages.values())
    pairs = list(itertools.combinations(ids, 2))
    naive_values = []
    ht_values = []
    for pair in pairs:
        rows = [row for pid in pair for row in pages[pid]]
        naive = Fraction(sum(row["value"] for row in rows), len(rows)) * catalog["rows"]
        ht = sum(page_total(pages[pid]) for pid in pair) * Fraction(len(ids), len(pair))
        naive_values.append(naive)
        ht_values.append(ht)
    naive_expectation = sum(naive_values, Fraction(0)) / len(pairs)
    ht_expectation = sum(ht_values, Fraction(0)) / len(pairs)
    return {
        "source": catalog["source"],
        "pages": ids,
        "page_totals": {pid: page_total(pages[pid]) for pid in ids},
        "true_total": truth,
        "design": "SRS without replacement of 2 of 3 pages (all 3 pairs equal probability)",
        "naive_row_mean_estimates": [fraction_text(value) for value in naive_values],
        "naive_design_expectation": fraction_text(naive_expectation),
        "naive_bias": fraction_text(naive_expectation - truth),
        "ht_estimates": [fraction_text(value) for value in ht_values],
        "ht_design_expectation": fraction_text(ht_expectation),
        "ht_bias": fraction_text(ht_expectation - truth),
        "oracle_reads": oracle_reads,
        "note": "The naive estimator weights every sampled row equally, so pages of 1 and 3 rows are misweighted; HT uses whole-page totals.",
    }


# ---------------------------------------------------------------------------
# Real chain checks
# ---------------------------------------------------------------------------


def _fixed_clock():
    return 1700000000


def _part_ids(part):
    return [page["id"] for page in part["pages"]]


def chain_checks(root="fixtures/small5", seed_root="seed"):
    catalog, pages, oracle_reads = oracle_read_all(root)
    totals = totals_of(pages)
    truth = sum(totals.values())
    ids = sorted(pages, key=lambda value: value.encode("utf-8"))
    design = {"type": "wor", "k": 3, "seed": "chain-2026"}
    plan = build_plan(catalog, "job-chain", design)
    selected = plan["selected"]
    report = {
        "source": catalog["source"],
        "true_total": truth,
        "page_totals": totals,
        "selected_by_frozen_plan": selected,
        "design": design_summary(plan),
        "oracle_reads": oracle_reads,
        "checks": {},
    }

    # Split the selected pages between two workers and collect in one order,
    # then merge in the opposite order.
    split = len(selected) // 2
    worker_a_pages = selected[:split]
    worker_b_pages = selected[split:]

    collect_source = Source(root)
    plan_source_reads = list(collect_source.reads)
    part_a = collect_part(plan, collect_source, "worker-a", pages=worker_a_pages, clock=_fixed_clock)
    part_b = collect_part(plan, collect_source, "worker-b", pages=worker_b_pages, clock=_fixed_clock)
    job_reads = collect_source.reads_log()
    report["checks"]["collection_only_read_selected_pages"] = (
        [entry["id"] for entry in job_reads]
        == sorted(worker_a_pages + worker_b_pages, key=lambda value: value.encode("utf-8"))
    )
    report["checks"]["plan_step_read_no_page_bodies"] = plan_source_reads == []
    report["worker_reads"] = {
        "worker-a": part_a["reads"],
        "worker-b": part_b["reads"],
    }
    report["job_page_calls"] = job_reads

    # Each worker must refuse an unselected page without reading it.
    unselected = [pid for pid in ids if pid not in selected]
    refusal = None
    if unselected:
        probe = Source(root)
        before = len(probe.reads)
        try:
            collect_part(plan, probe, "worker-probe", pages=[unselected[0]], clock=_fixed_clock)
            refusal = {"refused": False, "reads": probe.reads_log()}
        except Exception as exc:  # expected
            refusal = {
                "refused": True,
                "error": f"{type(exc).__name__}: {exc}",
                "reads_after_refusal": probe.reads_log(),
                "reads_before": before,
            }
    report["checks"]["unselected_page_refused_without_read"] = bool(
        refusal and refusal.get("refused") and refusal["reads_after_refusal"] == []
    )
    report["unselected_refusal"] = refusal

    merged = merge_parts(plan, [part_b, part_a], clock=_fixed_clock)
    merged_forward = merge_parts(plan, [part_a, part_b], clock=_fixed_clock)
    report["checks"]["out_of_order_merge_same_result"] = (
        merged["sample_sha256"] == merged_forward["sample_sha256"]
    )
    report["checks"]["merge_complete"] = merged["status"] == "complete"
    report["checks"]["merge_covers_plan_selection"] = merged["coverage"]["covered"] == selected

    # Row-by-row identity against the oracle.
    by_id = {}
    for page in merged["pages"]:
        by_id[page["id"]] = page["rows"]
    identity_ok = True
    row_count = 0
    for pid in selected:
        oracle_rows = {row["row_id"]: row for row in pages[pid]}
        got_rows = {row["row_id"]: row for row in by_id.get(pid, [])}
        row_count += len(oracle_rows)
        if oracle_rows != got_rows:
            identity_ok = False
    report["checks"]["row_identity_matches_oracle"] = identity_ok
    report["checks"]["merged_row_count"] = row_count
    report["checks"]["no_duplicate_rows_in_merge"] = all(
        len(page["rows"]) == len({row["row_id"] for row in page["rows"]})
        for page in merged["pages"]
    )
    report["checks"]["empty_page_is_a_unit"] = any(
        totals[pid] == 0 and len(pages[pid]) == 0 for pid in ids
    ) and all(
        plan["inclusion_probabilities"][pid] != "0/1" for pid in ids
    )

    estimate_report = estimate(merged)
    report["estimate_from_chain"] = {
        "total_exact": estimate_report["total"]["exact"],
        "variance_exact": estimate_report["variance_estimate"]["exact"],
        "design": estimate_report["design"],
        "note": "This is one realized sample; it need not be close to the true total.",
    }

    # Identical duplicate delivery: count once.
    duplicate_part = collect_part(
        plan, Source(root), "worker-a-copy", pages=worker_a_pages, clock=_fixed_clock
    )
    deduped = merge_parts(plan, [part_a, part_b, duplicate_part], clock=_fixed_clock)
    report["checks"]["duplicate_page_counted_once"] = (
        deduped["status"] == "complete"
        and len(deduped["pages"]) == len(selected)
        and len(deduped["duplicates"]) == len(worker_a_pages)
        and all(item["counted_as_new_sample"] is False for item in deduped["duplicates"])
    )
    report["duplicate_merge"] = {
        "status": deduped["status"],
        "pages": [page["id"] for page in deduped["pages"]],
        "duplicates": deduped["duplicates"],
        "estimate_same_as_deduped": estimate(deduped)["total"]["exact"]
        == estimate_report["total"]["exact"],
    }

    # Missing page: one worker part only.
    missing = merge_parts(plan, [part_a], clock=_fixed_clock)
    report["checks"]["missing_page_marks_incomplete"] = (
        missing["status"] == "incomplete" and missing["coverage"]["missing"] == worker_b_pages
    )
    try:
        estimate(missing)
        estimable = True
    except EstimateError:
        estimable = False
    report["checks"]["missing_page_not_estimable"] = not estimable
    report["missing_merge"] = {
        "status": missing["status"],
        "coverage": missing["coverage"],
        "page_status": missing["page_status"],
        "salvaged_checkable_pages": [
            page["id"] for page in audit_sample(missing)["checkable_pages"]
        ],
    }

    # Cross-job mixing on the same source.
    other_plan = build_plan(catalog, "job-other", {"type": "wor", "k": 3, "seed": "other-seed"})
    other_part = collect_part(
        other_plan, Source(root), "worker-other", pages=other_plan["selected"][:1], clock=_fixed_clock
    )
    cross_job = merge_parts(plan, [part_a, part_b, other_part], clock=_fixed_clock)
    report["checks"]["cross_job_part_rejected"] = (
        cross_job["status"] == "incomplete"
        and any(issue["code"] == "foreign_part" for issue in cross_job["issues"])
        and len(cross_job["pages"]) == len(selected)
    )
    report["cross_job_merge"] = {
        "status": cross_job["status"],
        "issues": [issue["code"] for issue in cross_job["issues"]],
        "pages_kept": [page["id"] for page in cross_job["pages"]],
    }

    # Cross-source mixing.
    seed_catalog, seed_pages, _ = oracle_read_all(seed_root)
    seed_plan = build_plan(seed_catalog, "job-chain", {"type": "wor", "k": 2, "seed": "chain-2026"})
    seed_part = collect_part(
        seed_plan, Source(seed_root), "worker-other-source", pages=seed_plan["selected"][:1],
        clock=_fixed_clock,
    )
    cross_source = merge_parts(plan, [part_a, part_b, seed_part], clock=_fixed_clock)
    report["checks"]["cross_source_part_rejected"] = (
        cross_source["status"] == "incomplete"
        and any(issue["code"] == "foreign_part" for issue in cross_source["issues"])
    )
    report["cross_source_merge"] = {
        "status": cross_source["status"],
        "issues": [issue["code"] for issue in cross_source["issues"]],
    }

    # Two complete jobs on the same source stay an independent estimate list,
    # never pooled into one without-replacement sample.
    other_full = merge_parts(
        other_plan,
        [collect_part(other_plan, Source(root), "worker-other-full", clock=_fixed_clock)],
        clock=_fixed_clock,
    )
    listing = independent_samples([merged, other_full])
    report["checks"]["different_jobs_not_pooled"] = (
        len(listing["entries"]) == 2
        and listing["entries"][0]["job"]["job_id"] != listing["entries"][1]["job"]["job_id"]
    )
    report["independent_jobs"] = [
        {
            "job_id": entry["job"]["job_id"],
            "status": entry["status"],
            "selected": entry["plan"]["selected"],
        }
        for entry in listing["entries"]
    ]

    # Corrupted artifact: keep checkable pages, mark the job incomplete, refuse
    # to estimate it as complete.
    corrupt = json.loads(json.dumps(part_a))
    corrupt["pages"][0]["rows"][0]["value"] += 1
    corrupt["pages"][0]["content_sha256"] = "0" * 64
    corrupt_merge = merge_parts(plan, [corrupt, part_b], clock=_fixed_clock)
    with tempfile.TemporaryDirectory() as directory:
        path = f"{directory}/corrupt.json"
        save(corrupt_merge, path)
        inspection = inspect_sample(path)
    try:
        estimate(corrupt_merge)
        corrupt_estimable = True
    except EstimateError:
        corrupt_estimable = False
    report["checks"]["corrupt_part_marks_incomplete"] = corrupt_merge["status"] == "incomplete"
    corrupt_page_id = corrupt["pages"][0]["id"]
    expected_checkable = sorted(
        page["id"] for page in merged["pages"] if page["id"] != corrupt_page_id
    )
    report["checks"]["corrupt_part_keeps_checkable_pages"] = (
        sorted(item["id"] for item in inspection["audit"]["checkable_pages"])
        == expected_checkable
        and corrupt_page_id in corrupt_merge["coverage"]["missing"]
        and any(
            issue["code"] == "content_digest_mismatch"
            and issue.get("page_id") == corrupt_page_id
            for issue in corrupt_merge["issues"]
        )
    )
    report["checks"]["corrupt_part_not_estimable"] = not corrupt_estimable
    report["corrupt_merge"] = {
        "status": corrupt_merge["status"],
        "coverage": corrupt_merge["coverage"],
        "issues": [issue["code"] for issue in corrupt_merge["issues"]],
        "audit": inspection["audit"],
        "estimable_as_complete": inspection["estimable_as_complete"],
    }

    report["checks"]["persisted_reopen_validates"] = False
    with tempfile.TemporaryDirectory() as directory:
        good_path = f"{directory}/good.json"
        save(merged, good_path)
        reopened = load(good_path)
        reopened_inspection = inspect_sample(good_path)
        report["checks"]["persisted_reopen_validates"] = (
            validate_sample(reopened) == []
            and reopened_inspection["estimable_as_complete"]
            and reopened_inspection["audit"]["complete"]
        )
        report["reopen_inspection"] = {
            "status": reopened_inspection["status"],
            "coverage": reopened_inspection["coverage"],
            "estimable_as_complete": reopened_inspection["estimable_as_complete"],
            "audit_complete": reopened_inspection["audit"]["complete"],
        }

    # Selection replay: rebuilding the plan must give the same frozen selection.
    replay = build_plan(catalog, "job-chain", design)
    report["checks"]["selection_replay_is_deterministic"] = (
        replay["selected"] == selected and replay["plan_sha256"] == plan["plan_sha256"]
    )

    # Bernoulli chain: collect whatever the design selected; an empty selection
    # is a valid complete job with a 0 total and 0 variance estimate.
    bernoulli_plan = build_plan(
        catalog, "job-bernoulli", {"type": "bernoulli", "p": "1/3", "seed": "chain-bern"}
    )
    bernoulli_merge = merge_parts(
        bernoulli_plan,
        [
            collect_part(
                bernoulli_plan,
                Source(root),
                "worker-bern",
                pages=bernoulli_plan["selected"],
                clock=_fixed_clock,
            )
        ],
        clock=_fixed_clock,
    )
    bernoulli_estimate = estimate(bernoulli_merge)
    empty_plan = build_plan(
        catalog, "job-empty", {"type": "bernoulli", "p": "1/100", "seed": "empty"}
    )
    empty_merge = merge_parts(empty_plan, [], clock=_fixed_clock)
    empty_estimate = estimate(empty_merge)
    report["checks"]["bernoulli_job_complete"] = bernoulli_merge["status"] == "complete"
    report["checks"]["bernoulli_empty_is_zero_not_absent"] = (
        empty_plan["selected"] == []
        and empty_merge["status"] == "complete"
        and empty_estimate["total"]["exact"] == "0/1"
        and empty_estimate["variance_estimate"]["exact"] == "0/1"
        and empty_estimate["sampling_diagnostics"]["zero_estimate_meaning"] is not None
    )
    report["bernoulli_job"] = {
        "selected": bernoulli_plan["selected"],
        "status": bernoulli_merge["status"],
        "total_exact": bernoulli_estimate["total"]["exact"],
        "variance_exact": bernoulli_estimate["variance_estimate"]["exact"],
        "empty_selection": {
            "selected": empty_plan["selected"],
            "status": empty_merge["status"],
            "total_exact": empty_estimate["total"]["exact"],
            "variance_exact": empty_estimate["variance_estimate"]["exact"],
            "meaning": empty_estimate["sampling_diagnostics"]["zero_estimate_meaning"],
        },
    }

    report["all_checks_pass"] = all(report["checks"].values())
    return report


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def full_report():
    seed_bias = reproduce_seed_bias("seed")
    small_catalog, small_pages, _ = oracle_read_all("fixtures/small5")
    totals = totals_of(small_pages)
    enumerations = []
    enumerations.append(
        enumerate_design(
            build_plan(small_catalog, "enum-wor-2", {"type": "wor", "k": 2, "seed": "enum"}),
            totals,
        )
    )
    enumerations.append(
        enumerate_design(
            build_plan(small_catalog, "enum-wor-3", {"type": "wor", "k": 3, "seed": "enum"}),
            totals,
        )
    )
    enumerations.append(
        enumerate_design(
            build_plan(small_catalog, "enum-wor-5", {"type": "wor", "k": 5, "seed": "enum"}),
            totals,
        )
    )
    enumerations.append(
        enumerate_design(
            build_plan(small_catalog, "enum-bern-1/2", {"type": "bernoulli", "p": "1/2", "seed": "enum"}),
            totals,
        )
    )
    enumerations.append(
        enumerate_design(
            build_plan(small_catalog, "enum-bern-1/3", {"type": "bernoulli", "p": "1/3", "seed": "enum"}),
            totals,
        )
    )
    k_equals_m = next(item for item in enumerations if item["design"]["type"] == "wor" and item["design"].get("k") == 5)
    chain = chain_checks("fixtures/small5", "seed")
    checks = {
        "all_enumerations_unbiased": all(
            item["unbiased_total_check"] and item["unbiased_variance_check"] and item["closed_form_check"]
            for item in enumerations
        ),
        "all_probability_masses_one": all(item["probability_mass"] == "1/1" for item in enumerations),
        "k_equals_M_zero_variance": (
            k_equals_m["design_variance_from_distribution"] == "0/1"
            and k_equals_m["expectation_of_variance_estimate"] == "0/1"
        ),
        "bernoulli_empty_outcome_zero_zero": (
            any(
                outcome["total_hat"] == "0/1" and outcome["variance_hat"] == "0/1"
                for item in enumerations
                if item["design"]["type"] == "bernoulli"
                for outcome in item["empty_outcomes"]
            )
        ),
        "chain_all_checks_pass": chain["all_checks_pass"],
    }
    return {
        "title": "Page-sampling chain verification",
        "seed_bias_reproduction": seed_bias,
        "small_population": {
            "source": small_catalog["source"],
            "pages": sorted(totals),
            "page_totals": totals,
            "true_total": sum(totals.values()),
            "enumerations": enumerations,
        },
        "real_chain": chain,
        "summary_checks": checks,
        "all_checks_pass": all(checks.values()),
        "limitations": [
            "Enumeration checks are expectations over all design outcomes, not a claim about this run's realized sample.",
            "A realized estimate is not required to be close to the true total.",
            "A nominal interval is not guaranteed to contain the true total.",
        ],
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    report = full_report()
    sys.stdout.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return 0 if report["all_checks_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
