"""partial stage: original rows -> portable, ordered, re-openable state.

State format (file suffix ``.state``), all integers little-endian:

    MAGIC b"ORDAG1\\n"
    header JSON line (UTF-8, terminated by b"\\n")
    record frames, sorted by (group text, key NULL last, key asc, id asc)
    id section: u64 count, then one s64 per unique id, sorted ascending
    footer: u32 header_len | u32 records_len | u32 ids_len
            | u32 record_count | b"ORDFOOT1"

Record frame (also the exact canonical bytes hashed into the digest, so the
digest is independent of the arrival order of rows inside the chunk):

    u16 group_bytes | group UTF-8 | u8 null_flag
    | s64 key (0 when NULL) | s64 id | u16 value_bytes | value UTF-8

Ordering semantics (fixed for every stage): within one group, occurrences are
ordered by key ascending with NULL key last, then by id ascending.  Equal
value text with different ids are distinct occurrences and are both kept.

The digest is sha256 over the concatenation of record frames in that sorted
order.  Re-sending the same chunk with identical content reproduces byte-for-
byte identical state (same digest, same id set); rendered strings never appear
in the state, so any merge tree / arrival order yields the same final order.

partial never trusts a reducer to re-open the originals: it reads the original
rows itself, validates them, and spills external-sort runs when its resident
buffer exceeds the configured threshold, so active payload stays inside the
128 KiB budget even when the chunk is much larger.
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Iterable, Iterator

from meter import Meter, deep_size

MAGIC = b"ORDAG1\n"
FOOT_MAGIC = b"ORDFOOT1"
FOOTER = struct.Struct("<IIII8s")
HDR_U16 = struct.Struct("<H")
ID64 = struct.Struct("<q")
U64 = struct.Struct("<Q")
FLAG_KEY = struct.Struct("<Bq")
VERSION = 1

# Resident encoded-frame buffer threshold before a sort run is spilled.
DEFAULT_SPILL_BYTES = 32 * 1024


class StateError(ValueError):
    pass


# --------------------------------------------------------------------------
# row validation / framing
# --------------------------------------------------------------------------
def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise StateError(msg)


def encode_frame(group: str, key, rid: int, value: str) -> bytes:
    gb = group.encode("utf-8")
    vb = value.encode("utf-8")
    _require(len(gb) <= 32, f"group longer than 32 UTF-8 bytes: {group!r}")
    _require(len(vb) <= 256, f"value longer than 256 UTF-8 bytes (id={rid})")
    null = key is None
    keyv = 0 if null else int(key)
    if not null:
        _require(-(1 << 63) <= keyv < (1 << 63), f"key out of int64: {keyv}")
    _require(-(1 << 63) <= rid < (1 << 63), f"id out of int64: {rid}")
    return (HDR_U16.pack(len(gb)) + gb + FLAG_KEY.pack(1 if null else 0, keyv)
            + ID64.pack(rid) + HDR_U16.pack(len(vb)) + vb)


def decode_frame(buf: bytes, pos: int) -> tuple[tuple, int]:
    (gl,), pos = HDR_U16.unpack_from(buf, pos), pos + 2
    group = buf[pos:pos + gl].decode("utf-8")
    pos += gl
    null, keyv = FLAG_KEY.unpack_from(buf, pos)
    pos += FLAG_KEY.size
    (rid,) = ID64.unpack_from(buf, pos)
    pos += 8
    (vl,) = HDR_U16.unpack_from(buf, pos)
    pos += 2
    value = buf[pos:pos + vl].decode("utf-8")
    pos += vl
    key = None if null else keyv
    return (group, key, rid, value), pos


def sort_key(rec: tuple):
    group, key, rid, _value = rec
    return (group, key is None, 0 if key is None else key, rid)


def frame_sort_key(frame: bytes) -> tuple:
    (rec, _) = decode_frame(frame, 0)
    return sort_key(rec)


def validate_row(r: dict) -> tuple[str, object, int, str]:
    _require(isinstance(r, dict), "row is not an object")
    for name in ("group", "key", "id", "value"):
        _require(name in r, f"row missing field {name!r}")
    group, key, rid, value = r["group"], r["key"], r["id"], r["value"]
    _require(isinstance(group, str) and group != "", "group must be non-empty text")
    _require(key is None or isinstance(key, int) and not isinstance(key, bool),
             "key must be int64 or null")
    _require(isinstance(rid, int) and not isinstance(rid, bool), "id must be int64")
    _require(isinstance(value, str), "value must be non-empty text")
    _require(value != "", "value must be non-empty text")
    _require("|" not in value, "value must not contain '|'")
    return group, key, rid, value


def iter_rows(source) -> Iterator[dict]:
    """Yield rows from a JSON list, a JSONL path, or an in-memory iterable."""
    if isinstance(source, (str, os.PathLike)):
        p = Path(source)
        with p.open("r", encoding="utf-8") as fh:
            head = fh.read(1)
            if head == "[":
                rest = head + fh.read()
                data = json.loads(rest)
                if not isinstance(data, list):
                    raise StateError("JSON input must be a list of rows")
                yield from data
                return
            if head != "":
                yield json.loads(head + fh.readline())
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
        return
    for r in source:
        yield r


# --------------------------------------------------------------------------
# spill runs (concatenated sorted record frames + companion sorted-id files)
# --------------------------------------------------------------------------
def _write_run(frames: list[bytes], tmpdir: str, meter: Meter) -> tuple[str, str]:
    frames.sort(key=frame_sort_key)
    fd, run_path = tempfile.mkstemp(prefix="run-", suffix=".rec", dir=tmpdir)
    ids = sorted({decode_frame(f, 0)[0][2] for f in frames})
    try:
        with os.fdopen(fd, "wb") as fh:
            for f in frames:
                fh.write(f)
        id_path = run_path + ".ids"
        with open(id_path, "wb") as fh:
            fh.write(U64.pack(len(ids)))
            for i in ids:
                fh.write(ID64.pack(i))
    finally:
        meter.wrote("temp_run", os.path.getsize(run_path)
                    + os.path.getsize(id_path))
    return run_path, id_path


def _iter_run_frames(path: str, meter: Meter) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        while True:
            head = fh.read(2)
            if not head:
                return
            (gl,) = HDR_U16.unpack(head)
            rest = fh.read(gl + FLAG_KEY.size + 8 + 2)
            (vl,) = HDR_U16.unpack_from(rest, gl + FLAG_KEY.size + 8)
            frame = head + rest + fh.read(vl)
            meter.read("temp_run", len(frame))
            yield frame


def _iter_run_ids(path: str, meter: Meter) -> Iterator[int]:
    with open(path, "rb") as fh:
        data = fh.read(8)
        (n,) = U64.unpack(data)
        meter.read("temp_run", 8)
        for _ in range(n):
            (i,) = ID64.unpack(fh.read(8))
            meter.read("temp_run", 8)
            yield i


# --------------------------------------------------------------------------
# final state file
# --------------------------------------------------------------------------
def write_state(out_path: str, chunk: str, frame_sources: list[Iterable[bytes]],
                id_sources: list[Iterable[int]], record_count: int,
                group_count: int, id_count: int, meter: Meter,
                tmp_cleanup: list[str]) -> dict:
    """Merge sorted frame sources and sorted id sources into a state file.

    The header is written once with a fixed-width 64-hex-char digest slot and
    all length fields, then only the digest slot is patched in place, so the
    record section and footer offsets never shift.
    """
    digest = hashlib.sha256()
    merged_frames = heapq.merge(*frame_sources, key=frame_sort_key)
    merged_ids = heapq.merge(*id_sources)

    tmp = out_path + ".tmp"
    header = {"format": "ordag-state", "version": VERSION, "chunk": chunk,
              # fixed-width slot keeps header length stable for in-place patch
              "digest": "0" * 64, "record_count": record_count,
              "id_count": id_count, "group_count": group_count}
    header_blob = json.dumps(header, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8") + b"\n"
    records_len = 0
    ids_len = 8
    with open(tmp, "wb") as fh:
        fh.write(MAGIC)
        fh.write(header_blob)
        for frame in merged_frames:
            digest.update(frame)
            fh.write(frame)
            records_len += len(frame)
            meter.wrote("state", len(frame))
        fh.write(U64.pack(id_count))
        for i in merged_ids:
            fh.write(ID64.pack(i))
            ids_len += 8
            meter.wrote("state", 8)
        fh.write(FOOTER.pack(len(header_blob), records_len, ids_len,
                             record_count, FOOT_MAGIC))
        meter.wrote("state", len(MAGIC) + len(header_blob) + 8
                    + FOOTER.size)
    os.replace(tmp, out_path)
    for p in tmp_cleanup:
        for q in (p, p + ".ids"):
            if os.path.exists(q):
                os.unlink(q)

    final_header = dict(header)
    final_header["digest"] = digest.hexdigest()
    _patch_digest(out_path, header["digest"], final_header["digest"])
    return final_header


def _patch_digest(path: str, old_slot: str, new_digest: str) -> None:
    assert len(old_slot) == len(new_digest) == 64
    with open(path, "r+b") as fh:
        blob = fh.read(4096)  # header is tiny
        pos = blob.index(old_slot.encode("ascii"))
        fh.seek(pos)
        fh.write(new_digest.encode("ascii"))


def read_header(path: str) -> dict:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if fh.read(len(MAGIC)) != MAGIC:
            raise StateError(f"not an ordag state file: {path}")
        fh.seek(size - FOOTER.size)
        hl, rl, il, rc, fmag = FOOTER.unpack(fh.read(FOOTER.size))
        if fmag != FOOT_MAGIC:
            raise StateError(f"bad footer magic: {path}")
        fh.seek(len(MAGIC))
        header = json.loads(fh.read(hl).decode("utf-8").rstrip("\n"))
    header["_lens"] = (hl, rl, il, rc)
    return header


def verify_state(path: str, meter: Meter | None = None) -> dict:
    """Self-check a state file: recompute the frame digest and validate ids.

    Detects on-disk tampering even when the header digest field itself is
    stale, because the digest is recomputed from the actual record frames and
    compared with the header.  Returns the header on success, raises
    StateError otherwise.
    """
    import hashlib
    header = read_header(path)
    hl, rl, il, rc = header["_lens"]
    digest = hashlib.sha256()
    count = 0
    for frame in _read_records(path, meter):
        digest.update(frame)
        count += 1
    if count != rc or count != header["record_count"]:
        raise StateError(f"{path}: record count mismatch on verify")
    if digest.hexdigest() != header["digest"]:
        raise StateError(f"{path}: digest mismatch on verify (tampered?)")
    ids = list(read_ids(path, meter))
    if len(ids) != header["id_count"] or ids != sorted(set(ids)):
        raise StateError(f"{path}: id section invalid on verify")
    return header


def _read_records(path: str, meter: Meter | None = None) -> Iterator[bytes]:
    header = read_header(path)
    hl, rl, il, rc = header["_lens"]
    start = len(MAGIC) + hl
    with open(path, "rb") as fh:
        fh.seek(start)
        remaining = rl
        while remaining:
            head = fh.read(2)
            (gl,) = HDR_U16.unpack(head)
            rest = fh.read(gl + FLAG_KEY.size + 8 + 2)
            (vl,) = HDR_U16.unpack_from(rest, gl + FLAG_KEY.size + 8)
            frame = head + rest + fh.read(vl)
            remaining -= len(frame)
            if meter:
                meter.read("state", len(frame))
            yield frame


def read_records(path: str, meter: Meter | None = None) -> Iterator[tuple]:
    for frame in _read_records(path, meter):
        rec, _ = decode_frame(frame, 0)
        yield rec


def read_ids(path: str, meter: Meter | None = None) -> Iterator[int]:
    header = read_header(path)
    hl, rl, il, _rc = header["_lens"]
    start = len(MAGIC) + hl + rl
    with open(path, "rb") as fh:
        fh.seek(start)
        (n,) = U64.unpack(fh.read(8))
        if meter:
            meter.read("state", 8)
        for _ in range(n):
            (i,) = ID64.unpack(fh.read(8))
            if meter:
                meter.read("state", 8)
            yield i


# --------------------------------------------------------------------------
# partial
# --------------------------------------------------------------------------
def partial(source, chunk: str, out_path: str, *, meter: Meter | None = None,
            spill_bytes: int = DEFAULT_SPILL_BYTES,
            tmpdir: str | None = None) -> dict:
    """Read original rows for one chunk and emit its ordered state file.

    Returns the chunk manifest descriptor (chunk identity, digest, sorted id
    list, counts).  Encoded frames are buffered only up to ``spill_bytes``
    resident bytes, then external-sort runs are spilled; the final state is a
    streaming k-way merge, so active payload is bounded.
    """
    meter = meter or Meter()
    meter.event("partial_start", chunk=chunk, spill_bytes=spill_bytes)
    if not isinstance(chunk, str) or not chunk:
        raise StateError("chunk id must be a non-empty string")
    own_tmp = tmpdir is None
    tmpdir = tmpdir or tempfile.mkdtemp(prefix=f"partial-{chunk}-")
    buf: list[bytes] = []
    buf_bytes = 0
    run_paths: list[str] = []
    id_paths: list[str] = []
    total = 0
    seen_ids: set[int] = set()
    seen_groups: set[str] = set()
    completed = False
    try:
        def spill() -> None:
            nonlocal buf, buf_bytes
            rp, ip = _write_run(buf, tmpdir, meter)
            run_paths.append(rp)
            id_paths.append(ip)
            meter.event("spill_run", chunk=chunk, frames=len(buf),
                        bytes=buf_bytes)
            buf = []
            meter.release("run_buffer", buf_bytes)
            buf_bytes = 0

        for row in iter_rows(source):
            row_size = deep_size(row)
            meter.add("input_batch", row_size)
            try:
                group, key, rid, value = validate_row(row)
                if rid in seen_ids:
                    raise StateError(
                        f"duplicate id inside chunk {chunk!r}: {rid}")
                seen_ids.add(rid)
                seen_groups.add(group)
                frame = encode_frame(group, key, rid, value)
                buf.append(frame)
                buf_bytes += len(frame)
                meter.add("run_buffer", len(frame))
                total += 1
                if buf_bytes >= spill_bytes:
                    spill()
            finally:
                # the decoded source row is consumed once the frame is built
                meter.release("input_batch", row_size)

        frame_sources: list[Iterable[bytes]] = []
        id_sources: list[Iterable[int]] = []
        if run_paths:
            if buf:
                rp, ip = _write_run(buf, tmpdir, meter)
                run_paths.append(rp)
                id_paths.append(ip)
                meter.release("run_buffer", buf_bytes)
                buf = []
                buf_bytes = 0
            frame_sources = [_iter_run_frames(p, meter) for p in run_paths]
            id_sources = [_iter_run_ids(p + ".ids", meter) for p in run_paths]
        else:
            # Single small chunk: one in-memory sorted source, no temp traffic.
            buf.sort(key=frame_sort_key)
            meter.event("in_memory_sort", chunk=chunk, frames=len(buf))
            mem_ids = sorted(seen_ids)
            frame_sources = [iter(buf)]
            id_sources = [iter(mem_ids)]

        header = write_state(out_path, chunk, frame_sources, id_sources, total,
                             len(seen_groups), len(seen_ids), meter, run_paths)
        if buf_bytes:
            meter.release("run_buffer", buf_bytes)
        ids = list(read_ids(out_path))
        descriptor = {"chunk": chunk, "digest": header["digest"], "ids": ids,
                      "record_count": total,
                      "state_file": os.path.basename(out_path)}
        completed = True
        meter.event("partial_done", chunk=chunk, records=total,
                    runs=len(run_paths), **meter.traffic())
        return descriptor
    finally:
        if not completed:
            # Failure path: do not leak spill runs or the temp directory.
            for p in run_paths:
                for q in (p, p + ".ids"):
                    if os.path.exists(q):
                        os.unlink(q)
            if own_tmp and os.path.isdir(tmpdir) and not os.listdir(tmpdir):
                os.rmdir(tmpdir)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="partial: rows -> ordered state")
    ap.add_argument("input", help="rows as JSON list or JSONL")
    ap.add_argument("chunk")
    ap.add_argument("out_state")
    ap.add_argument("--manifest-out")
    ap.add_argument("--meter-log")
    ap.add_argument("--spill-bytes", type=int, default=DEFAULT_SPILL_BYTES)
    args = ap.parse_args(argv)
    meter = Meter(args.meter_log)
    desc = partial(args.input, args.chunk, args.out_state, meter=meter,
                   spill_bytes=args.spill_bytes)
    if args.manifest_out:
        Path(args.manifest_out).write_text(
            json.dumps(desc, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in desc.items() if k != "ids"},
                     ensure_ascii=False))
    meter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
