"""Public end-to-end spill demonstration.

Runs the whole pipeline with real OS processes and real state files:

  1. generate 4096 distinct values interleaved across 8 shards (one empty),
     plus NULL-key, repeated-text, Chinese byte-boundary and over-limit groups;
  2. build one ordered state per shard in *separate processes* (partial);
  3. merge the states in three different ways (reversed flat admission,
     shuffled admission, two-level pool-of-pools merge tree);
  4. finalize in normal and distinct mode at several byte limits and compare
     every group against the independent full-sort oracle;
  5. demonstrate all four conflict rejections and idempotent re-send;
  6. record peak resident payload and disk read/write traffic;
  7. benchmark the alternative all-SQLite state layout (see sqlite_layout.py).

It must succeed while keeping active payload within 128 KiB -- never refusing
for budget reasons.  All artifacts stay under --work (default ./demo-work).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from gen_demo import generate
from meter import PAYLOAD_BUDGET, Meter
from oracle import oracle
from run_pool import (ChunkConflict, ManifestConflict, OverlapConflict, Pool,
                      load_manifest)
from finalizer import finalize
from ordered_state import partial

HERE = Path(__file__).parent


def _run(cmd: list[str], log: Path) -> str:
    p = subprocess.run([sys.executable, *cmd], cwd=HERE, check=True,
                       capture_output=True, text=True)
    log.write_text(p.stdout + p.stderr, encoding="utf-8")
    return p.stdout


def _finalize_to_map(pool_dir: str, meter: Meter, **kw) -> dict:
    pool = Pool(pool_dir, meter=meter, create=False)
    try:
        return {r["group"]: r for r in finalize(pool, meter=meter, **kw)}
    finally:
        pool.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=str(HERE / "demo-work"))
    args = ap.parse_args()
    work = Path(args.work)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    logs = work / "logs"
    logs.mkdir()
    record: dict = {"steps": []}

    def step(name: str, **extra):
        rec = {"step": name, **extra}
        record["steps"].append(rec)
        print(f"[step] {name}: "
              f"{json.dumps({k: v for k, v in extra.items()}, ensure_ascii=False)}")

    # 1. dataset -------------------------------------------------------------
    info = generate(str(work / "data"))
    manifest = load_manifest(str(work / "data" / "manifest.json"))
    all_rows = json.loads((work / "data" / "ALL-rows.json").read_text())
    empty = [c["chunk"] for c in manifest["chunks"].values() if c["rows"] == 0]
    step("dataset", rows=info["rows"], distinct_spill=info["distinct_spill"],
         empty_shards=empty, groups=info["groups"])

    # 2. partial in independent processes -----------------------------------
    state_paths = []
    t0 = time.perf_counter()
    for c in manifest["chunks"].values():
        state = work / f"{c['chunk']}.state"
        _run(["ordered_state.py", str(work / "data" / c["file"]),
              c["chunk"], str(state),
              "--meter-log", str(logs / "partial-meter.jsonl")],
             logs / f"partial-{c['chunk']}.log")
        state_paths.append(str(state))
    step("partial_processes", processes=len(state_paths),
         seconds=round(time.perf_counter() - t0, 3),
         state_bytes=sum(os.path.getsize(p) for p in state_paths))

    # 3+4. three merge/finalize variants vs oracle --------------------------
    variants: dict[str, list[str]] = {
        "reversed_flat": list(reversed(state_paths)),
        "shuffled_flat": state_paths[::3] + state_paths[1::3]
                         + state_paths[2::3],
    }
    for name, order in variants.items():
        pool_dir = work / f"pool-{name}"
        merge_meter = Meter(str(logs / f"{name}-merge-meter.jsonl"))
        pool = Pool(str(pool_dir), meter=merge_meter)
        t0 = time.perf_counter()
        admissions = pool.merge(order, str(work / "data" / "manifest.json"))
        merge_s = time.perf_counter() - t0
        assert all(a["action"] == "admitted" for a in admissions)
        pool.close()
        merge_meter.close()
        # One fresh meter per (mode, limit) so spill traffic is not counted
        # several times over; record the distinct-mode peak as the headline.
        per_mode_peaks = {}
        for distinct in (False, True):
            for limit in (4096, 256, 64, 19, 18):
                fm = Meter(str(logs /
                               f"{name}-{'d' if distinct else 'n'}-{limit}.jsonl"))
                got = _finalize_to_map(str(pool_dir), fm,
                                       limit=limit, distinct=distinct)
                ref = oracle(all_rows, limit=limit, distinct=distinct)
                assert got == ref, (name, distinct, limit)
                per_mode_peaks[f"{'distinct' if distinct else 'normal'}-"
                               f"{limit}"] = fm.peak
                fm.close()
        step(f"merge_{name}", seconds=round(merge_s, 3),
             peak_payload_bytes=max(per_mode_peaks.values()),
             per_mode_peaks=per_mode_peaks,
             within_budget=max(per_mode_peaks.values()) <= PAYLOAD_BUDGET,
             merge_meter_log=str(logs / f"{name}-merge-meter.jsonl"))

    # two-level merge tree: per-shard-pools -> two parents -> root ----------
    tree_merge_meter = Meter(str(logs / "tree-merge-meter.jsonl"))
    left = Pool(str(work / "tree-L"), meter=tree_merge_meter)
    right = Pool(str(work / "tree-R"), meter=tree_merge_meter)
    half = len(state_paths) // 2
    left.merge(state_paths[:half])
    right.merge(state_paths[half:])
    root = Pool(str(work / "pool-tree"), meter=tree_merge_meter)
    t0 = time.perf_counter()
    root.merge_pool(str(work / "tree-L"))
    root.merge_pool(str(work / "tree-R"))
    tree_merge_s = time.perf_counter() - t0
    left.close()
    right.close()
    root.close()
    tree_merge_meter.close()
    fin_meter = Meter(str(logs / "tree-final-meter.jsonl"))
    got = _finalize_to_map(str(work / "pool-tree"), fin_meter, distinct=True)
    assert got == oracle(all_rows, distinct=True)
    step("merge_tree", merge_seconds=round(tree_merge_s, 3),
         peak_payload_bytes=fin_meter.peak,
         within_budget=fin_meter.peak <= PAYLOAD_BUDGET,
         traffic=fin_meter.traffic())
    fin_meter.close()

    # 5. conflict rejections and idempotency --------------------------------
    conflict_meter = Meter()
    cpool = Pool(str(work / "pool-conflict"), meter=conflict_meter)
    cpool.merge(state_paths, str(work / "data" / "manifest.json"))
    # 5a. identical re-send is idempotent, not double counted
    before = cpool.db.execute("SELECT COUNT(*) FROM chunk_ids").fetchone()[0]
    again = cpool.add_state(state_paths[0],
                            load_manifest(str(work / "data" / "manifest.json")))
    after = cpool.db.execute("SELECT COUNT(*) FROM chunk_ids").fetchone()[0]
    assert again["action"] == "idempotent" and before == after
    # 5b. same chunk, different content
    changed = work / "changed.state"
    partial([{"group": "x", "key": 0, "id": 999_001, "value": "changed"}],
            manifest["chunks"][[*manifest["chunks"]][0]]["chunk"],
            str(changed))
    try:
        cpool.add_state(str(changed))
        raise AssertionError("ChunkConflict not raised")
    except ChunkConflict:
        pass
    # 5c. cross-chunk id overlap
    overlap_src = work / "data" / manifest["chunks"]["shard-0"]["file"]
    overlap_rows = [json.loads(l) for l in
                    overlap_src.read_text().splitlines() if l][:1]
    overlap_state = work / "overlap.state"
    partial(overlap_rows, "rogue", str(overlap_state))
    try:
        cpool.add_state(str(overlap_state))
        raise AssertionError("OverlapConflict not raised")
    except OverlapConflict:
        pass
    # 5d. manifest digest mismatch
    desc0 = manifest["chunks"]["shard-0"]
    bad_manifest = {"chunks": [{"chunk": desc0["chunk"], "digest": "f" * 64,
                                "ids": desc0["ids"]}]}
    try:
        cpool.add_state(state_paths[0], bad_manifest)
        raise AssertionError("ManifestConflict not raised")
    except ManifestConflict:
        pass
    step("conflicts", idempotent_resend=True, chunk_conflict=True,
         overlap_conflict=True, manifest_conflict=True)
    cpool.close()

    # 6. distinct spill evidence on the headline group ----------------------
    evidence_log = logs / "reversed_flat-d-4096.jsonl"
    spill_events = [json.loads(l) for l in
                    evidence_log.read_text().splitlines()
                    if '"distinct_group"' in l and '"spilled": true' in l]
    meter_log = [json.loads(l) for l in evidence_log.read_text().splitlines()]
    dedup_writes = sum(e.get("bytes_written", {}).get("dedup_spill", 0)
                       for e in meter_log)
    assert dedup_writes > 0, "external distinct spill did not happen"
    step("distinct_spill", spilled_groups=[e["group"] for e in spill_events],
         dedup_spill_bytes_written=dedup_writes)

    # 7. alternative SQLite layout benchmark --------------------------------
    try:
        from sqlite_layout import benchmark
        record["sqlite_layout_benchmark"] = benchmark(
            all_rows, str(work / "sqlite-alt"), PAYLOAD_BUDGET)
        step("sqlite_layout_benchmark",
             **record["sqlite_layout_benchmark"]["summary"])
    except Exception as exc:  # pragma: no cover - benchmark is auxiliary
        record["sqlite_layout_error"] = repr(exc)

    (work / "report.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    peak_seen = max((s.get("peak_payload_bytes", 0)
                     for s in record["steps"] if isinstance(s, dict)), default=0)
    summary = {"status": "SUCCESS", "budget_bytes": PAYLOAD_BUDGET,
               "max_peak_payload_bytes": peak_seen,
               "within_budget": peak_seen <= PAYLOAD_BUDGET,
               "report": str(work / "report.json")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    assert peak_seen <= PAYLOAD_BUDGET
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
