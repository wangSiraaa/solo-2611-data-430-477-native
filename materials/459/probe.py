#!/usr/bin/env python3
"""Probe for question 459.

First re-runs the original expectations (``new york city`` must match
``nyc city`` and must not match ``nyc york``), then reports the compiled
rules, per-query path evidence, and storage statistics.
"""

from __future__ import annotations

import json
import os
import tempfile

from graph_index import GraphIndex
from phrase import search
from rules import RuleSet

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(HERE, "rules.json")


def main() -> int:
    rules = RuleSet.from_json_file(RULES_PATH)
    with tempfile.TemporaryDirectory() as tmp:
        index = GraphIndex(os.path.join(tmp, "g.sqlite"), rules)
        index.put("d", ["new", "york", "city"])
        normal = search(index, ["new", "york"])
        false_phrase = search(index, ["nyc", "york"])
        nyc_city = search(index, ["nyc", "city"])

        checks = {
            "linear_normal": normal,
            "false_phrase": false_phrase,
            "missed_phrase": nyc_city,
            "expected_normal": ["d"],
            "expected_false": [],
            "expected_nyc_city": ["d"],
        }
        ok = (
            normal == ["d"] and false_phrase == [] and nyc_city == ["d"]
        )
        checks["ok"] = ok
        print(json.dumps(checks))

        detailed = index.query(["nyc", "city"])
        print(json.dumps({"nyc_city_evidence": detailed["hits"]}, sort_keys=True))
        print(json.dumps({"rules": detailed["rules"]}, sort_keys=True))
        print(
            json.dumps(
                {"storage": index.inspect()["storage_bytes"]}, sort_keys=True
            )
        )
        index.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
