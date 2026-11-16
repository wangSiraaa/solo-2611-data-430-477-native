"""Tests for the multi-domain atomic admission scheduler.

Run with ``python3 -m unittest -v test_scheduler``.
"""

from __future__ import annotations

import random
import threading
import time
import unittest
from fractions import Fraction

from dispatcher import Dispatcher, Request
from limits import Bucket, QuotaLedger
from transport import ControlledTransport, ThreadTransport


class SyncTransport:
    """Completes inside start(): exercises callback re-entrancy during tick."""

    def __init__(self, responder):
        self.responder = responder
        self.started = []
        self.lock = threading.Lock()

    def start(self, request, callback):
        with self.lock:
            self.started.append(request.id)
        callback(self.responder(request))

    def cancel(self, request_id):
        return False


class LedgerTests(unittest.TestCase):
    def test_virtual_clock_exact_refill_and_charge(self):
        bucket = Bucket(5, Fraction(1, 3), 10, 0)
        ledger = QuotaLedger({"d": bucket})
        seen = []
        for now in (0, 1, 2, 3, 6, 9, 15, 16):
            bucket.refill(now)
            seen.append(bucket.tokens)
        self.assertEqual(
            seen,
            [
                Fraction(0),
                Fraction(1, 3),
                Fraction(2, 3),
                Fraction(1),
                Fraction(2),
                Fraction(3),
                Fraction(5),  # capped at capacity
                Fraction(5),
            ],
        )

        self.assertTrue(ledger.admit("a", ("d",), 2, 16).admitted)
        self.assertEqual(bucket.tokens, 3)
        ledger.settle("a", 17)
        self.assertEqual(bucket.tokens, 3)  # no refund on settle
        self.assertTrue(ledger.admit("b", ("d",), 3, 17).admitted)
        self.assertEqual(bucket.tokens, Fraction(1, 3))
        self.assertFalse(ledger.admit("c", ("d",), 3, 17).admitted)

    def test_reject_reserves_nothing(self):
        buckets = {"a": Bucket(5, 1, 5, 5), "b": Bucket(5, 1, 5, 0)}
        ledger = QuotaLedger(buckets)
        decision = ledger.admit("x", ("a", "b"), 1, 0)
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.reasons, {"b": ("tokens",)})
        self.assertEqual(buckets["a"].active, 0)
        self.assertEqual(buckets["a"].tokens, 5)
        self.assertEqual(buckets["b"].active, 0)
        self.assertIsNone(ledger.ticket("x"))

    def test_atomic_admission_charges_all_domains(self):
        buckets = {
            "g": Bucket(10, 1, 5, 10),
            "t": Bucket(3, 1, 5, 2),
        }
        ledger = QuotaLedger(buckets)
        self.assertTrue(ledger.admit("r", ("g", "t"), 2, 0).admitted)
        self.assertEqual(buckets["g"].tokens, 8)
        self.assertEqual(buckets["t"].tokens, 0)
        self.assertEqual(buckets["g"].active, 1)
        self.assertEqual(buckets["t"].active, 1)
        ticket = ledger.ticket("r")
        self.assertEqual(ticket.domains, ("g", "t"))
        self.assertEqual(ticket.cost, 2)
        self.assertFalse(ticket.terminal)

    def test_settle_is_exactly_once(self):
        buckets = {"g": Bucket(5, 1, 1, 5)}
        ledger = QuotaLedger(buckets)
        ledger.admit("r", ("g",), 1, 0)
        first = ledger.settle("r", 1, "completed")
        second = ledger.settle("r", 2, "cancelled")
        self.assertEqual(first.outcome, "completed")
        self.assertEqual(second.outcome, "completed")  # first terminal wins
        self.assertEqual(buckets["g"].active, 0)
        self.assertEqual(buckets["g"].tokens, 4)
        self.assertTrue(ledger.reconcile()["consistent"])
        self.assertTrue(any(h[0] == "settle-duplicate" for h in ledger.history))

    def test_validation(self):
        with self.assertRaises(ValueError):
            Bucket(21, 1, 1)
        with self.assertRaises(ValueError):
            Bucket(0, 1, 1)
        with self.assertRaises(ValueError):
            Bucket(2, 0, 1)  # non-positive refill
        buckets = {"g": Bucket(2, 1, 2, 2)}
        ledger = QuotaLedger(buckets)
        with self.assertRaises(ValueError):
            ledger.admit("dup", ("g", "g"), 1, 0)
        with self.assertRaises(ValueError):
            ledger.admit("big", ("g",), 3, 0)  # cost > capacity
        with self.assertRaises(ValueError):
            ledger.admit("neg", ("g",), -1, 0)
        with self.assertRaises(ValueError):
            ledger.admit("unknown", ("nope",), 1, 0)
        with self.assertRaises(TypeError):
            ledger.admit("float", ("g",), 1.5, 0)
        with self.assertRaises(TypeError):
            ledger.admit("clock", ("g",), 1, 1.5)
        ledger.admit("ok", ("g",), 1, 0)
        with self.assertRaises(ValueError):
            ledger.admit("ok", ("g",), 1, 0)  # duplicate admission

    def test_domain_never_frozen_by_other_scope(self):
        buckets = {"a": Bucket(5, 1, 5, 5), "b": Bucket(5, 1, 5, 5)}
        ledger = QuotaLedger(buckets)
        ledger.throttle("a", 100, 0)
        self.assertTrue(ledger.check(("b",), 1, 0).admitted)
        blocked = ledger.check(("a", "b"), 1, 0)
        self.assertFalse(blocked.admitted)
        self.assertEqual(blocked.reasons, {"a": ("frozen",)})
        self.assertEqual(blocked.next_times["a"], 100)
        self.assertTrue(ledger.check(("a",), 1, 100).admitted)


class DispatcherTests(unittest.TestCase):
    def test_multi_domain_shared_and_unrelated_progress(self):
        buckets = {
            "global": Bucket(10, 1, 4, 10),
            "tenant": Bucket(2, 1, 4, 1),
            "unrelated": Bucket(10, 1, 4, 10),
        }
        transport = ControlledTransport()
        dispatcher = Dispatcher(buckets, transport)
        dispatcher.submit(Request("a", ("global", "tenant"), 2), now=0)
        dispatcher.submit(Request("b", ("unrelated",), 1), now=0)
        self.assertEqual(dispatcher.tick(0), 1)
        self.assertEqual(transport.started, ["b"])  # disjoint request went out
        self.assertEqual(buckets["global"].tokens, 10)  # no partial charge
        self.assertEqual(buckets["tenant"].tokens, 1)

        self.assertEqual(dispatcher.tick(1), 1)  # tenant now has 2
        self.assertIn("a", transport.started)
        ticket = dispatcher.ledger.ticket("a")
        self.assertEqual(ticket.domains, ("global", "tenant"))
        self.assertEqual(buckets["global"].tokens, 8)
        self.assertEqual(buckets["tenant"].tokens, 0)

    def test_freeze_scope(self):
        buckets = {
            "g": Bucket(5, 1, 5, 5),
            "t": Bucket(5, 1, 5, 5),
            "o": Bucket(5, 1, 5, 5),
        }
        transport = ControlledTransport()
        dispatcher = Dispatcher(buckets, transport)
        dispatcher.submit(Request("first", ("g", "t")), now=0)
        dispatcher.tick(0)
        transport.finish("first", {"status": 429, "scope": "t", "retry_after": 30})
        self.assertEqual(buckets["t"].frozen_until, 30)
        self.assertEqual(buckets["g"].frozen_until, 0)

        dispatcher.submit(Request("blocked", ("g", "t")), now=5)
        dispatcher.submit(Request("free", ("o",)), now=5)
        self.assertEqual(dispatcher.tick(5), 1)
        self.assertIn("free", transport.started)
        self.assertNotIn("blocked", transport.started)
        self.assertEqual(dispatcher.tick(29), 0)
        self.assertEqual(dispatcher.tick(30), 1)
        self.assertIn("blocked", transport.started)

    def test_sent_request_not_withdrawn_by_freeze(self):
        buckets = {"a": Bucket(5, 1, 1, 5), "b": Bucket(5, 1, 1, 5)}
        transport = ControlledTransport()
        dispatcher = Dispatcher(buckets, transport)
        dispatcher.submit(Request("live", ("a", "b")), now=0)
        dispatcher.submit(Request("next", ("a", "b")), now=0)
        dispatcher.tick(0)
        transport.finish("live", {"status": 429, "scope": "a", "retry_after": 20})
        self.assertEqual(dispatcher.requests["live"].state, "throttled")
        self.assertEqual(dispatcher.ledger.ticket("live").state, "settled")
        self.assertEqual(dispatcher.requests["next"].state, "queued")
        self.assertEqual(buckets["a"].active, 0)  # slot released once

    def test_cancel_queued_costs_no_token(self):
        buckets = {"g": Bucket(1, 1, 1, 1)}
        transport = ControlledTransport()
        dispatcher = Dispatcher(buckets, transport)
        dispatcher.submit(Request("run", ("g",)), now=0)
        dispatcher.submit(Request("queued", ("g",)), now=0)
        dispatcher.tick(0)
        self.assertEqual(buckets["g"].tokens, 0)
        self.assertTrue(dispatcher.cancel("queued", now=0))
        self.assertIsNone(dispatcher.ledger.ticket("queued"))
        self.assertEqual(buckets["g"].tokens, 0)
        self.assertEqual(dispatcher.requests["queued"].state, "cancelled")

    def test_cancel_after_send_races_response(self):
        buckets = {"g": Bucket(5, 1, 1, 5)}
        transport = ControlledTransport()
        dispatcher = Dispatcher(buckets, transport)
        dispatcher.submit(Request("r", ("g",)), now=0)
        dispatcher.tick(0)
        self.assertTrue(dispatcher.cancel("r", now=1))
        self.assertFalse(transport.finish("r", {"status": 200}))  # late
        self.assertEqual(buckets["g"].active, 0)
        self.assertEqual(buckets["g"].tokens, 4)
        self.assertTrue(dispatcher.ledger.reconcile()["consistent"])
        # The reverse order: response first, then cancel is a no-op.
        dispatcher.submit(Request("r2", ("g",)), now=2)
        dispatcher.tick(2)
        self.assertTrue(transport.finish("r2", {"status": 200}))
        self.assertFalse(dispatcher.cancel("r2", now=3))
        self.assertEqual(buckets["g"].active, 0)
        self.assertEqual(buckets["g"].tokens, 4)

    def test_fairness_no_starvation(self):
        # Capacity 4, one token per ms.  A cost-3 request arrives first; small
        # cost-1 requests keep arriving on every tick they could otherwise
        # use to overtake it.
        buckets = {"s": Bucket(4, 1, 10, 0)}
        transport = ControlledTransport()
        dispatcher = Dispatcher(buckets, transport)
        dispatcher.submit(Request("big", ("s",), 3), now=0)
        dispatcher.submit(Request("small0", ("s",), 1), now=0)
        for now in range(1, 20):
            dispatcher.submit(Request(f"n{now}", ("s",), 1), now=now)
            dispatcher.tick(now)
            if dispatcher.requests["big"].state != "queued":
                break
        self.assertEqual(dispatcher.requests["big"].state, "active")
        self.assertEqual(dispatcher.requests["big"].admitted_at, 3)
        # No overlapping small request was sent before the big one.
        self.assertEqual(transport.started[0], "big")

    def test_priority_order_within_queue(self):
        buckets = {"g": Bucket(3, 1, 1, 0)}
        transport = ControlledTransport()
        dispatcher = Dispatcher(buckets, transport)
        dispatcher.submit(Request("low", ("g",), 1, priority=0), now=0)
        dispatcher.submit(Request("high", ("g",), 1, priority=5), now=0)
        self.assertEqual(dispatcher.tick(1), 1)
        self.assertEqual(transport.started, ["high"])
        self.assertEqual(dispatcher.lifecycle()["pending"], ["low"])

    def test_synchronous_completion_reentrancy(self):
        # The transport calls back inside start(), during the dispatcher tick.
        buckets = {"g": Bucket(10, 1, 2, 10)}
        transport = SyncTransport(lambda request: {"status": 200})
        dispatcher = Dispatcher(buckets, transport)
        for i in range(6):
            dispatcher.submit(Request(f"r{i}", ("g",), 1), now=0)
        sent = dispatcher.tick(0)
        self.assertEqual(sent, 6)
        self.assertEqual(len(transport.started), 6)
        self.assertEqual(buckets["g"].active, 0)
        self.assertEqual(buckets["g"].tokens, 4)
        self.assertTrue(dispatcher.ledger.reconcile()["consistent"])
        self.assertTrue(all(
            dispatcher.requests[f"r{i}"].state == "completed" for i in range(6)
        ))

    def test_synchronous_failure_is_terminal(self):
        class Failing(SyncTransport):
            def start(self, request, callback):
                self.started.append(request.id)
                raise RuntimeError("boom")

        buckets = {"g": Bucket(5, 1, 5, 5)}
        transport = Failing(lambda request: {})
        dispatcher = Dispatcher(buckets, transport)
        dispatcher.submit(Request("r", ("g",)), now=0)
        dispatcher.tick(0)
        self.assertEqual(dispatcher.requests["r"].outcome, "failed")
        self.assertEqual(buckets["g"].active, 0)
        self.assertEqual(buckets["g"].tokens, 4)
        self.assertTrue(dispatcher.ledger.reconcile()["consistent"])

    def test_query_surfaces(self):
        buckets = {"g": Bucket(2, 1, 1, 1)}
        transport = ControlledTransport()
        dispatcher = Dispatcher(buckets, transport)
        dispatcher.submit(Request("first", ("g",)), now=0)
        dispatcher.submit(Request("second", ("g",)), now=0)
        dispatcher.tick(0)
        metering = dispatcher.metering(0)
        lifecycle = dispatcher.lifecycle()
        status = dispatcher.status("second", now=0)
        self.assertEqual(lifecycle["active"], ["first"])
        self.assertEqual(lifecycle["pending"], ["second"])
        self.assertEqual(metering["ledger"]["domains"]["g"]["active"], 1)
        self.assertEqual(metering["ledger"]["tickets"][0]["request_id"], "first")
        self.assertFalse(status["admission"]["admitted"])
        self.assertIn("concurrency", status["admission"]["reasons"]["g"])
        self.assertIsNone(status["ticket"])


class ThreadTransportTests(unittest.TestCase):
    def wait_until(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def test_async_completion_and_cancel_race(self):
        for trial in range(10):
            buckets = {
                "g": Bucket(20, 1, 20, 20),
                "t": Bucket(20, 1, 20, 20),
            }
            rng = random.Random(trial)

            def handler(request, rng=rng):
                time.sleep(rng.random() * 0.002)
                if request.id.endswith("err"):
                    raise ValueError("synthetic")
                return {"status": 200}

            transport = ThreadTransport(handler)
            dispatcher = Dispatcher(buckets, transport)
            ids = [f"r{i}" for i in range(12)]
            for rid in ids:
                dispatcher.submit(Request(rid, ("g", "t")), now=0)
            self.assertEqual(dispatcher.tick(0), 12)

            cancelled = set(rng.sample(ids, 5))
            for rid in cancelled:
                dispatcher.cancel(rid, now=1)
            self.assertTrue(transport.wait_idle(3.0))
            self.assertTrue(self.wait_until(lambda: not dispatcher.active))

            self.assertTrue(dispatcher.ledger.reconcile()["consistent"])
            self.assertEqual(buckets["g"].active, 0)
            self.assertEqual(buckets["t"].active, 0)
            # Every send charged exactly one token in each domain.
            self.assertEqual(buckets["g"].tokens, 8)
            self.assertEqual(buckets["t"].tokens, 8)
            for rid in ids:
                ticket = dispatcher.ledger.ticket(rid)
                self.assertIsNotNone(ticket)
                self.assertTrue(ticket.terminal)
                self.assertIn(ticket.outcome, {"completed", "cancelled"})
            for rid in cancelled:
                # A post-send cancellation races the normal response; either
                # terminal outcome is legal, but it must be settled once.
                self.assertIn(
                    dispatcher.ledger.ticket(rid).outcome,
                    {"completed", "cancelled"},
                )

    def test_concurrent_cancel_and_finish_double_release(self):
        for trial in range(10):
            buckets = {"g": Bucket(20, 1, 20, 20)}
            gate = threading.Event()

            def handler(request):
                gate.wait(2.0)
                return {"status": 200}

            transport = ThreadTransport(handler)
            dispatcher = Dispatcher(buckets, transport)
            n = 20
            for i in range(n):
                dispatcher.submit(Request(f"r{i}", ("g",)), now=0)
            self.assertEqual(dispatcher.tick(0), n)

            barrier = threading.Barrier(2 * n)

            def do_cancel(rid):
                barrier.wait()
                dispatcher.cancel(rid, now=1)

            def do_finish(rid):
                barrier.wait()
                transport.finish(rid, {"status": 200})

            threads = []
            for i in range(n):
                rid = f"r{i}"
                threads.append(threading.Thread(target=do_cancel, args=(rid,)))
                threads.append(threading.Thread(target=do_finish, args=(rid,)))
            for thread in threads:
                thread.start()
            gate.set()
            for thread in threads:
                thread.join(3.0)
            self.assertTrue(transport.wait_idle(3.0))
            self.assertTrue(self.wait_until(lambda: not dispatcher.active))

            self.assertTrue(dispatcher.ledger.reconcile()["consistent"])
            self.assertEqual(buckets["g"].active, 0)
            self.assertEqual(buckets["g"].tokens, 0)  # 20 charged, none refunded
            for i in range(n):
                ticket = dispatcher.ledger.ticket(f"r{i}")
                self.assertEqual(ticket.state, "settled")
                self.assertIn(ticket.outcome, {"completed", "cancelled"})
            # Every ticket settled at most once.
            settled = [h for h in dispatcher.ledger.history if h[0] == "settled"]
            self.assertEqual(len(settled), n)

    def test_errors_are_collected(self):
        buckets = {"g": Bucket(5, 1, 5, 5)}
        transport = ThreadTransport(lambda request: 1 / 0)
        dispatcher = Dispatcher(buckets, transport)
        dispatcher.submit(Request("bad", ("g",)), now=0)
        dispatcher.tick(0)
        self.assertTrue(transport.wait_idle(3.0))
        self.assertTrue(self.wait_until(lambda: not dispatcher.active))
        self.assertEqual(dispatcher.requests["bad"].state, "failed")
        self.assertEqual(buckets["g"].active, 0)
        self.assertEqual(buckets["g"].tokens, 4)
        self.assertTrue(dispatcher.ledger.reconcile()["consistent"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
