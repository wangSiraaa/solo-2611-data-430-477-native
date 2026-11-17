"""Unit tests for the abstract null-rejection derivation.

These are pure logic tests over the predicate language with no data access,
demonstrating that qualification is structural rather than sample-driven.
"""
from common import normalize_predicate
from properties import abstract_outcomes, rejects_padding


def P(raw):
    return normalize_predicate(raw)


def test_comparison_on_padded_column_rejects():
    predicate = P(["B.k", "C.k"])
    assert rejects_padding(predicate, {"B"}) is True
    assert abstract_outcomes(predicate, {"B"}) == {None}
    # Same predicate must NOT be treated as rejecting a non-padded table.
    assert rejects_padding(predicate, {"D"}) is False


def test_isnull_keeps_padded_rows():
    predicate = P({"op": "isnull", "arg": "B.v"})
    assert rejects_padding(predicate, {"B"}) is False
    assert abstract_outcomes(predicate, {"B"}) == {True}


def test_isnotnull_rejects_padded_rows():
    predicate = P({"op": "isnotnull", "arg": "B.grp"})
    assert rejects_padding(predicate, {"B"}) is True
    assert abstract_outcomes(predicate, {"B"}) == {False}


def test_real_null_is_distinct_from_padding():
    # A free column may itself be a real NULL, so IS NULL over a free column
    # can be either true or false and never certifies rejection.
    predicate = P({"op": "isnull", "arg": "B.v"})
    assert abstract_outcomes(predicate, set()) == {True, False}
    assert rejects_padding(predicate, set()) is False


def test_and_or_not_three_valued():
    # B.x >= 5 OR NOT(B.y IS NULL): padded B makes first UNKNOWN, NOT UNKNOWN
    # = UNKNOWN, UNKNOWN OR UNKNOWN = UNKNOWN -> rejects.
    predicate = P({"op": "or", "args": [
        {"op": "cmp", "cmp": ">=", "left": "B.x", "right": {"lit": 5}},
        {"op": "not", "arg": {"op": "isnull", "arg": "B.y"}},
    ]})
    assert abstract_outcomes(predicate, {"B"}) == {None}
    assert rejects_padding(predicate, {"B"}) is True

    # AND where one arm is B.v IS NULL (true on padding) cannot reject.
    predicate = P({"op": "and", "args": [
        {"op": "isnull", "arg": "B.v"},
        ["A.k", "C.k"],
    ]})
    assert rejects_padding(predicate, {"B"}) is False


def test_literal_only_predicate_does_not_reference_padding():
    predicate = P({"op": "cmp", "cmp": ">", "left": {"lit": 1},
                   "right": {"lit": 0}})
    assert rejects_padding(predicate, {"B"}) is False
    assert abstract_outcomes(predicate, {"B"}) == {True}


def test_mixed_padded_and_free_comparison_is_unknown():
    # Any padded operand forces the comparison to UNKNOWN.
    predicate = P(["B.k", "C.k"])
    assert abstract_outcomes(predicate, {"B"}) == {None}
    assert abstract_outcomes(predicate, {"C"}) == {None}
    # With neither side padded, every truth value remains possible.
    assert abstract_outcomes(predicate, set()) == {True, False, None}


def main():
    tests = [v for name, v in sorted(globals().items())
             if name.startswith("test_") and callable(v)]
    for test in tests:
        test()
    print(f"ok: {len(tests)} abstract null-rejection unit tests passed")


if __name__ == "__main__":
    main()
