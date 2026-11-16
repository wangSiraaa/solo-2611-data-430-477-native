"""SQLite session isolation primitives for the shared connection pool.

A :class:`Session` owns exactly one real ``sqlite3.Connection``.  During a
lease the connection may accumulate state: an ATTACHed tenant database, TEMP
objects, transactions, PRAGMA overrides, active cursors and worker threads.
:meth:`Session.cleanup` restores the connection to the defaults captured from
the pool configuration when the connection was created, and then *proves* the
connection is clean.  A connection that cannot be proven clean is reported as
failed and must be closed by the pool instead of being reused.
"""

from __future__ import annotations

import sqlite3
import threading

#: Error text used when a lease return aborts an in-flight query.
QUERY_ABORTED = "query aborted by lease return"

#: Default connection-level session state.  The values are applied when a new
#: connection is created and are also the values that cleanup restores.
DEFAULT_CONFIG = {
    "foreign_keys": 1,
    "query_only": 0,
    "temp_store": 0,
    "busy_timeout": 5000,
    "recursive_triggers": 0,
    "defer_foreign_keys": 0,
}

#: Only these PRAGMAs participate in the defaults/restore protocol.  The
#: list is deliberately finite: cleanup restores exactly the state declared
#: when the connection was built.
_RESTORE_KEYS = tuple(DEFAULT_CONFIG)


class SessionError(RuntimeError):
    """Base class for session/protocol errors."""


class CleanupError(SessionError):
    """The connection could not be proven clean and must not be reused."""

    def __init__(self, message, problems=None):
        self.problems = list(problems or ())
        if self.problems:
            message = "{}: {}".format(message, "; ".join(self.problems))
        super().__init__(message)


class StaleLeaseError(SessionError):
    """An operation was attempted through a lease that no longer owns the session."""


class QueryAborted(SessionError):
    """An in-flight query was aborted because its lease is being returned."""


class LeaseClosedError(SessionError):
    """A query was started on a lease that is no longer accepting work."""


def _quote_identifier(name):
    return '"' + str(name).replace('"', '""') + '"'


class Session:
    """One physical SQLite connection with a strict clean/dirty protocol."""

    def __init__(self, config=None):
        merged = dict(DEFAULT_CONFIG)
        if config:
            unknown = set(config) - set(_RESTORE_KEYS)
            if unknown:
                raise ValueError(
                    "unsupported session pragma(s): {}".format(", ".join(sorted(unknown)))
                )
            merged.update(config)
        self.config = merged

        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.lock = threading.RLock()

        # Live state that cleanup must reclaim.
        self.cursors = set()
        self.workers = set()
        self.tenant = None
        self.path = None
        self.current_lease = None
        self.generation = 0

        self.alive = True
        self.cleanup_count = 0
        self.last_cleanup_error = None

        #: Test hook: when true, cleanup raises after partially cleaning.
        self.fail_cleanup = False

        # Defaults are *read back* from the new connection, so the observed
        # values (not merely the requested ones) define the session baseline.
        self.defaults = {}
        with self.lock:
            for key in _RESTORE_KEYS:
                self._set_pragma(key, merged[key])
            for key in _RESTORE_KEYS:
                self.defaults[key] = self._read_pragma(key)

    # ------------------------------------------------------------------ pragmas

    @staticmethod
    def _validate_pragma(key):
        if key not in _RESTORE_KEYS:
            raise ValueError("unsupported session pragma: {!r}".format(key))

    def _set_pragma(self, key, value):
        self._validate_pragma(key)
        # Keys/values come from trusted pool configuration, never from SQL.
        self.db.execute("PRAGMA {} = {}".format(key, int(value)))

    def _read_pragma(self, key):
        self._validate_pragma(key)
        row = self.db.execute("PRAGMA {}".format(key)).fetchone()
        return int(row[0]) if row else None

    # --------------------------------------------------------------- leasing

    def prepare(self, tenant, path):
        """Verify cleanliness, bind ``tenant`` and return the new generation.

        Called by the pool while it exclusively owns this session.  Raises
        rather than handing a dirty connection to a new tenant.
        """

        with self.lock:
            if not self.alive:
                raise SessionError("session is closed")
            if self.current_lease is not None:
                raise SessionError("session already has a live lease")

            # A reusable session must be clean before we attach anything.
            problems = self._verify_clean(expect_bound=False)
            if problems:
                raise CleanupError("idle connection was not clean", problems)

            try:
                self._rollback_all()
                self._detach_extra_databases()
                self.db.execute(
                    "ATTACH DATABASE ? AS tenant", (str(path),)
                )
                self._restore_defaults()
            except BaseException:
                # Leave the session marked dirty; the pool will discard it.
                raise

            self.tenant = tenant
            self.path = str(path)
            self.generation += 1
            return self.generation

    def adopt(self, lease):
        """Install ``lease`` as the current owner after :meth:`prepare`."""

        with self.lock:
            if not self.alive:
                raise SessionError("session is closed")
            if self.current_lease is not None:
                raise SessionError("session already has a live lease")
            self.current_lease = lease

    # ------------------------------------------------------------------ query

    def rows(self, sql, params=(), lease=None, abort=None):
        """Yield rows, tracking the cursor for cleanup.

        ``lease`` is checked when the cursor is opened.  ``abort`` is polled at
        row boundaries; the caller (``Query``) is responsible for closing the
        generator promptly when it is set.
        """

        with self.lock:
            if not self.alive:
                raise SessionError("session is closed")
            if lease is not None and self.current_lease is not lease:
                raise StaleLeaseError("lease no longer owns this session")
            cursor = self.db.execute(sql, params)
            self.cursors.add(cursor)
        try:
            for row in cursor:
                if abort is not None and abort.is_set():
                    raise QueryAborted(QUERY_ABORTED)
                if lease is not None and lease.abort.is_set():
                    raise QueryAborted(QUERY_ABORTED)
                yield row
        finally:
            with self.lock:
                self.cursors.discard(cursor)
                try:
                    cursor.close()
                except Exception:
                    pass

    # ---------------------------------------------------------------- workers

    def add_worker(self, worker):
        with self.lock:
            self.workers.add(worker)

    def remove_worker(self, worker):
        with self.lock:
            self.workers.discard(worker)

    def worker_count(self):
        with self.lock:
            return sum(1 for worker in self.workers if worker.is_alive())

    # --------------------------------------------------------------- cleanup

    def cleanup(self):
        """Return the connection to the declared session defaults.

        Order matters: roll back first, force write access if a lease enabled
        ``query_only``, drop every TEMP object, detach every non-main/temp
        database (including the tenant), restore the PRAGMAs and finally
        verify.  Any doubt raises :class:`CleanupError`.
        """

        with self.lock:
            if not self.alive:
                raise CleanupError("session is already closed")
            problems = []

            try:
                self._rollback_all()
            except Exception as exc:  # pragma: no cover - defensive
                problems.append("rollback: {}".format(exc))

            # DROP/DETACH work is easiest with writes enabled; the declared
            # query_only value is restored below.
            try:
                if self._read_pragma("query_only"):
                    self.db.execute("PRAGMA query_only = 0")
            except Exception as exc:  # pragma: no cover - defensive
                problems.append("query_only reset: {}".format(exc))

            # Cursors are normally closed by their worker before we get here.
            # Anything left belongs to this lease and is now safe to close.
            for cursor in list(self.cursors):
                try:
                    cursor.close()
                except Exception as exc:
                    problems.append("cursor close: {}".format(exc))
            self.cursors.clear()

            try:
                self._drop_temp_objects()
            except Exception as exc:
                problems.append("temp cleanup: {}".format(exc))

            # main is a private :memory: database, so anything a tenant created
            # there is also lease residue.
            try:
                self._drop_main_objects()
            except Exception as exc:
                problems.append("main cleanup: {}".format(exc))

            if self.fail_cleanup:
                self.last_cleanup_error = "injected cleanup failure"
                raise CleanupError(
                    "injected cleanup failure (connection left half-cleaned)",
                    problems or ["injected"],
                )

            try:
                self._detach_extra_databases()
            except Exception as exc:
                problems.append("detach: {}".format(exc))

            try:
                self._restore_defaults()
            except Exception as exc:
                problems.append("pragma restore: {}".format(exc))

            self.current_lease = None
            self.tenant = None
            self.path = None

            problems.extend(self._verify_clean(expect_bound=False))
            if problems:
                self.last_cleanup_error = "; ".join(problems)
                raise CleanupError("session cleanup failed", problems)

            self.cleanup_count += 1
            self.last_cleanup_error = None

    # Keep the historical name available: it now means "rollback pending
    # transaction", not "the whole return protocol".
    def reset(self):
        with self.lock:
            self._rollback_all()

    def _rollback_all(self):
        for _ in range(16):
            if not self.db.in_transaction:
                return
            try:
                self.db.execute("ROLLBACK")
            except sqlite3.OperationalError as exc:
                if "no transaction" in str(exc).lower():
                    return
                raise
        if self.db.in_transaction:
            raise CleanupError("could not end transaction")

    def _drop_temp_objects(self):
        self._drop_schema_objects("sqlite_temp_master", "temp")

    def _drop_main_objects(self):
        self._drop_schema_objects("main.sqlite_master", "main")

    def _drop_schema_objects(self, master, schema):
        order = {"trigger": 0, "view": 1, "index": 2, "table": 3}
        query = (
            "SELECT type, name FROM {} "
            "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'".format(master)
        )
        for _ in range(16):
            objects = self.db.execute(query).fetchall()
            if not objects:
                return
            objects.sort(key=lambda item: order.get(item[0], 9))
            for kind, name in objects:
                self.db.execute(
                    "DROP {} IF EXISTS {}.{}".format(
                        kind.upper(), schema, _quote_identifier(name)
                    )
                )
        remaining = self.db.execute(query).fetchall()
        if remaining:
            raise CleanupError(
                "{} objects survived cleanup: {}".format(schema, remaining)
            )

    def _detach_extra_databases(self):
        for _ in range(16):
            names = [
                name
                for _seq, name, _file in self.db.execute(
                    "PRAGMA database_list"
                ).fetchall()
                if name not in ("main", "temp")
            ]
            if not names:
                return
            for name in names:
                self.db.execute("DETACH DATABASE {}".format(_quote_identifier(name)))
        names = [
            name
            for _seq, name, _file in self.db.execute("PRAGMA database_list").fetchall()
            if name not in ("main", "temp")
        ]
        if names:
            raise CleanupError("databases survived detach: {}".format(names))

    def _restore_defaults(self):
        # Restore query_only last: all cleanup work above may need writes.
        keys = sorted(self.defaults, key=lambda key: key == "query_only")
        for key in keys:
            self._set_pragma(key, self.defaults[key])

    def _verify_clean(self, expect_bound):
        problems = []
        try:
            if self.db.in_transaction:
                problems.append("transaction still open")
        except Exception as exc:  # pragma: no cover
            problems.append("transaction check: {}".format(exc))

        try:
            temp_objects = self.db.execute(
                "SELECT type, name FROM sqlite_temp_master "
                "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
            ).fetchall()
            if temp_objects:
                problems.append("TEMP objects remain: {}".format(temp_objects))
        except Exception as exc:  # pragma: no cover
            problems.append("temp check: {}".format(exc))

        try:
            main_objects = self.db.execute(
                "SELECT type, name FROM main.sqlite_master "
                "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
            ).fetchall()
            if main_objects:
                problems.append("main objects remain: {}".format(main_objects))
        except Exception as exc:  # pragma: no cover
            problems.append("main check: {}".format(exc))

        try:
            names = [
                name
                for _seq, name, _file in self.db.execute(
                    "PRAGMA database_list"
                ).fetchall()
            ]
            expected = {"main", "temp"}
            if expect_bound:
                expected.add("tenant")
            if set(names) != expected or len(names) != len(expected):
                problems.append("unexpected databases: {}".format(names))
        except Exception as exc:  # pragma: no cover
            problems.append("database check: {}".format(exc))

        for key, expected in self.defaults.items():
            try:
                actual = self._read_pragma(key)
            except Exception as exc:  # pragma: no cover
                problems.append("pragma {}: {}".format(key, exc))
                continue
            if actual != expected:
                problems.append(
                    "pragma {} is {} (expected {})".format(key, actual, expected)
                )
        return problems

    # ----------------------------------------------------------- observability

    def snapshot(self):
        """Best-effort observable state; never raises for a live connection."""

        with self.lock:
            data = {
                "alive": self.alive,
                "tenant": self.tenant,
                "path": self.path,
                "generation": self.generation,
                "cleanup_count": self.cleanup_count,
                "last_cleanup_error": self.last_cleanup_error,
                "active_cursors": len(self.cursors),
                "active_workers": self.worker_count(),
                "current_lease_serial": (
                    None
                    if self.current_lease is None
                    else self.current_lease.serial
                ),
            }
            try:
                data["in_transaction"] = self.db.in_transaction
                data["temp_objects"] = [
                    [kind, name]
                    for kind, name in self.db.execute(
                        "SELECT type, name FROM sqlite_temp_master "
                        "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
                    ).fetchall()
                ]
                data["databases"] = [
                    name
                    for _seq, name, _file in self.db.execute(
                        "PRAGMA database_list"
                    ).fetchall()
                ]
                data["pragmas"] = {
                    key: self._read_pragma(key) for key in self.defaults
                }
                data["snapshot_error"] = None
            except Exception as exc:
                data["snapshot_error"] = str(exc)
            return data

    # -------------------------------------------------------------------- close

    def interrupt(self):
        """Abort the currently executing SQLite statement, if any."""

        try:
            self.db.interrupt()
        except Exception:
            pass

    def close(self):
        with self.lock:
            if not self.alive:
                return
            self.alive = False
            self.current_lease = None
            self.tenant = None
            self.path = None
            for cursor in list(self.cursors):
                try:
                    cursor.close()
                except Exception:
                    pass
            self.cursors.clear()
            try:
                self.db.close()
            except Exception:
                pass
