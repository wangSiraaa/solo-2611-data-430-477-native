"""Curated cases: every required semantic facet plus the public benchmark.

A case bundles the normalized plan shape (INNER/LEFT binary tree, optional
top WHERE), the three bags of rows, and published statistics used by the
cost model.  Tables may be empty but always carry their declared schema.
"""
from __future__ import annotations


def _r(alias, index, **fields):
    row = {"occurrence_id": f"{alias}{index}"}
    row.update(fields)
    return row


# --------------------------------------------------------------------------- #
# 1. probe.py counterexample family: INNER ON references the padded B table
# --------------------------------------------------------------------------- #

def case_probe():
    plan = {
        "join": "inner",
        "left": {
            "join": "left",
            "left": {"scan": "A"},
            "right": {"scan": "B"},
            "on": ["A.k", "B.k"],
        },
        "right": {"scan": "C"},
        "on": ["B.k", "C.k"],
    }
    tables = {
        "A": [_r("a", 1, k=1), _r("a", 2, k=2)],
        "B": [_r("b", 1, k=1)],
        "C": [_r("c", 1, k=1)],
    }
    stats = {
        "A": {"rows": 2, "keys": {"A.k": 2}},
        "B": {"rows": 1, "keys": {"B.k": 1}},
        "C": {"rows": 1, "keys": {"C.k": 1}},
    }
    return {"name": "probe_bk_ck_must_not_rotate", "plan": plan,
            "tables": tables, "stats": stats,
            "expect_r1": False, "expected_rows": 1}


# --------------------------------------------------------------------------- #
# 2. Public benchmark family: (A LEFT B) INNER C with the INNER predicate on A
# --------------------------------------------------------------------------- #

def case_family1_benchmark():
    rows_a = [_r("a", i, k=i) for i in range(1, 101)]
    rows_b = [_r(f"b{i}_", j, k=i)
              for i in range(1, 101) for j in range(1, 11)]
    rows_c = [_r("c", 1, k=1)]
    plan = {
        "join": "inner",
        "left": {
            "join": "left",
            "left": {"scan": "A"},
            "right": {"scan": "B"},
            "on": ["A.k", "B.k"],
        },
        "right": {"scan": "C"},
        "on": ["A.k", "C.k"],
    }
    stats = {
        "A": {"rows": 100, "keys": {"A.k": 100}},
        "B": {"rows": 1000, "keys": {"B.k": 100}},
        "C": {"rows": 1, "keys": {"C.k": 1}},
    }
    return {"name": "family1_benchmark", "plan": plan,
            "tables": {"A": rows_a, "B": rows_b, "C": rows_c},
            "stats": stats, "expect_r1": True,
            "expected_rows": 10, "max_intermediate_limit": 10}


# --------------------------------------------------------------------------- #
# 3. B has no matching rows: padding must survive the R1 rewrite
# --------------------------------------------------------------------------- #

def case_b_no_match():
    plan = {
        "join": "inner",
        "left": {
            "join": "left",
            "left": {"scan": "A"},
            "right": {"scan": "B"},
            "on": ["A.k", "B.k"],
        },
        "right": {"scan": "C"},
        "on": ["A.k", "C.k"],
    }
    tables = {
        "A": [_r("a", 1, k=1), _r("a", 2, k=2), _r("a", 3, k=3)],
        "B": [],
        "C": [_r("c", 1, k=1), _r("c", 2, k=2)],
    }
    stats = {
        "A": {"rows": 3, "keys": {"A.k": 3}},
        "B": {"rows": 0, "keys": {"B.k": 0}},
        "C": {"rows": 2, "keys": {"C.k": 2}},
    }
    return {"name": "b_no_match_padding_survives", "plan": plan,
            "tables": tables, "stats": stats, "expect_r1": True,
            "expected_rows": 2}


# --------------------------------------------------------------------------- #
# 4. C matches multiple A rows; B and C carry duplicate keys
# --------------------------------------------------------------------------- #

def case_c_multi_match_duplicate_keys():
    plan = {
        "join": "inner",
        "left": {
            "join": "left",
            "left": {"scan": "A"},
            "right": {"scan": "B"},
            "on": ["A.k", "B.k"],
        },
        "right": {"scan": "C"},
        "on": ["A.k", "C.k"],
    }
    tables = {
        "A": [_r("a", 1, k=1), _r("a", 2, k=1), _r("a", 3, k=2)],
        "B": [_r("b", 1, k=1), _r("b", 2, k=1), _r("b", 3, k=2)],
        "C": [_r("c", 1, k=1), _r("c", 2, k=1)],
    }
    stats = {
        "A": {"rows": 3, "keys": {"A.k": 2}},
        "B": {"rows": 3, "keys": {"B.k": 2}},
        "C": {"rows": 2, "keys": {"C.k": 1}},
    }
    return {"name": "c_multi_match_duplicate_bag", "plan": plan,
            "tables": tables, "stats": stats, "expect_r1": True,
            "expected_rows": 8}


# --------------------------------------------------------------------------- #
# 5. Real NULL keys never equate; NULL B.v participates in 3VL
# --------------------------------------------------------------------------- #

def case_real_null_keys():
    plan = {
        "join": "inner",
        "left": {
            "join": "left",
            "left": {"scan": "A"},
            "right": {"scan": "B"},
            "on": ["A.k", "B.k"],
        },
        "right": {"scan": "C"},
        "on": ["A.k", "C.k"],
    }
    tables = {
        "A": [_r("a", 1, k=1), _r("a", 2, k=None), _r("a", 3, k=3)],
        "B": [_r("b", 1, k=None, v="real-null-key"),
              _r("b", 2, k=3, v=None)],
        "C": [_r("c", 1, k=1), _r("c", 2, k=None)],
    }
    stats = {
        "A": {"rows": 3, "keys": {"A.k": 2}},
        "B": {"rows": 2, "keys": {"B.k": 1}},
        "C": {"rows": 2, "keys": {"C.k": 1}},
    }
    return {"name": "real_null_three_valued_logic", "plan": plan,
            "tables": tables, "stats": stats, "expect_r1": True,
            "expected_rows": 1}


# --------------------------------------------------------------------------- #
# 6. WHERE B.v IS NULL must keep padded rows; WHERE stays above the joins
# --------------------------------------------------------------------------- #

def case_where_isnull_keeps_padding():
    plan = {
        "join": "inner",
        "left": {
            "join": "left",
            "left": {"scan": "A"},
            "right": {"scan": "B"},
            "on": ["A.k", "B.k"],
        },
        "right": {"scan": "C"},
        "on": ["A.k", "C.k"],
        "where": {"op": "isnull", "arg": "B.v"},
    }
    tables = {
        "A": [_r("a", 1, k=1), _r("a", 2, k=2)],
        # a1 matches a real NULL; a2 has no B row at all -> schema padding.
        "B": [_r("b", 1, k=1, v=None)],
        "C": [_r("c", 1, k=1), _r("c", 2, k=2)],
    }
    stats = {
        "A": {"rows": 2, "keys": {"A.k": 2}},
        "B": {"rows": 1, "keys": {"B.k": 1}},
        "C": {"rows": 2, "keys": {"C.k": 2}},
    }
    return {"name": "where_isnull_keeps_padded_rows", "plan": plan,
            "tables": tables, "stats": stats, "expect_r1": True,
            "expected_rows": 2}


# --------------------------------------------------------------------------- #
# 7. WHERE rejects padded B rows: R2 reduces LEFT to INNER, then inner reorder
# --------------------------------------------------------------------------- #

def case_where_rejects_padding_r2():
    plan = {
        "join": "inner",
        "left": {
            "join": "left",
            "left": {"scan": "A"},
            "right": {"scan": "B"},
            "on": ["A.k", "B.k"],
        },
        "right": {"scan": "C"},
        "on": ["A.k", "C.k"],
        "where": {"op": "isnotnull", "arg": "B.grp"},
    }
    tables = {
        "A": [_r("a", 1, k=1), _r("a", 2, k=2), _r("a", 3, k=3)],
        "B": [_r("b", 1, k=1, grp=9), _r("b", 2, k=2, grp=None)],
        "C": [_r("c", 1, k=1), _r("c", 2, k=2), _r("c", 3, k=3)],
    }
    stats = {
        "A": {"rows": 3, "keys": {"A.k": 3}},
        "B": {"rows": 2, "keys": {"B.k": 2}},
        "C": {"rows": 3, "keys": {"C.k": 3}},
    }
    return {"name": "where_isnotnull_enables_r2", "plan": plan,
            "tables": tables, "stats": stats, "expect_r1": True,
            "expect_r2": True, "expected_rows": 1}


# --------------------------------------------------------------------------- #
# 8. Multiple ON equality conditions, mixed int/text and AND structure
# --------------------------------------------------------------------------- #

def case_multi_on_conditions():
    plan = {
        "join": "inner",
        "left": {
            "join": "left",
            "left": {"scan": "A"},
            "right": {"scan": "B"},
            "on": {"op": "and", "args": [
                ["A.k", "B.k"],
                ["A.v", "B.v"],
            ]},
        },
        "right": {"scan": "C"},
        "on": {"op": "and", "args": [
            ["A.k", "C.k"],
            ["A.v", "C.v"],
        ]},
    }
    tables = {
        "A": [_r("a", 1, k=1, v="p"), _r("a", 2, k=1, v="q"),
              _r("a", 3, k=2, v="p")],
        "B": [_r("b", 1, k=1, v="p"), _r("b", 2, k=1, v="p"),
              _r("b", 3, k=2, v="other")],
        "C": [_r("c", 1, k=1, v="p")],
    }
    stats = {
        "A": {"rows": 3, "keys": {"A.k": 2, "A.v": 2}},
        "B": {"rows": 3, "keys": {"B.k": 2, "B.v": 2}},
        "C": {"rows": 1, "keys": {"C.k": 1, "C.v": 1}},
    }
    return {"name": "multi_and_on_conditions", "plan": plan,
            "tables": tables, "stats": stats, "expect_r1": True,
            "expected_rows": 2}


# --------------------------------------------------------------------------- #
# 9. Right-deep shape: A INNER (B LEFT C) with the INNER predicate only on A/B
# --------------------------------------------------------------------------- #

def case_right_deep_r1():
    plan = {
        "join": "inner",
        "left": {"scan": "A"},
        "right": {
            "join": "left",
            "left": {"scan": "B"},
            "right": {"scan": "C"},
            "on": ["B.k", "C.k"],
        },
        "on": ["A.k", "B.k"],
    }
    tables = {
        "A": [_r("a", 1, k=1), _r("a", 2, k=2)],
        "B": [_r("b", 1, k=1), _r("b", 2, k=2), _r("b", 3, k=3)],
        "C": [_r("c", 1, k=1)],
    }
    stats = {
        "A": {"rows": 2, "keys": {"A.k": 2}},
        "B": {"rows": 3, "keys": {"B.k": 3}},
        "C": {"rows": 1, "keys": {"C.k": 1}},
    }
    return {"name": "right_deep_mirrored_r1", "plan": plan,
            "tables": tables, "stats": stats, "expect_r1": True,
            "expected_rows": 2}


# --------------------------------------------------------------------------- #
# 10. OR/NOT in WHERE with 3VL: UNKNOWN must not be retained
# --------------------------------------------------------------------------- #

def case_or_not_three_valued_where():
    plan = {
        "join": "inner",
        "left": {
            "join": "left",
            "left": {"scan": "A"},
            "right": {"scan": "B"},
            "on": ["A.k", "B.k"],
        },
        "right": {"scan": "C"},
        "on": ["A.k", "C.k"],
        "where": {"op": "or", "args": [
            {"op": "cmp", "cmp": ">=", "left": "B.grp",
             "right": {"lit": 5}},
            {"op": "not", "arg": {"op": "isnull", "arg": "B.note"}},
        ]},
    }
    tables = {
        "A": [_r("a", 1, k=1), _r("a", 2, k=2), _r("a", 3, k=3)],
        "B": [_r("b", 1, k=1, grp=9, note="kept"),
              _r("b", 2, k=2, grp=None, note=None),
              _r("b", 3, k=3, grp=1, note="kept")],
        "C": [_r("c", 1, k=1), _r("c", 2, k=2), _r("c", 3, k=3)],
    }
    stats = {
        "A": {"rows": 3, "keys": {"A.k": 3}},
        "B": {"rows": 3, "keys": {"B.k": 3}},
        "C": {"rows": 3, "keys": {"C.k": 3}},
    }
    return {"name": "or_not_3vl_where", "plan": plan,
            "tables": tables, "stats": stats, "expect_r1": True,
            "expected_rows": 2}


ALL_CASES = [
    case_probe,
    case_family1_benchmark,
    case_b_no_match,
    case_c_multi_match_duplicate_keys,
    case_real_null_keys,
    case_where_isnull_keeps_padding,
    case_where_rejects_padding_r2,
    case_multi_on_conditions,
    case_right_deep_r1,
    case_or_not_three_valued_where,
]
