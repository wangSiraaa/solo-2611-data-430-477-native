"""Independent full-sort oracle, deliberately separate from the product code.

It does not import any product module.  Given every original row (the complete
input materialized in one process, which is allowed for a reference oracle),
it performs a plain Python full sort and renders results with its own byte
math, then returns the expected per-group answers.  Test suites use it to
check sharded, re-ordered, multi-level merged product runs.
"""
from __future__ import annotations

import json
from pathlib import Path

SEP = "|"


def oracle(rows: list[dict], limit: int = 4096, distinct: bool = False) -> dict:
    # Full sort in one shot using the stated ordering semantics.
    ordered = sorted(rows, key=lambda r: (
        r["group"], r["key"] is None,
        -(1 << 63) if r["key"] is None else r["key"], r["id"]))

    by_group: dict[str, list[str]] = {}
    occurrences: dict[str, int] = {}
    for r in ordered:
        by_group.setdefault(r["group"], [])
        occurrences[r["group"]] = occurrences.get(r["group"], 0) + 1
        seq = by_group[r["group"]]
        if not distinct or r["value"] not in seq:
            seq.append(r["value"])

    out: dict[str, dict] = {}
    for group in sorted(by_group):
        values = by_group[group]
        kept: list[str] = []
        used = 0
        stopped = False
        for v in values:
            vb = v.encode("utf-8")
            if stopped:
                continue
            if len(vb) > limit:
                stopped = True
                continue
            extra = len(vb) + (1 if kept else 0)
            if used + extra > limit:
                stopped = True
                continue
            kept.append(v)
            used += extra
        text = SEP.join(kept)
        out[group] = {
            "group": group,
            "text": text,
            "omitted": len(values) - len(kept),
            "occurrences": occurrences[group],
            "distinct_values": len(values),
        }
    return out


def canonical_digest(rows: list[dict]) -> str:
    """Independent reimplementation of the chunk digest convention."""
    import hashlib
    import struct
    h = hashlib.sha256()
    u16, bq, q = struct.Struct("<H"), struct.Struct("<Bq"), struct.Struct("<q")
    ordered = sorted(rows, key=lambda r: (
        r["group"], r["key"] is None,
        -(1 << 63) if r["key"] is None else r["key"], r["id"]))
    for r in ordered:
        gb, vb = r["group"].encode(), r["value"].encode()
        null = r["key"] is None
        h.update(u16.pack(len(gb)) + gb
                 + bq.pack(1 if null else 0, 0 if null else r["key"])
                 + q.pack(r["id"]) + u16.pack(len(vb)) + vb)
    return h.hexdigest()


def load_rows(*paths: str) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        text = Path(p).read_text(encoding="utf-8")
        if text.lstrip().startswith("["):
            rows.extend(json.loads(text))
        else:
            rows.extend(json.loads(line) for line in text.splitlines() if line)
    return rows
