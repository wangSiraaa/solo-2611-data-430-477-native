"""Contract tests for the stable-identity sequence CRDT.

Run with::

    python3 -m unittest -v tests.py

The suite covers the probe scenario plus concurrent same-character inserts,
delete-vs-insert races, endpoint deletion, duplicate/out-of-order/forwarded
packets, missing causal prefixes, atomic rejection with reopen, persistence,
limits, and raw code-point handling.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from annotations import Annotations
from merge import Replica, validate_union
from sequence import MergeError, Sequence, canonical_event
from verifier import IndependentView, verify


def ev(replica, seq, op, context, lamport, **fields):
    return {
        "replica": replica,
        "seq": seq,
        "op": op,
        "context": context,
        "lamport": lamport,
        **fields,
    }


def base_maogou():
    return ev("R", 1, "insert", {}, 1, after=None, text="猫狗")


def concurrent_chars():
    a = ev("A", 1, "insert", {"R": 1}, 2, after="R:1:0", text="甲")
    b = ev("B", 1, "insert", {"R": 1}, 2, after="R:1:0", text="乙")
    return a, b


class ProbeCase(unittest.TestCase):
    def test_probe_order_independent_and_annotation_stable(self):
        s1, s2 = Sequence(), Sequence()
        r1, r2 = Replica(s1), Replica(s2)
        r1.merge([base_maogou()])
        r2.merge([base_maogou()])

        ann = Annotations()
        # Creation-time visible range [1, 2) => exactly the character 狗.
        ann.add("label-dog", 1, 2, "animal")

        a, b = concurrent_chars()
        r1.merge([a, b])
        r2.merge([b, a])

        self.assertEqual(s1.text(), "猫乙甲狗")
        self.assertEqual(s2.text(), s1.text())
        exported = ann.export(s1)[0]
        self.assertEqual(exported["status"], "RESOLVED")
        self.assertEqual(exported["quote"], "狗")
        self.assertEqual(exported["start_id"], "R:1:1")
        self.assertEqual(exported["end_id"], "R:1:1")

    def test_original_integer_annotations_adapter(self):
        sequence = Sequence()
        replica = Replica(sequence)
        replica.merge([base_maogou()])
        ann = Annotations()
        ann.add("label-dog", 1, 2, "animal")  # legacy integer offsets
        a, b = concurrent_chars()
        replica.merge([a, b])
        self.assertEqual(ann.export(sequence)[0]["quote"], "狗")


class ConcurrencyCase(unittest.TestCase):
    def test_concurrent_same_character(self):
        s1, s2 = Sequence(), Sequence()
        r1, r2 = Replica(s1), Replica(s2)
        for replica in (r1, r2):
            replica.merge([base_maogou()])
        a1 = ev("A", 1, "insert", {"R": 1}, 2, after="R:1:0", text="同")
        b1 = ev("B", 1, "insert", {"R": 1}, 2, after="R:1:0", text="同")
        r1.merge([a1, b1])
        r2.merge([b1, a1])
        self.assertEqual(s1.text(), s2.text())
        self.assertEqual(s1.text(), "猫同同狗")
        # Both copies exist with distinct identities.
        self.assertEqual(s1.visible_ids()[1:3], ["B:1:0", "A:1:0"])

    def test_multichar_event_chains_internally(self):
        s = Sequence()
        r = Replica(s)
        r.merge([base_maogou()])
        event = ev("A", 1, "insert", {"R": 1}, 2, after="R:1:0", text="abc")
        r.merge([event])
        self.assertEqual(s.visible_ids(), ["R:1:0", "A:1:0", "A:1:1", "A:1:2", "R:1:1"])
        self.assertEqual(s.text(), "猫abc狗")

    def test_delete_old_char_while_other_inserts_after_it(self):
        s1, s2 = Sequence(), Sequence()
        r1, r2 = Replica(s1), Replica(s2)
        for replica in (r1, r2):
            replica.merge([base_maogou()])
        delete = ev("A", 1, "delete", {"R": 1}, 2, ids=["R:1:1"])
        insert = ev("B", 1, "insert", {"R": 1}, 2, after="R:1:1", text="新")
        r1.merge([delete, insert])
        r2.merge([insert, delete])
        self.assertEqual(s1.text(), "猫新")
        self.assertEqual(s2.text(), "猫新")
        # Deleted character remains in the tree as an anchor.
        self.assertIn("R:1:1", s1.ids())
        self.assertTrue(s1.nodes["R:1:1"]["deleted"])

    def test_delete_does_not_tombstone_later_equal_text(self):
        s1, s2 = Sequence(), Sequence()
        r1, r2 = Replica(s1), Replica(s2)
        for replica in (r1, r2):
            replica.merge([base_maogou()])
        delete = ev("A", 1, "delete", {"R": 1}, 2, ids=["R:1:1"])
        new_dog = ev("B", 1, "insert", {"R": 1}, 2, after="R:1:1", text="狗")
        r1.merge([delete, new_dog])
        r2.merge([new_dog, delete])
        self.assertEqual(s1.text(), s2.text())
        self.assertEqual(s1.text(), "猫狗")
        self.assertTrue(s1.nodes["R:1:1"]["deleted"])
        self.assertFalse(s1.nodes["B:1:0"]["deleted"])
        self.assertIn("B:1:0", s1.visible_ids())
        self.assertNotIn("R:1:1", s1.visible_ids())


class PacketCase(unittest.TestCase):
    def _replicas(self):
        s1, s2 = Sequence(), Sequence()
        r1, r2 = Replica(s1), Replica(s2)
        r1.merge([base_maogou()])
        r2.merge([base_maogou()])
        return r1, r2, s1, s2

    def test_duplicates_are_noop(self):
        r1, r2, s1, s2 = self._replicas()
        a, b = concurrent_chars()
        first = r1.merge([a, b])
        self.assertEqual(first["status"], "accepted")
        second = r1.merge([a, b])
        self.assertEqual(second["status"], "noop")
        self.assertEqual(s1.text(), "猫乙甲狗")
        self.assertEqual(len(r1.events), 3)

    def test_out_of_order_and_indirect_forwarding(self):
        r1, r2, s1, s2 = self._replicas()
        a, b = concurrent_chars()
        # r2 receives B first, then a relay packet containing A on top.
        r2.merge([b])
        r2.merge([a])
        self.assertEqual(s2.text(), r1.merge([a, b]) and s1.text())
        # A third node forwards everything already merged, shuffled.
        s3 = Sequence()
        r3 = Replica(s3)
        r3.merge(r2.event_list()[::-1])
        self.assertEqual(s3.text(), s1.text())

    def test_missing_causal_prefix_rejected_atomically(self):
        r1, r2, s1, s2 = self._replicas()
        a, b = concurrent_chars()
        a2 = ev("A", 2, "insert", {"A": 1, "R": 1}, 3, after="A:1:0", text="后")
        before = r2.export()
        with self.assertRaises(MergeError):
            r2.merge([a2])  # A:1 missing
        self.assertEqual(r2.export(), before)
        # Explicit atomicity check: one bad event poisons the whole packet,
        # so the otherwise valid A and B must not be partially applied.
        bad = ev("B", 2, "insert", {"B": 1, "R": 1}, 3, after="missing:1:0", text="x")
        before = r2.export()
        with self.assertRaises(MergeError):
            r2.merge([a, b, bad])
        self.assertEqual(r2.export(), before)
        self.assertEqual(s2.text(), "猫狗")
        # And the valid prefix applies cleanly once the bad event is removed.
        r2.merge([a, b])
        self.assertEqual(s2.text(), "猫乙甲狗")

    def test_missing_prefix_in_one_of_several_replicas(self):
        r = Replica(Sequence(), replica_id="R")
        r.merge([base_maogou()])
        # A:2 arrives with A:1, but B:1 is referenced and absent.
        a1 = ev("A", 1, "insert", {"R": 1}, 2, after="R:1:0", text="甲")
        a2 = ev("A", 2, "insert", {"A": 1, "R": 1, "B": 1}, 4, after="A:1:0", text="后")
        with self.assertRaises(MergeError):
            r.merge([a1, a2])
        self.assertEqual(r.sequence.text(), "猫狗")

    def test_forwarded_events_keep_identity(self):
        r = Replica(Sequence(), replica_id="R")
        r.merge([base_maogou()])
        a1 = ev("A", 1, "insert", {"R": 1}, 2, after="R:1:0", text="甲")
        # Relay adds its own event on top of A:1 and forwards both.
        c1 = ev("C", 1, "insert", {"R": 1, "A": 1}, 3, after="A:1:0", text="丙")
        r.merge([a1, c1])
        ids_before = list(r.sequence.ids())
        # Re-sending through another order must not renumber anything.
        r.merge([c1, a1])
        self.assertEqual(r.sequence.ids(), ids_before)
        self.assertEqual(r.events[("A", 1)], canonical_event(a1))

    def test_bad_lamport_rejected(self):
        r = Replica(Sequence(), replica_id="R")
        bad = ev("A", 1, "insert", {"R": 1}, 9, after="R:1:0", text="x")
        with self.assertRaises(MergeError):
            r.merge([base_maogou(), bad])

    def test_unseen_anchor_rejected(self):
        r = Replica(Sequence(), replica_id="R")
        # Context claims R:1 but empty context: anchor not covered.
        bad = ev("A", 1, "insert", {}, 1, after="R:1:0", text="x")
        with self.assertRaises(MergeError):
            r.merge([base_maogou(), bad])

    def test_conflicting_same_identity_rejected(self):
        r = Replica(Sequence(), replica_id="R")
        r.merge([base_maogou()])
        conflicting = ev("R", 1, "insert", {}, 1, after=None, text="別")
        with self.assertRaises(MergeError):
            r.merge([conflicting])
        self.assertEqual(r.sequence.text(), "猫狗")

    def test_delete_non_visible_in_context_rejected(self):
        r = Replica(Sequence(), replica_id="R")
        r.merge([base_maogou()])
        first = ev("A", 1, "delete", {"R": 1}, 2, ids=["R:1:1"])
        r.merge([first])
        self.assertEqual(r.sequence.text(), "猫")
        # B's context now sees that R:1:1 is already tombstoned, so naming it
        # again violates "delete only names characters visible in context".
        second = ev("B", 1, "delete", {"R": 1, "A": 1}, 3, ids=["R:1:1"])
        with self.assertRaises(MergeError):
            r.merge([second])


class AnnotationCase(unittest.TestCase):
    def test_endpoint_deleted_is_unresolved_and_kept(self):
        s = Sequence()
        r = Replica(s, replica_id="A")
        r.merge([base_maogou()])
        r.annotate(1, 2, "animal", annotation_id="label-dog")
        dog = "R:1:1"
        r.delete([dog])
        exported = Annotations.from_events(r.events.values()).export(s)
        self.assertEqual(len(exported), 1)
        item = exported[0]
        self.assertEqual(item["status"], "UNRESOLVED")
        self.assertEqual(item["start_id"], dog)
        self.assertEqual(item["end_id"], dog)
        self.assertEqual(item["quote"], "狗")
        training = Annotations.from_events(r.events.values()).training_data(s)
        self.assertEqual(training, [])
        unresolved = Annotations.from_events(r.events.values()).unresolved(s)
        self.assertEqual(len(unresolved), 1)

    def test_concurrent_insert_between_endpoints_is_included(self):
        s1, s2 = Sequence(), Sequence()
        r1, r2 = Replica(s1, replica_id="A"), Replica(s2, replica_id="B")
        r1.merge([base_maogou()])
        r2.merge([base_maogou()])
        # Annotate the whole visible text using anchored endpoints.
        r1.annotate(0, 2, "all", annotation_id="all")
        b = ev("B", 1, "insert", {"R": 1}, 2, after="R:1:0", text="X")
        r1.merge([b])
        exported = Annotations.from_events(r1.events.values()).export(s1)[0]
        self.assertEqual(exported["status"], "RESOLVED")
        self.assertEqual(exported["quote"], "猫X狗")
        self.assertEqual(exported["ids"], ["R:1:0", "B:1:0", "R:1:1"])

    def test_annotation_is_not_silently_retargeted(self):
        s = Sequence()
        r = Replica(s, replica_id="A")
        r.merge([base_maogou()])
        r.annotate(1, 2, "animal", annotation_id="label-dog")
        # Insert before the dog: integer offset 1 would now mean something
        # else, but the anchored id must still resolve to the dog.
        r.insert("R:1:0", "XX")
        exported = Annotations.from_events(r.events.values()).export(s)[0]
        self.assertEqual(exported["quote"], "狗")
        self.assertEqual(exported["start_id"], "R:1:1")

    def test_every_annotation_has_a_destination(self):
        s = Sequence()
        r = Replica(s, replica_id="A")
        r.merge([base_maogou()])
        r.annotate(0, 1, "cat", annotation_id="cat")
        r.annotate(1, 2, "dog", annotation_id="dog")
        r.delete(["R:1:1"])
        ann = Annotations.from_events(r.events.values())
        everything = ann.export(s)
        resolved = ann.resolved(s)
        unresolved = ann.unresolved(s)
        self.assertEqual(len(everything), 2)
        self.assertEqual({i["id"] for i in resolved} | {i["id"] for i in unresolved},
                         {i["id"] for i in everything})
        self.assertEqual({i["id"] for i in resolved} & {i["id"] for i in unresolved}, set())

    def test_conflicting_annotation_ids_rejected(self):
        r1, r2 = Replica(Sequence(), replica_id="A"), Replica(Sequence(), replica_id="B")
        r1.merge([base_maogou()])
        r2.merge([base_maogou()])
        # Concurrent annotations reuse the same id with different content.
        r1.annotate(0, 1, "cat", annotation_id="shared")
        r2.annotate(1, 2, "dog", annotation_id="shared")
        with self.assertRaises(MergeError):
            r1.merge(r2.export_delta())
        self.assertEqual(r1.sequence.text(), "猫狗")


class CodePointCase(unittest.TestCase):
    def test_raw_codepoints_not_normalised(self):
        text = "é👍👨‍👩"  # combining acute + emoji + ZWJ sequence
        r = Replica(Sequence(), replica_id="A")
        r.insert(None, text)
        self.assertEqual(r.sequence.text(), text)
        self.assertEqual(len(r.sequence.text()), len(text))
        # Annotate only the combining mark: code-point contract, not graphemes.
        r.annotate(1, 2, "mark", annotation_id="mark")
        exported = Annotations.from_events(r.events.values()).export(r.sequence)[0]
        self.assertEqual(exported["quote"], "́")

    def test_visible_limit(self):
        r = Replica(Sequence(), replica_id="A")
        r.insert(None, "x" * 2000)
        with self.assertRaises(MergeError):
            # One local event, but would exceed the visible limit.
            event = ev("A", 2, "insert", {"A": 1}, 2, after="A:1:1999", text="y")
            r.merge([event])

    def test_event_limit(self):
        r = Replica(Sequence(), replica_id="A")
        events = []
        for seq in range(1, 501):
            context = {"A": seq - 1} if seq > 1 else {}
            events.append(
                ev("A", seq, "insert", context, seq, after=None, text=str(seq)[-1])
            )
        # Anchoring every insert at the root is legal ordering-wise.
        validate_union({(e["replica"], e["seq"]): canonical_event(e) for e in events})
        events.append(ev("A", 501, "insert", {"A": 500}, 501, after=None, text="z"))
        with self.assertRaises(MergeError):
            r.merge(events[-1:])


class PersistenceCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crdt-test-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_reopen_reproduces_state_after_rejection(self):
        path = os.path.join(self.dir, "a.json")
        r = Replica(replica_id="A", storage_path=path)
        r.merge([base_maogou()])
        r.annotate(1, 2, "animal", annotation_id="label-dog")
        good = ev("B", 1, "insert", {"R": 1}, 2, after="R:1:0", text="乙")
        r.merge([good])
        snapshot = r.export()
        with open(path, encoding="utf-8") as handle:
            state_on_disk = handle.read()

        bad = ev("B", 2, "insert", {"B": 1, "R": 1}, 3, after="A:1:0", text="x")
        with self.assertRaises(MergeError):
            r.merge([bad])
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), state_on_disk)

        reopened = Replica(storage_path=path)
        self.assertEqual(reopened.export(), snapshot)
        self.assertEqual(reopened.sequence.text(), "猫乙狗")

    def test_reopen_after_rejected_packet_is_not_partially_applied(self):
        path = os.path.join(self.dir, "b.json")
        r = Replica(replica_id="A", storage_path=path)
        r.merge([base_maogou()])
        before = r.export()
        a, b = concurrent_chars()
        bad = ev("B", 2, "insert", {"B": 1, "R": 1}, 3, after="ghost:1:0", text="x")
        with self.assertRaises(MergeError):
            r.merge([a, bad, b])
        # Directly reopen: the valid events in the rejected packet must not
        # have leaked to disk.
        reopened = Replica(storage_path=path)
        self.assertEqual(reopened.export(), before)
        self.assertNotIn("A:1", [f"{e['replica']}:{e['seq']}" for e in reopened.event_list()])
        self.assertNotIn("B:1", [f"{e['replica']}:{e['seq']}" for e in reopened.event_list()])

    def test_shuffled_full_dag_converges_on_disk(self):
        path = os.path.join(self.dir, "c.json")
        source = Replica(Sequence(), replica_id="A")
        source.insert(None, "猫狗")
        source.insert("A:1:0", "甲")
        b = ev("B", 1, "insert", {"A": 2}, 3, after="A:1:0", text="乙")
        source.merge([b])
        events = source.event_list()
        target = Replica(replica_id="C", storage_path=path)
        target.merge(list(reversed(events)))
        self.assertEqual(target.sequence.text(), source.sequence.text())
        reopened = Replica(storage_path=path)
        self.assertEqual(reopened.sequence.text(), source.sequence.text())
        self.assertEqual(reopened.version(), source.version())

    def test_reopen_after_accept(self):
        path = os.path.join(self.dir, "a.json")
        r = Replica(replica_id="A", storage_path=path)
        r.insert(None, "猫狗")
        r.annotate(1, 2, "animal", annotation_id="label-dog")
        r.insert("A:1:0", "甲")
        reopened = Replica(storage_path=path)
        self.assertEqual(reopened.sequence.text(), r.sequence.text())
        self.assertEqual(reopened.version(), r.version())
        self.assertEqual(len(reopened.receipts), 0)  # local ops, no merge receipt
        self.assertEqual(
            Annotations.from_events(reopened.events.values()).export(reopened.sequence),
            Annotations.from_events(r.events.values()).export(r.sequence),
        )

    def test_replica_mismatch_rejected(self):
        path = os.path.join(self.dir, "a.json")
        Replica(replica_id="A", storage_path=path)
        with self.assertRaises(MergeError):
            Replica(replica_id="B", storage_path=path)


class IndependentVerifierCase(unittest.TestCase):
    def _random_dag(self):
        s1, s2 = Sequence(), Sequence()
        r1, r2 = Replica(s1, replica_id="A"), Replica(s2, replica_id="B")
        r1.insert(None, "猫狗")
        r2.merge(r1.export_delta())
        r1.insert("A:1:0", "甲")
        r2.insert("A:1:0", "乙")
        r2.insert("A:1:1", "尾")
        r1.merge(r2.export_delta())
        r2.merge(r1.export_delta())
        return r1, s1

    def test_independent_interpreter_matches(self):
        r1, s1 = self._random_dag()
        events = r1.event_list()
        independent = IndependentView(events)
        self.assertEqual(independent.text(), s1.text())
        self.assertEqual(independent.visible_ids(), s1.visible_ids())
        ann = Annotations.from_events(events)
        report = verify(events, ann.export(s1))
        for span in report["spans"]:
            self.assertTrue(span["matches_independent"], span)
            self.assertEqual(span["independent_quote"], "".join(
                s1.nodes[cid]["char"]
                for cid in ann.export(s1)[0]["ids"]
            ) if ann.export(s1) else "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
