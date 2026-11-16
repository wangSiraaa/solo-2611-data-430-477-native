"""Original (defective) implementation kept for side-by-side comparison.

Defects demonstrated by probe.py:

1. ``partial`` renders ``|``-joined text and treats it as the mergeable state,
   so equal text with different ids, later distinct filtering and byte-exact
   limits cannot be reconstructed;
2. the pool uses ``INSERT OR REPLACE`` with no chunk identity / digest /
   id-overlap checks, so re-sends and cross-chunk duplicates silently
   overwrite and double-count;
3. ``finish`` concatenates states in arrival order (sharding changes the
   result), truncates mid-character and reports the byte difference as the
   omitted occurrence count.
"""
from collections import defaultdict
import json
import sqlite3


def legacy_partial(rows, chunk):
    groups = defaultdict(list)
    for r in rows:
        groups[r["group"]].append(r)
    return [{"chunk": chunk, "group": g,
             "text": "|".join(r["value"] for r in sorted(
                 rs, key=lambda x: (x["key"] is None, x["key"] or 0, x["id"]))),
             "count": len(rs)} for g, rs in groups.items()]


class LegacyPool:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS runs(chunk TEXT,g TEXT,payload TEXT,"
            "PRIMARY KEY(chunk,g))")

    def add(self, states):
        with self.db:
            for s in self.db.execute("SELECT 1"):
                pass
            for s in states:
                self.db.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?)",
                                (s["chunk"], s["group"], json.dumps(s)))

    def states(self):
        for row in self.db.execute("SELECT payload FROM runs ORDER BY rowid"):
            yield json.loads(row[0])

    def close(self):
        self.db.close()


def legacy_finish(pool, limit=4096, distinct=False):
    groups = defaultdict(list)
    for state in pool.states():
        groups[state["group"]].extend(state["text"].split("|"))
    for g, values in sorted(groups.items()):
        if distinct:
            values = list(dict.fromkeys(values))
        encoded = "|".join(values).encode("utf8")
        yield {"group": g,
               "text": encoded[:limit].decode("utf8", errors="ignore"),
               "omitted": max(0, len(encoded) - limit)}
