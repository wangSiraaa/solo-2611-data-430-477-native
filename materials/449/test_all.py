"""Correctness and performance tests, with two independent oracles.

For every case we check:

1. the original tree agrees across the real executor, the naive nested-loop
   oracle and SQLite (bag equality, duplicates and padded rows included);
2. every candidate the planner certifies is bag-equivalent to the original on
   the case data (executed, but only as verification — the planner never does);
3. explicitly illegal shapes (e.g. the probe.py rotation) are proven different
   by both oracles, so refusal is grounded;
4. the public benchmark keeps every join intermediate at <= 10 rows;
5. R1/R2 acceptance and refusal match the documented expectations.
"""
from __future__ import annotations

import sys

from cases import ALL_CASES, case_family1_benchmark, case_probe
from common import Schema, canonical, normalize_plan, render
from executor import execute
from oracle import naive_original, sorted_bag, sqlite_original
from properties import check_outer_inner_reassociation
from reorder import plan

SCHEMA = Schema.load("schemas.json")


def _illegal_probe_rotation(case):
    """The generic associativity rotation the old, broken reorderer applied."""
    root = normalize_plan(case["plan"])
    body = root["filter"] if "filter" in root else root
    left = body["left"]
    return {
        "join": "left",
        "left": left["left"],
        "right": {
            "join": "inner",
            "left": left["right"],
            "right": body["right"],
            "on": body["on"],
        },
        "on": left["on"],
    }


def test_oracles_agree():
    for factory in ALL_CASES:
        case = factory()
        expected_naive = naive_original(case["plan"], case["tables"], SCHEMA)
        expected_sqlite = sqlite_original(case["plan"], case["tables"], SCHEMA)
        actual = sorted_bag(execute(case["plan"], case["tables"], schema=SCHEMA))
        assert actual == expected_naive == expected_sqlite, case["name"]
        assert len(actual) == case["expected_rows"], (
            case["name"], len(actual), case["expected_rows"])
    print("ok: executor == naive oracle == SQLite on all 10 cases")


def test_certified_candidates_equivalent():
    total = 0
    for factory in ALL_CASES:
        case = factory()
        expected = sqlite_original(case["plan"], case["tables"], SCHEMA)
        report = plan(case["plan"], schema=SCHEMA, stats=case["stats"])
        for candidate in report["candidates"]:
            total += 1
            actual = sorted_bag(execute(
                candidate["plan"], case["tables"], schema=SCHEMA))
            via_sqlite = sqlite_original(
                candidate["plan"], case["tables"], SCHEMA)
            assert actual == via_sqlite == expected, (
                case["name"], candidate["proof"],
                render(candidate["plan"]))
    print(f"ok: all {total} planner-certified candidates are bag-equivalent "
          "to both oracles")


def test_illegal_shapes_are_refused_and_different():
    # probe.py: the generic rotation must not be certified and must differ.
    probe = case_probe()
    report = plan(probe["plan"], schema=SCHEMA, stats=probe["stats"])
    r1 = check_outer_inner_reassociation(
        normalize_plan(probe["plan"]))
    assert r1["ok"] is False
    illegal = _illegal_probe_rotation(probe)
    illegal_bag = sorted_bag(execute(illegal, probe["tables"], schema=SCHEMA))
    expected = sqlite_original(probe["plan"], probe["tables"], SCHEMA)
    assert illegal_bag != expected and len(illegal_bag) == 2
    assert len(expected) == 1
    certified = {canonical(c["plan"]) for c in report["candidates"]}
    assert canonical(illegal) not in certified
    refusal = next(r for r in report["rejections"]
                   if r["transform"] == "R1_OUTER_INNER_REASSOCIATION")
    assert refusal["rejected"] and "B" in refusal["reason"]

    # A fully generic reorder on the benchmark tree (rotating despite no proof)
    # is also excluded from the certified space.
    bench = case_family1_benchmark()
    bench_report = plan(bench["plan"], schema=SCHEMA, stats=bench["stats"])
    bench_illegal = _illegal_probe_rotation(bench)
    bench_certified = {canonical(c["plan"]) for c in bench_report["candidates"]}
    assert canonical(bench_illegal) not in bench_certified
    print("ok: illegal generic rotations are refused, and oracles show they "
          "change the bag (probe: 2 padded rows vs 1 expected)")


def test_rule_acceptance_matrix():
    for factory in ALL_CASES:
        case = factory()
        r1 = check_outer_inner_reassociation(normalize_plan(case["plan"]))
        assert r1["ok"] is case["expect_r1"], case["name"]
    r2_case = next(f() for f in ALL_CASES
                   if f()["name"] == "where_isnotnull_enables_r2")
    report = plan(r2_case["plan"], schema=SCHEMA, stats=r2_case["stats"])
    r2_refusals = [r for r in report["rejections"]
                   if r["transform"] == "R2_LEFT_TO_INNER"]
    assert any(not r["rejected"] for r in r2_refusals)
    assert any("R2_LEFT_TO_INNER" in p for c in report["candidates"]
               for p in c["proof"]["rules"])

    isnull_case = next(f() for f in ALL_CASES
                       if f()["name"] == "where_isnull_keeps_padded_rows")
    report = plan(isnull_case["plan"], schema=SCHEMA, stats=isnull_case["stats"])
    r2_refusals = [r for r in report["rejections"]
                   if r["transform"] == "R2_LEFT_TO_INNER"]
    assert all(r["rejected"] for r in r2_refusals), "IS NULL must not reduce LEFT"
    print("ok: R1/R2 acceptance and refusal match the proven conditions")


def test_benchmark_intermediate_bound():
    case = case_family1_benchmark()
    report = plan(case["plan"], schema=SCHEMA, stats=case["stats"])
    chosen = report["chosen"]
    metrics = []
    rows = execute(chosen, case["tables"], metrics=metrics, schema=SCHEMA)
    assert len(rows) == case["expected_rows"] == 10
    actual_max = max(m["out_rows"] for m in metrics)
    assert actual_max <= case["max_intermediate_limit"], metrics

    original_metrics = []
    original_rows = execute(case["plan"], case["tables"],
                            metrics=original_metrics, schema=SCHEMA)
    original_first = original_metrics[0]
    assert original_first["left_rows"] == 100
    assert original_first["right_rows"] == 1000
    assert original_first["out_rows"] == 1000
    assert sorted_bag(rows) == sorted_bag(original_rows)
    print("ok: benchmark original first join emits 1000 rows; chosen plan's "
          f"largest intermediate is {actual_max} rows (<=10), final bags equal")


def test_cost_model_uses_only_stats():
    case = case_family1_benchmark()
    report = plan(case["plan"], schema=SCHEMA, stats=case["stats"])
    assert report["chosen_estimate"]["max_intermediate"] <= 10
    assert report["baseline_estimate"]["max_intermediate"] == 1000
    # Without stats the planner must refuse to guess rather than execute.
    try:
        plan(case["plan"], schema=SCHEMA, stats=None)
    except ValueError as exc:
        assert "published row count" in str(exc)
    else:
        raise AssertionError("planner must not invent statistics")
    print("ok: cost decisions use published stats only; missing stats are an "
          "error instead of hidden candidate execution")


def test_strategies():
    case = case_family1_benchmark()
    local = plan(case["plan"], schema=SCHEMA, stats=case["stats"],
                 strategy="local")
    bounded = plan(case["plan"], schema=SCHEMA, stats=case["stats"],
                   strategy="bounded")
    assert len(local["candidates"]) <= 2
    assert len(bounded["candidates"]) >= len(local["candidates"])
    assert local["chosen_estimate"]["max_intermediate"] <= 10
    assert bounded["chosen_estimate"]["max_intermediate"] <= 10
    print("ok: conservative local rules and bounded enumeration both hit the "
          "benchmark bound; bounded explores no fewer candidates")


def main():
    tests = [
        test_oracles_agree,
        test_certified_candidates_equivalent,
        test_illegal_shapes_are_refused_and_different,
        test_rule_acceptance_matrix,
        test_benchmark_intermediate_bound,
        test_cost_model_uses_only_stats,
        test_strategies,
    ]
    for test in tests:
        test()
    print(f"\nall {len(tests)} test groups passed")


if __name__ == "__main__":
    sys.exit(main())
