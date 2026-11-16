"""Threaded keyed operator runtime with savepoint-based re-partitioning.

Architecture
------------
* 16 fixed key-groups derived from a stable sha256 mapping.  Real worker
  threads (1..4) own groups by ``owner = group % worker_count``.
* Each worker is a real thread with a queue; it mutates only the GroupState
  objects of the groups it owns and never touches another worker's groups.
* Results are committed through a generation (epoch) fence *after* processing.
  The commit point can be held with an :class:`threading.Event`, which lets a
  test park an already-processed result in flight; after a rescale such a
  stale-epoch result is rejected and cannot alter the new owner's progress.
* Rescaling pauses global input, reaches an explicit safe boundary signalled
  by a condition variable (never ``sleep``), snapshots every group into a
  staged savepoint and only then swaps the worker generation.  Old threads are
  joined before new threads start, so two threads never process one group.
"""

import hashlib
import threading
import queue
from contextlib import contextmanager

from state import GroupState, DuplicateTimerError
from savepoint import (
    KEY_GROUP_COUNT,
    save as sp_save,
    stage as sp_stage,
    commit as sp_commit,
    restore as sp_restore,
    load as sp_load,
)


def key_group(key):
    """Stable sha256 mapping of a key onto one of the 16 fixed groups."""
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big') \
        % KEY_GROUP_COUNT


class ResultStore:
    """Re-readable result storage keyed by timer_id.

    The output list can no longer be wiped to hide a duplicate: every
    committed output is indexed by its timer identity and restored verbatim
    from the savepoint.  ``outputs()`` returns the canonical event-time order
    ``(due, timer_id)``; ``all_outputs()`` preserves arrival order.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._by_id = {}
        self._order = []

    def add(self, result):
        """Commit one result. Returns False if the identity is already stored."""
        tid = result['timer_id']
        with self._lock:
            if tid in self._by_id:
                return False
            self._by_id[tid] = dict(result)
            self._order.append(tid)
            return True

    def snapshot(self):
        with self._lock:
            return {tid: dict(r) for tid, r in self._by_id.items()}

    def contains(self, timer_id):
        with self._lock:
            return timer_id in self._by_id

    def outputs(self):
        with self._lock:
            return sorted(self._by_id.values(),
                          key=lambda r: (r['due'], r['timer_id']))

    def all_outputs(self):
        with self._lock:
            return [dict(self._by_id[tid]) for tid in self._order]

    def __len__(self):
        with self._lock:
            return len(self._by_id)


class _Inflight:
    """Condition-variable counters defining the migration safe boundaries.

    Three phases are distinguished per work item, all under one condition
    variable:

    * ``processing`` - the worker is actively mutating GroupState;
    * ``parked``     - processing is done and the item is waiting at the
                       commit Event (the gate is held);
    * ``committing`` - the gate was open and the item is committing results.

    A fence handoff waits on ``wait_processing_idle`` (no state mutation in
    progress; parked items are inert) and then on ``wait_idle`` after the
    controlled gate release.  A drain handoff keeps the gate open so no item
    ever parks and waits on ``wait_idle`` directly.  Every wait is a condition
    wait on real completion events - no timed sleep anywhere.
    """

    def __init__(self):
        self._cv = threading.Condition()
        self._processing = 0
        self._parked = 0
        self._committing = 0

    @contextmanager
    def track_processing(self):
        with self._cv:
            self._processing += 1
        try:
            yield
        finally:
            with self._cv:
                self._processing -= 1
                self._cv.notify_all()

    def enter_parked(self):
        with self._cv:
            self._parked += 1
            self._cv.notify_all()

    def exit_parked(self):
        with self._cv:
            self._parked -= 1
            self._cv.notify_all()

    @contextmanager
    def track_committing(self):
        with self._cv:
            self._committing += 1
        try:
            yield
        finally:
            with self._cv:
                self._committing -= 1
                self._cv.notify_all()

    def wait_processing_idle(self):
        """Fence boundary: no state mutation in progress."""
        with self._cv:
            while self._processing:
                self._cv.wait()

    def wait_parked(self, at_least=1):
        """Wait until at least ``at_least`` items are held at the commit gate.

        Bounded by a real thread reaching the Event, never a timed sleep.
        """
        with self._cv:
            while self._parked < at_least:
                self._cv.wait()

    def wait_idle(self):
        """Drain boundary: nothing processing, parked or committing."""
        with self._cv:
            while self._processing or self._parked or self._committing:
                self._cv.wait()

    @property
    def count(self):
        with self._cv:
            return self._processing + self._parked + self._committing

    @property
    def processing(self):
        with self._cv:
            return self._processing

    @property
    def parked(self):
        with self._cv:
            return self._parked


class Worker:
    def __init__(self, runtime, seq, epoch, states):
        self.runtime = runtime
        self.seq = seq
        self.epoch = epoch
        self.states = {st.group: st for st in states}
        self.q = queue.Queue()
        self.thread = threading.Thread(
            target=self.run, name="operator-worker-%d-epoch%d" % (seq, epoch),
            daemon=True)
        self.thread.start()

    def run(self):
        rt = self.runtime
        while True:
            item = self.q.get()
            try:
                if item is None:
                    return
                op, args = item
                # The epoch captured at dequeue time travels with the results;
                # a later generation bump makes every commit here stale.
                epoch = self.epoch
                pending = []  # results processed but not yet committed
                with rt.inflight.track_processing():
                    if op == 'add':
                        key, delta = args
                        self.states[key_group(key)].add(key, delta)
                    elif op == 'timer':
                        timer_id, key, due = args
                        gid = key_group(key)
                        try:
                            pending = self.states[gid].timer(
                                timer_id, key, due)
                        except DuplicateTimerError as exc:
                            rt.note_error(exc)
                    elif op == 'watermark':
                        (time,) = args
                        for st in self.states.values():
                            pending.extend(st.advance(time))
                # ---- Event control point ------------------------------
                # Processing (state mutation) is done; the results have not
                # been submitted yet.  A held gate parks the item here so a
                # fence rescale can make its epoch stale; a drain rescale
                # keeps the gate set and passes through without waiting on
                # anything external.
                rt.inflight.enter_parked()
                try:
                    rt.commit_gate.wait()
                finally:
                    rt.inflight.exit_parked()
                with rt.inflight.track_committing():
                    for r in pending:
                        rt.commit(r, epoch)
            finally:
                self.q.task_done()

    def close(self):
        self.q.put(None)
        self.thread.join()


class Runtime:
    def __init__(self, count):
        if not 1 <= count <= 4:
            raise ValueError("worker count must be in 1..4, got %d" % count)
        self._init_common()
        all_groups = [GroupState(gid) for gid in range(KEY_GROUP_COUNT)]
        self.epoch = 0
        self.workers = self._start_generation(0, [
            [all_groups[gid] for gid in range(KEY_GROUP_COUNT)
             if gid % count == w]
            for w in range(count)
        ])

    def _init_common(self):
        self.store = ResultStore()
        self.inflight = _Inflight()
        # Set = open: parked items may complete. Cleared = hold the control
        # point between processing and result commit.
        self.commit_gate = threading.Event()
        self.commit_gate.set()
        self._input_cv = threading.Condition()
        self._paused = False
        self._lock = threading.Lock()
        self._worker_seq = 0
        self.errors = []
        self.rejections = []
        # Convergence audit trail of every retired worker generation.
        self.retired = []

    # ------------------------------------------------------------- lifecycle
    def _start_generation(self, epoch, partitioned_states):
        workers = []
        for states in partitioned_states:
            self._worker_seq += 1
            workers.append(Worker(self, self._worker_seq, epoch, states))
        return workers

    @classmethod
    def from_savepoint(cls, path, count):
        """Reconstruct a live runtime from a complete committed savepoint."""
        partitioned, results, _dist = sp_restore(path, count)
        epoch, _groups, _results = sp_load(path)
        self = cls.__new__(cls)
        self._init_common()
        for tid, record in results.items():
            self.store.add(record)
        self.epoch = epoch
        self.workers = self._start_generation(epoch, partitioned)
        return self

    def close(self):
        for w in self.workers:
            w.close()

    # --------------------------------------------------------------- routing
    def _route(self, op, args):
        if op == 'watermark':
            raise ValueError("watermark is fan-out; call Runtime.watermark()")
        key = args[0] if op == 'add' else args[1]
        return key_group(key) % len(self.workers), key

    def submit(self, op, args):
        # Global input pause: external items block at the boundary, no item is
        # taken in while a migration is in progress.  Operator-internal control
        # traffic (watermark fan-out) uses enqueue_watermark and bypasses this.
        with self._input_cv:
            while self._paused:
                self._input_cv.wait()
        owner, _key = self._route(op, args)
        self.workers[owner].q.put((op, args))

    def watermark(self, time):
        with self._input_cv:
            while self._paused:
                self._input_cv.wait()
        self.enqueue_watermark(time)
        self.drain()

    def enqueue_watermark(self, time):
        """Fan out a watermark to every worker without waiting for completion.

        Watermark is operator-internal control traffic and therefore does not
        block on the external-input pause.  Needed when a held commit Event
        must park the resulting outputs at the control point.
        """
        for w in self.workers:
            w.q.put(('watermark', (time,)))

    def drain(self):
        """Wait for all accepted work to finish.

        Refuses to block while the commit gate is held: doing so would wait on
        an external Event instead of on work completion.  Callers performing a
        drain migration must keep the control point open (controlled release).
        """
        if not self.commit_gate.is_set():
            raise RuntimeError(
                "drain() cannot quiesce while commit_gate is held; "
                "release the gate or use mode='fence'")
        for w in self.workers:
            w.q.join()
        self.inflight.wait_idle()

    # ------------------------------------------------------- Event control
    def pause_input(self):
        with self._input_cv:
            self._paused = True

    def resume_input(self):
        with self._input_cv:
            self._paused = False
            self._input_cv.notify_all()

    def hold_commitments(self):
        """Insert the Event control point: processing continues, commit waits."""
        self.commit_gate.clear()

    def release_commitments(self):
        """Controlled release of every item parked at the commit Event."""
        self.commit_gate.set()

    # ---------------------------------------------------------------- commit
    def commit(self, result, epoch):
        """Generation-fenced, idempotent result commit."""
        with self._lock:
            if epoch != self.epoch:
                self.rejections.append({
                    'timer_id': result['timer_id'],
                    'group': result['group'],
                    'epoch': epoch,
                    'current_epoch': self.epoch,
                    'reason': 'stale epoch after rescale',
                    # Carry the full payload so the new generation can adopt
                    # the output with its original fire-time value, if needed.
                    'payload': dict(result),
                })
                return False
            return self.store.add(result)

    def resurrect(self, result):
        """Commit a result whose old-epoch attempt was fenced off.

        The new owner decides, after the handoff, that the fired timer's output
        is still part of the operator's committed history (it was never stored
        anywhere).  Used in fence mode so a handoff loses no timer output;
        drain mode never needs it because nothing is rejected.
        """
        with self._lock:
            return self.store.add(result)

    def note_error(self, exc):
        with self._lock:
            self.errors.append(repr(exc))

    @property
    def outputs(self):
        """Canonical output list, ordered by (due, timer_id)."""
        return self.store.outputs()

    def owner_distribution(self):
        return {gid: gid % len(self.workers)
                for gid in range(KEY_GROUP_COUNT)}

    def worker_states(self):
        """Flat ordered list of every live group state (for savepoints)."""
        return [w.states[gid]
                for gid in range(KEY_GROUP_COUNT)
                for w in self.workers if gid in w.states]

    # -------------------------------------------------------------- rescale
    def rescale(self, path, count, mode='drain'):
        """Migrate all 16 groups to ``count`` worker threads via savepoint.

        mode='drain' (default, equivalence-preserving):
            pause input -> quiesce at the inflight condition boundary with the
            commit gate open -> staged savepoint -> swap generations.
        mode='fence' (generation handoff):
            input is paused and the epoch is bumped while work may still be
            queued or parked at the held commit Event; the gate is then
            released under controlled release, the finite backlog is joined,
            stale-epoch commits are fenced off and adopted once by the new
            generation, and only after every old thread has joined do the new
            owners start.
        """
        if not 1 <= count <= 4:
            raise ValueError("worker count must be in 1..4, got %d" % count)
        if mode not in ('drain', 'fence'):
            raise ValueError("mode must be 'drain' or 'fence'")

        old_workers = self.workers
        self.pause_input()
        gate_was_set = self.commit_gate.is_set()
        try:
            if mode == 'drain':
                # The gate is open so every accepted item runs to its commit;
                # the condition returns exactly when the last item finishes.
                if not self.commit_gate.is_set():
                    raise RuntimeError(
                        "drain rescale requires an open commit_gate")
                self.inflight.wait_idle()
                new_epoch = self.epoch + 1
            else:
                # Fence: wait for state mutation to stop.  Input is paused so
                # the queue backlog is finite; any item not yet processed is
                # either still queued or already parked at the held gate.
                self.inflight.wait_processing_idle()
                new_epoch = self.epoch + 1
                with self._lock:
                    self.epoch = new_epoch

            if mode == 'fence':
                # Release the gate under the new epoch.  Every queued/parked
                # item then runs to completion: its commit fails the epoch
                # check, and the fire-time payload is retained in the
                # rejection record.  q.join + wait_idle define a finite
                # boundary (the backlog cannot grow while input is paused).
                prior_rejections = len(self.rejections)
                self.commit_gate.set()
                for w in old_workers:
                    w.q.join()
                self.inflight.wait_idle()
                for rec in self.rejections[prior_rejections:]:
                    self.resurrect(rec['payload'])

            # All old threads are quiescent but alive; no state is mutating.
            # Stage and validate the re-partition before publishing anything,
            # so a failure leaves the previous savepoint (and old runtime) up.
            tmp = sp_stage(self.worker_states(), self.store.snapshot(),
                           path, new_epoch)
            try:
                partitioned, _results, _dist = sp_restore(tmp, count)
                sp_commit(tmp, path)
            except BaseException:
                # tmp is an unpublished temp file; the old path is untouched.
                import os
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

            # Commit point published: retire the old generation, then start the
            # new one.  Joining before starting guarantees no two threads ever
            # process the same group.
            for w in old_workers:
                w.q.put(None)
            for w in old_workers:
                w.thread.join()
                self.retired.append({
                    'worker_seq': w.seq,
                    'thread_name': w.thread.name,
                    'epoch': w.epoch,
                    'groups': sorted(w.states),
                    'joined': True,
                })
            if mode == 'drain':
                with self._lock:
                    self.epoch = new_epoch
            self.workers = self._start_generation(new_epoch, partitioned)
            if mode == 'fence' and not gate_was_set:
                # Restore the caller's held control point only after the old
                # threads have all joined: re-clearing earlier would make a
                # queued item block at the gate and deadlock the join.
                self.commit_gate.clear()
        finally:
            self.resume_input()

    # -------------------------------------------------------------- evidence
    def evidence(self):
        return {
            'epoch': self.epoch,
            'worker_count': len(self.workers),
            'live_threads': [w.thread.name for w in self.workers],
            'live_thread_is_alive': [w.thread.is_alive()
                                     for w in self.workers],
            'distribution': self.owner_distribution(),
            'inflight': self.inflight.count,
            'committed_results': len(self.store),
            'rejected': list(self.rejections),
            'retired_workers': list(self.retired),
            'errors': list(self.errors),
        }
