"""End-to-end demonstration of the session return protocol (question 463).

Run from this directory::

    python3 probe.py          # deterministic scenario report (JSON)
    python3 probe.py --json   # same, machine friendly (default)
    python3 probe.py --human  # readable summary

The original repro is still the first field: a TEMP table created under tenant
``a`` must not be visible to the next lease, tenant ``b``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

from executor import Query
from pool import Pool


def build_tenants(root):
    paths = {}
    for name in ("a", "b", "c"):
        path = Path(root) / name
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE data(v)")
        conn.execute("INSERT INTO data VALUES(?)", (name,))
        conn.commit()
        conn.close()
        paths[name] = path
    return paths


def temp_names(session):
    return [
        name
        for _kind, name in session.db.execute(
            "SELECT type, name FROM sqlite_temp_master "
            "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        ).fetchall()
    ]


def databases(session):
    return [name for _s, name, _f in session.db.execute("PRAGMA database_list")]


def pragma(session, name):
    return session.db.execute("PRAGMA {}".format(name)).fetchone()[0]


def leak_repro(paths):
    """The original one-connection leak repro, now expected to be clean."""

    pool = Pool(1)
    try:
        first = pool.acquire("a", paths["a"])
        assert first.session.db.execute(
            "SELECT v FROM tenant.data"
        ).fetchone()[0] == "a"
        first.session.db.execute("CREATE TEMP TABLE prior_secret(v)")
        first.session.db.execute("INSERT INTO prior_secret VALUES('secret')")
        first.session.db.commit()
        pool.release(first)

        second = pool.acquire("b", paths["b"])
        try:
            return {
                "normal_tenant": second.session.db.execute(
                    "SELECT v FROM tenant.data"
                ).fetchone()[0],
                "leaked_temp_table": bool(
                    second.session.db.execute(
                        "SELECT 1 FROM sqlite_temp_master WHERE name='prior_secret'"
                    ).fetchone()
                ),
            }
        finally:
            pool.release(second)
    finally:
        pool.close()


def paused_return(paths):
    """Return the connection while the consumer is paused mid-result-set."""

    pool = Pool(1, worker_timeout=2.0)
    try:
        lease = pool.acquire("a", paths["a"])
        lease.session.db.executemany(
            "INSERT INTO tenant.data VALUES(?)", [("a",)] * 100
        )
        lease.session.db.commit()
        pause = threading.Event()  # unset -> consumer has stopped consuming
        first_row = threading.Event()

        def gate(query):
            if len(query.rows) == 1:
                first_row.set()

        query = Query(pool, lease, "SELECT v FROM tenant.data").start(
            pause=pause, gate=gate
        )
        first_row.wait(2.0)
        before = {
            "paused_done": query.done.is_set(),
            "worker_alive": bool(query.worker and query.worker.is_alive()),
        }
        started = time.monotonic()
        query.return_connection(timeout=2.0)
        query.done.wait(2.0)
        elapsed = time.monotonic() - started

        fresh = pool.acquire("b", paths["b"])
        try:
            after = {
                "return_seconds": round(elapsed, 3),
                "worker_stopped": not (query.worker and query.worker.is_alive()),
                "next_tenant": fresh.session.db.execute(
                    "SELECT v FROM tenant.data"
                ).fetchone()[0],
                "next_temp_objects": temp_names(fresh.session),
                "next_databases": databases(fresh.session),
                "next_in_transaction": fresh.session.db.in_transaction,
                "next_foreign_keys": pragma(fresh.session, "foreign_keys"),
            }
        finally:
            pool.release(fresh)
        return {"before": before, "after": after}
    finally:
        pool.close()


def cleanup_error_eviction(paths):
    """A failed cleanup must evict, never recycle, the connection."""

    pool = Pool(1)
    try:
        lease = pool.acquire("a", paths["a"])
        lease.session.db.execute("CREATE TEMP TABLE leak(v)")
        lease.session.fail_cleanup = True
        session = lease.session
        pool.release(lease)
        rebuilt = pool.acquire("b", paths["b"])
        try:
            result = {
                "lease_state": lease.state,
                "evicted": lease.evicted,
                "cleanup_error": lease.cleanup_error,
                "old_session_alive": session.alive,
                "total_connections": pool.total_connections(),
                "capacity": pool.capacity,
                "rebuilt_tenant": rebuilt.session.db.execute(
                    "SELECT v FROM tenant.data"
                ).fetchone()[0],
                "failed_cleanups": pool.stats["failed_cleanups"],
            }
        finally:
            pool.release(rebuilt)
        return result
    finally:
        pool.close()


def late_cancel(paths):
    """A stale cancel after the lease was returned must not touch new work."""

    pool = Pool(1, worker_timeout=2.0)
    try:
        old_lease = pool.acquire("a", paths["a"])
        old_pause = threading.Event()
        old_started = threading.Event()

        def old_gate(query):
            if len(query.rows) == 1:
                old_started.set()

        old = Query(pool, old_lease, "SELECT v FROM tenant.data").start(
            pause=old_pause, gate=old_gate
        )
        old_started.wait(2.0)
        old.return_connection(timeout=2.0)
        old.done.wait(2.0)

        new_lease = pool.acquire("b", paths["b"])
        new_pause = threading.Event()
        new_started = threading.Event()

        def new_gate(query):
            if len(query.rows) == 1:
                new_started.set()

        new = Query(pool, new_lease, "SELECT v FROM tenant.data").start(
            pause=new_pause, gate=new_gate
        )
        new_started.wait(2.0)

        stale_delivered = old.cancel()
        time.sleep(0.05)
        still_running = not new.done.is_set()

        new_pause.set()
        new.done.wait(2.0)
        pool.release(new_lease)
        return {
            "stale_cancel_delivered": stale_delivered,
            "new_query_survived": still_running,
            "new_query_state": new.state,
            "new_query_error": new.error,
            "new_query_rows": [row[0] for row in new.rows],
        }
    finally:
        pool.close()


def capacity_report(paths):
    """In-flight connections never exceed capacity, including rebuilds."""

    pool = Pool(2, max_leases=8)
    try:
        two = [pool.acquire(t, paths[t]) for t in ("a", "b")]
        peak = pool.total_connections()
        pool.release(two[0])
        pool.release(two[1])
        snap = pool.snapshot()
        return {
            "capacity": snap["capacity"],
            "peak_total_connections": peak,
            "total_connections": snap["total_connections"],
            "idle_connections": snap["idle_connections"],
            "active_leases": snap["active_leases"],
            "lease_slots_in_use": snap["lease_slots_in_use"],
            "max_leases": snap["max_leases"],
        }
    finally:
        pool.close()


def main():
    human = "--human" in sys.argv
    with tempfile.TemporaryDirectory() as td:
        paths = build_tenants(td)
        report = {
            "leak_repro": leak_repro(paths),
            "paused_return": paused_return(paths),
            "cleanup_error_eviction": cleanup_error_eviction(paths),
            "late_cancel": late_cancel(paths),
            "capacity": capacity_report(paths),
        }

    if human:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(json.dumps(report, sort_keys=True))
    ok = (
        not report["leak_repro"]["leaked_temp_table"]
        and report["paused_return"]["after"]["worker_stopped"]
        and report["cleanup_error_eviction"]["evicted"]
        and not report["cleanup_error_eviction"]["old_session_alive"]
        and not report["late_cancel"]["stale_cancel_delivered"]
        and report["late_cancel"]["new_query_survived"]
        and report["capacity"]["peak_total_connections"] <= report["capacity"]["capacity"]
    )
    if not ok:
        raise SystemExit("protocol check failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
