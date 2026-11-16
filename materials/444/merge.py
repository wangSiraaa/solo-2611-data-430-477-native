"""Replica, event DAG validation, exchange and persistence.

An import packet is *all or nothing*.  Before a single event is applied the
whole packet (together with the events already held locally) must satisfy:

* every event is canonical (see :func:`sequence.canonical_event`);
* the event identities ``(replica, seq)`` form a contiguous ``1..n`` prefix
  per replica (no holes, no more than ``MAX_EVENTS_PER_REPLICA``);
* every event referenced by a context version vector exists, and each context
  is transitively closed (the context of every context entry is a sub-context);
* ``lamport == 1 + max(lamport of events known in context)`` (``1`` if the
  context is empty);
* insert anchors, delete ids and annotation endpoints exist and are covered by
  the creating event's context;
* deletes only name characters visible in their creating context and
  annotations cover a non-empty, ordered, currently visible range whose quote
  matches the context;
* the resulting tree is a single tree and the visible text is within
  ``MAX_VISIBLE_CODEPOINTS``.

Only then are the events committed and persisted.  A rejected packet leaves
memory *and* the backing store untouched, so reopening reproduces the exact
pre-import state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sequence import (
    CRDTError,
    EventError,
    MAX_EVENTS_PER_REPLICA,
    MAX_REPLICAS,
    MAX_VISIBLE_CODEPOINTS,
    MergeError,
    Sequence,
    _canonical_context,
    canonical_event,
    canonical_json,
    event_id,
    event_key,
    nodes_from_events,
    parse_char_id,
    tombstoned_ids,
    tree_order,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _version_of(events: Dict[Tuple[str, int], Dict[str, Any]]) -> Dict[str, int]:
    version: Dict[str, int] = {}
    for replica, seq in events:
        if seq > version.get(replica, 0):
            version[replica] = seq
    return version


def _prefix_events(
    events: Dict[Tuple[str, int], Dict[str, Any]], context: Dict[str, int]
) -> Iterable[Dict[str, Any]]:
    for (replica, seq), event in events.items():
        if seq <= context.get(replica, 0):
            yield event


def _prefix_view(
    events: Dict[Tuple[str, int], Dict[str, Any]], context: Dict[str, int]
) -> Tuple[List[str], List[str], Dict[str, Dict[str, Any]]]:
    """Return ``(visible_ids, ordered_ids, nodes)`` at ``context``."""

    subset = list(_prefix_events(events, context))
    nodes = nodes_from_events(e for e in subset if e["op"] == "insert")
    order = tree_order(nodes)
    deleted = tombstoned_ids(subset)
    visible = [cid for cid in order if cid not in deleted]
    return visible, order, nodes


def _seen_by_context(event: Dict[str, Any], cid: str) -> None:
    replica, seq, _ = parse_char_id(cid)
    if event["context"].get(replica, 0) < seq:
        raise MergeError(
            f"event {event_id(event)} references {cid!r}, which its context has not seen"
        )


def _require_reference(
    event: Dict[str, Any], cid: str, nodes: Dict[str, Dict[str, Any]]
) -> None:
    parse_char_id(cid)
    if cid not in nodes:
        raise MergeError(
            f"event {event_id(event)} references unknown character {cid!r}"
        )
    _seen_by_context(event, cid)


def validate_union(events: Dict[Tuple[str, int], Dict[str, Any]]) -> None:
    """Validate a complete event DAG.  Raises :class:`MergeError`."""

    by_replica: Dict[str, List[int]] = {}
    for replica, seq in events:
        by_replica.setdefault(replica, []).append(seq)
    if len(by_replica) > MAX_REPLICAS:
        raise MergeError(
            f"{len(by_replica)} replicas exceed the fixed limit of {MAX_REPLICAS}"
        )
    for replica, seqs in by_replica.items():
        seqs.sort()
        if len(seqs) > MAX_EVENTS_PER_REPLICA:
            raise MergeError(
                f"replica {replica!r} exceeds the {MAX_EVENTS_PER_REPLICA}-event limit"
            )
        if seqs != list(range(1, len(seqs) + 1)):
            raise MergeError(f"replica {replica!r} does not have a contiguous prefix")

    # Context closure, causality and lamport consistency.
    for (replica, seq), event in events.items():
        context = event["context"]
        if context.get(replica, 0) != seq - 1:
            raise MergeError(f"event {event_id(event)} has an inconsistent own clock")
        for dep_replica, dep_seq in context.items():
            dep = events.get((dep_replica, dep_seq))
            if dep is None:
                raise MergeError(
                    f"event {event_id(event)} is missing dependency "
                    f"{dep_replica}:{dep_seq}"
                )
            for transitive_replica, transitive_seq in dep["context"].items():
                if context.get(transitive_replica, 0) < transitive_seq:
                    raise MergeError(
                        f"event {event_id(event)} has a context that is not "
                        "transitively closed"
                    )
        known_lamports = [
            other["lamport"]
            for (other_replica, other_seq), other in events.items()
            if other_seq <= context.get(other_replica, 0)
        ]
        expected = (max(known_lamports) + 1) if known_lamports else 1
        if event["lamport"] != expected:
            raise MergeError(
                f"event {event_id(event)} has lamport {event['lamport']}, "
                f"expected {expected}"
            )

    # Character tree and references.
    inserts = [event for event in events.values() if event["op"] == "insert"]
    nodes = nodes_from_events(inserts)
    order = tree_order(nodes)
    deleted = tombstoned_ids(events.values())
    for cid in deleted:
        if cid not in nodes:
            raise MergeError(f"delete of unknown character {cid!r}")

    for event in events.values():
        if event["op"] == "insert":
            if event["after"] is not None:
                _require_reference(event, event["after"], nodes)
        elif event["op"] == "delete":
            visible, _, _ = _prefix_view(events, event["context"])
            visible_set = set(visible)
            for cid in event["ids"]:
                _require_reference(event, cid, nodes)
                if cid not in visible_set:
                    raise MergeError(
                        f"delete {event_id(event)} names {cid!r}, which is not "
                        "visible in its context"
                    )
        else:  # annotate
            for cid in (event["start"], event["end"]):
                _require_reference(event, cid, nodes)
            visible, _, prefix_nodes = _prefix_view(events, event["context"])
            index = {cid: i for i, cid in enumerate(visible)}
            if event["start"] not in index or event["end"] not in index:
                raise MergeError(
                    f"annotation {event_id(event)} endpoints are not visible in "
                    "their context"
                )
            start, end = index[event["start"]], index[event["end"]]
            if start > end:
                raise MergeError(
                    f"annotation {event_id(event)} endpoints are out of order"
                )
            quote = "".join(prefix_nodes[cid]["char"] for cid in visible[start : end + 1])
            if quote != event["quote"]:
                raise MergeError(
                    f"annotation {event_id(event)} quote does not match its context"
                )

    # An annotation id names one annotation.  Two events may not reuse an id
    # with different content, otherwise export would silently pick one.
    annotation_ids: Dict[str, Dict[str, Any]] = {}
    for event in events.values():
        if event["op"] != "annotate":
            continue
        annotation_id = event.get("annotation_id") or event_id(event)
        content = (
            event["start"],
            event["end"],
            event["label"],
            event["quote"],
        )
        existing = annotation_ids.get(annotation_id)
        if existing is not None and existing["content"] != content:
            raise MergeError(
                f"conflicting annotations share id {annotation_id!r}"
            )
        annotation_ids[annotation_id] = {"content": content}

    visible_count = sum(1 for cid in order if cid not in deleted)
    if visible_count > MAX_VISIBLE_CODEPOINTS:
        raise MergeError(
            f"visible text is {visible_count} code points, over the "
            f"{MAX_VISIBLE_CODEPOINTS}-code-point limit"
        )


# ---------------------------------------------------------------------------
# Replica
# ---------------------------------------------------------------------------

class Replica:
    """One editor replica: local operations, validation, merge and storage."""

    def __init__(
        self,
        sequence: Optional[Sequence] = None,
        replica_id: Optional[str] = None,
        storage_path: Optional[str] = None,
    ) -> None:
        self.sequence = sequence if sequence is not None else Sequence()
        self.replica_id = replica_id
        self.storage_path = storage_path
        self.events: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self.receipts: List[Dict[str, Any]] = []
        if storage_path and os.path.exists(storage_path):
            self._load()
        elif storage_path:
            self._save(self.events, self.receipts)

    # -- views ------------------------------------------------------------
    def version(self) -> Dict[str, int]:
        return _version_of(self.events)

    def event_list(self) -> List[Dict[str, Any]]:
        return [self.events[key] for key in sorted(self.events)]

    def export_delta(self, version: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
        """Events not covered by ``version``, in a causal (lamport) order."""

        version = _canonical_context(version or {})
        delta = [
            event
            for (replica, seq), event in self.events.items()
            if seq > version.get(replica, 0)
        ]
        delta.sort(key=lambda e: (e["lamport"], e["replica"], e["seq"]))
        return delta

    # -- local operations -------------------------------------------------
    def _local_identity(self) -> str:
        if not self.replica_id:
            raise EventError("this replica has no replica_id for local operations")
        return self.replica_id

    def _new_local_event(self, op: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        replica = self._local_identity()
        context = self.version()
        seq = context.get(replica, 0) + 1
        known = [
            event["lamport"]
            for (other_replica, other_seq), event in self.events.items()
            if other_seq <= context.get(other_replica, 0)
        ]
        lamport = (max(known) + 1) if known else 1
        event = {
            "replica": replica,
            "seq": seq,
            "op": op,
            "context": context,
            "lamport": lamport,
            **fields,
        }
        return canonical_event(event)

    def insert(self, after: Optional[str], text: str) -> Dict[str, Any]:
        """Insert ``text`` after the known character ``after`` (``None`` = root)."""

        if after is not None:
            parse_char_id(after)
            if after not in self.sequence.nodes:
                raise EventError(f"unknown insert anchor {after!r}")
        event = self._new_local_event("insert", {"after": after, "text": text})
        self._commit([event], receipt=False)
        return event

    def delete(self, ids: Iterable[str]) -> Dict[str, Any]:
        """Tombstone exactly the visible character ``ids`` named here."""

        deduped: List[str] = []
        for cid in ids:
            parse_char_id(cid)
            if cid not in deduped:
                deduped.append(cid)
        if not deduped:
            raise EventError("delete needs at least one character id")
        visible = set(self.sequence.visible_ids())
        for cid in deduped:
            if cid not in visible:
                raise EventError(f"delete target {cid!r} is not visible")
        event = self._new_local_event("delete", {"ids": deduped})
        self._commit([event], receipt=False)
        return event

    def annotate(
        self,
        start: int,
        end: int,
        label: str,
        annotation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Annotate visible code points ``[start, end)`` (half-open).

        The creation-time range is converted to inclusive first/last character
        ids.  The current quote is recorded so an unresolved annotation keeps
        its historical evidence.
        """

        ids = self.sequence.visible_range_halfopen(start, end)
        quote = "".join(self.sequence.nodes[cid]["char"] for cid in ids)
        replica = self._local_identity()
        seq = self.version().get(replica, 0) + 1
        fields = {
            "start": ids[0],
            "end": ids[-1],
            "label": label,
            "quote": quote,
            "annotation_id": annotation_id or f"{replica}:{seq}",
        }
        event = self._new_local_event("annotate", fields)
        self._commit([event], receipt=False)
        return event

    # -- merge ------------------------------------------------------------
    def merge(
        self, packet: Iterable[Dict[str, Any]], receipt: bool = True
    ) -> Dict[str, Any]:
        """Validate and atomically apply an out-of-order event packet."""

        return self._commit(list(packet), receipt=receipt)

    def _commit(
        self, incoming: List[Dict[str, Any]], receipt: bool = True
    ) -> Dict[str, Any]:
        candidate = dict(self.events)
        imported: List[Dict[str, Any]] = []
        duplicates: List[str] = []
        canonical_packet: List[Dict[str, Any]] = []

        for raw in incoming:
            try:
                event = canonical_event(raw)
            except EventError as exc:
                raise MergeError(f"rejected packet: {exc}") from exc
            canonical_packet.append(event)
            key = event_key(event)
            existing = candidate.get(key)
            if existing is not None:
                if canonical_json(existing) != canonical_json(event):
                    raise MergeError(
                        f"rejected packet: conflicting content for event "
                        f"{event_id(event)}"
                    )
                duplicates.append(event_id(event))
                continue
            candidate[key] = event
            imported.append(event)

        if not imported:
            return {
                "status": "noop",
                "imported": [],
                "duplicates": sorted(duplicates),
                "version": self.version(),
            }

        try:
            validate_union(candidate)
        except MergeError:
            raise
        except CRDTError as exc:
            raise MergeError(f"rejected packet: {exc}") from exc

        canonical_packet.sort(key=lambda e: (e["replica"], e["seq"]))
        digest = hashlib.sha256(
            canonical_json(canonical_packet).encode("utf-8")
        ).hexdigest()
        result = {
            "status": "accepted",
            "imported": [event_id(e) for e in imported],
            "duplicates": sorted(duplicates),
            "version": _version_of(candidate),
            "digest": digest,
        }
        receipts = self.receipts + [result] if receipt else self.receipts

        # Persist before mutating memory: an I/O failure must not leave a
        # half-merged in-memory state, and a rejection never reaches this point.
        if self.storage_path:
            self._save(candidate, receipts)

        self.events = candidate
        self.receipts = receipts
        self.sequence.apply_events(candidate.values())
        return result

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        assert self.storage_path is not None
        with open(self.storage_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        stored_replica = data.get("replica")
        if self.replica_id and stored_replica and self.replica_id != stored_replica:
            raise MergeError(
                f"store belongs to replica {stored_replica!r}, not {self.replica_id!r}"
            )
        if not self.replica_id:
            self.replica_id = stored_replica
        events: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for raw in data.get("events", []):
            event = canonical_event(raw)
            key = event_key(event)
            if key in events and canonical_json(events[key]) != canonical_json(event):
                raise MergeError(f"store has conflicting event {event_id(event)}")
            events[key] = event
        validate_union(events)
        self.events = events
        self.receipts = list(data.get("receipts", []))
        self.sequence.apply_events(events.values())

    def _save(
        self,
        events: Dict[Tuple[str, int], Dict[str, Any]],
        receipts: List[Dict[str, Any]],
    ) -> None:
        assert self.storage_path is not None
        payload = {
            "replica": self.replica_id,
            "version": _version_of(events),
            "events": [events[key] for key in sorted(events)],
            "receipts": receipts,
        }
        directory = os.path.dirname(os.path.abspath(self.storage_path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-crdt-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.storage_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -- export -----------------------------------------------------------
    def export(self, annotations: Optional[Any] = None) -> Dict[str, Any]:
        from annotations import Annotations

        ann = annotations if annotations is not None else Annotations.from_events(
            self.events.values()
        )
        return {
            "replica": self.replica_id,
            "version": self.version(),
            "text": self.sequence.text(),
            "visible_ids": self.sequence.visible_ids(),
            "annotations": ann.export(self.sequence),
            "resolved": ann.resolved(self.sequence),
            "unresolved": ann.unresolved(self.sequence),
        }


# ---------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------

def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Stable-identity sequence CRDT")
    parser.add_argument("--store", required=True, help="path to the replica store")
    parser.add_argument("--replica", help="replica id (required by init)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create an empty replica store")
    p_insert = sub.add_parser("insert", help="insert text after a character")
    p_insert.add_argument("--after", default=None, help="anchor id, omit for root")
    p_insert.add_argument("--text", required=True)
    p_delete = sub.add_parser("delete", help="tombstone visible character ids")
    p_delete.add_argument("--ids", nargs="+", required=True)
    p_annotate = sub.add_parser("annotate", help="annotate visible [start, end)")
    p_annotate.add_argument("--start", type=int, required=True)
    p_annotate.add_argument("--end", type=int, required=True)
    p_annotate.add_argument("--label", required=True)
    p_annotate.add_argument("--id", dest="annotation_id", default=None)
    p_delta = sub.add_parser("export-delta", help="events beyond a version vector")
    p_delta.add_argument("--version", default="{}", help="JSON version vector")
    p_merge = sub.add_parser("merge", help="atomically merge an event packet")
    p_merge.add_argument("packet", help="JSON file containing a list of events")
    sub.add_parser("export", help="export text and annotation destinations")

    args = parser.parse_args(argv)

    if args.command == "init":
        if not args.replica:
            parser.error("init requires --replica")
        if os.path.exists(args.store):
            parser.error(f"{args.store} already exists")
        replica = Replica(replica_id=args.replica, storage_path=args.store)
        print(json.dumps({"replica": replica.replica_id, "version": {}}, ensure_ascii=False))
        return 0

    replica = Replica(replica_id=args.replica, storage_path=args.store)
    if args.command == "insert":
        event = replica.insert(args.after, args.text)
        print(json.dumps(event, ensure_ascii=False))
    elif args.command == "delete":
        event = replica.delete(args.ids)
        print(json.dumps(event, ensure_ascii=False))
    elif args.command == "annotate":
        event = replica.annotate(args.start, args.end, args.label, args.annotation_id)
        print(json.dumps(event, ensure_ascii=False))
    elif args.command == "export-delta":
        print(json.dumps(replica.export_delta(json.loads(args.version)), ensure_ascii=False))
    elif args.command == "merge":
        print(json.dumps(replica.merge(_read_json(args.packet)), ensure_ascii=False))
    elif args.command == "export":
        print(json.dumps(replica.export(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CRDTError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
