"""inspect entry: state files, pool catalog and resource logs.

Usage:
    python inspect.py state  <path.state>
    python inspect.py pool   <pool_dir>
    python inspect.py meter  <meter.jsonl>
"""
from __future__ import annotations

import argparse
import json

from meter import PAYLOAD_BUDGET, SQLITE_PAGE_CACHE
from ordered_state import read_header, read_ids, read_records
from run_pool import Pool


def inspect_state(path: str) -> dict:
    header = read_header(path)
    hl, rl, il, rc = header.pop("_lens")
    groups: dict[str, int] = {}
    null_keys = 0
    for group, key, _id, _value in read_records(path):
        groups[group] = groups.get(group, 0) + 1
        null_keys += key is None
    ids = list(read_ids(path))
    return {
        "path": path,
        "header": header,
        "sections": {"header_len": hl, "records_len": rl,
                     "ids_len": il, "footer_len": 24},
        "ordering": "group asc, key asc NULL last, id asc; "
                    "digest = sha256(canonical frames in that order)",
        "group_occurrences": groups,
        "null_key_occurrences": null_keys,
        "id_count": len(ids),
        "ids_sorted_unique": ids == sorted(set(ids)),
        "first_ids": ids[:8],
        "last_ids": ids[-8:],
    }


def inspect_meter(path: str) -> dict:
    events = [json.loads(line) for line in open(path, encoding="utf-8")]
    peak = max((e["peak"] for e in events), default=0)
    last = events[-1] if events else {}
    return {"events": len(events), "peak_payload_bytes": peak,
            "payload_budget_bytes": PAYLOAD_BUDGET,
            "within_budget": peak <= PAYLOAD_BUDGET,
            "sqlite_page_cache_bytes": SQLITE_PAGE_CACHE,
            "final_snapshot": {k: last.get(k) for k in
                               ("live", "live_total", "bytes_written",
                                "bytes_read", "written_total", "read_total")},
            "spill_events": [e for e in events if "spill" in e["event"]
                             or e["event"] == "distinct_group"][:20],
            "tail": events[-5:]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="inspect states / pool / meters")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state").add_argument("path")
    sub.add_parser("pool").add_argument("path")
    sub.add_parser("meter").add_argument("path")
    args = ap.parse_args(argv)
    if args.cmd == "state":
        print(json.dumps(inspect_state(args.path), ensure_ascii=False, indent=2))
    elif args.cmd == "pool":
        pool = Pool(args.path, create=False)
        try:
            print(json.dumps(pool.inspect(), ensure_ascii=False, indent=2))
        finally:
            pool.close()
    else:
        print(json.dumps(inspect_meter(args.path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
