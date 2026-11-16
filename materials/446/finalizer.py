"""finalize: consume merged ordered states and emit per-group text + omitted.

Streaming semantics:

* records arrive already globally ordered by the pool's stable k-way merge
  (group, NULL-last key asc, id asc); groups are processed one at a time with
  one-record lookahead and the full output is never retained;
* normal mode keeps every occurrence; distinct mode keeps the first occurrence
  of each *exactly equal* value in that global order;
* the separator is ``|`` and never appears at either end;
* ``limit`` (1..4096 UTF-8 bytes) bounds the joined output: values are taken
  from the ordered (and possibly distinct-filtered) sequence as the longest
  prefix of *complete* values whose joined size stays within the limit; once a
  value does not fit, no later value is taken past it;
* ``omitted`` counts value *occurrences* not emitted (in distinct mode, the
  number of distinct values not emitted) -- byte counts are never used as
  quantities;
* a single value longer than the limit yields an empty text and every valid
  occurrence (distinct: every distinct value) is omitted.

Distinct dedup is memory-bounded.  The fast path holds a resident set capped at
``dedup_cap_bytes`` and filters first occurrences online.  A group that exceeds
the cap takes the external path: every value frame is spilled, externally
sorted in bounded runs and de-duplicated to a sorted unique file, then the
group is replayed from pool records against streamed membership, so first-
occurrence order is preserved without a resident whole-group list.
"""
from __future__ import annotations

import argparse
import heapq
import json
import os
import shutil
import tempfile
from typing import Iterator

from meter import Meter, deep_size
from ordered_state import HDR_U16

DEFAULT_DEDUP_BYTES = 32 * 1024
SEP = b"|"


class FinalizeError(ValueError):
    pass


# --------------------------------------------------------------------------
# bounded distinct
# --------------------------------------------------------------------------
class _OnlineDistinct:
    """Resident-set first-occurrence filter while it fits in the cap."""

    def __init__(self, meter: Meter, cap_bytes: int):
        self.meter = meter
        self.cap = cap_bytes
        self.seen: set[bytes] = set()
        self.live = 0
        self.overflow = False

    def add(self, value_bytes: bytes) -> bool:
        if value_bytes in self.seen:
            return False
        if self.live + len(value_bytes) > self.cap:
            self.overflow = True
            return True
        self.seen.add(value_bytes)
        self.live += len(value_bytes)
        self.meter.add("dedup_set", len(value_bytes))
        return True

    def release(self) -> None:
        self.meter.release("dedup_set", self.live)
        self.seen.clear()
        self.live = 0


class _ExternalDistinct:
    """Two-pass external distinct with bounded resident payload."""

    def __init__(self, meter: Meter, cap_bytes: int, tmpdir: str):
        self.meter = meter
        self.cap = cap_bytes
        self.tmpdir = tmpdir
        fd, self.raw_path = tempfile.mkstemp(prefix="dedup-", suffix=".raw",
                                             dir=tmpdir)
        os.close(fd)
        self.raw = open(self.raw_path, "wb")
        self.count = 0

    def add(self, value_bytes: bytes) -> None:
        self.raw.write(HDR_U16.pack(len(value_bytes)) + value_bytes)
        self.count += 1
        self.meter.wrote("dedup_spill", 2 + len(value_bytes))

    def _read_frames(self, path: str) -> Iterator[bytes]:
        with open(path, "rb") as fh:
            while True:
                head = fh.read(2)
                if not head:
                    return
                (vl,) = HDR_U16.unpack(head)
                vb = fh.read(vl)
                self.meter.read("dedup_spill", 2 + vl)
                yield vb

    def _write_run(self, vals: list[bytes]) -> str:
        vals.sort()
        fd, path = tempfile.mkstemp(prefix="dedup-run-", suffix=".srt",
                                    dir=self.tmpdir)
        with os.fdopen(fd, "wb") as fh:
            for vb in vals:
                fh.write(HDR_U16.pack(len(vb)) + vb)
        self.meter.wrote("dedup_spill", os.path.getsize(path))
        return path

    def build_unique(self) -> str:
        """External sort + adjacent de-dup -> sorted unique-value file."""
        self.raw.close()
        run_paths: list[str] = []
        buf: list[bytes] = []
        buf_bytes = 0
        for vb in self._read_frames(self.raw_path):
            buf.append(vb)
            buf_bytes += len(vb)
            self.meter.add("dedup_sortbuf", len(vb))
            if buf_bytes >= self.cap:
                run_paths.append(self._write_run(buf))
                self.meter.release("dedup_sortbuf", buf_bytes)
                buf, buf_bytes = [], 0
        if buf:
            run_paths.append(self._write_run(buf))
            self.meter.release("dedup_sortbuf", buf_bytes)

        fd, unique_path = tempfile.mkstemp(prefix="dedup-", suffix=".uni",
                                           dir=self.tmpdir)
        os.close(fd)
        prev = None
        with open(unique_path, "wb") as out:
            for vb in heapq.merge(*(self._read_frames(p) for p in run_paths)):
                if vb != prev:
                    out.write(HDR_U16.pack(len(vb)) + vb)
                    self.meter.wrote("dedup_spill", 2 + len(vb))
                    prev = vb
        for p in run_paths:
            os.unlink(p)
        os.unlink(self.raw_path)
        return unique_path

    def first_order_values(self, pool, group: str) -> Iterator[bytes]:
        """Replay one group from the pool against streamed unique membership.

        The unique file is sorted; the group is re-read from the pool in
        global order and membership is consumed with an iterator, so only one
        value is resident at a time while first-occurrence order is restored.
        """
        unique_path = self.build_unique()
        unique_it = self._read_frames(unique_path)
        pending = next(unique_it, None)
        for rec in pool.iter_records():
            g, _key, _id, value = rec
            if g < group:
                continue
            if g != group:
                break
            vb = value.encode("utf-8")
            if pending is not None and vb == pending:
                yield vb
                pending = next(unique_it, None)
        os.unlink(unique_path)


# --------------------------------------------------------------------------
# group rendering
# --------------------------------------------------------------------------
class _Renderer:
    """Streaming complete-value-prefix renderer with accurate omitted count."""

    def __init__(self, limit: int):
        self.limit = limit
        self.total = 0
        self.out: list[bytes] = []
        self.used = 0
        self.stopped = False

    def add(self, vb: bytes) -> None:
        self.total += 1
        if self.stopped:
            return
        if len(vb) > self.limit:
            self.stopped = True  # can never fit; nothing past it considered
            return
        extra = len(vb) + (1 if self.out else 0)
        if self.used + extra > self.limit:
            self.stopped = True  # do not skip ahead to later values
            return
        self.out.append(vb)
        self.used += extra

    def finish(self) -> tuple[str, int, int]:
        text = SEP.join(self.out).decode("utf-8")
        return text, self.total - len(self.out), self.total


def finalize(pool, limit: int = 4096, distinct: bool = False, *,
             meter: Meter | None = None, out_path: str | None = None,
             dedup_cap_bytes: int = DEFAULT_DEDUP_BYTES) -> Iterator[dict]:
    """Yield one result dict per group in global group order.

    Records are streamed one at a time with one-record lookahead; no group is
    ever materialized.  If ``out_path`` is given results are streamed to JSONL
    as well.  Active payload is bounded regardless of group size.
    """
    if not (1 <= limit <= 4096):
        raise FinalizeError("limit must be within 1..4096 UTF-8 bytes")
    meter = meter or Meter()
    meter.event("finalize_start", limit=limit, distinct=distinct)
    out_fh = open(out_path, "w", encoding="utf-8") if out_path else None
    tmpdir = tempfile.mkdtemp(prefix="finalize-")
    record_iter = iter(pool.iter_records())
    try:
        current = next(record_iter, None)
        while current is not None:
            group = current[0]
            renderer = _Renderer(limit)
            online = _OnlineDistinct(meter, dedup_cap_bytes) if distinct else None
            ext: _ExternalDistinct | None = None
            # bounded first-occurrence buffer while the online set still fits;
            # once external spill starts, rendering is deferred to the replay.
            ordered: list[bytes] = []
            ordered_bytes = 0
            occurrences = 0
            distinct_count = 0
            rec = current
            while rec is not None and rec[0] == group:
                resident = deep_size(rec)
                meter.add("input_record", resident)
                occurrences += 1
                vb = rec[3].encode("utf-8")
                try:
                    if not distinct:
                        renderer.add(vb)
                    elif ext is not None:
                        ext.add(vb)
                    else:
                        first = online.add(vb)
                        if online.overflow:
                            # Switch to external path; seed with values seen.
                            online.release()
                            ext = _ExternalDistinct(meter, dedup_cap_bytes,
                                                    tmpdir)
                            for earlier in ordered:
                                ext.add(earlier)
                            ext.add(vb)
                            meter.release("group_ordered", ordered_bytes)
                            ordered, ordered_bytes = [], 0
                        elif first:
                            distinct_count += 1
                            ordered.append(vb)
                            ordered_bytes += len(vb)
                            meter.add("group_ordered", len(vb))
                            renderer.add(vb)
                finally:
                    meter.release("input_record", resident)
                rec = next(record_iter, None)
            current = rec  # first record of the next group (or None)

            if not distinct:
                text, omitted, distinct_total = renderer.finish()
            elif ext is None:
                meter.event("distinct_group", group=group,
                            occurrences=occurrences,
                            distinct=distinct_count, spilled=False)
                text, omitted, distinct_total = renderer.finish()
                meter.release("group_ordered", ordered_bytes)
                online.release()
            else:
                meter.event("distinct_group", group=group,
                            occurrences=occurrences, spilled=True)
                replay_renderer = _Renderer(limit)
                distinct_total = 0
                for vb in ext.first_order_values(pool, group):
                    distinct_total += 1
                    replay_renderer.add(vb)
                text, omitted, _ = replay_renderer.finish()
            result = {"group": group, "text": text, "omitted": omitted,
                      "occurrences": occurrences,
                      "distinct_values": distinct_total}
            meter.event("group_done", group=group,
                        emitted_bytes=len(text.encode("utf-8")),
                        omitted=omitted)
            if out_fh is not None:
                out_fh.write(json.dumps(result, ensure_ascii=False) + "\n")
            yield result
    finally:
        if out_fh is not None:
            out_fh.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
        meter.event("finalize_done", **meter.traffic())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="finalize pool -> group results")
    ap.add_argument("pool_dir")
    ap.add_argument("--limit", type=int, default=4096)
    ap.add_argument("--distinct", action="store_true")
    ap.add_argument("--out", help="stream results to JSONL")
    ap.add_argument("--meter-log")
    args = ap.parse_args(argv)
    from run_pool import Pool
    meter = Meter(args.meter_log)
    pool = Pool(args.pool_dir, meter=meter, create=False)
    try:
        n = 0
        for res in finalize(pool, args.limit, args.distinct, meter=meter,
                            out_path=args.out):
            n += 1
            print(json.dumps(res, ensure_ascii=False))
        print(json.dumps({"groups": n, "peak_payload_bytes": meter.peak,
                          "budget_bytes": meter.budget,
                          "within_budget": meter.peak <= meter.budget,
                          **meter.traffic()}, ensure_ascii=False))
    finally:
        pool.close()
        meter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
