"""External aggregation pool: merge cross-process ordered states.

A pool is a directory containing:

* ``catalog.sqlite``   - chunk identity, digest and per-id rows.  Opened with
  ``page_size=4096`` and ``cache_size=-16`` (at most 16 KiB page cache), so
  the cache is reported separately from the 128 KiB payload budget;
* ``states/<chunk>.state`` - the portable ordered state files produced by
  ``ordered_state.partial`` (the actual payload lives on disk, not resident).

Merge contract (enforced, never silently replaced):

1. same chunk id + same digest + same id set  -> idempotent, not double
   counted (re-send of an identical chunk);
2. same chunk id, different content           -> ChunkConflict, rejected;
3. id appearing in two different chunks       -> OverlapConflict, rejected;
4. manifest provided whose digest/id set does not match the state file, or a
   chunk/ id missing from it                  -> ManifestConflict, rejected.

Merge is arrival-order independent: states are stored unchanged and the
finalize stage merges their sorted runs with a stable k-way merge, so any
arrival order and any merge tree produce the identical final ordering.
"""
from __future__ import annotations

import argparse
import heapq
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Iterator

from meter import Meter
from ordered_state import (read_header, read_ids, read_records, verify_state)

CATALOG = "catalog.sqlite"
STATEDIR = "states"


class PoolError(RuntimeError):
    pass


class ChunkConflict(PoolError):
    pass


class OverlapConflict(PoolError):
    pass


class ManifestConflict(PoolError):
    pass


def _normalize_manifest(data: dict) -> dict:
    raw = data["chunks"]
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list):
        raise ManifestConflict("manifest 'chunks' must be a list or map")
    by_id: dict[str, dict] = {}
    for c in raw:
        for name in ("chunk", "digest", "ids"):
            if name not in c:
                raise ManifestConflict(f"manifest entry missing {name!r}")
        if not isinstance(c["ids"], list):
            raise ManifestConflict("manifest ids must be a list")
        if c["chunk"] in by_id:
            raise ManifestConflict(f"manifest repeats chunk {c['chunk']!r}")
        by_id[c["chunk"]] = c
    return {"chunks": by_id}


def load_manifest(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "chunks" not in data:
        # A single-chunk descriptor (from partial --manifest-out) is accepted.
        if isinstance(data, dict) and "chunk" in data and "digest" in data:
            data = {"chunks": [data]}
        else:
            raise ManifestConflict("manifest must contain 'chunks'")
    return _normalize_manifest(data)


class Pool:
    """Ordered-run pool with a 16 KiB-capped SQLite catalog."""

    layout = "ordered-run + sqlite catalog (16KiB cache)"

    def __init__(self, directory: str, meter: Meter | None = None,
                 create: bool = True):
        self.dir = Path(directory)
        if create:
            (self.dir / STATEDIR).mkdir(parents=True, exist_ok=True)
        elif not self.dir.exists():
            raise PoolError(f"no such pool: {directory}")
        self.meter = meter or Meter()
        self.db = sqlite3.connect(self.dir / CATALOG)
        # 4 KiB pages * 4 = 16 KiB maximum page cache; temp spills go to disk.
        self.db.execute("PRAGMA page_size=4096")
        self.db.execute("PRAGMA cache_size=-16")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS chunks("
            "chunk TEXT PRIMARY KEY, digest TEXT NOT NULL, "
            "record_count INTEGER NOT NULL, state_file TEXT NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS chunk_ids("
            "id INTEGER NOT NULL, chunk TEXT NOT NULL)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunk_ids_id "
            "ON chunk_ids(id)")
        self.db.commit()

    # ------------------------------------------------------------------
    def existing_chunk(self, chunk: str):
        return self.db.execute(
            "SELECT digest FROM chunks WHERE chunk=?", (chunk,)).fetchone()

    def _stream_overlap_ok(self, incoming_ids: list[int]) -> int:
        """Check id disjointness without materializing all ids in Python.

        The catalog lookup is index-backed and bounded by the 16 KiB cache;
        incoming ids are walked in sorted order (state files store them
        sorted).  Returns the count checked.
        """
        checked = 0
        for rid in incoming_ids:
            checked += 1
            row = self.db.execute(
                "SELECT 1 FROM chunk_ids WHERE id=? LIMIT 1", (rid,)).fetchone()
            if row is not None:
                raise OverlapConflict(f"id {rid} already belongs to a chunk")
        return checked

    def add_state(self, state_path: str, manifest: dict | None = None) -> dict:
        """Validate and admit one state file.  See module docstring contract."""
        if manifest is not None and isinstance(manifest.get("chunks"), list):
            manifest = _normalize_manifest(manifest)
        state_path = os.path.abspath(state_path)
        # Full self-verification: recompute digest from frames so a tampered
        # record section is rejected even if its header digest is stale.
        header = verify_state(state_path, self.meter)
        chunk = header["chunk"]
        digest = header["digest"]
        hl, rl, il, rc = header["_lens"]
        if rc != header["record_count"]:
            raise ManifestConflict(
                f"{chunk}: footer record_count {rc} != header "
                f"{header['record_count']}")

        ids = list(read_ids(state_path, self.meter))
        if len(ids) != header["id_count"] or len(set(ids)) != len(ids):
            raise ManifestConflict(f"{chunk}: id section corrupt")

        # Optional external manifest anchor: chunk identity, disjoint id sets
        # and original-content digest are pinned by the shard manifest.
        man_entry = None
        if manifest is not None:
            entry = manifest["chunks"].get(chunk)
            if entry is None:
                raise ManifestConflict(
                    f"chunk {chunk!r} not present in manifest")
            if entry["digest"] != digest:
                raise ManifestConflict(
                    f"{chunk}: state digest {digest} != manifest "
                    f"{entry['digest']}")
            if list(entry["ids"]) != ids:
                raise ManifestConflict(f"{chunk}: id set differs from manifest")
            man_entry = entry

        prev = self.existing_chunk(chunk)
        if prev is not None:
            if prev[0] != digest:
                raise ChunkConflict(
                    f"chunk {chunk!r} re-sent with different content "
                    f"(stored digest {prev[0]}, incoming {digest})")
            # Same chunk, same digest: verify id set then treat as idempotent.
            stored = [r[0] for r in self.db.execute(
                "SELECT id FROM chunk_ids WHERE chunk=? ORDER BY id",
                (chunk,))]
            if stored != ids:
                raise ChunkConflict(
                    f"chunk {chunk!r}: same digest but different id set")
            self.meter.event("idempotent_resend", chunk=chunk, ids=len(ids))
            return {"chunk": chunk, "action": "idempotent", "ids": len(ids)}

        # New chunk: full disjointness check before anything is committed.
        n_checked = self._stream_overlap_ok(ids)
        self.meter.event("overlap_check", chunk=chunk, ids_checked=n_checked)

        dest = self.dir / STATEDIR / f"{chunk}.state"
        tmp_dest = str(dest) + ".incoming"
        shutil.copyfile(state_path, tmp_dest)
        self.meter.wrote("state_copy", os.path.getsize(state_path))
        try:
            with self.db:  # single transaction: all-or-nothing admission
                self.db.execute(
                    "INSERT INTO chunks VALUES(?,?,?,?)",
                    (chunk, digest, rc, dest.name))
                self.db.executemany(
                    "INSERT INTO chunk_ids(id, chunk) VALUES(?,?)",
                    ((rid, chunk) for rid in ids))
            os.replace(tmp_dest, dest)
        except BaseException:
            if os.path.exists(tmp_dest):
                os.unlink(tmp_dest)
            raise
        self.meter.event("admit_chunk", chunk=chunk, ids=len(ids),
                         records=rc, digest=digest[:12])
        return {"chunk": chunk, "action": "admitted",
                "ids": len(ids), "records": rc, "digest": digest}

    def merge(self, state_paths: list[str],
              manifest_path: str | None = None) -> list[dict]:
        manifest = load_manifest(manifest_path) if manifest_path else None
        results = []
        for p in state_paths:
            results.append(self.add_state(p, manifest))
        return results

    def merge_pool(self, other_dir: str) -> list[dict]:
        """Admit every state of another pool (multi-level merge)."""
        other = Pool(other_dir, create=False)
        try:
            paths = [str(Path(other_dir) / STATEDIR / r[0])
                     for r in other.db.execute(
                         "SELECT state_file FROM chunks ORDER BY chunk")]
        finally:
            other.close()
        return self.merge(paths)

    # ------------------------------------------------------------------
    def chunks(self) -> list[dict]:
        return [{"chunk": r[0], "digest": r[1], "record_count": r[2],
                 "state_file": r[3]}
                for r in self.db.execute(
                    "SELECT chunk, digest, record_count, state_file "
                    "FROM chunks ORDER BY chunk")]

    def iter_state_files(self) -> Iterator[str]:
        for r in self.db.execute(
                "SELECT state_file FROM chunks ORDER BY chunk"):
            yield str(self.dir / STATEDIR / r[0])

    def iter_records(self) -> Iterator[tuple]:
        """Stable k-way merge of all admitted sorted state files."""
        sources = [read_records(p, self.meter) for p in self.iter_state_files()]
        if not sources:
            return iter(())
        return heapq.merge(*sources,
                           key=lambda rec: (rec[0], rec[1] is None,
                                            0 if rec[1] is None else rec[1],
                                            rec[2]))

    def inspect(self) -> dict:
        return {"dir": str(self.dir), "layout": self.layout,
                "sqlite_page_cache_bytes": 16 * 1024,
                "chunks": self.chunks(),
                "total_ids": self.db.execute(
                    "SELECT COUNT(*) FROM chunk_ids").fetchone()[0],
                "total_records": self.db.execute(
                    "SELECT COALESCE(SUM(record_count),0) FROM chunks"
                ).fetchone()[0],
                "state_bytes": sum(
                    os.path.getsize(p) for p in self.iter_state_files())}

    def close(self) -> None:
        self.db.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="merge ordered states into a pool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("merge")
    m.add_argument("pool_dir")
    m.add_argument("states", nargs="+")
    m.add_argument("--manifest")
    m.add_argument("--meter-log")
    mp = sub.add_parser("merge-pool")
    mp.add_argument("pool_dir")
    mp.add_argument("other_pool_dir")
    mp.add_argument("--meter-log")
    ins = sub.add_parser("inspect")
    ins.add_argument("pool_dir")
    args = ap.parse_args(argv)
    if args.cmd == "inspect":
        pool = Pool(args.pool_dir, create=False)
        try:
            print(json.dumps(pool.inspect(), ensure_ascii=False, indent=2))
        finally:
            pool.close()
        return 0
    meter = Meter(getattr(args, "meter_log", None))
    pool = Pool(args.pool_dir, meter=meter)
    try:
        if args.cmd == "merge":
            res = pool.merge(args.states, args.manifest)
        else:
            res = pool.merge_pool(args.other_pool_dir)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        meter.event("merge_done", **meter.traffic())
    finally:
        pool.close()
        meter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
