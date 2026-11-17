"""Randomized differential testing against both oracles.

Generates random three-table bags (duplicate keys, real NULLs, occasional empty
tables) and random valid INNER/LEFT trees, then checks every planner-certified
candidate against the naive nested-loop oracle and SQLite.  The illegal probe
rotation is also generated to confirm it is never certified even when the
random instance happens to make it agree.
"""
from __future__ import annotations

import random

from common import Schema, canonical, normalize_plan
from executor import execute
from oracle import naive_original, sorted_bag, sqlite_original
from reorder import plan

SCHEMA = Schema.load("schemas.json")
random.seed(449)


def _random_table(alias, size):
    rows = []
    for index in range(size):
        key = random.choice([None, 1, 1, 2, 2, 3, 4])
        text = random.choice([None, "p", "p", "q"])
        rows.append({
            "occurrence_id": f"{alias}{index}",
            "k": key,
            "v": text,
            "grp": random.choice([None, 1, 5, 9]),
            "note": random.choice([None, "x", "y"]),
        })
    return rows


def _random_stats(tables):
    stats = {}
    for alias, rows in tables.items():
        keys = {}
        for column in ("k", "grp"):
            values = {r[column] for r in rows if r[column] is not None}
            keys[f"{alias}.{column}"] = max(1, len(values))
        stats[alias] = {"rows": len(rows), "keys": keys}
    return stats


def _random_tree():
    on_ab = {"op": "and", "args": [["A.k", "B.k"]]}
    on_ac = {"op": "and", "args": [["A.k", "C.k"]]}
    on_bc = {"op": "and", "args": [["B.k", "C.k"]]}
    shapes = [
        {"join": "inner", "left": {"join": "left",
                                   "left": {"scan": "A"},
                                   "right": {"scan": "B"}, "on": on_ab},
         "right": {"scan": "C"}, "on": on_ac},
        {"join": "inner", "left": {"join": "left",
                                   "left": {"scan": "A"},
                                   "right": {"scan": "B"}, "on": on_ab},
         "right": {"scan": "C"}, "on": on_bc},
        {"join": "inner", "left": {"scan": "A"},
         "right": {"join": "left", "left": {"scan": "B"},
                   "right": {"scan": "C"}, "on": on_bc}, "on": on_ab},
    ]
    tree = random.choice(shapes)
    if random.random() < 0.4:
        where = random.choice([
            {"op": "isnull", "arg": "B.v"},
            {"op": "isnotnull", "arg": "B.grp"},
            {"op": "cmp", "cmp": ">=", "left": "A.grp",
             "right": {"lit": 5}},
        ])
        tree = dict(tree)
        return {"filter": tree, "where": where}
    return tree


def _illegal_rotation(root):
    body = root["filter"] if "filter" in root else root
    if not (body.get("join") == "inner"
            and body.get("left", {}).get("join") == "left"):
        return None
    left = body["left"]
    rotated = {
        "join": "left", "left": left["left"],
        "right": {"join": "inner", "left": left["right"],
                  "right": body["right"], "on": body["on"]},
        "on": left["on"],
    }
    if "filter" in root:
        return {"filter": rotated, "where": root["where"]}
    return rotated


def main():
    trials = 200
    total_candidates = 0
    illegal_seen_different = 0
    for trial in range(trials):
        tables = {
            "A": _random_table("A", random.randint(0, 12)),
            "B": _random_table("B", random.randint(0, 10)),
            "C": _random_table("C", random.randint(0, 8)),
        }
        stats = _random_stats(tables)
        raw_tree = _random_tree()
        expected_naive = naive_original(raw_tree, tables, SCHEMA)
        expected_sqlite = sqlite_original(raw_tree, tables, SCHEMA)
        assert expected_naive == expected_sqlite, trial

        report = plan(raw_tree, schema=SCHEMA, stats=stats)
        for candidate in report["candidates"]:
            total_candidates += 1
            actual = sorted_bag(execute(candidate["plan"], tables, schema=SCHEMA))
            via_sqlite = sqlite_original(candidate["plan"], tables, SCHEMA)
            assert actual == via_sqlite == expected_naive, (
                trial, candidate["proof"])

        illegal = _illegal_rotation(normalize_plan(raw_tree))
        if illegal is not None:
            certified = {canonical(c["plan"]) for c in report["candidates"]}
            assert canonical(illegal) not in certified
            illegal_bag = sorted_bag(execute(illegal, tables, schema=SCHEMA))
            if illegal_bag != expected_naive:
                illegal_seen_different += 1

    print(f"ok: {trials} random trials, {total_candidates} certified candidates "
          "all match both oracles")
    print(f"ok: blind rotation never certified; it produced a different bag on "
          f"{illegal_seen_different} trials")


if __name__ == "__main__":
    main()
