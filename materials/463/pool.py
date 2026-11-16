"""Capacity-bounded SQLite connection pool with a full lease return protocol.

The pool keeps at most ``capacity`` *live physical* connections.  A connection
handed out by :meth:`Pool.acquire` is owned by exactly one
:class:`~pool.Lease` until :meth:`Pool.release` returns it.  Release is the
critical section of the protocol:

1. close the lease (no new queries may start),
2. abort in-flight work for that lease only,
3. wait for the lease's worker threads to unwind and close their cursors
   (without holding the pool lock),
4. clean the connection (``Session.cleanup``) and verify it,
5. put it back on the idle queue *only* if verification passed, otherwise
   close it and evict it.

A connection being built or retired counts against ``capacity`` until it is
actually closed/registered, so rebuilding can never grow the number of live
connections.  All public methods are safe to call from different threads; in
particular ``Query.cancel`` and ``Query.return_connection`` may race with query
workers and with each other.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from session import CleanupError, LeaseClosedError, Session


class PoolError(RuntimeError):
    """Base class for pool errors."""


class PoolClosedError(PoolError):
    """The pool has been closed."""


class PoolTimeout(PoolError):
    """No connection/lease slot became available before the deadline."""


LEASE_ACTIVE = "active"        # handed to the caller, may run queries
LEASE_RETURNING = "returning"  # release in progress, rejecting new work
LEASE_RETURNED = "returned"    # cleanup passed, connection back in the idle list
LEASE_EVICTED = "evicted"      # cleanup failed, connection closed and discarded


@dataclass
class Lease:
    """Ownership handle for one tenant's use of one physical connection."""

    serial: int
    tenant: str
    path: str
    session: Session
    generation: int
    state: str = LEASE_ACTIVE
    abort: threading.Event = field(default_factory=threading.Event)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _workers: set = field(default_factory=set)
    _worker_cond: Optional[threading.Condition] = field(default=None)
    _queries: set = field(default_factory=set)
    closed: bool = False
    releaser: Optional[str] = None
    cleanup_error: Optional[str] = None
    evicted: bool = False
    acquired_at: float = field(default_factory=time.monotonic)
    released_at: Optional[float] = None

    def __post_init__(self):
        self._worker_cond = threading.Condition(self._lock)

    # ------------------------------------------------------------------ state

    def is_owner(self):
        """True while this lease still owns its session and accepts work."""

        with self._lock:
            return self._can_start_worker_locked()

    def _can_start_worker_locked(self):
        return (
            not self.closed
            and self.state == LEASE_ACTIVE
            and self.session.current_lease is self
        )

    def assert_open(self):
        if not self.is_owner():
            raise LeaseClosedError(
                "lease {} ({}) is no longer active".format(self.serial, self.tenant)
            )

    # ---------------------------------------------------------------- queries

    def register_query(self, query):
        """Admit a query; atomically rejects work on a closing lease."""

        with self._lock:
            if not self._can_start_worker_locked():
                raise LeaseClosedError(
                    "lease {} ({}) is no longer active".format(
                        self.serial, self.tenant
                    )
                )
            self._queries.add(query)

    def unregister_query(self, query):
        with self._lock:
            self._queries.discard(query)

    def register_worker(self, worker):
        with self._lock:
            if not self._can_start_worker_locked():
                raise LeaseClosedError(
                    "lease {} ({}) is no longer active".format(
                        self.serial, self.tenant
                    )
                )
            self._workers.add(worker)
            self.session.add_worker(worker)

    def finish_worker(self, worker):
        with self._worker_cond:
            self._workers.discard(worker)
            self.session.remove_worker(worker)
            self._worker_cond.notify_all()

    def worker_count(self):
        with self._lock:
            return sum(1 for worker in self._workers if worker.is_alive())

    # ----------------------------------------------------------- cancellation

    def cancel(self):
        """Abort this lease's in-flight work.

        Returns ``True`` if an interrupt was delivered.  Once the lease has
        been closed (its connection returned to the pool) this is a no-op, so
        a late ``cancel`` can never reach the next tenant's work.
        """

        with self._lock:
            if not self._can_start_worker_locked():
                return False
            self.abort.set()
            active = any(worker.is_alive() for worker in self._workers)
            if active:
                self.session.interrupt()
            return True

    # ---------------------------------------------------------------- release

    def begin_return(self, releaser=None):
        """Close the lease to new work; returns False if it was already closed."""

        with self._lock:
            if self.closed:
                return False
            self.closed = True
            self.state = LEASE_RETURNING
            self.releaser = releaser
            self.abort.set()
            self.session.interrupt()
            return True

    def wait_for_workers(self, timeout=None):
        """Wait for this lease's query threads to exit.

        Returns ``True`` when no live worker remains.  Never called while the
        pool lock is held.
        """

        deadline = None if timeout is None else time.monotonic() + timeout
        with self._worker_cond:
            while True:
                live = [w for w in self._workers if w.is_alive()]
                if not live:
                    return True
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                self._worker_cond.wait(remaining)

    def snapshot(self):
        with self._lock:
            session = self.session
            return {
                "serial": self.serial,
                "tenant": self.tenant,
                "path": self.path,
                "generation": self.generation,
                "state": self.state,
                "closed": self.closed,
                "releaser": self.releaser,
                "evicted": self.evicted,
                "cleanup_error": self.cleanup_error,
                "aborted": self.abort.is_set(),
                "active_workers": session.worker_count() if session.alive else 0,
                "active_cursors": len(session.cursors) if session.alive else 0,
                "elapsed": (
                    (self.released_at or time.monotonic()) - self.acquired_at
                ),
            }


class Pool:
    """Bounded pool of real SQLite sessions with deterministic return."""

    def __init__(self, size=2, *, config=None, max_leases=8,
                 worker_timeout=5.0):
        if not isinstance(size, int) or size < 1:
            raise ValueError("size must be a positive integer")
        if max_leases < 1:
            raise ValueError("max_leases must be positive")

        self.size = size
        self.capacity = size
        self.max_leases = max_leases
        self.config = config
        self.worker_timeout = worker_timeout

        self._guard = threading.Lock()
        self._cond = threading.Condition(self._guard)
        self._idle = []                  # ready-to-use sessions
        self._connections = set()        # every live physical session
        self._retiring = set()           # being closed, still counts as capacity
        self._pending = 0                # connections being built (capacity reserved)
        self._active = set()             # sessions currently leased
        self._leases = {}                # serial -> Lease (for observability)
        self._serial = 0
        self._closed = False
        self._lease_slots = 0            # admitted leases (active or returning)

        self.stats = {
            "created": 0,
            "reused": 0,
            "evicted": 0,
            "cleanups": 0,
            "failed_cleanups": 0,
            "aborted_workers": 0,
            "acquire_timeouts": 0,
            "waited": 0,
        }

        for _ in range(size):
            session = self._new_session()
            self._connections.add(session)
            self._idle.append(session)

    # ------------------------------------------------------------- internals

    def _new_session(self):
        session = Session(self.config)
        with self._guard:
            self.stats["created"] += 1
        return session

    def _untrack_locked(self, session):
        """Mark a connection as retiring (pool lock held).

        It leaves ``_connections``/``_active`` but stays in ``_retiring`` until
        it has actually been closed, so a concurrent ``acquire`` cannot build a
        replacement while the old handle is still open.  Callers must follow
        with :meth:`_finish_retire`.
        """

        self._connections.discard(session)
        self._active.discard(session)
        self._retiring.add(session)
        self.stats["evicted"] += 1
        self._cond.notify_all()

    def _finish_retire(self, session):
        self._close_quietly(session)
        with self._cond:
            self._retiring.discard(session)
            self._cond.notify_all()

    def _retire(self, session):
        with self._cond:
            self._untrack_locked(session)
        self._finish_retire(session)

    def note_aborted_worker(self):
        """Record that a query worker unwound due to an abort/interrupt."""

        with self._guard:
            self.stats["aborted_workers"] += 1

    def _close_quietly(self, session):
        try:
            session.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- acquire

    def acquire(self, tenant, path, timeout=1.0):
        """Lease a cleaned connection bound to ``tenant`` at ``path``.

        Raises :class:`PoolClosedError`, :class:`PoolTimeout`, or a bind/
        cleanup error.  On a bind failure the connection is destroyed rather
        than returned half-bound.
        """

        deadline = time.monotonic() + timeout

        with self._cond:
            while True:
                if self._closed:
                    raise PoolClosedError("pool is closed")
                if self._lease_slots < self.max_leases:
                    self._lease_slots += 1
                    break
                self.stats["waited"] += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.stats["acquire_timeouts"] += 1
                    raise PoolTimeout("no lease slot available")
                self._cond.wait(remaining)

        try:
            session, built = self._take_session(deadline)
        except BaseException:
            self._drop_slot()
            raise

        try:
            generation = session.prepare(tenant, path)
        except BaseException:
            # Never return a session whose bind failed.
            with self._cond:
                self._untrack_locked(session)
                if built:
                    self._pending = max(0, self._pending - 1)
                self._cond.notify_all()
            self._finish_retire(session)
            self._drop_slot()
            raise

        with self._cond:
            if self._closed:
                self._untrack_locked(session)
                if built:
                    self._pending = max(0, self._pending - 1)
                if self._lease_slots > 0:
                    self._lease_slots -= 1
                self._cond.notify_all()
                retire = True
            else:
                retire = False
        if retire:
            self._finish_retire(session)
            raise PoolClosedError("pool is closed")
        with self._cond:
            self._serial += 1
            serial = self._serial
            self._active.add(session)
            if built:
                self._pending = max(0, self._pending - 1)
            self.stats["reused"] += 1
            self._cond.notify_all()

        lease = Lease(
            serial=serial,
            tenant=tenant,
            path=str(path),
            session=session,
            generation=generation,
        )
        session.adopt(lease)

        with self._cond:
            if self._closed:
                self._untrack_locked(session)
                self._release_slot_locked(lease)
                retire = True
            else:
                self._leases[serial] = lease
                retire = False
        if retire:
            self._finish_retire(session)
            raise PoolClosedError("pool is closed")
        return lease

    def _take_session(self, deadline):
        """Return ``(session, built)``; build outside the pool lock if needed."""

        with self._cond:
            while True:
                if self._closed:
                    raise PoolClosedError("pool is closed")
                if self._idle:
                    return self._idle.pop(), False
                in_flight = (
                    len(self._connections) + len(self._retiring) + self._pending
                )
                if in_flight < self.capacity:
                    # Reserve a capacity slot before releasing the lock so
                    # concurrent acquires cannot overshoot the cap.
                    self._pending += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.stats["acquire_timeouts"] += 1
                    raise PoolTimeout("no connection available")
                self.stats["waited"] += 1
                self._cond.wait(remaining)

        # Build outside the pool lock: opening a real connection must not
        # block unrelated acquire/release calls.
        try:
            session = self._new_session()
        except BaseException:
            with self._cond:
                self._pending = max(0, self._pending - 1)
                self._cond.notify_all()
            raise
        with self._cond:
            if self._closed:
                self._pending = max(0, self._pending - 1)
                self._cond.notify_all()
                self._close_quietly(session)
                raise PoolClosedError("pool is closed")
            self._connections.add(session)
        return session, True

    def _drop_slot(self):
        with self._cond:
            if self._lease_slots > 0:
                self._lease_slots -= 1
            self._cond.notify_all()

    # ---------------------------------------------------------------- release

    def release(self, lease, *, releaser=None, worker_timeout=None):
        """Return ``lease`` using the full protocol.

        Steps 1-2 happen under the lease lock; step 3 waits for workers without
        the pool lock; steps 4-5 run without the pool lock.  A failed cleanup
        evicts (closes) the connection instead of recycling it.
        """

        if not lease.begin_return(releaser=releaser):
            return False  # already returned; idempotent

        timeout = self.worker_timeout if worker_timeout is None else worker_timeout
        finished = lease.wait_for_workers(timeout=timeout)
        session = lease.session

        if not finished:
            # A worker refused to unwind.  Do not recycle a connection with
            # live work attached to it.
            return self._finish_evicted(
                lease, "worker did not stop before release", close=True
            )

        try:
            session.cleanup()
        except CleanupError as exc:
            return self._finish_evicted(lease, str(exc), close=True)
        except Exception as exc:  # defensive: any doubt means evict
            return self._finish_evicted(
                lease, "unexpected cleanup error: {}".format(exc), close=True
            )

        lease.released_at = time.monotonic()
        with self._guard:
            if self._closed:
                self._untrack_locked(session)
                lease.state = LEASE_EVICTED
                lease.evicted = True
                self._release_slot_locked(lease)
                close_now = True
            else:
                lease.state = LEASE_RETURNED
                self.stats["cleanups"] += 1
                self._active.discard(session)
                self._idle.append(session)
                self._release_slot_locked(lease)
                close_now = False
        if close_now:
            self._finish_retire(session)
        return True

    def _finish_evicted(self, lease, reason, close=True):
        lease.cleanup_error = reason
        lease.evicted = True
        lease.released_at = time.monotonic()
        tracked = False
        with self._guard:
            lease.state = LEASE_EVICTED
            self.stats["failed_cleanups"] += 1
            self._release_slot_locked(lease)
            if lease.session in self._connections:
                self._untrack_locked(lease.session)
                tracked = True
        if tracked:
            self._finish_retire(lease.session)
        elif close:
            self._close_quietly(lease.session)
        return True

    def _release_slot_locked(self, lease):
        self._leases.pop(lease.serial, None)
        if self._lease_slots > 0:
            self._lease_slots -= 1
        self._cond.notify_all()

    # ------------------------------------------------------------ observability

    def snapshot(self):
        """Structured, deadlock-free view of the pool."""

        with self._guard:
            leases = list(self._leases.values())
            data = {
                "capacity": self.capacity,
                "total_connections": len(self._connections),
                "pending_connections": self._pending,
                "retiring_connections": len(self._retiring),
                "idle_connections": len(self._idle),
                "active_connections": len(self._active),
                "active_leases": len(leases),
                "lease_slots_in_use": self._lease_slots,
                "max_leases": self.max_leases,
                "closed": self._closed,
                "stats": dict(self.stats),
                "leases": {},
            }
        for lease in leases:
            data["leases"][lease.serial] = lease.snapshot()
        return data

    def lease_status(self, lease):
        return lease.snapshot()

    def active_connections(self):
        with self._guard:
            return len(self._active)

    def idle_connections(self):
        with self._guard:
            return len(self._idle)

    def total_connections(self):
        with self._guard:
            return len(self._connections)

    # -------------------------------------------------------------------- close

    def close(self):
        """Close the pool.

        Idle connections are closed immediately.  Leased connections are
        interrupted so their workers unwind, then closed when they are
        released (release sees ``_closed`` and evicts).  Connections being
        built are closed as soon as they register.
        """

        with self._guard:
            if self._closed:
                return
            self._closed = True
            idle = list(self._idle)
            self._idle.clear()
            for session in idle:
                self._connections.discard(session)
            active = list(self._active)
            self._cond.notify_all()

        for session in idle:
            self._close_quietly(session)
        for session in active:
            try:
                session.interrupt()
            except Exception:
                pass
