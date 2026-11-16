"""Query execution bound to a lease.

A :class:`Query` owns one worker thread and one SQLite cursor at a time.  Its
lifetime is tied to a :class:`~pool.Lease`:

* :meth:`Query.start` is rejected once the lease is closing or returned.
* :meth:`Query.cancel` only interrupts the lease that started the query.  A
  late cancel after the connection was returned is a no-op, so it cannot
  disturb the next tenant.
* :meth:`Query.return_connection` closes the lease, aborts this query, waits
  for the worker, then cleans/recycles (or evicts) the connection.

No pool-wide lock is held while a worker is running or while waiting for one.
"""

from __future__ import annotations

import threading

from session import QUERY_ABORTED, LeaseClosedError, QueryAborted

QUERY_PENDING = "pending"
QUERY_RUNNING = "running"
QUERY_DONE = "done"
QUERY_ABORTED_STATE = "aborted"
QUERY_FAILED = "failed"


class Query:
    """A cancellable, row-at-a-time query executed on a leased session."""

    def __init__(self, pool, lease, sql, params=()):
        self.pool = pool
        self.lease = lease
        self.sql = sql
        self.params = params

        self.rows = []
        self.error = None
        self.state = QUERY_PENDING
        self.done = threading.Event()
        self.aborted = threading.Event()
        self.started = False
        self.worker = None
        self._lock = threading.Lock()
        self._cursor_open = threading.Event()  # set while a cursor is live

    # ---------------------------------------------------------------- lifecycle

    def start(self, pause=None, gate=None):
        """Start the worker.

        ``pause`` is an event on which the worker blocks after each row (used
        to model a consumer that stops consuming).  ``gate`` is called with
        the query after each produced row; it may raise to stop early.

        Admission and thread start happen under the lease lock, so a concurrent
        ``release`` either sees no worker (and rejects this query) or sees a
        started worker it can wait for.  It can never clean the connection out
        from under a thread that starts afterwards.
        """

        with self._lock:
            if self.started:
                raise RuntimeError("query already started")
            self.started = True

        def run():
            try:
                self.lease.assert_open()
                for row in self._iterate(pause=pause):
                    with self._lock:
                        self.rows.append(row)
                    if gate is not None:
                        gate(self)
                with self._lock:
                    if self.state == QUERY_RUNNING:
                        self.state = QUERY_DONE
            except QueryAborted as exc:
                self.error = str(exc)
                self.pool.note_aborted_worker()
                with self._lock:
                    self.state = QUERY_ABORTED_STATE
            except Exception as exc:
                self.error = "{}: {}".format(type(exc).__name__, exc)
                with self._lock:
                    self.state = QUERY_FAILED
            finally:
                self.lease.unregister_query(self)
                self.done.set()
                self.lease.finish_worker(threading.current_thread())

        # Atomically with respect to Lease.begin_return(): register the query
        # and the worker, then start the thread while still holding the lock.
        # begin_return() also takes this lock, so it cannot slip in between.
        with self.lease._lock:
            if not self.lease._can_start_worker_locked():
                self.lease.unregister_query(self)
                with self._lock:
                    self.state = QUERY_ABORTED_STATE
                self.done.set()
                raise LeaseClosedError(
                    "lease {} ({}) is no longer active".format(
                        self.lease.serial, self.lease.tenant
                    )
                )
            self.lease.register_query(self)
            with self._lock:
                self.state = QUERY_RUNNING
            self.worker = threading.Thread(
                target=run, name="query-{}".format(self.lease.serial), daemon=True
            )
            self.lease.register_worker(self.worker)
            try:
                self.worker.start()
            except BaseException:
                self.lease.finish_worker(self.worker)
                self.lease.unregister_query(self)
                with self._lock:
                    self.state = QUERY_FAILED
                self.done.set()
                raise
        return self

    def _iterate(self, pause=None):
        self._cursor_open.set()
        try:
            # session.rows checks lease ownership when the cursor opens.
            for row in self.lease.session.rows(
                self.sql, self.params, lease=self.lease, abort=self.aborted
            ):
                yield row
                if pause is not None:
                    # Match the original API: an unset ``pause`` event means
                    # "the consumer stopped consuming".  We keep polling both
                    # abort flags so either a query cancel or a lease return
                    # unblocks a paused worker without the consumer.
                    while (
                        not pause.is_set()
                        and not self.aborted.is_set()
                        and not self.lease.abort.is_set()
                    ):
                        pause.wait(0.01)
                    if self.aborted.is_set() or self.lease.abort.is_set():
                        raise QueryAborted(QUERY_ABORTED)
        finally:
            self._cursor_open.clear()

    # -------------------------------------------------------------- operations

    def cancel(self):
        """Interrupt this query's own lease.

        Returns False when the lease has already been returned, which is the
        key property for late cancels: they cannot reach a new tenant.
        """

        with self._lock:
            if self.state in (QUERY_DONE, QUERY_ABORTED_STATE, QUERY_FAILED):
                return False
            self.aborted.set()
        delivered = self.lease.cancel()
        return delivered

    def return_connection(self, timeout=None):
        """Return the lease, aborting this query first.

        Safe to call from a different thread than the worker, and idempotent:
        a second call after the lease was returned is a no-op.
        """

        self.aborted.set()
        return self.pool.release(
            self.lease,
            releaser="query.return_connection",
            worker_timeout=timeout,
        )

    def cancel_and_return(self, timeout=None):
        self.cancel()
        return self.return_connection(timeout=timeout)

    def wait(self, timeout=None):
        return self.done.wait(timeout)

    def status(self):
        with self._lock:
            return {
                "sql": self.sql,
                "state": self.state,
                "rows": len(self.rows),
                "error": self.error,
                "started": self.started,
                "done": self.done.is_set(),
                "aborted": self.aborted.is_set(),
                "cursor_open": self._cursor_open.is_set(),
                "worker_alive": bool(self.worker and self.worker.is_alive()),
                "lease": self.lease.serial,
                "lease_state": self.lease.state,
            }
