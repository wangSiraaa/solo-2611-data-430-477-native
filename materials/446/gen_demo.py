"""Generate the public spill demonstration dataset.

The headline group ``spill`` contains 4096 *distinct* values (~186 UTF-8 bytes
each, >800 KiB of payload) interleaved round-robin across 8 shards, with extra
duplicate occurrences, so no single shard is ordered and finalize's distinct
working set cannot stay resident inside 128 KiB: the external dedup path must
run and the demo must succeed rather than refuse on budget.

Additional fixed groups cover the required edge cases:

* ``repeat``   - identical text under different ids, split across shards;
* ``nullkey``  - NULL keys (sorted last) mixed with equal integer keys;
* ``emptyN``   - an empty shard contributing no rows;
* ``cjk-fit``  - Chinese values that fit a byte limit exactly;
* ``cjk-one``  - the same sequence with one byte less budget, dropping a value;
* ``huge``     - one value longer than any limit -> empty text, all omitted.

Outputs under <out>/: shard-0.jsonl .. shard-7.jsonl plus manifest.json with
each chunk's fixed identity, disjoint id set and original-content digest.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from oracle import canonical_digest

N_DISTINCT = 4096
N_SHARDS = 8
VALUE_BYTES = 186


def _value(i: int) -> str:
    # Deterministic, distinct, no '|', 186 UTF-8 bytes each.
    tag = f"v{i:04d}-"
    pad = "x" * (VALUE_BYTES - len(tag.encode()))
    return tag + pad


def generate(out_dir: str) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    shards: list[list[dict]] = [[] for _ in range(N_SHARDS)]
    next_id = 1

    def emit(shard: int, group: str, key, value: str) -> int:
        nonlocal next_id
        rid = next_id
        next_id += 1
        shards[shard].append({"group": group, "key": key, "id": rid,
                              "value": value})
        return rid

    # --- headline: 4096 distinct values interleaved across shards 0..6 -----
    for i in range(N_DISTINCT):
        emit(i % (N_SHARDS - 1), "spill", i, _value(i))
    # duplicate occurrences of some values under new ids, in other shards
    for i in range(0, N_DISTINCT, 257):
        emit((i + 3) % (N_SHARDS - 1), "spill", 10_000 + i, _value(i))

    # --- repeated identical text with distinct ids across shards -----------
    for k in range(6):
        emit(k % (N_SHARDS - 1), "repeat", k, "same")

    # --- NULL keys last, plus equal integer keys tied by id ----------------
    emit(0, "nullkey", None, "n0")
    emit(1, "nullkey", None, "n1")
    emit(2, "nullkey", 5, "a5")
    emit(3, "nullkey", 5, "b5")   # same key -> id tie-break
    emit(4, "nullkey", 2, "k2")
    emit(5, "nullkey", None, "n2")

    # --- Chinese byte-boundary groups --------------------------------------
    # Every value is one Chinese char = 3 UTF-8 bytes, so the joined size of
    # five values is 5*3 + 4 separators = 19 bytes.  limit 19 fits all five
    # exactly; limit 18 is one byte short and must drop the whole fifth value
    # (omitted counts one occurrence, never one byte).
    cjk = ["甲", "乙", "丙", "丁", "戊"]
    for k, v in enumerate(cjk):
        emit(k % (N_SHARDS - 1), "cjk-fit", k, v)
        emit(k % (N_SHARDS - 1), "cjk-one", k, v)

    # --- single value longer than any relevant limit (max legal 256 B) ------
    emit(6, "huge", 0, "H" * 256)

    # shard 7 deliberately stays empty (still a fixed chunk identity): all
    # emits above target shards 0..6 only.
    manifest_chunks = []
    for idx, rows in enumerate(shards):
        chunk = f"shard-{idx}"
        path = out / f"{chunk}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        ids = sorted(r["id"] for r in rows)
        manifest_chunks.append({
            "chunk": chunk,
            "digest": canonical_digest(rows),
            "ids": ids,
            "rows": len(rows),
            "file": path.name,
        })

    # sanity: disjoint id sets across chunks
    seen: set[int] = set()
    for c in manifest_chunks:
        assert not (seen & set(c["ids"])), "id overlap in generated shards"
        seen.update(c["ids"])

    manifest = {"chunks": manifest_chunks,
                "n_distinct_spill": N_DISTINCT,
                "ordering": "group asc, key asc NULL last, id asc"}
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    all_rows = [r for s in shards for r in s]
    (out / "ALL-rows.json").write_text(
        json.dumps(all_rows, ensure_ascii=False), encoding="utf-8")
    return {"dir": str(out), "shards": N_SHARDS,
            "rows": len(all_rows), "distinct_spill": N_DISTINCT,
            "empty_shard": "shard-7",
            "groups": sorted({r["group"] for r in all_rows})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    args = ap.parse_args()
    print(json.dumps(generate(args.out_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
