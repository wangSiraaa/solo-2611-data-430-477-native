"""End-to-end verification against the independent full-sort oracle.

Coverage required by the task:

* partial states produced by *independent OS processes* (subprocess), then
  re-opened and merged in this process - genuine cross-process exchange;
* different arrival orders, shard groupings and merge trees must yield the
  identical final order, checked against oracle.oracle on all original rows;
* duplicate text across shards, NULL/equal-key ordering, empty shards,
  Chinese values fitting exactly / missing by one byte, conflicting re-sends;
* idempotent identical re-send, same-chunk/different-content rejection,
  cross-chunk id overlap rejection, manifest digest mismatch rejection;
* external distinct spill on a group far larger than the 128 KiB budget;
* the 128 KiB payload budget is enforced (no silent overrun).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from finalizer import finalize
from gen_demo import generate as gen_demo_dataset
from meter import Meter, PAYLOAD_BUDGET
from ordered_state import partial, read_header
from oracle import canonical_digest, oracle
from run_pool import (ChunkConflict, ManifestConflict, OverlapConflict, Pool,
                      load_manifest)

HERE = Path(__file__).parent


def run(cmd: list[str]) -> str:
    p = subprocess.run([sys.executable, *cmd], cwd=HERE, check=True,
                       capture_output=True, text=True)
    return p.stdout


class OrderingCore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rows = json.loads((HERE / "rows.json").read_text())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pool_from_partitions(self, partitions, order=None, tree=False,
                              manifest=None):
        states = []
        for name, part in partitions.items():
            p = f"{self.tmp}/{name}.state"
            partial(part, name, p)
            states.append(p)
        if order is not None:
            states = [states[i] for i in order]
        pool = Pool(f"{self.tmp}/pool-{uuid()}", meter=Meter())
        if tree:
            # two-level tree: merge halves into separate pools, then combine
            left = Pool(f"{self.tmp}/L-{uuid()}", meter=Meter())
            right = Pool(f"{self.tmp}/R-{uuid()}", meter=Meter())
            half = len(states) // 2 or 1
            left.merge(states[:half])
            right.merge(states[half:])
            for sp in (left, right):
                pool.merge_pool(sp.dir)
                sp.close()
        else:
            pool.merge(states, manifest)
        return pool

    def test_arrival_order_invariance(self):
        parts = {"a": self.rows[:2], "b": self.rows[2:]}
        expected = oracle(self.rows)
        texts = set()
        for order in ([0, 1], [1, 0]):
            pool = self._pool_from_partitions(parts, order=order)
            got = {r["group"]: r for r in finalize(pool)}
            pool.close()
            self.assertEqual(got, expected)
            texts.add(next(iter(got.values()))["text"])
        self.assertEqual(texts, {"甲|乙|丙|丁"})

    def test_merge_tree_invariance(self):
        # four shards, flat merge vs two-level pool-of-pools
        quads = {"q0": self.rows[0:1], "q1": self.rows[1:2],
                 "q2": self.rows[2:3], "q3": self.rows[3:4]}
        flat = self._pool_from_partitions(quads, order=[3, 1, 2, 0])
        tree = self._pool_from_partitions(quads, tree=True)
        a = {r["group"]: r for r in finalize(flat)}
        b = {r["group"]: r for r in finalize(tree)}
        self.assertEqual(a, b)
        self.assertEqual(a, oracle(self.rows))
        flat.close()
        tree.close()

    def test_shard_regroup_invariance(self):
        p1 = {"x": self.rows}
        p2 = {"x": self.rows[::2], "y": self.rows[1::2]}
        p3 = {"x": [self.rows[3]], "y": [self.rows[1]],
              "z": [self.rows[0], self.rows[2]]}
        ref = oracle(self.rows)
        for parts in (p1, p2, p3):
            pool = self._pool_from_partitions(parts)
            self.assertEqual({r["group"]: r for r in finalize(pool)}, ref)
            pool.close()

    def test_cross_process_state_exchange(self):
        # partial runs in separate OS processes; this process only merges.
        s1 = f"{self.tmp}/p1.state"
        s2 = f"{self.tmp}/p2.state"
        f1 = f"{self.tmp}/p1.jsonl"
        f2 = f"{self.tmp}/p2.jsonl"
        Path(f1).write_text("\n".join(
            json.dumps(r, ensure_ascii=False) for r in self.rows[:2]))
        Path(f2).write_text("\n".join(
            json.dumps(r, ensure_ascii=False) for r in self.rows[2:]))
        run(["ordered_state.py", f1, "proc-a", s1])
        run(["ordered_state.py", f2, "proc-b", s2])
        # re-open in this process and verify digest matches the oracle digest
        self.assertEqual(read_header(s1)["digest"],
                         canonical_digest(self.rows[:2]))
        self.assertEqual(read_header(s2)["digest"],
                         canonical_digest(self.rows[2:]))
        pool = Pool(f"{self.tmp}/xpool", meter=Meter())
        pool.merge([s2, s1])  # reversed arrival
        self.assertEqual({r["group"]: r for r in finalize(pool)},
                         oracle(self.rows))
        pool.close()


class NullAndRepeats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_null_last_and_key_tie_by_id(self):
        rows = [
            {"group": "g", "key": None, "id": 9, "value": "null9"},
            {"group": "g", "key": 5, "id": 3, "value": "k5-id3"},
            {"group": "g", "key": 5, "id": 1, "value": "k5-id1"},
            {"group": "g", "key": 2, "id": 7, "value": "k2"},
        ]
        ref = oracle(rows)
        d1 = f"{self.tmp}/d1.jsonl"
        d2 = f"{self.tmp}/d2.jsonl"
        Path(d1).write_text(json.dumps(rows[0], ensure_ascii=False) + "\n"
                            + json.dumps(rows[3], ensure_ascii=False))
        Path(d2).write_text(json.dumps(rows[1], ensure_ascii=False) + "\n"
                            + json.dumps(rows[2], ensure_ascii=False))
        s1, s2 = f"{self.tmp}/a.state", f"{self.tmp}/b.state"
        run(["ordered_state.py", d1, "a", s1])
        run(["ordered_state.py", d2, "b", s2])
        pool = Pool(f"{self.tmp}/p", meter=Meter())
        pool.merge([s2, s1])
        got = list(finalize(pool))
        self.assertEqual(got[0]["text"],
                         "k2|k5-id1|k5-id3|null9")
        self.assertEqual(got[0], ref["g"])
        pool.close()

    def test_duplicate_text_distinct_ids_is_distinct_occurrences(self):
        rows = [{"group": "g", "key": i, "id": 100 + i, "value": "same"}
                for i in range(4)]
        rows[2]["id"] = 500  # shard boundary on equal text
        a, b = rows[:2], rows[2:]
        sa = f"{self.tmp}/a.state"
        sb = f"{self.tmp}/b.state"
        partial(a, "a", sa)
        partial(b, "b", sb)
        pool = Pool(f"{self.tmp}/p", meter=Meter())
        pool.merge([sb, sa])
        normal = list(finalize(pool))[0]
        distinct = list(finalize(pool, distinct=True))[0]
        self.assertEqual(normal["text"], "same|same|same|same")
        self.assertEqual(normal["omitted"], 0)
        self.assertEqual(distinct["text"], "same")
        self.assertEqual(distinct["omitted"], 0)
        self.assertEqual(distinct["occurrences"], 4)
        self.assertEqual(distinct["distinct_values"], 1)
        pool.close()


class ByteLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rows = [{"group": "g", "key": i, "id": i + 1,
                      "value": v} for i, v in enumerate(["甲", "乙", "丙"])]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pool(self):
        s = f"{self.tmp}/s.state"
        partial(self.rows, "c", s)
        pool = Pool(f"{self.tmp}/p", meter=Meter())
        pool.merge([s])
        return pool

    def test_chinese_exact_fit(self):
        # 3 chars * 3 bytes + 2 separators = 11 bytes
        pool = self._pool()
        r = list(finalize(pool, limit=11))[0]
        self.assertEqual(r["text"], "甲|乙|丙")
        self.assertEqual(r["omitted"], 0)
        pool.close()

    def test_chinese_one_byte_short_drops_whole_value(self):
        pool = self._pool()
        r = list(finalize(pool, limit=10))[0]
        self.assertEqual(r["text"], "甲|乙")
        self.assertEqual(r["omitted"], 1)  # one occurrence, not one byte
        pool.close()

    def test_five_cjk_exact_vs_one_byte_short(self):
        # five 3-byte values joined: 5*3 + 4 separators = 19 bytes exactly
        rows = [{"group": "g", "key": i, "id": i + 1, "value": v}
                for i, v in enumerate(["甲", "乙", "丙", "丁", "戊"])]
        s = f"{self.tmp}/cjk.state"
        partial(rows, "c", s)
        pool = Pool(f"{self.tmp}/cjkp", meter=Meter())
        pool.merge([s])
        fit = list(finalize(pool, limit=19))[0]
        short = list(finalize(pool, limit=18))[0]
        self.assertEqual(fit["text"], "甲|乙|丙|丁|戊")
        self.assertEqual(fit["omitted"], 0)
        self.assertEqual(short["text"], "甲|乙|丙|丁")
        self.assertEqual(short["omitted"], 1)
        self.assertNotIn("|", fit["text"][:0])  # separator never at ends
        self.assertFalse(fit["text"].startswith("|") or fit["text"].endswith("|"))
        pool.close()

    def test_single_value_over_limit(self):
        # Values are capped at 256 UTF-8 bytes by the input contract; a value
        # at that cap still exceeds every smaller limit -> empty, all omitted.
        rows = [{"group": "g", "key": 0, "id": 1, "value": "H" * 256}]
        s = f"{self.tmp}/big.state"
        partial(rows, "big", s)
        pool = Pool(f"{self.tmp}/bp", meter=Meter())
        pool.merge([s])
        for limit in (1, 100, 255):
            r = list(finalize(pool, limit=limit))[0]
            self.assertEqual(r["text"], "")
            self.assertEqual(r["omitted"], 1)
        pool.close()

    def test_limit_validation(self):
        pool = self._pool()
        with self.assertRaises(ValueError):
            list(finalize(pool, limit=0))
        with self.assertRaises(ValueError):
            list(finalize(pool, limit=4097))
        pool.close()

    def test_distinct_prefix_then_omitted_counts_distinct(self):
        rows = ([{"group": "g", "key": 0, "id": 1, "value": "aa"},
                 {"group": "g", "key": 1, "id": 2, "value": "aa"},
                 {"group": "g", "key": 2, "id": 3, "value": "bbbbbb"},
                 {"group": "g", "key": 3, "id": 4, "value": "cc"}])
        s = f"{self.tmp}/d.state"
        partial(rows, "d", s)
        pool = Pool(f"{self.tmp}/dp", meter=Meter())
        pool.merge([s])
        # distinct sequence: aa(2), bbbbbb(6), cc(2); limit 5 -> only aa fits,
        # bbbbbb blocks, cc must not be skipped past it
        r = list(finalize(pool, distinct=True, limit=5))[0]
        self.assertEqual(r["text"], "aa")
        self.assertEqual(r["omitted"], 2)
        pool.close()


class Conflicts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rows = [{"group": "g", "key": 1, "id": 1, "value": "a"},
                     {"group": "g", "key": 2, "id": 2, "value": "b"}]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_idempotent_identical_resend(self):
        s = f"{self.tmp}/c.state"
        desc = partial(self.rows, "c", s)
        pool = Pool(f"{self.tmp}/p", meter=Meter())
        first = pool.add_state(s)
        again = pool.add_state(s)
        self.assertEqual(first["action"], "admitted")
        self.assertEqual(again["action"], "idempotent")
        # not double counted
        self.assertEqual(pool.db.execute(
            "SELECT COUNT(*) FROM chunk_ids").fetchone()[0], 2)
        got = list(finalize(pool))[0]
        self.assertEqual(got["occurrences"], 2)
        pool.close()

    def test_same_chunk_different_content_rejected(self):
        s1 = f"{self.tmp}/c1.state"
        s2 = f"{self.tmp}/c2.state"
        partial(self.rows, "c", s1)
        partial([{"group": "g", "key": 1, "id": 1, "value": "CHANGED"}],
                "c", s2)
        pool = Pool(f"{self.tmp}/p", meter=Meter())
        pool.add_state(s1)
        with self.assertRaises(ChunkConflict):
            pool.add_state(s2)
        # original content remains authoritative
        got = list(finalize(pool))[0]
        self.assertEqual(got["text"], "a|b")
        pool.close()

    def test_cross_chunk_overlap_rejected(self):
        s1 = f"{self.tmp}/c1.state"
        s2 = f"{self.tmp}/c2.state"
        partial(self.rows, "c1", s1)
        partial([{"group": "g", "key": 9, "id": 2, "value": "x"}],
                "c2", s2)  # id 2 reused across chunks
        pool = Pool(f"{self.tmp}/p", meter=Meter())
        pool.add_state(s1)
        with self.assertRaises(OverlapConflict):
            pool.add_state(s2)
        self.assertEqual([c["chunk"] for c in pool.chunks()], ["c1"])
        pool.close()

    def test_manifest_digest_mismatch_rejected(self):
        s = f"{self.tmp}/c.state"
        desc = partial(self.rows, "c", s)
        bad = {"chunks": [{"chunk": "c", "digest": "0" * 64,
                           "ids": desc["ids"]}]}
        pool = Pool(f"{self.tmp}/p", meter=Meter())
        with self.assertRaises(ManifestConflict):
            pool.add_state(s, bad)
        # correct manifest is accepted
        good = {"chunks": [{k: desc[k] for k in ("chunk", "digest", "ids")}]}
        self.assertEqual(pool.add_state(s, good)["action"], "admitted")
        pool.close()

    def test_manifest_id_set_mismatch_rejected(self):
        s = f"{self.tmp}/c.state"
        desc = partial(self.rows, "c", s)
        # digest matches the state but the pinned id set does not
        bad = {"chunks": [{"chunk": "c", "digest": desc["digest"],
                           "ids": [desc["ids"][0]]}]}
        pool = Pool(f"{self.tmp}/p", meter=Meter())
        with self.assertRaises(ManifestConflict):
            pool.add_state(s, bad)
        pool.close()

    def test_manifest_missing_chunk_rejected(self):
        s = f"{self.tmp}/c.state"
        desc = partial(self.rows, "c", s)
        manifest = {"chunks": [{"chunk": "other", "digest": desc["digest"],
                                "ids": desc["ids"]}]}
        pool = Pool(f"{self.tmp}/p", meter=Meter())
        with self.assertRaises(ManifestConflict):
            pool.add_state(s, manifest)
        pool.close()

    def test_foreign_file_rejected(self):
        bogus = f"{self.tmp}/bogus.state"
        Path(bogus).write_text("not a state")
        pool = Pool(f"{self.tmp}/p", meter=Meter())
        with self.assertRaises(Exception):
            pool.add_state(bogus)
        pool.close()

    def test_tampered_state_rejected_on_self_verify(self):
        from ordered_state import StateError
        s = f"{self.tmp}/c.state"
        desc = partial(self.rows, "c", s)
        tampered = f"{self.tmp}/tampered.state"
        data = bytearray(Path(s).read_bytes())
        # flip a payload byte inside the record section
        h = read_header(s)
        hl = h["_lens"][0]
        data[len(b"ORDAG1\n") + hl + 30] ^= 0xFF
        Path(tampered).write_bytes(bytes(data))
        pool = Pool(f"{self.tmp}/p", meter=Meter())
        # recomputed frame digest no longer matches the header digest
        with self.assertRaises(StateError):
            pool.add_state(tampered)
        # the untouched state still verifies and admits cleanly
        self.assertEqual(pool.add_state(s)["action"], "admitted")
        pool.close()


class EmptyShard(unittest.TestCase):
    def test_empty_chunk_round_trip(self):
        tmp = tempfile.mkdtemp()
        try:
            s = f"{tmp}/empty.state"
            desc = partial([], "empty", s)
            self.assertEqual(desc["record_count"], 0)
            self.assertEqual(desc["ids"], [])
            pool = Pool(f"{tmp}/p", meter=Meter())
            self.assertEqual(pool.add_state(s)["action"], "admitted")
            self.assertEqual(list(finalize(pool)), [])
            pool.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BudgetAndSpill(unittest.TestCase):
    def test_partial_external_spill_stays_in_budget(self):
        tmp = tempfile.mkdtemp()
        try:
            rows = [{"group": "g", "key": i, "id": i + 1,
                     "value": "y" * 200} for i in range(2000)]
            meter = Meter(enforce=True)
            desc = partial(rows, "big", f"{tmp}/big.state", meter=meter,
                           spill_bytes=16 * 1024)
            self.assertEqual(desc["record_count"], 2000)
            self.assertLessEqual(meter.peak, PAYLOAD_BUDGET)
            self.assertGreater(
                meter.bytes_written.get("temp_run", 0), 0,
                "external sort runs must actually be spilled")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_distinct_external_spill_matches_oracle(self):
        # One group whose distinct working set far exceeds 128 KiB.
        tmp = tempfile.mkdtemp()
        try:
            rows = []
            n, width = 1500, 180
            for i in range(n):
                rows.append({"group": "big", "key": i, "id": i + 1,
                             "value": f"v{i:04d}-" + "z" * (
                                 width - len(f"v{i:04d}-"))})
            # duplicates interleaved
            for i in range(0, n, 37):
                rows.append({"group": "big", "key": n + i, "id": n + i + 1,
                             "value": rows[i]["value"]})
            s = f"{tmp}/big.state"
            meter = Meter(enforce=True)
            partial(rows, "big", s, meter=meter, spill_bytes=16 * 1024)
            pool = Pool(f"{tmp}/p", meter=meter)
            pool.merge([s])
            got = list(finalize(pool, distinct=True, meter=meter,
                                dedup_cap_bytes=16 * 1024))[0]
            ref = oracle(rows, distinct=True)["big"]
            self.assertEqual(got["text"], ref["text"])
            self.assertEqual(got["omitted"], ref["omitted"])
            self.assertEqual(got["distinct_values"], n)
            self.assertEqual(got["occurrences"], len(rows))
            self.assertLessEqual(meter.peak, PAYLOAD_BUDGET)
            self.assertGreater(meter.bytes_written.get("dedup_spill", 0), 0)
            pool.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DemoDatasetOracle(unittest.TestCase):
    def test_full_demo_agrees_with_oracle_all_modes(self):
        tmp = tempfile.mkdtemp()
        try:
            info = gen_demo_dataset(f"{tmp}/data")
            manifest = load_manifest(f"{tmp}/data/manifest.json")
            # independent processes build the states
            states = []
            for c in manifest["chunks"].values():
                state = f"{tmp}/{c['chunk']}.state"
                run(["ordered_state.py",
                     f"{tmp}/data/{c['file']}", c["chunk"], state])
                states.append(state)
            all_rows = json.loads(
                Path(f"{tmp}/data/ALL-rows.json").read_text())
            for distinct in (False, True):
                for limit in (4096, 256, 64):
                    pool = Pool(f"{tmp}/p-{distinct}-{limit}-{uuid()}",
                                meter=Meter())
                    # shuffled admission order
                    order = list(range(len(states)))
                    order.reverse()
                    pool.merge([states[i] for i in order],
                               f"{tmp}/data/manifest.json")
                    got = {r["group"]: r for r in
                           finalize(pool, limit=limit, distinct=distinct,
                                    meter=pool.meter)}
                    self.assertEqual(got, oracle(all_rows, limit, distinct))
                    self.assertLessEqual(pool.meter.peak, PAYLOAD_BUDGET)
                    pool.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def uuid() -> str:
    return os.urandom(6).hex()


if __name__ == "__main__":
    unittest.main(verbosity=2)
