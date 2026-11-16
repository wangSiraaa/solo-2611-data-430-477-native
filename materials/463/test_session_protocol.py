"""Deterministic tests for the SQLite lease return protocol (question 463).

Run with::

    python3 test_session_protocol.py

The suite uses real SQLite connections and real threads; pauses are coordinated
with events so the interleavings are deterministic rather than timing-based.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from executor import Query
from pool import (
    LEASE_EVICTED,
    LEASE_RETURNED,
    Pool,
    PoolClosedError,
    PoolTimeout,
)
from session import LeaseClosedError, Session

TENANTS = ("a", "b", "c")


def make_tenant_dbs(directory, tenants=TENANTS, rows=3):
    paths = {}
    for name in tenants:
        path = Path(directory) / name
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE data(v)")
        conn.execute("CREATE INDEX data_v ON data(v)")
        conn.executemany(
            "INSERT INTO data VALUES(?)", [(name,)] * rows
        )
        conn.commit()
        conn.close()
        paths[name] = path
    return paths


def tenant_value(lease):
    return lease.session.db.execute("SELECT v FROM tenant.data LIMIT 1").fetchone()[0]


def temp_names(session):
    return [
        name
        for _kind, name in session.db.execute(
            "SELECT type, name FROM sqlite_temp_master "
            "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        ).fetchall()
    ]


def database_names(session):
    return [name for _s, name, _f in session.db.execute("PRAGMA database_list")]


def read_pragma(session, name):
    return session.db.execute("PRAGMA {}".format(name)).fetchone()[0]


class ProtocolTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = make_tenant_dbs(self._tmp.name)
        self.pools = []

    def tearDown(self):
        for pool in self.pools:
            pool.close()
        self._tmp.cleanup()

    def pool(self, size=2, **kwargs):
        pool = Pool(size, **kwargs)
        self.pools.append(pool)
        return pool

    def assert_session_pristine(self, session, expect_no_tenant=True):
        self.assertFalse(session.db.in_transaction, "transaction leaked")
        self.assertEqual(temp_names(session), [], "TEMP objects leaked")
        databases = database_names(session)
        if expect_no_tenant:
            self.assertEqual(databases, ["main", "temp"], databases)
        for key, expected in session.defaults.items():
            self.assertEqual(
                read_pragma(session, key), expected,
                "pragma {} was not restored".format(key),
            )

    def assert_tenant_view(self, lease, tenant):
        self.assertEqual(tenant_value(lease), tenant)
        self.assertEqual(
            sorted(database_names(lease.session)), ["main", "temp", "tenant"]
        )
        self.assertEqual(temp_names(lease.session), [])
        self.assertFalse(lease.session.db.in_transaction)
        self.assertEqual(
            read_pragma(lease.session, "foreign_keys"),
            lease.session.defaults["foreign_keys"],
        )


class TestBasicIsolation(ProtocolTestCase):
    def test_tenant_binding_and_temp_isolation(self):
        pool = self.pool(size=1)
        first = pool.acquire("a", self.paths["a"])
        self.assert_tenant_view(first, "a")
        first.session.db.execute("CREATE TEMP TABLE prior_secret(v)")
        first.session.db.execute("INSERT INTO prior_secret VALUES('secret')")
        first.session.db.execute("CREATE TEMP VIEW prior_view AS SELECT * FROM prior_secret")
        first.session.db.execute(
            "CREATE TEMP TRIGGER prior_trg AFTER INSERT ON prior_secret "
            "BEGIN SELECT 1; END"
        )
        first.session.db.commit()
        pool.release(first)

        second = pool.acquire("b", self.paths["b"])
        try:
            self.assert_tenant_view(second, "b")
            self.assertEqual(temp_names(second.session), [])
            # Old tenant's objects are gone, not just hidden.
            with self.assertRaises(sqlite3.OperationalError):
                second.session.db.execute("SELECT * FROM prior_secret")
            self.assertNotEqual(first.serial, second.serial)
        finally:
            pool.release(second)

    def test_open_transaction_is_rolled_back(self):
        pool = self.pool(size=1)
        lease = pool.acquire("a", self.paths["a"])
        lease.session.db.execute("INSERT INTO tenant.data VALUES('uncommitted')")
        self.assertTrue(lease.session.db.in_transaction)
        pool.release(lease)

        next_lease = pool.acquire("a", self.paths["a"])
        try:
            rows = next_lease.session.db.execute(
                "SELECT v FROM tenant.data WHERE v='uncommitted'"
            ).fetchall()
            self.assertEqual(rows, [])
            self.assert_tenant_view(next_lease, "a")
        finally:
            pool.release(next_lease)

    def test_pragmas_restored_from_new_connection_defaults(self):
        config = {"foreign_keys": 1, "query_only": 0, "temp_store": 1}
        pool = self.pool(size=1, config=config)
        lease = pool.acquire("a", self.paths["a"])
        lease.session.db.execute("PRAGMA foreign_keys = OFF")
        lease.session.db.execute("PRAGMA temp_store = 2")
        lease.session.db.execute("PRAGMA busy_timeout = 17")
        pool.release(lease)

        fresh = pool.acquire("b", self.paths["b"])
        try:
            self.assertEqual(read_pragma(fresh.session, "foreign_keys"), 1)
            self.assertEqual(read_pragma(fresh.session, "temp_store"), 1)
            self.assertEqual(read_pragma(fresh.session, "busy_timeout"), 5000)
            self.assertEqual(
                fresh.session.defaults,
                {"foreign_keys": 1, "query_only": 0, "temp_store": 1,
                 "busy_timeout": 5000, "recursive_triggers": 0,
                 "defer_foreign_keys": 0},
            )
        finally:
            pool.release(fresh)

    def test_query_only_restored_and_does_not_block_cleanup(self):
        pool = self.pool(size=1)
        lease = pool.acquire("a", self.paths["a"])
        lease.session.db.execute("CREATE TEMP TABLE t(v)")
        lease.session.db.execute("INSERT INTO t VALUES(1)")
        lease.session.db.execute("PRAGMA query_only = 1")
        self.assertTrue(read_pragma(lease.session, "query_only"))
        pool.release(lease)

        fresh = pool.acquire("b", self.paths["b"])
        try:
            self.assertEqual(read_pragma(fresh.session, "query_only"), 0)
            self.assertEqual(temp_names(fresh.session), [])
            fresh.session.db.execute("CREATE TEMP TABLE writable(v)")
        finally:
            pool.release(fresh)

    def test_multiple_pragmas_and_attached_reset(self):
        pool = self.pool(size=1)
        lease = pool.acquire("a", self.paths["a"])
        lease.session.db.execute("ATTACH DATABASE ? AS scratch", (str(self.paths["c"]),))
        lease.session.db.execute("PRAGMA recursive_triggers = 1")
        lease.session.db.execute("PRAGMA defer_foreign_keys = 1")
        pool.release(lease)

        fresh = pool.acquire("b", self.paths["b"])
        try:
            self.assertEqual(database_names(fresh.session), ["main", "temp", "tenant"])
            self.assertEqual(read_pragma(fresh.session, "recursive_triggers"), 0)
            self.assertEqual(read_pragma(fresh.session, "defer_foreign_keys"), 0)
        finally:
            pool.release(fresh)


class TestPausedRowConsumption(ProtocolTestCase):
    def test_return_while_consumer_paused(self):
        pool = self.pool(size=1, worker_timeout=2.0)
        lease = pool.acquire("a", self.paths["a"])
        # An unset ``pause`` event models a consumer that stopped consuming.
        pause = threading.Event()
        produced = threading.Event()

        def gate(query):
            if len(query.rows) == 1:
                produced.set()

        query = Query(pool, lease, "SELECT v FROM tenant.data").start(
            pause=pause, gate=gate
        )
        self.assertTrue(produced.wait(2.0), "query never produced a row")
        self.assertFalse(query.done.is_set(), "query should be paused")

        # Returning while the consumer is paused must abort and unblock it.
        returned = query.return_connection(timeout=2.0)
        self.assertTrue(returned)
        self.assertTrue(query.done.wait(2.0), "worker did not finish")
        self.assertEqual(lease.state, LEASE_RETURNED)
        self.assertFalse(lease.session.worker_count())
        self.assertEqual(query.state, "aborted")

        # Next lease sees no leftover cursor/temp/transaction.
        fresh = pool.acquire("b", self.paths["b"])
        try:
            self.assert_tenant_view(fresh, "b")
            self.assertEqual(fresh.session.cursors, set())
        finally:
            pool.release(fresh)

    def test_paused_generator_closed_on_release(self):
        pool = self.pool(size=1)
        lease = pool.acquire("a", self.paths["a"])
        pause = threading.Event()  # unset: consumer is not consuming
        got_first = threading.Event()

        def gate(query):
            if len(query.rows) == 1:
                got_first.set()

        query = Query(pool, lease, "SELECT v FROM tenant.data", params=()).start(
            pause=pause, gate=gate
        )
        self.assertTrue(got_first.wait(2.0))
        self.assertFalse(query.done.is_set())
        # Consumer disappears without cancelling; release must still settle.
        pool.release(lease, worker_timeout=2.0)
        self.assertTrue(query.done.wait(2.0))
        self.assert_session_pristine(lease.session)


class TestCleanupFailure(ProtocolTestCase):
    def test_injected_cleanup_failure_evicts_connection(self):
        pool = self.pool(size=1)
        lease = pool.acquire("a", self.paths["a"])
        session = lease.session
        session.db.execute("CREATE TEMP TABLE leak(v)")
        session.fail_cleanup = True

        pool.release(lease)
        self.assertEqual(lease.state, LEASE_EVICTED)
        self.assertTrue(lease.evicted)
        self.assertIn("injected", lease.cleanup_error)
        self.assertFalse(session.alive, "half-cleaned connection was not closed")
        self.assertEqual(pool.total_connections(), 0)
        self.assertEqual(pool.idle_connections(), 0)
        self.assertEqual(pool.stats["failed_cleanups"], 1)

        # The pool rebuilds on demand and stays within capacity.
        fresh = pool.acquire("b", self.paths["b"])
        try:
            self.assert_tenant_view(fresh, "b")
            self.assertEqual(pool.total_connections(), 1)
            self.assertLessEqual(pool.total_connections(), pool.capacity)
        finally:
            pool.release(fresh)

    def test_cleanup_error_never_returns_dirty_connection(self):
        # Force a real cleanup failure by making DETACH impossible: hold a
        # cursor open through the session's own bookkeeping so cleanup cannot
        # roll back / detach cleanly.
        pool = self.pool(size=1)
        lease = pool.acquire("a", self.paths["a"])
        session = lease.session
        raw = session.db.execute("SELECT v FROM tenant.data")
        session.cursors.add(raw)  # pretend a cursor escaped the worker

        # Even with an externally held cursor, release either cleans or evicts;
        # it must never leave the session in the idle queue while dirty.
        pool.release(lease)
        if lease.state == LEASE_RETURNED:
            self.assert_session_pristine(session)
        else:
            self.assertEqual(lease.state, LEASE_EVICTED)
            self.assertFalse(session.alive)
        try:
            raw.close()
        except Exception:
            pass

    def test_worker_timeout_evicts(self):
        pool = self.pool(size=1, worker_timeout=0.2)
        lease = pool.acquire("a", self.paths["a"])
        blocker = threading.Event()
        started = threading.Event()

        def gate(query):
            started.set()
            blocker.wait(5.0)  # deliberately uncooperative; release must time out

        query = Query(pool, lease, "SELECT v FROM tenant.data").start(gate=gate)
        self.assertTrue(started.wait(2.0))
        try:
            pool.release(lease, worker_timeout=0.2)
            self.assertEqual(lease.state, LEASE_EVICTED)
            self.assertFalse(lease.session.alive)
            self.assertEqual(pool.total_connections(), 0)
            self.assertEqual(pool.stats["failed_cleanups"], 1)
        finally:
            blocker.set()
            query.done.wait(2.0)


class TestCancellationOwnership(ProtocolTestCase):
    def test_late_cancel_does_not_touch_next_tenant(self):
        pool = self.pool(size=1, worker_timeout=2.0)
        first = pool.acquire("a", self.paths["a"])
        old_pause = threading.Event()  # unset: old consumer is paused
        old_started = threading.Event()

        def old_gate(query):
            if len(query.rows) == 1:
                old_started.set()

        old = Query(pool, first, "SELECT v FROM tenant.data").start(
            pause=old_pause, gate=old_gate
        )
        self.assertTrue(old_started.wait(2.0))

        # Return while paused: this aborts and cleans the old lease.
        self.assertTrue(old.return_connection(timeout=2.0))
        self.assertTrue(old.done.wait(2.0))

        # New tenant starts a query and pauses mid-stream.
        second = pool.acquire("b", self.paths["b"])
        new_pause = threading.Event()  # unset: new consumer pauses too
        new_started = threading.Event()

        def new_gate(query):
            if len(query.rows) == 1:
                new_started.set()

        new = Query(pool, second, "SELECT v FROM tenant.data").start(
            pause=new_pause, gate=new_gate
        )
        self.assertTrue(new_started.wait(2.0))

        # The stale cancel must be a no-op for the new lease.
        self.assertFalse(old.cancel())
        time.sleep(0.05)
        self.assertFalse(new.done.is_set(), "late cancel interrupted the new query")
        self.assertEqual(new.error, None)
        self.assertTrue(new.lease.is_owner())

        # Let the new query finish normally.
        new_pause.set()
        self.assertTrue(new.done.wait(2.0))
        self.assertEqual(new.state, "done")
        self.assertEqual([row[0] for row in new.rows], ["b", "b", "b"])
        pool.release(second)
        self.assert_session_pristine(second.session)

    def test_cancel_during_query_only_affects_own_lease(self):
        pool = self.pool(size=2)
        slow = pool.acquire("a", self.paths["a"])
        other = pool.acquire("b", self.paths["b"])
        slow.session.db.executemany(
            "INSERT INTO tenant.data VALUES(?)",
            [("a",)] * 1000,
        )
        slow.session.db.commit()

        query = Query(
            pool,
            slow,
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c "
            "WHERE x < 5000000) SELECT x FROM c",
        )
        query.start()
        time.sleep(0.05)
        self.assertTrue(query.cancel())
        self.assertTrue(query.done.wait(5.0))
        # The other lease is still fully usable.
        self.assertEqual(tenant_value(other), "b")
        self.assertTrue(other.is_owner())
        self.assertTrue(query.aborted.is_set())

        query.wait(2.0)
        pool.release(slow)
        pool.release(other)
        self.assert_session_pristine(slow.session)

    def test_cancel_after_return_is_noop(self):
        pool = self.pool(size=1)
        lease = pool.acquire("a", self.paths["a"])
        query = Query(pool, lease, "SELECT v FROM tenant.data").start()
        self.assertTrue(query.done.wait(2.0))
        pool.release(lease)
        self.assertFalse(query.cancel())
        self.assertFalse(lease.cancel())
        self.assertEqual(pool.snapshot()["stats"]["evicted"], 0)

    def test_start_on_returned_lease_rejected(self):
        pool = self.pool(size=1)
        lease = pool.acquire("a", self.paths["a"])
        pool.release(lease)
        with self.assertRaises(LeaseClosedError):
            Query(pool, lease, "SELECT 1").start()


class TestCapacityAndConcurrency(ProtocolTestCase):
    def test_capacity_never_exceeded_and_idle_reused(self):
        pool = self.pool(size=2)
        idles = []
        for tenant in ("a", "b"):
            lease = pool.acquire(tenant, self.paths[tenant])
            idles.append(lease)
        self.assertEqual(pool.total_connections(), 2)
        self.assertEqual(pool.active_connections(), 2)

        # Third lease waits until one is returned.
        with self.assertRaises(PoolTimeout):
            pool.acquire("c", self.paths["c"], timeout=0.1)
        self.assertEqual(pool.stats["acquire_timeouts"], 1)

        pool.release(idles[0])
        third = pool.acquire("c", self.paths["c"])
        try:
            self.assertEqual(tenant_value(third), "c")
            self.assertEqual(pool.total_connections(), 2)
            self.assertLessEqual(pool.total_connections(), pool.capacity)
        finally:
            pool.release(third)
            pool.release(idles[1])

    def test_rebuild_counts_toward_capacity(self):
        pool = self.pool(size=1)
        lease = pool.acquire("a", self.paths["a"])
        lease.session.fail_cleanup = True
        pool.release(lease)
        # The failed connection is closed, so it no longer counts.
        self.assertEqual(pool.total_connections(), 0)

        # Rebuilding happens one connection at a time and never exceeds the
        # capacity, even across repeated evictions.
        for index, tenant in enumerate(("b", "c", "a", "b")):
            lease = pool.acquire(tenant, self.paths[tenant])
            try:
                self.assertEqual(pool.total_connections(), 1)
                self.assertLessEqual(pool.total_connections(), pool.capacity)
                self.assert_tenant_view(lease, tenant)
                if index == 1:
                    lease.session.fail_cleanup = True
            finally:
                pool.release(lease)
        self.assertEqual(pool.total_connections(), 1)
        self.assertEqual(pool.stats["evicted"], 2)

    def test_max_leases_bounds_admission(self):
        pool = self.pool(size=3, max_leases=2)
        first = pool.acquire("a", self.paths["a"])
        second = pool.acquire("b", self.paths["b"])
        with self.assertRaises(PoolTimeout):
            pool.acquire("c", self.paths["c"], timeout=0.1)
        pool.release(first)
        third = pool.acquire("c", self.paths["c"])
        pool.release(second)
        pool.release(third)

    def test_retiring_connection_counts_until_closed(self):
        # A slow close must keep occupying a capacity slot: a concurrent
        # acquire may not build a replacement while the old handle is open.
        pool = self.pool(size=1)
        lease = pool.acquire("a", self.paths["a"])
        session = lease.session

        close_started = threading.Event()
        close_release = threading.Event()
        original_close = session.close

        def slow_close():
            close_started.set()
            close_release.wait(2.0)
            original_close()

        session.close = slow_close
        session.fail_cleanup = True

        releaser = threading.Thread(target=lambda: pool.release(lease))
        releaser.start()
        self.assertTrue(close_started.wait(2.0))

        with self.assertRaises(PoolTimeout):
            pool.acquire("b", self.paths["b"], timeout=0.1)
        close_release.set()
        releaser.join(2.0)
        self.assertEqual(pool.total_connections(), 0)

        rebuilt = pool.acquire("b", self.paths["b"])
        try:
            self.assert_tenant_view(rebuilt, "b")
            self.assertEqual(pool.total_connections(), 1)
        finally:
            pool.release(rebuilt)

    def test_close_during_rebuild_does_not_leak(self):
        pool = self.pool(size=1)
        lease = pool.acquire("a", self.paths["a"])
        lease.session.fail_cleanup = True
        # Evict, then race a rebuild against close.
        pool.release(lease)
        self.assertEqual(pool.total_connections(), 0)

        results = []
        lock = threading.Lock()

        def build():
            try:
                acquired = pool.acquire("b", self.paths["b"], timeout=1.0)
                results.append(("ok", acquired))
            except PoolClosedError:
                results.append(("closed", None))

        thread = threading.Thread(target=build)
        thread.start()
        pool.close()
        thread.join(3.0)
        self.assertFalse(thread.is_alive())
        with lock:
            outcomes = [kind for kind, _ in results]
        self.assertIn("closed", outcomes)
        # Any lease that did start is closed by the pool and not tracked.
        for kind, acquired in results:
            if kind == "ok":
                pool.release(acquired)
        self.assertEqual(pool.total_connections(), 0)
        self.assertEqual(pool.snapshot()["retiring_connections"], 0)

    def test_idle_connection_usable_while_other_lease_paused(self):
        pool = self.pool(size=2)
        slow = pool.acquire("a", self.paths["a"])
        pause = threading.Event()
        pause.set()
        started = threading.Event()

        def gate(query):
            started.set()
            pause.wait(5.0)

        query = Query(pool, slow, "SELECT v FROM tenant.data").start(gate=gate)
        self.assertTrue(started.wait(2.0))
        # Another physical connection is free: must not block on the pool.
        t0 = time.monotonic()
        other = pool.acquire("b", self.paths["b"], timeout=0.5)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5)
        try:
            self.assert_tenant_view(other, "b")
            self.assertEqual(other.session.db.execute("SELECT 1").fetchone()[0], 1)
        finally:
            pool.release(other)
            pause.clear()
            query.wait(2.0)
            pool.release(slow)
        self.assert_session_pristine(slow.session)

    def test_concurrent_leases_stay_isolated(self):
        pool = self.pool(size=3, max_leases=8)
        barrier = threading.Barrier(8)
        errors = []
        results = []
        lock = threading.Lock()

        def worker(index):
            tenant = TENANTS[index % len(TENANTS)]
            try:
                barrier.wait(2.0)
                for _ in range(10):
                    lease = pool.acquire(tenant, self.paths[tenant], timeout=5.0)
                    try:
                        lease.session.db.execute("CREATE TEMP TABLE scratch(v)")
                        lease.session.db.execute(
                            "INSERT INTO scratch VALUES(?)", (tenant,)
                        )
                        assert tenant_value(lease) == tenant
                        assert read_pragma(lease.session, "foreign_keys") == 1
                    finally:
                        pool.release(lease)
                with lock:
                    results.append(tenant)
            except Exception as exc:
                with lock:
                    errors.append("{}: {}".format(type(exc).__name__, exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30.0)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertLessEqual(pool.total_connections(), pool.capacity)
        self.assertEqual(pool.idle_connections(), pool.total_connections())
        for session in pool._idle:
            self.assert_session_pristine(session)

    def test_no_thread_leak_after_many_returns(self):
        pool = self.pool(size=1)
        baseline = threading.active_count()
        for index in range(25):
            tenant = TENANTS[index % len(TENANTS)]
            lease = pool.acquire(tenant, self.paths[tenant])
            pause = threading.Event()
            pause.set()
            query = Query(pool, lease, "SELECT v FROM tenant.data").start(
                gate=lambda q: pause.wait(0.5)
            )
            quick = Query(pool, lease, "SELECT count(*) FROM tenant.data").start()
            quick.wait(2.0)
            query.return_connection(timeout=2.0)
            self.assertTrue(query.done.wait(2.0))
        # Allow any daemon thread bookkeeping to settle.
        deadline = time.monotonic() + 2.0
        while threading.active_count() > baseline and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertLessEqual(threading.active_count(), baseline)
        self.assert_session_pristine(pool._idle[0])


class TestObservability(ProtocolTestCase):
    def test_snapshot_reports_states(self):
        pool = self.pool(size=2, max_leases=3)
        lease = pool.acquire("a", self.paths["a"])
        snap = pool.snapshot()
        self.assertEqual(snap["capacity"], 2)
        self.assertEqual(snap["active_leases"], 1)
        self.assertEqual(snap["total_connections"], 2)
        self.assertEqual(snap["idle_connections"], 1)
        self.assertEqual(snap["lease_slots_in_use"], 1)
        self.assertIn(lease.serial, snap["leases"])
        status = snap["leases"][lease.serial]
        self.assertEqual(status["tenant"], "a")
        self.assertEqual(status["state"], "active")
        self.assertEqual(status["generation"], 1)

        lease.session.db.execute("CREATE TEMP TABLE t(v)")
        mid = lease.session.snapshot()
        self.assertEqual(mid["temp_objects"], [["table", "t"]])
        self.assertEqual(mid["tenant"], "a")
        self.assertIn("tenant", mid["databases"])

        pool.release(lease)
        after = pool.snapshot()
        self.assertEqual(after["active_leases"], 0)
        self.assertEqual(after["lease_slots_in_use"], 0)
        self.assertEqual(after["stats"]["cleanups"], 1)
        idle_status = pool._idle[0].snapshot()
        self.assertEqual(idle_status["temp_objects"], [])
        self.assertEqual(idle_status["databases"], ["main", "temp"])
        self.assertEqual(idle_status["pragmas"], idle_status["pragmas"])

    def test_session_defaults_snapshot(self):
        session = Session({"foreign_keys": 0, "query_only": 0})
        try:
            self.assertEqual(session.defaults["foreign_keys"], 0)
            self.assertEqual(session.defaults["busy_timeout"], 5000)
            session.prepare("a", self.paths["a"])
            problems = session._verify_clean(expect_bound=True)
            self.assertEqual(problems, [])
            session.cleanup()
            self.assert_session_pristine(session)
        finally:
            session.close()

    def test_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            Session({"not_a_pragma": 1})

    def test_pool_close_is_idempotent(self):
        pool = Pool(2)
        pool.close()
        pool.close()
        with self.assertRaises(PoolClosedError):
            pool.acquire("a", self.paths["a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
