"""Resource accounting for the ordered-aggregation candidate.

Budget rule (from the task): candidate *active* payload must stay within
128 KiB.  The interpreter, path strings and at most 32 shard manifests are
exempt; the SQLite page cache (capped at 16 KiB) is reported separately and is
not part of the payload budget.  Spill/state bytes that live on disk are not
resident payload but their read/write traffic is recorded.

The meter tracks two things:

* live resident bytes per category (add/release) with an enforced peak;
* cumulative disk read/write traffic per file kind.

Every meaningful stage emits an event to a JSONL log so the resource record is
produced by real runs, not asserted in prose.
"""
from __future__ import annotations

import json
import sys
import threading
import time

PAYLOAD_BUDGET = 128 * 1024
SQLITE_PAGE_CACHE = 16 * 1024


def deep_size(obj) -> int:
    """Approximate resident bytes of plain containers/strings/ints.

    Used to charge the decoded input/merge records that the frame-by-frame
    disk accounting does not see once Python objects exist on the heap.
    """
    seen: set[int] = set()
    total = 0

    def walk(o):
        nonlocal total
        oid = id(o)
        if oid in seen:
            return
        seen.add(oid)
        total += sys.getsizeof(o)
        if isinstance(o, dict):
            for k, v in o.items():
                walk(k)
                walk(v)
        elif isinstance(o, (list, tuple, set, frozenset)):
            for item in o:
                walk(item)

    walk(obj)
    return total


class BudgetExceeded(RuntimeError):
    pass


class Meter:
    def __init__(self, log_path: str | None = None, budget: int = PAYLOAD_BUDGET,
                 enforce: bool = True):
        self._live: dict[str, int] = {}
        self.peak = 0
        self.peak_event: str | None = None
        self.budget = budget
        self.enforce = enforce
        self.bytes_written: dict[str, int] = {}
        self.bytes_read: dict[str, int] = {}
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._log = open(log_path, "a", encoding="utf-8") if log_path else None

    # ---- resident payload -------------------------------------------------
    def add(self, category: str, n: int) -> None:
        with self._lock:
            self._live[category] = self._live.get(category, 0) + n
            total = self.live_total()
            if total > self.peak:
                self.peak = total
                self.peak_event = category
            if self.enforce and total > self.budget:
                raise BudgetExceeded(
                    f"payload {total} > budget {self.budget} in {category}")

    def release(self, category: str, n: int) -> None:
        with self._lock:
            cur = self._live.get(category, 0)
            self._live[category] = max(0, cur - n)

    def live_total(self) -> int:
        return sum(self._live.values())

    def snapshot(self) -> dict:
        return {"live": dict(self._live), "live_total": self.live_total(),
                "peak": self.peak, "peak_event": self.peak_event}

    # ---- disk traffic -----------------------------------------------------
    def wrote(self, kind: str, n: int) -> None:
        self.bytes_written[kind] = self.bytes_written.get(kind, 0) + n

    def read(self, kind: str, n: int) -> None:
        self.bytes_read[kind] = self.bytes_read.get(kind, 0) + n

    def event(self, name: str, **extra) -> None:
        rec = {"ts": time.time(), "event": name, **self.snapshot(), **extra}
        self.events.append(rec)
        if self._log is not None:
            self._log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._log.flush()

    def traffic(self) -> dict:
        return {"bytes_written": dict(self.bytes_written),
                "bytes_read": dict(self.bytes_read),
                "written_total": sum(self.bytes_written.values()),
                "read_total": sum(self.bytes_read.values())}

    def close(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None
