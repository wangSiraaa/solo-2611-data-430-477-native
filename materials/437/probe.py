"""Probe / verification harness.

Modes
-----
python3 probe.py normal    -> original 2-worker run, no rescale
python3 probe.py rescale   -> same inputs, 2 -> 3 worker rescale; the timer t
                              must survive (the baseline dropped it)
python3 probe.py suite     -> full scenario suite, including an independent
                              single-worker interpreter that replays the exact
                              same input sequence and is compared item by item.

Constraints honoured by every generated sequence: <=500 inputs, <=200 timers,
16 key-groups, 1..4 worker threads.
"""

import json
import os
import random
import sys
import tempfile

from runtime import Runtime, key_group
from savepoint import load as sp_load, restore as sp_restore, KEY_GROUP_COUNT

FAILURES = []


def check(cond, label, detail=None):
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILURES.append((label, detail))
        print("  FAIL  %s  %s" % (label, detail if detail is not None else ""))


# ---------------------------------------------------------------- baseline
def baseline(mode):
    r = Runtime(2)
    try:
        r.submit('add', ('k', 5))
        r.submit('timer', ('t', 'k', 10))
        r.drain()
        with tempfile.TemporaryDirectory() as d:
            if mode == 'rescale':
                r.rescale(d + '/save.json', 3)
            r.watermark(10)
        group = key_group('k')
        print(json.dumps({
            'mode': mode,
            'outputs': r.outputs,
            'expected': [{'timer_id': 't', 'key': 'k', 'value': 5,
                          'due': 10, 'group': group}],
        }, sort_keys=True))
        ok = (r.outputs == [{'timer_id': 't', 'key': 'k', 'value': 5,
                             'due': 10, 'group': group}])
        return ok, r
    finally:
        r.close()


# ------------------------------------------------- independent reference
class ReferenceRuntime:
    """Single-threaded, single-worker reference: one GroupState per group."""

    def __init__(self):
        from state import GroupState, DuplicateTimerError
        self.GroupState = GroupState
        self.DuplicateTimerError = DuplicateTimerError
        self.groups = {gid: GroupState(gid)
                       for gid in range(KEY_GROUP_COUNT)}
        self.outputs = []
        self.errors = []

    def submit(self, op, args):
        if op == 'add':
            key, delta = args
            self.groups[key_group(key)].add(key, delta)
        elif op == 'timer':
            tid, key, due = args
            try:
                self.outputs.extend(
                    self.groups[key_group(key)].timer(tid, key, due))
            except self.DuplicateTimerError as exc:
                self.errors.append(repr(exc))
        elif op == 'watermark':
            (time,) = args
            for gid in range(KEY_GROUP_COUNT):
                self.outputs.extend(self.groups[gid].advance(time))

    def watermark(self, time):
        self.submit('watermark', (time,))

    def drain(self):
        pass

    def close(self):
        pass

    def canonical_outputs(self):
        return sorted(self.outputs, key=lambda r: (r['due'], r['timer_id']))

    def snapshot(self):
        return {gid: st.snapshot() for gid, st in self.groups.items()}


def compare_to_reference(rt, ref, label):
    """Item-by-item comparison: counts, trigger identities, watermarks, output."""
    live = {gid: st.snapshot()
            for st in rt.worker_states()
            for gid in [st.group]}
    ref_snap = ref.snapshot()
    check(set(live) == set(range(KEY_GROUP_COUNT)),
          label + ": all 16 groups present")
    for gid in range(KEY_GROUP_COUNT):
        check(live[gid]['counts'] == ref_snap[gid]['counts'],
              "%s: group %d counts match" % (label, gid),
              (live[gid]['counts'], ref_snap[gid]['counts']))
        check(live[gid]['watermark'] == ref_snap[gid]['watermark'],
              "%s: group %d watermark matches" % (label, gid),
              (live[gid]['watermark'], ref_snap[gid]['watermark']))
        check(set(live[gid]['timers']) == set(ref_snap[gid]['timers']),
              "%s: group %d pending timer identities match" % (label, gid),
              (live[gid]['timers'], ref_snap[gid]['timers']))
        check(live[gid]['timers'] == ref_snap[gid]['timers'],
              "%s: group %d timer payloads match" % (label, gid))
        check(set(live[gid]['completed']) ==
              set(ref_snap[gid]['completed']),
              "%s: group %d completed identities match" % (label, gid))
    check(rt.outputs == ref.canonical_outputs(),
          label + ": final outputs identical (due, timer_id order)",
          (rt.outputs, ref.canonical_outputs()))


def replay(rt, ref, seq):
    for op, args in seq:
        if op == 'watermark':
            if not isinstance(args, tuple):
                args = (args,)
            rt.watermark(*args)  # fan-out + drain, like the reference engine
        else:
            rt.submit(op, args)
        ref.submit(op, args)
    rt.drain()


# ------------------------------------------------------------- scenarios
def scenario_142():
    print("\n[1] planned rescale 1 -> 4 -> 2, timers across both rescales")
    ref = ReferenceRuntime()
    with tempfile.TemporaryDirectory() as d:
        rt = Runtime(1)
        seq1 = [('add', ('a', 3)), ('add', ('b', -1)),
                ('timer', ('t1', 'a', 5)), ('timer', ('t2', 'b', 8)),
                ('watermark', (2,)),
                ('timer', ('t3', 'a', 5))]
        replay(rt, ref, seq1)
        check(len(rt.workers) == 1, "start with 1 worker")
        rt.rescale(d + '/s1.json', 4)
        check(len(rt.workers) == 4, "now 4 workers")
        check(sorted(rt.owner_distribution().values()).count(0)
              == sum(1 for g in range(16) if g % 4 == 0),
              "ownership follows group % 4")
        compare_to_reference(rt, ref, "after 1->4")
        seq2 = [('add', ('a', 2)), ('timer', ('t4', 'b', 9)),
                ('watermark', (6,)),
                ('timer', ('t5', 'a', 6))]  # due <= current wm: fires at once
        replay(rt, ref, seq2)
        rt.rescale(d + '/s2.json', 2)
        check(len(rt.workers) == 2, "now 2 workers")
        check(all(rt.owner_distribution()[g] == g % 2
                  for g in range(16)),
              "each group has exactly one owner under group % 2")
        compare_to_reference(rt, ref, "after 4->2")
        seq3 = [('add', ('b', 10)), ('watermark', (100,))]
        replay(rt, ref, seq3)
        compare_to_reference(rt, ref, "final")
        # distribution evidence for the report
        dist = rt.owner_distribution()
        print("    group->owner:", json.dumps(dist))
        print("    per-owner counts:",
              json.dumps({w: sorted(g for g, o in dist.items() if o == w)
                          for w in range(2)}))
        rt.close()
    ref.close()


def scenario_multi_timer_same_key():
    print("\n[2] same key, many timers incl. same-due timer_id ordering")
    ref = ReferenceRuntime()
    with tempfile.TemporaryDirectory() as d:
        rt = Runtime(2)
        seq = [('add', ('hot', 7)),
               ('timer', ('z9', 'hot', 10)),
               ('timer', ('a1', 'hot', 10)),
               ('timer', ('m5', 'hot', 10)),
               ('timer', ('b2', 'hot', 3)),
               ('watermark', (10,))]
        replay(rt, ref, seq)
        rt.rescale(d + '/m.json', 3)
        rt.watermark(11)
        ref.watermark(11)
        compare_to_reference(rt, ref, "multi-timer")
        ids = [r['timer_id'] for r in rt.outputs]
        check(ids == ['b2', 'a1', 'm5', 'z9'],
              "same due ordered by timer_id; earlier due first", ids)
        check(all(r['group'] == key_group('hot') for r in rt.outputs),
              "every output carries its group")
        rt.close()


def scenario_negative_delta():
    print("\n[3] negative deltas and zero counters")
    ref = ReferenceRuntime()
    rt = Runtime(3)
    try:
        seq = [('add', ('n', 4)), ('add', ('n', -9)),
               ('timer', ('tn', 'n', 1)),
               ('add', ('fresh', 0)),
               ('timer', ('tf', 'fresh', 1)),
               ('watermark', (1,))]
        replay(rt, ref, seq)
        compare_to_reference(rt, ref, "negative delta")
        by_id = {r['timer_id']: r['value'] for r in rt.outputs}
        check(by_id == {'tn': -5, 'tf': 0},
              "math-integer count -5 and unseen key reads 0", by_id)
    finally:
        rt.close()


def scenario_boundaries():
    print("\n[4] timer boundaries: due==watermark, late registration, dup ids")
    ref = ReferenceRuntime()
    with tempfile.TemporaryDirectory() as d:
        rt = Runtime(2)
        seq = [('add', ('x', 1)),
               ('timer', ('eq', 'x', 5)),
               ('watermark', (4,)),
               ('watermark', (5,)),       # due == watermark fires
               ('timer', ('late', 'x', 5)),  # due <= current wm: fires now
               ('timer', ('late', 'x', 5)),  # identical retry: no second fire
               ('watermark', (5,)),       # non-advancing watermark: no replay
               ]
        replay(rt, ref, seq)
        check([r['timer_id'] for r in rt.outputs] == ['eq', 'late'],
              "boundary firing order and no duplicate",
              [r['timer_id'] for r in rt.outputs])
        # conflicting timer_id must be rejected, in both engines
        rt.submit('timer', ('eq', 'x', 6))
        rt.drain()
        ref.submit('timer', ('eq', 'x', 6))
        check(rt.errors and "DuplicateTimerError" in rt.errors[0],
              "conflicting timer_id rejected", rt.errors)
        rt.rescale(d + '/b.json', 4)
        rt.watermark(9)
        ref.watermark(9)
        compare_to_reference(rt, ref, "boundaries after rescale")
        check(len(rt.outputs) == 2,
              "restore cannot re-fire completed timers", len(rt.outputs))
        rt.close()


def scenario_fuzz():
    print("\n[5] fuzz equivalence vs independent single-worker interpreter")
    rng = random.Random(437437)
    ref = ReferenceRuntime()
    with tempfile.TemporaryDirectory() as d:
        rt = Runtime(rng.choice([1, 2, 3, 4]))
        seq = []
        next_tid = 0
        used_tids = []
        n_inputs = rng.randint(300, 500)
        wm = 0
        rescale_at = {rng.randint(50, 150), rng.randint(200, 350)}
        target_counts = [rng.choice([1, 2, 3, 4]) for _ in rescale_at]
        for i in range(n_inputs):
            if i in rescale_at:
                rt.drain()
                rt.rescale('%s/f%d.json' % (d, i),
                           target_counts.pop(0))
                compare_to_reference(rt, ref, "fuzz checkpoint %d" % i)
            roll = rng.random()
            if roll < 0.55:
                key = 'k%d' % rng.randrange(24)
                delta = rng.randint(-50, 50)
                item = ('add', (key, delta))
            elif roll < 0.85 and next_tid < 200:
                key = 'k%d' % rng.randrange(24)
                tid = 'f%d' % next_tid
                next_tid += 1
                used_tids.append((tid, key))
                due = rng.randint(0, 40)
                item = ('timer', (tid, key, due))
            elif used_tids and rng.random() < 0.5:
                tid, key = rng.choice(used_tids)  # identical retry
                item = ('timer', (tid, key, 0))
                # reference will reject as conflicting payload; mirror that
                seq.append(item)
                rt.submit(*item)
                try:
                    ref.submit(*item)
                except Exception:
                    pass
                rt.drain()
                continue
            else:
                wm += rng.randint(0, 3)
                item = ('watermark', (wm,))
            seq.append(item)
            if item[0] == 'watermark':
                rt.watermark(*item[1])
            else:
                rt.submit(*item)
            ref.submit(*item)
            if item[0] == 'watermark':
                rt.drain()
        rt.watermark(100)
        ref.watermark(100)
        check(len(seq) <= 500 and next_tid <= 200,
              "limits respected (%d inputs, %d timers)"
              % (len(seq), next_tid))
        compare_to_reference(rt, ref, "fuzz final")
        print("    fuzz workers ended at:", len(rt.workers),
              " committed results:", len(rt.outputs))
        rt.close()


def scenario_fence_handoff():
    print("\n[6] pause + Event control point: stale result fenced after handoff")
    with tempfile.TemporaryDirectory() as d:
        rt = Runtime(2)
        rt.submit('add', ('p', 4))
        rt.drain()
        rt.submit('timer', ('pt', 'p', 7))
        rt.drain()
        # Deterministic parked state: pause global input and quiesce first, so
        # the watermark enqueued afterwards is the only item in flight and must
        # be the one stopped at the held entry-side control point.
        rt.pause_input()
        rt.drain()
        rt.hold_commitments()
        rt.enqueue_watermark(7)
        # The fan-out reaches every worker; wait until all outstanding items
        # (one per worker) are parked.  The count equals the worker count.
        rt.inflight.wait_parked(at_least=len(rt.workers))
        check(rt.inflight.parked == len(rt.workers)
              and rt.inflight.processing == 0
              and not rt.store.contains('pt'),
              "watermark parked at the control point, nothing committed",
              (rt.inflight.parked, rt.inflight.processing,
               rt.store.outputs()))
        before = rt.evidence()
        n_rej_before = len(rt.rejections)
        rt.rescale(d + '/fence.json', 3, mode='fence')
        after = rt.evidence()
        check(len(rt.workers) == 3, "new generation has 3 owners")
        new_rejections = after['rejected'][n_rej_before:]
        check(any(x['timer_id'] == 'pt' and x['reason'].startswith('stale')
                  for x in new_rejections),
              "old owner's late result rejected by epoch fence",
              new_rejections)
        check(all(t['joined'] for t in after['retired_workers']),
              "every old worker thread joined", after['retired_workers'])
        old_names = set(t['thread_name'] for t in after['retired_workers'])
        check(not (old_names & set(after['live_threads'])),
              "no old thread remains among live threads")
        check(after['inflight'] == 0,
              "in-flight counter back at the safe boundary",
              after['inflight'])
        # The new owner must make independent progress afterwards.  The gate
        # was held across the handoff and stays held; release it explicitly.
        rt.release_commitments()
        rt.submit('add', ('p', 11))
        rt.submit('timer', ('pt2', 'p', 9))
        rt.watermark(9)
        ids = [r['timer_id'] for r in rt.outputs]
        check(ids == ['pt', 'pt2'],
              "fenced output adopted once; new owner progress unaffected",
              ids)
        check(rt.outputs[0]['value'] == 4,
              "adopted output keeps fire-time value (4), not a later count",
              rt.outputs[0])
        check(rt.outputs[1]['value'] == 15,
              "count carried with the group (4+11)", rt.outputs[1])
        # Savepoint: pt is a completed identity in its group state and the
        # adopted result is present in the re-readable store, exactly once.
        epoch, groups, results = sp_load(d + '/fence.json')
        check('pt' in groups[key_group('p')].completed,
              "fired identity preserved in group state savepoint",
              groups[key_group('p')].completed)
        check(set(results) == {'pt'},
              "fenced output adopted into the result store once",
              sorted(results))
        print("    pre-handoff evidence:", json.dumps(before, sort_keys=True))
        print("    post-handoff evidence:", json.dumps(after, sort_keys=True))
        rt.close()


def scenario_savepoint_and_restore():
    print("\n[7] savepoint contents, full reconstruction and atomicity")
    with tempfile.TemporaryDirectory() as d:
        path = d + '/sp.json'
        rt = Runtime(2)
        replay_seq = [('add', ('s1', 6)), ('add', ('s2', -2)),
                      ('timer', ('u1', 's1', 3)),
                      ('timer', ('u2', 's2', 9)),
                      ('watermark', (3,))]
        ref = ReferenceRuntime()
        replay(rt, ref, replay_seq)
        rt.rescale(path, 4)
        epoch, groups, results = sp_load(path)
        check(set(groups) == set(range(16)),
              "savepoint covers all 16 fixed groups")
        doc = json.load(open(path))
        check(set(doc) == {'version', 'key_group_count', 'epoch',
                           'groups', 'results'},
              "savepoint schema carries groups, timers, watermarks, "
              "identities, results", sorted(doc))
        g = groups[key_group('s2')]
        check(g.timers == {'u2': ('s2', 9)} and g.watermark == 3,
              "pending timer and per-group watermark serialized",
              (g.timers, g.watermark))
        check(groups[key_group('s1')].completed == {'u1': ('s1', 3)},
              "completed output identity serialized")
        check(set(results) == {'u1'} and results['u1']['value'] == 6,
              "result store re-reads prior output", results)
        # Rebuild a fresh runtime from the savepoint at yet another parallelism
        rt2 = Runtime.from_savepoint(path, 1)
        try:
            check(len(rt2.workers) == 1,
                  "reconstructed at worker count 1")
            rt2.watermark(20)
            check([r['timer_id'] for r in rt2.outputs] == ['u1', 'u2'],
                  "reconstructed runtime fires only the surviving timer",
                  rt2.outputs)
            rt2.watermark(21)
            check(len(rt2.outputs) == 2,
                  "no duplicate output after repeated watermarks",
                  len(rt2.outputs))
        finally:
            rt2.close()
        # Re-partition validation: each group exactly one owner, timers follow
        partitioned, _res, dist = sp_restore(path, 3)
        owners = list(dist.values())
        check(sorted(owners) == sorted(g % 3 for g in range(16)),
              "restore distribution equals group % 3", dist)
        check(sum(len(p) for p in partitioned) == 16,
              "all 16 states assigned exactly once")
        rt.close()

        # Atomicity: an unpublished staged file never replaces the good point.
        good = open(path, 'rb').read()
        from savepoint import stage
        tmp = stage(_empty_groups(), {}, path, epoch + 1)
        check(open(path, 'rb').read() == good,
              "uncommitted stage leaves previous savepoint usable")
        os.unlink(tmp)
        # Simulated failed re-partition: restore rejects a corrupt file and the
        # good path is still loadable.
        corrupt = d + '/bad.json'
        open(corrupt, 'w').write('{"version": 1, "groups":')
        try:
            sp_restore(corrupt, 2)
            check(False, "corrupt savepoint rejected")
        except (ValueError, json.JSONDecodeError):
            check(True, "corrupt/half-written savepoint cannot be selected")
        check(sp_load(path)[2] == results,
              "previous version still loads after failed reallocation")


def _empty_groups():
    from state import GroupState
    return [GroupState(gid) for gid in range(KEY_GROUP_COUNT)]


def scenario_rescue_failure_keeps_old_runtime():
    print("\n[8] failed rescale leaves old workers and old savepoint serving")
    with tempfile.TemporaryDirectory() as d:
        path = d + '/keep.json'
        rt = Runtime(2)
        try:
            rt.submit('add', ('q', 1))
            rt.submit('timer', ('qt', 'q', 2))
            rt.drain()
            rt.rescale(path, 3)
            good = open(path, 'rb').read()
            try:
                rt.rescale(d + '/other.json', 9)  # outside 1..4
                check(False, "out-of-range parallelism rejected")
            except ValueError:
                check(True, "out-of-range parallelism rejected")
            check(len(rt.workers) == 3,
                  "old generation still owns all groups after failure",
                  len(rt.workers))
            check(open(path, 'rb').read() == good,
                  "committed savepoint untouched by failed attempt")
            rt.watermark(2)
            check([r['timer_id'] for r in rt.outputs] == ['qt'],
                  "old runtime continues correctly", rt.outputs)
        finally:
            rt.close()


def scenario_concurrent_fence_stress():
    print("\n[9] concurrent producers, repeated fence handoffs, no shared groups")
    import threading
    with tempfile.TemporaryDirectory() as d:
        rt = Runtime(2)
        stop = threading.Event()
        errors = []

        def producer(pid):
            i = 0
            try:
                while not stop.is_set():
                    key = 'c%d-%d' % (pid, i % 8)
                    rt.submit('add', (key, 1))
                    if i % 3 == 0:
                        rt.submit('timer', ('p%d-%d' % (pid, i), key, 1))
                    i += 1
            except Exception as exc:  # pragma: no cover - diagnostic only
                errors.append(repr(exc))

        threads = [threading.Thread(target=producer, args=(p,))
                   for p in range(3)]
        for t in threads:
            t.start()
        generations = [1, 4, 2, 3, 1, 4, 2]
        for step, n in enumerate(generations):
            # Mix held-gate fence handoffs and plain drain handoffs.
            if step % 2 == 0:
                rt.hold_commitments()
                rt.inflight.wait_parked(at_least=1)
                rt.rescale('%s/cf%d.json' % (d, step), n, mode='fence')
                rt.release_commitments()
            else:
                rt.rescale('%s/cd%d.json' % (d, step), n, mode='drain')
            ev = rt.evidence()
            # At every instant exactly one live thread owns each group.
            live_groups = [g for w in rt.workers for g in sorted(w.states)]
            check(sorted(live_groups) == list(range(16)),
                  "step %d: 16 groups, one owner each" % step,
                  (len(live_groups), len(set(live_groups))))
            check(all(t['joined'] for t in ev['retired_workers']),
                  "step %d: all old threads joined" % step)
            check(ev['inflight'] == 0,
                  "step %d: safe boundary reached" % step, ev['inflight'])
        stop.set()
        for t in threads:
            t.join()
        rt.drain()
        rt.watermark(1000)
        check(not errors, "producers saw no exceptions", errors)
        # Every fired timer appears in the store exactly once.
        ids = [r['timer_id'] for r in rt.outputs]
        check(len(ids) == len(set(ids)),
              "no duplicate outputs under concurrent handoffs",
              (len(ids), len(set(ids))))
        # drain() must refuse to wait on a held gate (no hidden deadlock).
        rt.hold_commitments()
        try:
            rt.drain()
            check(False, "drain refuses while gate held")
        except RuntimeError:
            check(True, "drain refuses while gate held")
        rt.release_commitments()
        rt.close()
        print("    final generation:", rt.evidence()['worker_count'],
              "committed:", len(ids),
              "retired generations:",
              len(set((t['epoch']) for t in rt.retired)))


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ('normal', 'rescale', 'suite'):
        print(__doc__)
        return 2
    mode = sys.argv[1]
    if mode in ('normal', 'rescale'):
        ok, _ = baseline(mode)
        return 0 if ok else 1
    scenario_142()
    scenario_multi_timer_same_key()
    scenario_negative_delta()
    scenario_boundaries()
    scenario_fuzz()
    scenario_fence_handoff()
    scenario_savepoint_and_restore()
    scenario_rescue_failure_keeps_old_runtime()
    scenario_concurrent_fence_stress()
    print("\n=== summary ===")
    if FAILURES:
        print("%d FAILURE(S):" % len(FAILURES))
        for label, detail in FAILURES:
            print(" -", label, detail)
        return 1
    print("all checks passed")
    return 0


if __name__ == '__main__':
    sys.exit(main())
