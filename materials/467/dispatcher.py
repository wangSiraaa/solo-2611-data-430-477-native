"""Multi-domain atomic admission dispatcher.

A request names a tuple of *distinct* domains and an integer cost.  It may be
sent only when **every** domain simultaneously has enough tokens, a free
concurrency slot, and is not frozen.  The dispatcher never takes a token from
one domain and then waits on another: admission happens inside
:class:`limits.QuotaLedger` as one atomic step.

Scheduling rules
----------------
* Requests are grouped into priority queues; inside one priority they are
  considered in submission order.
* If a request cannot be admitted, its domains become *reserved* for the rest
  of that priority pass.  A later request that intersects a reserved domain
  is not allowed to jump it, while a request whose domains are disjoint is
  still admitted.  This is the "temporarily infeasible request must not block
  completely disjoint requests" rule, and at the same time it is the
  reservation that prevents starvation: the blocked request's domains are
  only refilled, never consumed by a later overlapping request.

No-starvation condition (same priority): every request has ``cost <=
capacity`` in each of its domains, every referenced domain has
``max_active >= 1``, admitted requests eventually settle, and the caller
keeps advancing the monotonic clock.  Under those conditions the head
blocked request's domains keep refilling and its overlapping successors are
held back, so it becomes feasible after a finite number of milliseconds.
Higher-priority queues may of course keep a lower-priority request waiting;
that is the meaning of priority.  Requests whose domains are disjoint from
the blocked head can always make progress.

Token accounting
----------------
Tokens are charged once, atomically, at send time and are never refunded.
Settling a request (the *single* terminal transition) releases its
concurrency slot exactly once.  Queued cancellation touches no token because
no ticket exists yet; cancelling after send is a race with a normal response
and whichever terminal transition wins, the ticket is settled once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Dict, List, Optional, Tuple

from limits import Admission, Bucket, QuotaLedger


@dataclass
class Request:
    id: str
    domains: Tuple[str, ...]
    cost: int = 1
    priority: int = 0
    state: str = "queued"
    # Filled in by the dispatcher.
    seq: int = 0
    submitted_at: Optional[int] = None
    admitted_at: Optional[int] = None
    finished_at: Optional[int] = None
    outcome: Optional[str] = None
    reserved: List[str] = field(default_factory=list)
    attempts: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "domains": list(self.domains),
            "cost": self.cost,
            "priority": self.priority,
            "state": self.state,
            "seq": self.seq,
            "submitted_at": self.submitted_at,
            "admitted_at": self.admitted_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome,
            "attempts": self.attempts,
        }


def _extract_429(response: Any) -> Tuple[Optional[int], Tuple[str, ...]]:
    """Return ``(retry_after_ms, scope_domains)`` for a 429-like response."""
    if not isinstance(response, dict):
        return None, ()
    status = response.get("status", response.get("code"))
    retry = response.get("retry_after", response.get("retry_after_ms"))
    scope = response.get("scope", response.get("domain", response.get("scopes")))
    is_429 = status == 429 or bool(response.get("throttled"))
    if not is_429 and retry is None:
        return None, ()
    if scope is None:
        scopes: Tuple[str, ...] = ()
    elif isinstance(scope, str):
        scopes = (scope,)
    else:
        scopes = tuple(scope)
    return retry, scopes


class Dispatcher:
    def __init__(self, buckets: Dict[str, Bucket], transport: Any) -> None:
        self.buckets = dict(buckets)
        self.ledger = QuotaLedger(self.buckets)
        self.transport = transport
        self.pending: List[Request] = []
        self.active: Dict[str, Request] = {}
        self.requests: Dict[str, Request] = {}
        self.events: List[Tuple[Any, ...]] = []
        self.now = 0
        self._seq = 0
        self._lock = threading.RLock()
        self._in_tick = 0

    # -- submission ------------------------------------------------------
    def submit(self, request: Request, now: Optional[int] = None) -> Request:
        if not isinstance(request, Request):
            raise TypeError("submit expects a Request")
        with self._lock:
            self._validate_request(request)
            if request.id in self.requests:
                raise ValueError(f"duplicate request id {request.id!r}")
            if now is None:
                now = self.now
            elif now < self.now:
                raise ValueError("clock must be monotonic")
            self.now = now
            request.domains = tuple(request.domains)
            request.cost = int(request.cost)
            request.seq = self._seq
            self._seq += 1
            request.submitted_at = now
            request.state = "queued"
            request.admitted_at = None
            request.finished_at = None
            request.outcome = None
            request.reserved = []
            request.attempts = 0
            self.pending.append(request)
            self.requests[request.id] = request
            self.events.append(("submit", request.id, now, list(request.domains), request.cost, request.priority))
            return request

    def _validate_request(self, request: Request) -> None:
        if not isinstance(request.id, str) or not request.id:
            raise ValueError("request id must be a non-empty string")
        domains = tuple(request.domains)
        if not domains:
            raise ValueError(f"request {request.id!r} must name at least one domain")
        if len(set(domains)) != len(domains):
            raise ValueError(f"request {request.id!r} has duplicate domains")
        for d in domains:
            if d not in self.buckets:
                raise ValueError(
                    f"request {request.id!r} references unknown domain {d!r}"
                )
        if isinstance(request.cost, bool) or not isinstance(request.cost, int):
            raise TypeError(f"request {request.id!r} cost must be an int")
        if request.cost < 0:
            raise ValueError(f"request {request.id!r} cost must be >= 0")
        for d in domains:
            bucket = self.buckets[d]
            if request.cost > bucket.capacity:
                raise ValueError(
                    f"request {request.id!r} cost {request.cost} exceeds "
                    f"capacity {bucket.capacity} of domain {d!r}"
                )
            if bucket.max_active < 1:
                raise ValueError(
                    f"domain {d!r} can never admit because max_active < 1"
                )
        if isinstance(request.priority, bool) or not isinstance(request.priority, int):
            raise TypeError(f"request {request.id!r} priority must be an int")

    # -- scheduling ------------------------------------------------------
    def tick(self, now: int) -> int:
        """Advance the caller clock and try to send every feasible request.

        Returns the number of requests started during this call.  Completion
        callbacks may already have settled some of them synchronously.
        """
        with self._lock:
            if isinstance(now, bool) or not isinstance(now, int):
                raise TypeError("clock must be an integer number of milliseconds")
            if now < self.now:
                raise ValueError(
                    f"clock must be monotonic: {now} < {self.now}"
                )
            self.now = now
            if self._in_tick:
                # A synchronous transport re-entered tick; the outer call
                # will finish the work.
                return 0
            self._in_tick += 1
            sent_total = 0
            try:
                while True:
                    sent = self._drain_once(now)
                    sent_total += sent
                    if sent == 0:
                        break
            finally:
                self._in_tick -= 1
            self.events.append(("tick", now, sent_total))
            return sent_total

    def _drain_once(self, now: int) -> int:
        sent = 0
        priorities = sorted(
            {r.priority for r in self.pending if r.state == "queued"},
            reverse=True,
        )
        # The reservation set is shared across priorities: a blocked
        # higher-priority request protects its domains from lower-priority
        # requests in the same pass, so they cannot consume the tokens it is
        # accumulating.
        reserved: set = set()
        for priority in priorities:
            candidates = sorted(
                (r for r in self.pending if r.priority == priority),
                key=lambda r: r.seq,
            )
            for request in candidates:
                if request.state != "queued":
                    continue
                if set(request.domains) & reserved:
                    # A prior, still-infeasible request has reserved one of
                    # these domains.  Do not jump it; its tokens must survive
                    # until it can be admitted.
                    request.attempts += 1
                    continue
                request.attempts += 1
                decision = self.ledger.admit(
                    request.id, request.domains, request.cost, now
                )
                if decision.admitted:
                    self._start(request, now)
                    sent += 1
                else:
                    # Reserve every domain this request needs so overlapping
                    # successors cannot consume the accumulating quota.
                    reserved.update(request.domains)
        return sent

    def _start(self, request: Request, now: int) -> None:
        self.pending.remove(request)
        request.state = "active"
        request.admitted_at = now
        request.reserved = list(request.domains)
        self.active[request.id] = request
        self.events.append(
            ("start", request.id, now, list(request.domains), request.cost)
        )
        try:
            self.transport.start(
                request, lambda response: self._on_response(request.id, response)
            )
        except Exception as exc:  # transport failure is a terminal outcome
            self._finalize(request.id, now, "failed", {"error": str(exc)})

    # -- completion ------------------------------------------------------
    def complete(self, request_id: str, response: Any, now: Optional[int] = None) -> None:
        """Compatibility entry point for a manual/out-of-band completion."""
        with self._lock:
            if now is not None:
                if now < self.now:
                    raise ValueError("clock must be monotonic")
                self.now = now
            self._finalize(request_id, self.now, None, response)

    def _on_response(self, request_id: str, response: Any) -> None:
        """Callback entry point; safe from any thread and synchronous starts."""
        with self._lock:
            self._finalize(request_id, self.now, None, response)

    def _finalize(
        self,
        request_id: str,
        now: int,
        forced_outcome: Optional[str],
        response: Any,
    ) -> None:
        request = self.active.pop(request_id, None)
        if request is None:
            # Already settled (cancel won a race, or a late error/response).
            self.events.append(("late-response", request_id, now, bool(response)))
            return

        outcome = forced_outcome or self._classify(response)
        response = response if isinstance(response, dict) else {}

        # A 429 freeze applies only to the named scope domain(s).  Requests
        # already sent are never withdrawn.
        retry_after, scopes = _extract_429(response)
        if outcome == "throttled":
            if not scopes:
                scopes = tuple(request.domains)
            until = now + max(0, int(retry_after or 0))
            for scope in scopes:
                if scope in self.buckets:
                    self.ledger.throttle(scope, until, now)
                    self.events.append(("freeze", scope, until, request.id))

        ticket = self.ledger.settle(request_id, now, outcome)
        request.finished_at = now
        if ticket is not None and ticket.terminal:
            # settle() may have won an earlier terminal transition (for
            # example a cancellation); the ticket is the source of truth.
            request.outcome = ticket.outcome
            request.state = ticket.outcome
        else:
            request.outcome = outcome
            request.state = outcome
        self.events.append(("finish", request_id, now, request.outcome))

    @staticmethod
    def _classify(response: Any) -> str:
        if not isinstance(response, dict):
            return "completed"
        if response.get("cancelled"):
            return "cancelled"
        if response.get("error") is not None:
            return "failed"
        status = response.get("status", response.get("code"))
        if status == 429 or response.get("throttled"):
            return "throttled"
        return "completed"

    # -- cancellation ----------------------------------------------------
    def cancel(self, request_id: str, now: Optional[int] = None) -> bool:
        """Cancel a queued or in-flight request.

        A queued request has no ticket, so no token is charged.  An in-flight
        request races with its normal response; the ledger settles the ticket
        exactly once, so the concurrency slot cannot be released twice.
        """
        with self._lock:
            request = self.requests.get(request_id)
            if request is None:
                return False
            if now is None:
                now = self.now
            elif now < self.now:
                raise ValueError("clock must be monotonic")
            else:
                self.now = now
            if request.state == "queued":
                self.pending.remove(request)
                request.state = "cancelled"
                request.outcome = "cancelled"
                request.finished_at = now
                self.events.append(("cancel-queued", request_id, now))
                return True
            if request.state in ("active", "cancelling"):
                request.state = "cancelling"
                self.events.append(("cancel-active", request_id, now))
                try:
                    self.transport.cancel(request_id)
                except Exception as exc:
                    self.events.append(("cancel-error", request_id, now, str(exc)))
                # If the transport did not synchronously complete, make the
                # cancellation the terminal transition.  A later response is
                # logged as late and cannot settle twice.
                if request_id in self.active:
                    self._finalize(request_id, now, "cancelled", {"cancelled": True})
                return True
            return False

    # -- queries ---------------------------------------------------------
    def metering(self, now: Optional[int] = None) -> Dict[str, Any]:
        """Quota-side view: tokens, slots, freezes, tickets, reconciliation."""
        with self._lock:
            return {
                "clock": self.now,
                "ledger": self.ledger.snapshot(self.now if now is None else now),
                "reconcile": self.ledger.reconcile(),
                "history": list(self.ledger.history),
            }

    def lifecycle(self) -> Dict[str, Any]:
        """Request-side view: queue order, active set, terminal outcomes."""
        with self._lock:
            return {
                "clock": self.now,
                "pending": [r.id for r in sorted(self.pending, key=lambda r: (-r.priority, r.seq))],
                "active": list(self.active),
                "requests": {
                    rid: r.as_dict() for rid, r in self.requests.items()
                },
                "events": list(self.events),
            }

    def status(self, request_id: str, now: Optional[int] = None) -> Dict[str, Any]:
        """Combined per-request view, including why a queued request is held."""
        with self._lock:
            request = self.requests.get(request_id)
            if request is None:
                return {"id": request_id, "found": False}
            ticket = self.ledger.ticket(request_id)
            result: Dict[str, Any] = {
                "id": request_id,
                "found": True,
                "request": request.as_dict(),
                "ticket": ticket.as_dict() if ticket else None,
            }
            if request.state == "queued":
                at = self.now if now is None else now
                decision = self.ledger.check(request.domains, request.cost, at)
                result["admission"] = {
                    "admitted": decision.admitted,
                    "reasons": {k: list(v) for k, v in decision.reasons.items()},
                    "next_times": decision.next_times,
                }
            return result

    def why_not(self, request_id: str, now: Optional[int] = None) -> Admission:
        with self._lock:
            request = self.requests.get(request_id)
            if request is None:
                raise KeyError(request_id)
            at = self.now if now is None else now
            return self.ledger.check(request.domains, request.cost, at)

    def pending_order(self, priority: Optional[int] = None) -> List[str]:
        with self._lock:
            rows = self.pending
            if priority is not None:
                rows = [r for r in rows if r.priority == priority]
            return [r.id for r in sorted(rows, key=lambda r: r.seq)]

    @property
    def queue(self) -> List[Request]:
        """Read-only compatibility view of the pending queue in order."""
        with self._lock:
            return sorted(self.pending, key=lambda r: (-r.priority, r.seq))
