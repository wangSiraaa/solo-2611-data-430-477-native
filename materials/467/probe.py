"""Acceptance probe: multi-domain atomic admission, settlement and freezes.

Run with ``python3 probe.py``.  Every section prints a JSON record so the
metering side (ledger) and the lifecycle side (dispatcher) can be compared.
"""

from __future__ import annotations

import json
from fractions import Fraction

from limits import Bucket, QuotaLedger
from dispatcher import Dispatcher, Request
from transport import ControlledTransport


def section(name):
    print(f"\n== {name} ==")


def dump(value):
    print(json.dumps(value, sort_keys=True, default=str))


def probe_multi_domain_atomic():
    section("multi-domain atomic admission")
    buckets = {
        "global": Bucket(10, 1, 4, 10),
        "tenant": Bucket(1, 1, 1, 0),
    }
    transport = ControlledTransport()
    dispatcher = Dispatcher(buckets, transport)
    dispatcher.submit(Request("r1", ("global", "tenant")), now=0)
    started = dispatcher.tick(0)

    record = {
        "started": started,
        "transport_started": transport.started,
        "global_active": buckets["global"].active,
        "global_tokens": str(buckets["global"].tokens),
        "tenant_active": buckets["tenant"].active,
        "why_not": {
            k: list(v)
            for k, v in dispatcher.why_not("r1").reasons.items()
        },
        "ledger_reconcile": dispatcher.ledger.reconcile(),
    }
    dump(record)
    assert started == 0
    assert buckets["global"].active == 0
    assert buckets["global"].tokens == 10  # no partial reservation
    assert dispatcher.ledger.ticket("r1") is None

    # Tenant gets one token; both domains are admitted together.
    assert dispatcher.tick(1) == 1
    dump(
        {
            "tick": 1,
            "started": transport.started,
            "ticket": dispatcher.ledger.ticket("r1").as_dict(),
            "global_tokens": str(buckets["global"].tokens),
            "tenant_tokens": str(buckets["tenant"].tokens),
        }
    )
    assert buckets["global"].tokens == 9
    assert buckets["tenant"].tokens == 0
    assert buckets["global"].active == 1 and buckets["tenant"].active == 1

    transport.finish("r1")
    dump(
        {
            "after_finish": dispatcher.ledger.ticket("r1").as_dict(),
            "global_tokens": str(buckets["global"].tokens),
            "tenant_tokens": str(buckets["tenant"].tokens),
            "active": [buckets["global"].active, buckets["tenant"].active],
            "reconcile": dispatcher.ledger.reconcile(),
        }
    )
    # Tokens are not refunded; only concurrency is released.
    assert buckets["global"].tokens == 9
    assert buckets["tenant"].tokens == 0
    assert buckets["global"].active == 0 and buckets["tenant"].active == 0


def probe_exact_tokens():
    section("exact token accounting on a virtual clock")
    curve = {"d": Bucket(5, Fraction(1, 3), 10, 0)}
    rows = []
    for now in (0, 2, 3, 6, 9):
        curve["d"].refill(now)
        rows.append({"now": now, "tokens": str(curve["d"].tokens)})
    dump({"refill_curve": rows})
    assert [row["tokens"] for row in rows] == ["0", "2/3", "1", "2", "3"]

    buckets = {"d": Bucket(5, Fraction(1, 3), 10, 3)}
    ledger = QuotaLedger(buckets)
    assert ledger.admit("a", ("d",), 2, 0).admitted
    dump({"admitted_cost_2_at_0": str(buckets["d"].tokens)})
    assert buckets["d"].tokens == 1
    ledger.settle("a", 1, "completed")
    # Finishing must NOT put the token back.
    assert buckets["d"].tokens == 1
    assert not ledger.admit("b", ("d",), 2, 1).admitted
    # Feasibility queries are side-effect free: no refill is applied until a
    # send actually occupies the bucket, so the charge from "a" still shows.
    assert str(buckets["d"].tokens) == "1"
    assert str(buckets["d"].project(3)["tokens"]) == "2"
    assert ledger.admit("c", ("d",), 2, 3).admitted  # exact refill at t=3
    dump(
        {
            "after_settle_tokens": "1",
            "second_at_same_clock": False,
            "admitted_again_at_3": True,
            "final_tokens": str(buckets["d"].tokens),
            "tickets": [t.as_dict() for t in ledger.tickets()],
        }
    )
    assert buckets["d"].tokens == 0


def probe_no_partial_on_reject():
    section("reject leaves no partial reservation")
    buckets = {"a": Bucket(1, 1, 1, 1), "b": Bucket(1, 1, 1, 0)}
    ledger = QuotaLedger(buckets)
    decision = ledger.admit("partial", ("a", "b"), 1, 0)
    dump(
        {
            "admitted": decision.admitted,
            "reasons": {k: list(v) for k, v in decision.reasons.items()},
            "a_tokens": str(buckets["a"].tokens),
            "a_active": buckets["a"].active,
            "b_tokens": str(buckets["b"].tokens),
            "b_active": buckets["b"].active,
            "ticket": ledger.ticket("partial"),
        }
    )
    assert not decision.admitted
    assert buckets["a"].active == 0 and buckets["b"].active == 0
    assert buckets["a"].tokens == 1


def probe_freeze_scope():
    section("429 freezes only its scope; disjoint requests continue")
    buckets = {
        "global": Bucket(10, 1, 10, 10),
        "tenant": Bucket(10, 1, 10, 10),
        "other": Bucket(10, 1, 10, 10),
    }
    transport = ControlledTransport()
    dispatcher = Dispatcher(buckets, transport)
    dispatcher.submit(Request("x", ("global", "tenant")), now=0)
    dispatcher.submit(Request("y", ("global", "other")), now=0)
    assert dispatcher.tick(0) == 2

    # x gets a 429 scoped to tenant with Retry-After 100ms.
    transport.finish("x", {"status": 429, "scope": "tenant", "retry_after": 100})
    transport.finish("y", {})
    dump(
        {
            "tenant_frozen_until": buckets["tenant"].frozen_until,
            "other_frozen_until": buckets["other"].frozen_until,
            "y_finished": dispatcher.requests["y"].state,
            "x_outcome": dispatcher.requests["x"].outcome,
        }
    )
    assert buckets["tenant"].frozen_until == 100
    assert buckets["other"].frozen_until == 0

    dispatcher.submit(Request("t", ("global", "tenant")), now=10)
    # Disjoint from *both* domains of the blocked request: must not wait.
    dispatcher.submit(Request("o", ("other",)), now=10)
    started = dispatcher.tick(10)
    dump(
        {
            "tick_10_started": started,
            "transport_started": transport.started,
            "tenant_ticket": dispatcher.ledger.ticket("t"),
            "other_ticket": bool(dispatcher.ledger.ticket("o")),
        }
    )
    assert "t" not in transport.started
    assert "o" in transport.started

    # At exactly frozen_until the tenant scope opens again; both queued
    # requests (t from t=10 and t2 from t=99) become feasible together.
    dispatcher.submit(Request("t2", ("global", "tenant")), now=99)
    assert dispatcher.tick(99) == 0
    assert dispatcher.tick(100) == 2
    dump(
        {
            "t_started_at": dispatcher.requests["t"].admitted_at,
            "t2_started_at": dispatcher.requests["t2"].admitted_at,
            "t2_ticket": dispatcher.ledger.ticket("t2").as_dict(),
        }
    )
    assert dispatcher.requests["t"].admitted_at == 100
    assert dispatcher.requests["t2"].admitted_at == 100


def probe_already_sent_survives_freeze():
    section("an already-sent request is not withdrawn by a freeze")
    buckets = {"a": Bucket(5, 1, 1, 5), "b": Bucket(5, 1, 1, 5)}
    transport = ControlledTransport()
    dispatcher = Dispatcher(buckets, transport)
    dispatcher.submit(Request("live", ("a", "b")), now=0)
    dispatcher.submit(Request("next", ("a", "b")), now=0)
    assert dispatcher.tick(0) == 1
    transport.finish("live", {"status": 429, "scope": "a", "retry_after": 50})
    dump(
        {
            "live_state": dispatcher.requests["live"].state,
            "live_ticket": dispatcher.ledger.ticket("live").as_dict(),
            "freeze_a": buckets["a"].frozen_until,
            "next_pending": dispatcher.requests["next"].state,
        }
    )
    assert dispatcher.requests["live"].state == "throttled"
    assert dispatcher.ledger.ticket("live").state == "settled"
    assert dispatcher.requests["next"].state == "queued"
    assert dispatcher.tick(49) == 0
    assert dispatcher.tick(50) == 1


def probe_cancellation():
    section("cancel before and after send")
    buckets = {
        "g": Bucket(1, 1, 1, 1),
        "h": Bucket(5, 1, 5, 5),
    }
    transport = ControlledTransport()
    dispatcher = Dispatcher(buckets, transport)

    dispatcher.submit(Request("queued", ("g",)), now=0)
    dispatcher.submit(Request("waiting", ("g",)), now=0)
    assert dispatcher.tick(0) == 1  # "queued" sends, "waiting" remains
    before = dispatch_snapshot(dispatcher, buckets)
    assert dispatcher.cancel("waiting", now=0)
    after = dispatch_snapshot(dispatcher, buckets)
    dump({"cancel_queued_before": before, "cancel_queued_after": after})
    # Cancelling a queued request deducts no token and leaves no ticket.
    assert dispatcher.ledger.ticket("waiting") is None
    assert str(buckets["g"].tokens) == before["g"]["tokens"]
    assert dispatcher.lifecycle()["pending"] == []

    # Cancel an in-flight request, then race the normal response.
    dispatcher.submit(Request("flight", ("h",)), now=1)
    assert dispatcher.tick(1) == 1
    assert dispatcher.cancel("flight", now=2)
    late = transport.finish("flight", {"status": 200})
    record = {
        "cancel_active_returned": True,
        "late_response_delivered": late,
        "ticket": dispatcher.ledger.ticket("flight").as_dict(),
        "h_active": buckets["h"].active,
        "h_tokens": str(buckets["h"].tokens),
        "reconcile": dispatcher.ledger.reconcile(),
        "events": [e for e in dispatcher.events if "flight" in str(e)],
    }
    dump(record)
    assert not late
    assert buckets["h"].active == 0
    assert str(buckets["h"].tokens) == "4"  # charged once at send, never refunded
    assert dispatcher.ledger.reconcile()["consistent"] is True


def dispatch_snapshot(dispatcher, buckets):
    return {
        name: {
            "tokens": str(bucket.tokens),
            "active": bucket.active,
            "frozen_until": bucket.frozen_until,
        }
        for name, bucket in buckets.items()
    } | {"pending": [r.id for r in dispatcher.pending]}


def probe_fairness():
    section("FIFO within a priority, reservation against starvation")
    buckets = {
        "shared": Bucket(3, Fraction(1, 2), 10, 0, name="shared"),
        "solo": Bucket(3, 1, 10, 3, name="solo"),
    }
    transport = ControlledTransport()
    dispatcher = Dispatcher(buckets, transport)

    # Big overlapping request waits for 2 tokens; small ones arrive later.
    dispatcher.submit(Request("big", ("shared",), 2), now=0)
    for i in range(4):
        dispatcher.submit(Request(f"small{i}", ("shared",), 1), now=0)
    # A disjoint request must not be blocked by the reserved "shared" domain.
    dispatcher.submit(Request("solo", ("solo",), 1), now=0)

    trace = []
    trace.append({"tick": 0, "started": dispatcher.tick(0), "started_ids": list(transport.started)})
    # At t=2 tokens reach 1; big still needs 2, so overlapping smalls must
    # keep waiting while the disjoint request already went out at t=0.
    trace.append({"tick": 2, "started": dispatcher.tick(2), "started_ids": list(transport.started)})
    assert "big" not in transport.started
    assert "small0" not in transport.started
    assert "solo" in transport.started

    trace.append({"tick": 4, "started": dispatcher.tick(4), "started_ids": list(transport.started)})
    trace.append(
        {
            "big_admitted_at": dispatcher.requests["big"].admitted_at,
            "pending_order": dispatcher.pending_order(),
        }
    )
    dump(trace)
    assert "big" in transport.started
    assert dispatcher.requests["big"].admitted_at == 4
    # The big request was admitted with the two accumulated tokens; the
    # overlapping small successors did not overtake it and remain queued.
    assert not any(rid.startswith("small") for rid in transport.started)
    assert dispatcher.pending_order()[0] == "small0"


def probe_async_thread_transport():
    section("ThreadTransport: re-entrant callbacks and cancellation races")
    import threading
    import time

    from transport import ThreadTransport

    buckets = {
        "g": Bucket(10, 1, 10, 10),
        "t": Bucket(10, 1, 10, 10),
    }
    gate = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    def handler(request):
        with calls_lock:
            calls.append(request.id)
        gate.wait(2.0)
        return {"status": 200}

    transport = ThreadTransport(handler)
    dispatcher = Dispatcher(buckets, transport)
    for i in range(8):
        dispatcher.submit(Request(f"a{i}", ("g", "t"), 1), now=0)
    started = dispatcher.tick(0)
    dump(
        {
            "started": started,
            "active": len(dispatcher.active),
            "reconcile_while_inflight": dispatcher.ledger.reconcile(),
            "tokens": [str(buckets["g"].tokens), str(buckets["t"].tokens)],
        }
    )
    assert started == 8
    assert len(dispatcher.active) == 8
    assert dispatcher.ledger.reconcile()["consistent"] is True

    # Cancel half while threads are in flight; races the normal response.
    cancelled = [rid for rid in list(dispatcher.active) if int(rid[1:]) % 2 == 0]
    for rid in cancelled:
        dispatcher.cancel(rid, now=1)
    gate.set()
    assert transport.wait_idle(2.0)
    for _ in range(100):
        with dispatcher._lock:
            if not dispatcher.active:
                break
        time.sleep(0.01)

    # Re-entrancy: the callback itself can submit and tick from a worker
    # thread; issue one final tick from the main thread as well.
    dispatcher.submit(Request("late", ("g",), 1), now=2)
    dispatcher.tick(2)
    assert transport.wait_idle(2.0)

    metering = dispatcher.metering(2)
    dump(
        {
            "cancelled": cancelled,
            "states": {
                rid: dispatcher.requests[rid].state
                for rid in sorted(dispatcher.requests)
            },
            "reconcile": dispatcher.ledger.reconcile(),
            "metering_domains": metering["ledger"]["domains"],
            "lifecycle_active": dispatcher.lifecycle()["active"],
        }
    )
    assert dispatcher.ledger.reconcile()["consistent"] is True
    assert buckets["g"].active == 0 and buckets["t"].active == 0
    # 9 sends on "g" and 8 on "t" were charged once, never refunded; two
    # milliseconds of refill at rate 1 add 2 to each bucket.
    assert metering["ledger"]["domains"]["g"]["tokens"] == "3"
    assert metering["ledger"]["domains"]["t"]["tokens"] == "4"
    for rid in cancelled:
        assert dispatcher.requests[rid].outcome == "cancelled"


def probe_queries():
    section("metering and lifecycle are separately queryable")
    buckets = {"g": Bucket(2, 1, 1, 1)}
    transport = ControlledTransport()
    dispatcher = Dispatcher(buckets, transport)
    dispatcher.submit(Request("first", ("g",)), now=0)
    dispatcher.submit(Request("second", ("g",)), now=0)
    dispatcher.tick(0)
    metering = dispatcher.metering(0)
    lifecycle = dispatcher.lifecycle()
    status = dispatcher.status("second", now=0)
    dump(
        {
            "metering_domains": metering["ledger"]["domains"],
            "metering_tickets": metering["ledger"]["tickets"],
            "lifecycle_pending": lifecycle["pending"],
            "lifecycle_active": lifecycle["active"],
            "second_status": status,
        }
    )
    # The queued request can be explained without sending it.
    assert status["admission"]["admitted"] is False
    assert "concurrency" in status["admission"]["reasons"]["g"]
    assert lifecycle["pending"] == ["second"]
    assert lifecycle["active"] == ["first"]
    assert dispatcher.ledger.ticket("first").state == "active"


def main():
    probe_multi_domain_atomic()
    probe_exact_tokens()
    probe_no_partial_on_reject()
    probe_freeze_scope()
    probe_already_sent_survives_freeze()
    probe_cancellation()
    probe_fairness()
    probe_async_thread_transport()
    probe_queries()
    print("\nall probe sections passed")


if __name__ == "__main__":
    main()
