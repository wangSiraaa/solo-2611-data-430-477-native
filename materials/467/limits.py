"""Multi-domain token buckets and the per-send quota ledger.

The public clock is always caller supplied and is an integer number of
milliseconds.  Token arithmetic uses :class:`fractions.Fraction` so virtual
clock checks can assert exact values (for example a refill rate of ``1/3``).

Two important invariants live here:

* admission is all-or-nothing across every domain of a send; we never take a
  token from one bucket and then wait for another bucket;
* a token consumed at send time is *never* returned.  Settling a ticket only
  releases the concurrency slot(s), exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

TERMINAL_OUTCOMES = frozenset({"completed", "throttled", "failed", "cancelled"})


def as_fraction(value: Any, name: str = "value") -> Fraction:
    """Convert an int, float, string or Fraction to an exact Fraction."""
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a rational number, not bool")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        return Fraction(value)
    if isinstance(value, float):
        # repr(0.1) == "0.1" -> 1/10 rather than the binary approximation.
        return Fraction(repr(value))
    raise TypeError(f"{name} must be an int/float/str/Fraction, got {type(value)!r}")


def _validate_clock(now: Any) -> int:
    if isinstance(now, bool) or not isinstance(now, int):
        raise TypeError("clock must be an integer number of milliseconds")
    if now < 0:
        raise ValueError("clock must be non-negative")
    return now


def _ceil_fraction(value: Fraction) -> int:
    if value <= 0:
        return 0
    return (value.numerator + value.denominator - 1) // value.denominator


@dataclass
class Bucket:
    """Per-domain token bucket plus concurrency and freeze accounting.

    The first four positional arguments keep the original material's layout:
    ``Bucket(capacity, per_ms, max_active, initial_tokens)``.  The domain
    name is assigned by :class:`QuotaLedger` from the mapping key.
    """

    capacity: int
    per_ms: Any
    max_active: int
    initial: int = 0
    name: str = ""
    tokens: Fraction = field(init=False)
    updated: int = field(init=False, default=0)
    active: int = field(init=False, default=0)
    frozen_until: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int):
            raise TypeError("capacity must be an int")
        if not 1 <= self.capacity <= 20:
            raise ValueError("capacity must be in 1..20")
        if isinstance(self.max_active, bool) or not isinstance(self.max_active, int):
            raise TypeError("max_active must be a non-negative int")
        if self.max_active < 0:
            raise ValueError("max_active must be >= 0")
        rate = as_fraction(self.per_ms, "per_ms")
        if rate <= 0:
            raise ValueError("refill rate must be positive")
        self.per_ms = rate
        if self.initial is None:
            self.initial = self.capacity
        if isinstance(self.initial, bool) or not isinstance(self.initial, int):
            raise TypeError("initial must be an int")
        if not 0 <= self.initial <= self.capacity:
            raise ValueError("initial must be within 0..capacity")
        self.tokens = Fraction(self.initial)
        self.updated = 0

    # -- queries ---------------------------------------------------------
    def refill(self, now: int) -> None:
        now = _validate_clock(now)
        if now < self.updated:
            raise ValueError(
                f"clock must be monotonic for bucket {self.name!r}: "
                f"{now} < {self.updated}"
            )
        if now > self.updated:
            self.tokens = min(
                Fraction(self.capacity),
                self.tokens + Fraction(now - self.updated) * self.per_ms,
            )
            self.updated = now

    def _projected_tokens(self, now: int) -> Fraction:
        """Tokens at ``now`` without mutating state."""
        now = _validate_clock(now)
        if now < self.updated:
            # Older clock: report current state rather than going backwards.
            return self.tokens
        return min(
            Fraction(self.capacity),
            self.tokens + Fraction(now - self.updated) * self.per_ms,
        )

    def blockers(self, cost: int, now: int) -> Tuple[str, ...]:
        """Non-mutating feasibility check: reasons (empty == available)."""
        now = _validate_clock(now)
        tokens = self._projected_tokens(now)
        reasons: List[str] = []
        if now < self.frozen_until:
            reasons.append("frozen")
        if tokens < cost:
            reasons.append("tokens")
        if self.active >= self.max_active:
            reasons.append("concurrency")
        return tuple(reasons)

    def available(self, cost: int, now: int) -> bool:
        return not self.blockers(cost, now)

    def availability(self, cost: int, now: int) -> Tuple[bool, str]:
        """Compatibility helper returning ``(available, primary_reason)``."""
        reasons = self.blockers(cost, now)
        return (not reasons, reasons[0] if reasons else "ok")

    def next_available_time(self, cost: int, now: int) -> Optional[int]:
        """Earliest integer clock at which this bucket may allow ``cost``.

        Non-mutating.  ``None`` means a time cannot be computed: the bucket is
        concurrency blocked (only a settlement can free a slot).
        """
        now = _validate_clock(now)
        if self.active >= self.max_active:
            return None
        ready = max(now, self.frozen_until)
        future_tokens = self._projected_tokens(ready)
        needed = Fraction(cost) - future_tokens
        if needed > 0:
            ready += _ceil_fraction(needed / self.per_ms)
        return ready

    def project(self, now: int) -> Dict[str, Any]:
        """Pure projection at ``now``; never mutates bucket state."""
        now = _validate_clock(now)
        if now >= self.updated:
            tokens = min(
                Fraction(self.capacity),
                self.tokens + Fraction(now - self.updated) * self.per_ms,
            )
        else:
            tokens = self.tokens
        return {
            "capacity": self.capacity,
            "per_ms": str(self.per_ms),
            "tokens": str(tokens),
            "max_active": self.max_active,
            "active": self.active,
            "updated": self.updated,
            "frozen_until": self.frozen_until,
            "frozen": now < self.frozen_until,
        }

    # -- mutation --------------------------------------------------------
    def occupy(self, cost: int, now: int) -> None:
        """Atomically consume ``cost`` tokens and one concurrency slot."""
        reasons = self.blockers(cost, now)
        if reasons:
            raise RuntimeError(f"bucket {self.name!r} unavailable: {reasons}")
        # blockers() is a pure projection, so apply the refill before we
        # deduct; this is the only place token state advances on a send.
        self.refill(now)
        self.tokens -= cost
        self.active += 1

    def release(self) -> None:
        if self.active <= 0:
            raise RuntimeError(
                f"bucket {self.name!r} has no active slot to release"
            )
        self.active -= 1

    # -- compatibility aliases (original material API) -------------------
    def reserve(self, cost: int, now: int) -> bool:
        """Boolean variant of :meth:`occupy` used by older callers."""
        if self.blockers(cost, now):
            return False
        self.occupy(cost, now)
        return True

    def complete(self) -> None:
        """Original name for releasing one concurrency slot."""
        self.release()

    def throttle(self, until: int) -> None:
        if isinstance(until, bool) or not isinstance(until, int):
            raise TypeError("freeze deadline must be an int (ms)")
        if until < 0:
            raise ValueError("freeze deadline must be non-negative")
        self.frozen_until = max(self.frozen_until, until)

    # -- introspection ---------------------------------------------------
    @property
    def frozen(self) -> bool:
        return self.updated < self.frozen_until

    def as_dict(self) -> Dict[str, Any]:
        return {
            "capacity": self.capacity,
            "per_ms": str(self.per_ms),
            "tokens": str(self.tokens),
            "max_active": self.max_active,
            "active": self.active,
            "updated": self.updated,
            "frozen_until": self.frozen_until,
            "frozen": self.frozen,
        }


@dataclass(frozen=True)
class Ticket:
    """One admitted send; queryable and settled exactly once."""

    request_id: str
    domains: Tuple[str, ...]
    cost: int
    admitted_at: int
    state: str = "active"  # active -> settled
    outcome: Optional[str] = None
    settled_at: Optional[int] = None

    @property
    def terminal(self) -> bool:
        return self.state == "settled"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "domains": list(self.domains),
            "cost": self.cost,
            "admitted_at": self.admitted_at,
            "state": self.state,
            "outcome": self.outcome,
            "settled_at": self.settled_at,
        }


@dataclass(frozen=True)
class Admission:
    """Result of an all-or-nothing admission attempt."""

    admitted: bool
    domains: Tuple[str, ...] = ()
    reasons: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    next_times: Dict[str, Optional[int]] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return not self.admitted

    @property
    def token_blocked_domains(self) -> Tuple[str, ...]:
        return tuple(
            d for d, reasons in self.reasons.items() if "tokens" in reasons
        )


class QuotaLedger:
    """Owns all quota state: atomic admission, tickets, one-shot settlement."""

    def __init__(self, buckets: Dict[str, Bucket]) -> None:
        if not buckets:
            raise ValueError("at least one bucket is required")
        self.buckets: Dict[str, Bucket] = dict(buckets)
        for name, bucket in self.buckets.items():
            if not isinstance(name, str) or not name:
                raise ValueError("domain names must be non-empty strings")
            if not isinstance(bucket, Bucket):
                raise TypeError(f"bucket for {name!r} must be a Bucket")
            bucket.name = name
        self._tickets: Dict[str, Ticket] = {}
        self.history: List[Tuple[Any, ...]] = []
        self._lock = threading.RLock()

    # -- validation ------------------------------------------------------
    @staticmethod
    def _unique_domains(domains: Iterable[str]) -> Tuple[str, ...]:
        seen: List[str] = []
        for d in domains:
            if not isinstance(d, str):
                raise TypeError("domain names must be strings")
            if d in seen:
                raise ValueError(f"duplicate domain in request: {d!r}")
            seen.append(d)
        return tuple(seen)

    @staticmethod
    def _validate_cost(cost: Any) -> int:
        if isinstance(cost, bool) or not isinstance(cost, int):
            raise TypeError("cost must be an int")
        if cost < 0:
            raise ValueError("cost must be >= 0")
        return cost

    def _validate(self, domains: Tuple[str, ...], cost: int, now: int) -> int:
        now = _validate_clock(now)
        if not domains:
            raise ValueError("a request must name at least one domain")
        for d in domains:
            if d not in self.buckets:
                raise ValueError(f"unknown domain {d!r}")
            if cost > self.buckets[d].capacity:
                raise ValueError(
                    f"cost {cost} exceeds capacity "
                    f"{self.buckets[d].capacity} of domain {d!r}"
                )
        return now

    # -- admission -------------------------------------------------------
    def _check(self, domains: Tuple[str, ...], cost: int, now: int) -> Admission:
        reasons: Dict[str, Tuple[str, ...]] = {}
        next_times: Dict[str, Optional[int]] = {}
        for d in domains:
            bucket = self.buckets[d]
            blockers = bucket.blockers(cost, now)
            if blockers:
                reasons[d] = blockers
                next_times[d] = bucket.next_available_time(cost, now)
        return Admission(not reasons, domains, reasons, next_times)

    def check(self, domains: Iterable[str], cost: int, now: int) -> Admission:
        """Side-effect-free feasibility probe (tokens are not consumed)."""
        with self._lock:
            doms = self._unique_domains(domains)
            cost = self._validate_cost(cost)
            now = self._validate(doms, cost, now)
            return self._check(doms, cost, now)

    def admit(
        self, request_id: str, domains: Iterable[str], cost: int, now: int
    ) -> Admission:
        """Atomically reserve every domain; no partial reservations on failure."""
        with self._lock:
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("request_id must be a non-empty string")
            doms = self._unique_domains(domains)
            cost = self._validate_cost(cost)
            now = self._validate(doms, cost, now)
            if request_id in self._tickets:
                raise ValueError(f"request {request_id!r} was already admitted")
            decision = self._check(doms, cost, now)
            if not decision.admitted:
                self.history.append(
                    ("rejected", request_id, now, dict(decision.reasons))
                )
                return decision
            # Every domain is known to be available, so occupy() cannot fail
            # midway.  This loop is the atomic step: all domains or nothing.
            for d in doms:
                self.buckets[d].occupy(cost, now)
            self._tickets[request_id] = Ticket(request_id, doms, cost, now)
            self.history.append(("admitted", request_id, now, list(doms), cost))
            return decision

    # -- settlement ------------------------------------------------------
    def settle(
        self, request_id: str, now: Optional[int] = None, outcome: str = "completed"
    ) -> Optional[Ticket]:
        """Release concurrency slots exactly once; tokens are never refunded."""
        with self._lock:
            ticket = self._tickets.get(request_id)
            if ticket is None:
                self.history.append(("settle-miss", request_id, now))
                return None
            if ticket.state == "settled":
                # Already terminal: a cancel/response race must not release
                # the concurrency slot a second time.
                self.history.append(
                    ("settle-duplicate", request_id, now, outcome)
                )
                return ticket
            if outcome not in TERMINAL_OUTCOMES:
                raise ValueError(f"unknown outcome {outcome!r}")
            for d in ticket.domains:
                self.buckets[d].release()
            settled = Ticket(
                ticket.request_id,
                ticket.domains,
                ticket.cost,
                ticket.admitted_at,
                state="settled",
                outcome=outcome,
                settled_at=now,
            )
            self._tickets[request_id] = settled
            self.history.append(("settled", request_id, now, outcome))
            return settled

    # -- freezes ---------------------------------------------------------
    def throttle(self, domain: str, until: int, now: Optional[int] = None) -> None:
        """Freeze admissions for one domain until ``until`` (inclusive end)."""
        with self._lock:
            if domain not in self.buckets:
                raise ValueError(f"unknown domain {domain!r}")
            self.buckets[domain].throttle(until)
            self.history.append(("throttle", domain, until, now))

    # -- compatibility aliases ------------------------------------------
    def begin(self, request_id: str, domains: Iterable[str], cost: int, now: int) -> bool:
        """Original-material API: return whether all domains were admitted."""
        return self.admit(request_id, domains, cost, now).admitted

    def finish(self, request_id: str, now: Optional[int] = None) -> Optional[Ticket]:
        """Original-material API: settle a ticket without refunding tokens."""
        return self.settle(request_id, now, "completed")

    # -- introspection ---------------------------------------------------
    def ticket(self, request_id: str) -> Optional[Ticket]:
        with self._lock:
            return self._tickets.get(request_id)

    def tickets(self) -> List[Ticket]:
        with self._lock:
            return list(self._tickets.values())

    def is_active(self, request_id: str) -> bool:
        with self._lock:
            ticket = self._tickets.get(request_id)
            return bool(ticket and ticket.state == "active")

    def active_tickets(self) -> List[Ticket]:
        with self._lock:
            return [t for t in self._tickets.values() if t.state == "active"]

    def snapshot(self, now: Optional[int] = None) -> Dict[str, Any]:
        """Read-only view.  If ``now`` is given it is projected, not applied."""
        with self._lock:
            if now is not None:
                now = _validate_clock(now)
            domains: Dict[str, Any] = {}
            for name, bucket in self.buckets.items():
                if now is None:
                    domains[name] = bucket.as_dict()
                else:
                    domains[name] = bucket.project(now)
            return {
                "clock": now,
                "domains": domains,
                "tickets": [t.as_dict() for t in self._tickets.values()],
            }

    def reconcile(self) -> Dict[str, Any]:
        """Compare ticket bookkeeping with bucket concurrency counters."""
        with self._lock:
            expected = {name: 0 for name in self.buckets}
            active_tickets = 0
            for ticket in self._tickets.values():
                if ticket.state == "active":
                    active_tickets += 1
                    for d in ticket.domains:
                        expected[d] += 1
            actual = {name: b.active for name, b in self.buckets.items()}
            return {
                "consistent": expected == actual,
                "expected_active": expected,
                "actual_active": actual,
                "active_tickets": active_tickets,
                "tickets": len(self._tickets),
            }
