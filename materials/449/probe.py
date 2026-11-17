"""probe.py — the B-join-C counterexample, kept on its original semantics.

The tree is::

    (A LEFT B ON A.k = B.k) INNER C ON B.k = C.k

The INNER predicate references B, the table the LEFT join NULL-extends.  A
generic inner-join associativity rotation would therefore emit extra
NULL-padded rows.  This script shows three things side by side:

* ``naive``   – the original tree executed by the real executor;
* ``rotated`` – the blind rotation the old reorderer applied (demonstrated to be
  a different bag, so it must never be certified);
* ``planned`` – what the proof-carrying reorderer actually chooses and why the
  R1 reassociation is refused.

Run from this directory (``schemas.json`` is resolved relative to the file).
"""
import json
from pathlib import Path

from common import Schema, canonical, normalize_plan, render
from executor import execute
from properties import check_outer_inner_reassociation
from reorder import plan

HERE = Path(__file__).resolve().parent
SCHEMA = Schema.load(HERE / "schemas.json")

PLAN = {
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
TABLES = {
    "A": [{"occurrence_id": "a1", "k": 1},
          {"occurrence_id": "a2", "k": 2}],
    "B": [{"occurrence_id": "b1", "k": 1}],
    "C": [{"occurrence_id": "c1", "k": 1}],
}
STATS = {
    "A": {"rows": 2, "keys": {"A.k": 2}},
    "B": {"rows": 1, "keys": {"B.k": 1}},
    "C": {"rows": 1, "keys": {"C.k": 1}},
}


def blind_rotation(root):
    """The unsound transform: treat LEFT like INNER and reassociate anyway."""
    body = root["filter"] if "filter" in root else root
    left = body["left"]
    rotated = {
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
    if "filter" in root:
        return {"filter": rotated, "where": root["where"]}
    return rotated


def main():
    root = normalize_plan(PLAN)
    eligibility = check_outer_inner_reassociation(root)
    report = plan(PLAN, schema=SCHEMA, stats=STATS)

    naive_bag = execute(root, TABLES, schema=SCHEMA)
    rotated_plan = blind_rotation(root)
    rotated_bag = execute(rotated_plan, TABLES, schema=SCHEMA)
    chosen_bag = execute(report["chosen"], TABLES, schema=SCHEMA)

    certified = {canonical(c["plan"]) for c in report["candidates"]}
    result = {
        "normal": naive_bag,
        "blind_rotated_unsound": rotated_bag,
        "expected_rows": 1,
        "r1_reassociation_allowed": eligibility["ok"],
        "refusal_reason": eligibility["reason"],
        "blind_rotation_certified": canonical(rotated_plan) in certified,
        "chosen_plan": render(report["chosen"]),
        "chosen_bag": chosen_bag,
        "chosen_equals_original": chosen_bag == naive_bag,
        "rotated_differs_from_original": rotated_bag != naive_bag,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # The whole point of the probe: the blind outer-join rotation is refused,
    # while whatever the planner chooses keeps the original one-row bag.
    assert eligibility["ok"] is False
    assert canonical(rotated_plan) not in certified
    assert len(naive_bag) == 1 and len(rotated_bag) == 2
    assert chosen_bag == naive_bag


if __name__ == "__main__":
    main()
