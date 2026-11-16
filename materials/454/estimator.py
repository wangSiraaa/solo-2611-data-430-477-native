"""Horvitz-Thompson total and design-based variance for whole pages.

The estimator works from whole-page totals, not from treating rows as i.i.d.
draws.

Notation for the finite population of ``M`` pages with page totals ``T_i``:

* HT total:            T_hat = sum_{i in s} T_i / pi_i
* design variance:     V     = sum_i (1-pi_i)/pi_i * T_i^2
                              + 2 sum_{i<j} (pi_ij - pi_i pi_j)/(pi_i pi_j) T_i T_j
* unbiased variance
  estimator:           v_hat = sum_{i in s} sum_{j in s}
                              (pi_ij - pi_i pi_j)/pi_ij * (T_i/pi_i)(T_j/pi_j)

where ``pi_i`` is the inclusion probability and ``pi_ij`` the joint inclusion
probability (with ``pi_ii = pi_i``).  For both designs here ``pi_ij`` is
constant, so each whole-page total is used exactly once per sample; rows are
never expanded as if they were independent draws.

Exact values are :class:`fractions.Fraction`; decimal renderings are rounded
half-even to 18 places and accompanied by the exact rational, so the rounding
error is at most 0.5e-18 in the last printed digit and does not affect the
exact comparisons used for verification.

An empty Bernoulli selection yields T_hat = 0 and v_hat = 0.  That is a valid
outcome of the design: it does *not* mean the population is empty and it is not
evidence of zero sampling error.
"""
from __future__ import annotations

from fractions import Fraction

from artifact import validate_sample
from design import validate_plan
from util import (
    NUMERIC_POLICY,
    decimal_sqrt_text,
    decimal_text,
    fraction_text,
    page_total,
)


class EstimateError(ValueError):
    pass


ESTIMATOR_VERSION = "page-ht/2"


def _design_info(plan):
    design = plan["design"]
    M = plan["population"]["M"]
    if design["type"] == "wor":
        k = design["k"]
        p = Fraction(k, M)
        pi_ij = Fraction(k, M) * Fraction(k - 1, M - 1) if M > 1 else Fraction(0)
        return {
            "type": "wor",
            "M": M,
            "k": k,
            "pi": p,
            "pi_ij": pi_ij,
            "pi_text": fraction_text(p),
            "pi_ij_text": fraction_text(pi_ij),
        }
    p = Fraction(design["p"])
    return {
        "type": "bernoulli",
        "M": M,
        "k": None,
        "pi": p,
        "pi_ij": p * p,
        "pi_text": fraction_text(p),
        "pi_ij_text": fraction_text(p * p),
    }


def _check_sample_complete(sample):
    problems = []
    if not isinstance(sample, dict):
        return ["sample must be an object"]
    if sample.get("schema") != "page-sample/1":
        problems.append("sample.schema is not page-sample/1")
    if sample.get("status") != "complete":
        problems.append(f"sample.status is {sample.get('status')!r}, not 'complete'")
    plan = sample.get("plan")
    if not isinstance(plan, dict):
        problems.append("sample.plan is missing")
    coverage = sample.get("coverage")
    if not isinstance(coverage, dict):
        problems.append("sample.coverage is missing")
    else:
        if coverage.get("missing"):
            problems.append("sample.coverage lists missing selected pages")
        if coverage.get("selected") != coverage.get("covered"):
            problems.append("sample.coverage selected and covered differ")
    if not isinstance(sample.get("pages"), list):
        problems.append("sample.pages must be an array")
    return problems


def _ht_core(info, totals):
    """Core HT total and unbiased variance estimate from whole-page totals.

    ``totals`` maps the *selected* page ids to their totals.  Both ``estimate``
    and the verification enumerator call this one implementation.
    """
    p = info["pi"]
    pi_ij = info["pi_ij"]
    if p <= 0:
        raise EstimateError("inclusion probability must be positive")
    selected = list(totals)
    ht_total = sum((Fraction(totals[pid]) / p for pid in selected), Fraction(0))
    variance_hat = Fraction(0)
    for pid_i in selected:
        T_i = Fraction(totals[pid_i])
        # i == j term uses pi_ii = pi_i.
        variance_hat += (p - p * p) / p * (T_i / p) * (T_i / p)
        for pid_j in selected:
            if pid_j == pid_i:
                continue
            T_j = Fraction(totals[pid_j])
            variance_hat += (pi_ij - p * p) / pi_ij * (T_i / p) * (T_j / p)
    # The ordered pair loop is exactly the double sum in the estimator.
    return ht_total, variance_hat


def estimate_from_totals(plan, totals):
    """Verification/simulation entry: HT over an arbitrary design outcome.

    Collection and artifact estimation go through :func:`estimate`, which also
    proves the plan is frozen and reproducible.  This helper exists so the
    enumerator can evaluate *every* possible outcome with the same core code.
    """
    info = _design_info(plan)
    return _ht_core(info, totals)


def estimate(sample):
    """Return an estimate report, or raise EstimateError if not estimable."""
    problems = _check_sample_complete(sample)
    if problems:
        raise EstimateError("sample is not estimable as complete: " + "; ".join(problems))
    # Refuse anything whose schema, digests or coverage do not check out, so a
    # tampered artifact can never be estimated as if it were complete.
    artifact_problems = validate_sample(sample)
    if artifact_problems:
        raise EstimateError(
            "sample artifact failed validation: " + "; ".join(artifact_problems)
        )
    plan = sample["plan"]
    plan_problems = validate_plan(plan)
    if plan_problems:
        raise EstimateError("sample.plan is invalid: " + "; ".join(plan_problems))
    info = _design_info(plan)

    pages = sample["pages"]
    ids = [page["id"] for page in pages]
    if len(set(ids)) != len(ids):
        raise EstimateError("sample.pages contains duplicate page ids")
    totals = {}
    for page in pages:
        total = page_total(page["rows"])
        if page.get("total") is not None and page["total"] != total:
            raise EstimateError(f"page {page['id']!r} total does not match its rows")
        totals[page["id"]] = total
    selected = list(plan["selected"])
    if sorted(totals) != sorted(selected):
        raise EstimateError("sample.pages does not cover exactly the plan's selected pages")

    ht_total, variance_hat = _ht_core(info, {pid: totals[pid] for pid in selected})
    p = info["pi"]

    se_note = None
    se_decimal = None
    if variance_hat < 0:
        se_note = (
            "the unbiased HT variance estimate is negative for this sample; "
            "no standard error or normal interval is reported"
        )
    else:
        se_decimal = decimal_sqrt_text(variance_hat)

    expected_copies = sum(1 / p for _ in selected)
    return {
        "estimator": ESTIMATOR_VERSION,
        "job": sample["job"],
        "design": {
            "type": info["type"],
            "M": info["M"],
            "k": info["k"],
            "inclusion_probability": info["pi_text"],
            "joint_inclusion_probability": info["pi_ij_text"],
            "finite_population_correction": fraction_text(1 - p),
        },
        "numeric_policy": NUMERIC_POLICY,
        "selected_pages": selected,
        "observed": {
            "n_selected_pages": len(selected),
            "sampled_rows": sum(page["n_rows"] for page in pages),
            "sampled_page_total": sum(totals.values()),
            "page_totals": {pid: totals[pid] for pid in selected},
        },
        "total": {
            "exact": fraction_text(ht_total),
            "decimal": decimal_text(ht_total),
            "formula": "sum_{i in s} page_total_i / pi_i",
        },
        "variance_estimate": {
            "exact": fraction_text(variance_hat),
            "decimal": decimal_text(variance_hat),
            "standard_error_decimal": se_decimal,
            "note": se_note,
            "formula": (
                "sum_{i,j in s} (pi_ij - pi_i*pi_j)/pi_ij * (T_i/pi_i)(T_j/pi_j), pi_ii=pi_i"
            ),
            "unbiased_for": "the design variance of the HT total",
        },
        "sampling_diagnostics": {
            "expected_selected_pages": fraction_text(expected_copies),
            "rows_not_used_as_iid": True,
            "zero_estimate_meaning": (
                "0 is a valid sampled estimate; it does not imply an empty population "
                "or zero sampling error"
                if ht_total == 0
                else None
            ),
            "design_note": (
                "k=M: every page is in the sample; the HT total is the population "
                "total and the variance is exactly 0"
                if info["type"] == "wor" and info["k"] == info["M"]
                else None
            ),
            "full_population_note": (
                "both pi and pi_ij are 1, so the HT variance estimator is 0"
                if info["type"] == "bernoulli" and p == 1
                else None
            ),
        },
    }


def design_variance(plan, all_totals):
    """Exact design variance of the HT total, for the verification/oracle path.

    ``all_totals`` maps every catalog page id to its true total.  This function
    must never be called by collection; it is used only by verify/simulation.
    """
    info = _design_info(plan)
    p = info["pi"]
    pi_ij = info["pi_ij"]
    ids = list(all_totals)
    variance = Fraction(0)
    for pid in ids:
        T = Fraction(all_totals[pid])
        variance += (1 - p) / p * T * T
    for i, pid_i in enumerate(ids):
        for pid_j in ids[i + 1 :]:
            Ti = Fraction(all_totals[pid_i])
            Tj = Fraction(all_totals[pid_j])
            variance += 2 * (pi_ij - p * p) / (p * p) * Ti * Tj
    return variance


def true_total_from_catalog_pages(pages):
    """Helper for verification/oracle paths only; never used by collection."""
    return sum(page_total(page["rows"]) for page in pages)
