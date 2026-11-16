"""Per-key-group operator state.

A ``GroupState`` instance owns *one* fixed key-group.  Every piece of state
that must survive a savepoint lives here:

* ``counts``    - arbitrary precision (Python int) keyed counters;
* ``timers``    - not-yet-fired timers, keyed by globally unique timer_id;
* ``watermark`` - monotonic integer event-time watermark seen by this group;
* ``completed`` - identity (and payload) of every timer that already fired,
                  so a restored runtime can never re-emit an output.

There is deliberately no thread-local timer facility: a timer is ordinary keyed
state and migrates together with its group when the parallelism changes.
"""


class DuplicateTimerError(ValueError):
    """Raised when a timer_id is reused with a different (key, due) payload."""


class GroupState:
    def __init__(self, group):
        self.group = group
        self.counts = {}
        # timer_id -> (key, due); a timer references the group through its key
        self.timers = {}
        self.watermark = None
        # timer_id -> (key, due): completed output identities with their
        # payload, which also lets us reject conflicting re-registration
        self.completed = {}

    # ------------------------------------------------------------------ counts
    def add(self, key, delta):
        # Python ints are arbitrary precision, so negative deltas and large
        # values behave like mathematical integers.
        self.counts[key] = self.counts.get(key, 0) + delta

    # ------------------------------------------------------------------ timers
    def timer(self, timer_id, key, due):
        """Register a globally unique timer.

        Same-id same-payload re-registration is an idempotent no-op (a retried
        registration must not fire twice).  Same-id different-payload raises.
        A timer whose due is already at or below the current watermark fires
        immediately, at the end of *this* registration call - it does not wait
        for a future watermark.
        """
        existing = self.timers.get(timer_id)
        if existing is not None:
            if existing != (key, due):
                raise DuplicateTimerError(
                    "timer_id %r already registered with payload %r"
                    % (timer_id, existing))
            return []
        done = self.completed.get(timer_id)
        if done is not None:
            if done != (key, due):
                raise DuplicateTimerError(
                    "timer_id %r already completed with payload %r"
                    % (timer_id, done))
            return []
        self.timers[timer_id] = (key, due)
        if self.watermark is not None and due <= self.watermark:
            return self._fire_due()
        return []

    def advance(self, watermark):
        """Advance the monotonic watermark and fire timers that become due."""
        if self.watermark is not None and watermark <= self.watermark:
            return []  # watermark is monotonic; nothing new can become due
        self.watermark = watermark
        return self._fire_due()

    def _fire_due(self):
        """Fire every due timer, ordered by (due, timer_id)."""
        due_ids = [tid for tid, (_, due) in self.timers.items()
                   if self.watermark is not None and due <= self.watermark]
        if not due_ids:
            return []
        # Same due is broken by timer_id; sorting (due, tid) gives one total
        # order that matches the specification.
        due_ids.sort(key=lambda tid: (self.timers[tid][1], tid))
        out = []
        for tid in due_ids:
            key, due = self.timers.pop(tid)
            self.completed[tid] = (key, due)
            out.append({
                'timer_id': tid,
                'key': key,
                'due': due,
                'value': self.counts.get(key, 0),
                'group': self.group,
            })
        return out

    # ------------------------------------------------------------- snapshots
    def snapshot(self):
        """Pure-data snapshot used by savepoints (deep, mutation-safe)."""
        return {
            'group': self.group,
            'counts': dict(self.counts),
            'timers': {tid: list(payload)
                       for tid, payload in self.timers.items()},
            'watermark': self.watermark,
            'completed': {tid: list(payload)
                          for tid, payload in self.completed.items()},
        }

    @classmethod
    def from_snapshot(cls, snap):
        st = cls(snap['group'])
        st.counts = dict(snap['counts'])
        st.timers = {tid: tuple(payload)
                     for tid, payload in snap['timers'].items()}
        st.watermark = snap['watermark']
        st.completed = {tid: tuple(payload)
                        for tid, payload in snap['completed'].items()}
        return st
