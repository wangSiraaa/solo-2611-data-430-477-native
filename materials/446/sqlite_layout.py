"""Alternative state layout: all-SQLite ordered aggregation.

Instead of portable ordered-run files plus a thin catalog, this layout stores
every record frame's decoded columns inside SQLite and lets the engine do the
sorting and de-duplication:

    chunks(chunk PRIMARY KEY, digest)
    records(id, chunk, grp, key_null, keyv, value)
    UNIQUE(id)                       -- cross-chunk overlap enforced in-engine
    INDEX (grp, key_null, keyv, id)  -- ordered scan / merge sort support

It is a genuinely viable design (the task allows library sorting), kept here
so run_demo.py can measure the real trade-offs against the ordered-run layout.

Trade-offs (quantified by the benchmark, not asserted in prose):

* disk read/write: SQLite writes WAL/journal/index pages in addition to row
  bytes, so admission writes more bytes than appending one state file; reads
  pay index traversal but avoid opening/merging N files;
* de-duplication: distinct is a single ``GROUP BY``/index scan inside the
  engine with no Python spill files, but its working set lives in SQLite temp
  pages governed by the 16 KiB cache, spilling via the engine's own sorter;
* time-to-first-result: no merge heap opens per state, but the first row waits
  on the engine's sort/index, whereas ordered runs start streaming immediately
  from already-sorted files;
* portability: the SQLite file is one self-contained artifact, while the
  ordered-run layout exchanges language-neutral binary state files any process
  can read without a database.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from meter import Meter
from ordered_state import read_records
from run_pool import ChunkConflict, ManifestConflict, OverlapConflict


class SqliteStatePool:
    layout = "all-sqlite (in-table records, index ordering)"

    def __init__(self, directory: str, meter: Meter):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meter = meter
        self.path = self.dir / "state.sqlite"
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA page_size=4096")
        self.db.execute("PRAGMA cache_size=-16")          # 16 KiB cap
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("CREATE TABLE IF NOT EXISTS chunks("
                        "chunk TEXT PRIMARY KEY, digest TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS records("
                        "id INTEGER NOT NULL UNIQUE, chunk TEXT NOT NULL, "
                        "grp TEXT NOT NULL, key_null INTEGER NOT NULL, "
                        "keyv INTEGER NOT NULL, value TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_order ON records("
                        "grp, key_null, keyv, id)")
        self.db.commit()

    def admit(self, state_path: str, expected_digest: str | None = None,
              expected_ids: list[int] | None = None) -> str:
        from ordered_state import verify_state
        header = verify_state(state_path, self.meter)
        chunk, digest = header["chunk"], header["digest"]
        if expected_digest is not None and expected_digest != digest:
            raise ManifestConflict(f"{chunk}: digest mismatch")
        prev = self.db.execute(
            "SELECT digest FROM chunks WHERE chunk=?", (chunk,)).fetchone()
        if prev is not None:
            if prev[0] != digest:
                raise ChunkConflict(f"{chunk}: different content re-send")
            return "idempotent"
        rows = [(rid, chunk, g, int(k is None), 0 if k is None else k, v)
                for g, k, rid, v in read_records(state_path, self.meter)]
        ids = [r[0] for r in rows]
        if expected_ids is not None and sorted(expected_ids) != sorted(ids):
            raise ManifestConflict(f"{chunk}: id set mismatch")
        try:
            size_before = os.path.getsize(self.path)
            with self.db:
                self.db.executemany(
                    "INSERT INTO records(id,chunk,grp,key_null,keyv,value) "
                    "VALUES(?,?,?,?,?,?)", rows)
                self.db.execute("INSERT INTO chunks VALUES(?,?)",
                                (chunk, digest))
            self.db.commit()
            # Engine page writes leave the Python meter blind; account for the
            # real on-disk growth (table + index pages) explicitly.
            grew = os.path.getsize(self.path) - size_before
            self.meter.wrote("sqlite_pages", max(grew, 0))
        except sqlite3.IntegrityError as exc:
            raise OverlapConflict(f"id overlap: {exc}") from exc
        return "admitted"

    def iter_records(self):
        for r in self.db.execute(
                "SELECT grp, CASE key_null WHEN 1 THEN NULL ELSE keyv END, "
                "id, value FROM records "
                "ORDER BY grp, key_null, keyv, id"):
            yield r

    def distinct_count(self, group: str) -> int:
        return self.db.execute(
            "SELECT COUNT(DISTINCT value) FROM records WHERE grp=?",
            (group,)).fetchone()[0]

    def _engine_page_reads(self) -> int:
        """Total page reads since the connection opened (best effort)."""
        try:
            rows = list(self.db.execute("PRAGMA stats"))
        except sqlite3.DatabaseError:
            return 0
        total = 0
        for name, value, *_ in rows:
            if name in ("sqlite_stat",):
                continue
            if isinstance(value, int) and "page" in str(name).lower():
                total += value
        return total

    def file_bytes(self) -> int:
        return os.path.getsize(self.path)

    def close(self):
        self.db.close()


def benchmark(all_rows: list[dict], work_dir: str, budget: int) -> dict:
    """Compare ordered-run vs all-SQLite layouts on identical inputs."""
    from run_pool import Pool
    from finalizer import finalize
    from ordered_state import partial

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    # Build four shard states from the full row set.
    shards = {f"sh-{i}": all_rows[i::4] for i in range(4)}
    state_paths = []
    for name, rows in shards.items():
        p = work / f"{name}.state"
        partial(rows, name, str(p), meter=Meter(), spill_bytes=16 * 1024)
        state_paths.append(str(p))

    # --- layout A: ordered runs + catalog ---------------------------------
    m_a = Meter()
    pool_a = Pool(str(work / "run-pool"), meter=m_a)
    t0 = time.perf_counter()
    pool_a.merge(state_paths)
    admit_a = time.perf_counter() - t0
    # time until the first ordered record is actually produced (generator)
    t0 = time.perf_counter()
    rec_iter = pool_a.iter_records()
    first_a_rec = next(rec_iter)
    first_a = time.perf_counter() - t0
    res_a = list(finalize(pool_a, distinct=True, meter=m_a))
    disk_a = pool_a.inspect()["state_bytes"] + os.path.getsize(
        work / "run-pool" / "catalog.sqlite")
    pool_a.close()

    # --- layout B: all-SQLite ----------------------------------------------
    m_b = Meter()
    pool_b = SqliteStatePool(str(work / "sql-pool"), m_b)
    t0 = time.perf_counter()
    for p in state_paths:
        pool_b.admit(p)
    admit_b = time.perf_counter() - t0
    # same measurement: time to the first row yielded by the ordered scan
    t0 = time.perf_counter()
    sql_iter = iter(pool_b.iter_records())
    first_b_rec = next(sql_iter)
    first_b = time.perf_counter() - t0
    disk_b = pool_b.file_bytes()
    page = pool_b.db.execute("PRAGMA page_size").fetchone()[0]
    # distinct done inside the engine for the largest group
    biggest = max(res_a, key=lambda r: r["occurrences"])["group"]
    t0 = time.perf_counter()
    distinct_b = pool_b.distinct_count(biggest)
    distinct_b_s = time.perf_counter() - t0
    engine_pages = pool_b._engine_page_reads()
    if engine_pages:
        m_b.read("sqlite_pages", engine_pages * page)
    pool_b.close()

    return {
        "summary": {
            "ordered_run": {
                "admit_seconds": round(admit_a, 4),
                "first_result_seconds": round(first_a, 4),
                "disk_bytes": disk_a,
                "peak_payload_bytes": m_a.peak,
                "dedup": "python bounded set + external spill files",
                "read_bytes": m_a.traffic()["read_total"],
                "written_bytes": m_a.traffic()["written_total"]},
            "all_sqlite": {
                "admit_seconds": round(admit_b, 4),
                "first_result_seconds": round(first_b, 4),
                "disk_bytes": disk_b,
                "distinct_seconds": round(distinct_b_s, 4),
                "distinct_values_biggest_group": distinct_b,
                "dedup": "engine COUNT(DISTINCT) / index, 16KiB cache",
                "read_bytes": m_b.traffic()["read_total"],
                "written_bytes": m_b.traffic()["written_total"]}},
        "groups": len(res_a),
        "within_budget": max(m_a.peak, m_b.peak) <= budget}
