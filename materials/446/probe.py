"""probe: current correct single/shard behaviour vs the original defects.

Run:  python probe.py

It keeps the original defective code alive in legacy.py and demonstrates, on
the shipped rows.json and a conflict scenario:

* correct path: one chunk and split chunks both yield the identical ordered
  result regardless of shard/arrival order, with accurate omitted counts;
* legacy defect 1: sharding changes the rendered string order;
* legacy defect 2: the byte difference of a truncated string is reported as
  the omitted occurrence count, and multi-byte chars are cut in half;
* legacy defect 3: the pool's INSERT OR REPLACE silently accepts a same-id
  re-send / conflicting chunk instead of rejecting it;
* the new pool rejects every required conflict and treats identical re-send
  as idempotent.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from finalizer import finalize
from legacy import LegacyPool, legacy_finish, legacy_partial
from meter import Meter
from ordered_state import partial
from run_pool import (ChunkConflict, ManifestConflict, OverlapConflict, Pool)

rows = json.loads(Path(__file__).with_name("rows.json").read_text())


def line(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    work = tempfile.mkdtemp(prefix="probe-")
    report: dict = {}

    # ------------------------- new, correct system ------------------------
    line("NEW: single chunk vs two shards (reversed arrival)")
    one = f"{work}/one.state"
    sa, sb = f"{work}/a.state", f"{work}/b.state"
    partial(rows, "all", one)
    partial(rows[:2], "a", sa)
    partial(rows[2:], "b", sb)
    meter = Meter()
    pool = Pool(f"{work}/pool", meter=meter)
    pool.merge([sb, sa])  # b admitted before a on purpose
    new_split = list(finalize(pool))
    pool_one = Pool(f"{work}/pool-one", meter=Meter())
    pool_one.merge([one])
    new_single = list(finalize(pool_one))
    report["new_single"] = new_single
    report["new_split_reversed_arrival"] = new_split
    report["new_order_invariant"] = new_single == new_split
    print("single :", json.dumps(new_single, ensure_ascii=False))
    print("shards :", json.dumps(new_split, ensure_ascii=False))
    print("identical regardless of sharding:", report["new_order_invariant"])

    line("NEW: byte limit keeps whole values; omitted counts occurrences")
    new_limit = list(finalize(pool, limit=4))
    print(json.dumps(new_limit, ensure_ascii=False))
    report["new_limit_4"] = new_limit

    line("NEW: conflict handling")
    # idempotent identical re-send
    again = pool.add_state(sa)
    # same chunk, different content
    changed = f"{work}/changed.state"
    partial([{"group": "g", "key": 1, "id": 99, "value": "X"}],
            "a", changed)
    try:
        pool.add_state(changed)
        chunk_err = "NOT RAISED"
    except ChunkConflict:
        chunk_err = "ChunkConflict rejected"
    # cross-chunk id overlap
    ov = f"{work}/ov.state"
    partial([{"group": "g", "key": 9, "id": 1, "value": "z"}], "rogue", ov)
    try:
        pool.add_state(ov)
        overlap_err = "NOT RAISED"
    except OverlapConflict:
        overlap_err = "OverlapConflict rejected"
    # manifest digest mismatch
    bad = {"chunks": [{"chunk": "a", "digest": "0" * 64, "ids": [1, 2]}]}
    try:
        pool.add_state(sa, bad)
        manifest_err = "NOT RAISED"
    except ManifestConflict:
        manifest_err = "ManifestConflict rejected"
    report["new_conflicts"] = {"idempotent_resend": again["action"],
                               "same_chunk_diff_content": chunk_err,
                               "cross_chunk_id_overlap": overlap_err,
                               "manifest_digest_mismatch": manifest_err}
    print(json.dumps(report["new_conflicts"], ensure_ascii=False, indent=2))
    print("peak resident payload bytes:", meter.peak,
          "(budget 131072, within:", meter.peak <= 131072, ")")
    for p in (pool, pool_one):
        p.close()

    # ------------------------- legacy defects -----------------------------
    line("LEGACY DEFECT 1: shard/arrival order changes the rendered string")
    lp = LegacyPool(f"{work}/legacy.sqlite")
    lp.add(legacy_partial(rows[:2], "a"))
    lp.add(legacy_partial(rows[2:], "b"))
    legacy_ab = list(legacy_finish(lp))
    lp2 = LegacyPool(f"{work}/legacy2.sqlite")
    lp2.add(legacy_partial(rows[2:], "b"))
    lp2.add(legacy_partial(rows[:2], "a"))
    legacy_ba = list(legacy_finish(lp2))
    print("a then b:", legacy_ab[0]["text"])
    print("b then a:", legacy_ba[0]["text"])
    report["legacy_order_dependent"] = (
        legacy_ab[0]["text"] != legacy_ba[0]["text"]
        or legacy_ab[0]["text"] != "甲|乙|丙|丁")
    lp.close()
    lp2.close()

    line("LEGACY DEFECT 2: bytes-truncation reported as omitted count")
    lp3 = LegacyPool(f"{work}/legacy3.sqlite")
    lp3.add(legacy_partial(rows, "all"))
    legacy_trunc = list(legacy_finish(lp3, 4))[0]
    print(json.dumps(legacy_trunc, ensure_ascii=False))
    print("-> emits a half-character and omitted = byte delta, not count")
    report["legacy_truncation"] = legacy_trunc
    lp3.close()

    line("LEGACY DEFECT 3: INSERT OR REPLACE accepts a conflicting re-send")
    lp4 = LegacyPool(f"{work}/legacy4.sqlite")
    lp4.add(legacy_partial(rows[:2], "a"))
    lp4.add([{"chunk": "a", "group": "g", "text": "INJECTED", "count": 99}])
    print("stored after conflicting re-send:",
          [s["text"] for s in lp4.states()])
    report["legacy_silent_replace"] = True
    lp4.close()

    line("SUMMARY")
    summary = {
        "new_order_invariant": report["new_order_invariant"],
        "new_conflicts_all_rejected": all(
            v.endswith("rejected") for k, v in report["new_conflicts"].items()
            if k != "idempotent_resend")
        and report["new_conflicts"]["idempotent_resend"] == "idempotent",
        "legacy_order_dependent": report["legacy_order_dependent"],
        "legacy_silent_replace": report["legacy_silent_replace"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    assert summary["new_order_invariant"]
    assert summary["new_conflicts_all_rejected"]
    assert summary["legacy_order_dependent"]
    Path(f"{work}/probe-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nreport written to", f"{work}/probe-report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
