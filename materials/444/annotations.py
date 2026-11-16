"""Anchored annotation export.

An annotation is stored as a pair of *character ids* (first and last, both
inclusive) plus its label and the quote the annotator saw.  It is never stored
as an integer offset, and it is never silently re-pointed at a neighbouring
character.

Export has exactly two destinations:

* ``resolved`` -- both endpoints are still visible and ordered.  The span is the
  current contiguous run of visible characters between them, so concurrent
  insertions between the endpoints are included.  The output carries the
  current code-point range, the quote and both endpoint ids.
* ``unresolved`` -- at least one endpoint is a tombstone (or the order is
  invalid).  The original creation anchors and quote are preserved and the
  annotation is reported as ``UNRESOLVED``.  It must not be treated as a
  successful training sample.

Every annotation therefore has a destination.  Training data is built from the
resolved list only.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from sequence import (
    EventError,
    Sequence,
    _is_int,
    canonical_event,
    parse_char_id,
)


UNRESOLVED = "UNRESOLVED"
RESOLVED = "RESOLVED"


class Annotations:
    def __init__(self) -> None:
        # id -> record with creation-time anchors and metadata.
        self.spans: Dict[str, Dict[str, Any]] = {}

    # -- construction -----------------------------------------------------
    def add(
        self,
        annotation_id: str,
        start: Any,
        end: Any,
        label: str,
        quote: Optional[str] = None,
        replica: Optional[str] = None,
        seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Register an anchored annotation record.

        ``start``/``end`` should be character ids at creation time (inclusive).
        For backward compatibility a pair of integer visible offsets is also
        accepted; it is translated to character ids *immediately* against the
        live sequences that currently contain that range.  Anchoring must
        happen at creation time -- integer offsets are never retained.
        """

        if not isinstance(annotation_id, str) or not annotation_id:
            raise EventError("annotation id must be a non-empty string")
        start, end, quote = self._anchor(start, end, quote)
        record = {
            "id": annotation_id,
            "start": start,
            "end": end,
            "label": label,
            "quote": quote,
            "replica": replica,
            "seq": seq,
        }
        existing = self.spans.get(annotation_id)
        if existing is not None:
            # Same identity, same content is a no-op; conflicting content is
            # an error rather than a silent overwrite.
            for key in ("start", "end", "label", "quote"):
                if existing.get(key) != record.get(key):
                    raise EventError(
                        f"annotation {annotation_id!r} already has different {key}"
                    )
            return existing
        self.spans[annotation_id] = record
        return record

    @staticmethod
    def _anchor(start: Any, end: Any, quote: Optional[str]) -> Tuple[str, str, Optional[str]]:
        if isinstance(start, str) and isinstance(end, str):
            parse_char_id(start)
            parse_char_id(end)
            return start, end, quote
        if not (_is_int(start) and _is_int(end)):
            raise EventError("annotation endpoints must be character ids")
        # Legacy integer offsets: resolve now, against a live sequence.
        from sequence import _ACTIVE_SEQUENCES

        candidates = {}
        for sequence in list(_ACTIVE_SEQUENCES):
            try:
                ids = sequence.visible_range_halfopen(start, end)
            except EventError:
                continue
            text = "".join(sequence.nodes[cid]["char"] for cid in ids)
            if quote is None or text == quote:
                candidates[(ids[0], ids[-1], text)] = True
        if not candidates:
            raise EventError(
                "cannot anchor integer annotation offsets: no live sequence has "
                "that visible range; pass character ids instead"
            )
        if len(candidates) > 1:
            raise EventError(
                "integer annotation offsets are ambiguous across live sequences; "
                "pass character ids instead"
            )
        (first, last, text), = candidates
        return first, last, quote if quote is not None else text

    def add_from_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event = canonical_event(event)
        if event["op"] != "annotate":
            raise EventError("not an annotate event")
        annotation_id = event.get("annotation_id") or f"{event['replica']}:{event['seq']}"
        return self.add(
            annotation_id,
            event["start"],
            event["end"],
            event["label"],
            event["quote"],
            event["replica"],
            event["seq"],
        )

    @classmethod
    def from_events(cls, events: Iterable[Dict[str, Any]]) -> "Annotations":
        annotations = cls()
        annotate_events = [canonical_event(e) for e in events if e.get("op") == "annotate"]
        annotate_events.sort(key=lambda e: (e["lamport"], e["replica"], e["seq"]))
        for event in annotate_events:
            annotations.add_from_event(event)
        return annotations

    # -- export -----------------------------------------------------------
    def _decode(
        self, record: Dict[str, Any], sequence: Sequence
    ) -> Dict[str, Any]:
        start_cid = record["start"]
        end_cid = record["end"]
        found = sequence.range_between(start_cid, end_cid)
        if found is None:
            visible = set(sequence.visible_ids())
            if start_cid in sequence.nodes and end_cid in sequence.nodes:
                reason = "endpoint_deleted"
            elif start_cid not in visible or end_cid not in visible:
                reason = "endpoint_not_visible"
            else:
                reason = "endpoint_misordered"
            return {
                "id": record["id"],
                "label": record["label"],
                "status": UNRESOLVED,
                "reason": reason,
                "start_id": start_cid,
                "end_id": end_cid,
                "quote": record["quote"],
                "created_quote": record["quote"],
            }

        start, end = found
        current_ids = sequence.visible_ids()[start : end + 1]
        quote = "".join(sequence.nodes[cid]["char"] for cid in current_ids)
        return {
            "id": record["id"],
            "label": record["label"],
            "status": RESOLVED,
            "start": start,
            "end": end,  # inclusive
            "range": [start, end + 1],  # half-open, convenient for slicing
            "start_id": start_cid,
            "end_id": end_cid,
            "quote": quote,
            "created_quote": record["quote"],
            "ids": current_ids,
        }

    def export(self, sequence: Sequence) -> List[Dict[str, Any]]:
        """All annotations, each resolved or explicitly UNRESOLVED."""

        return [self._decode(record, sequence) for _, record in sorted(self.spans.items())]

    def resolved(self, sequence: Sequence) -> List[Dict[str, Any]]:
        """Training data: only spans whose endpoints are still visible."""

        return [item for item in self.export(sequence) if item["status"] == RESOLVED]

    def unresolved(self, sequence: Sequence) -> List[Dict[str, Any]]:
        """Separately retained annotations; never training samples."""

        return [item for item in self.export(sequence) if item["status"] == UNRESOLVED]

    def training_data(self, sequence: Sequence) -> List[Dict[str, str]]:
        return [
            {
                "id": item["id"],
                "label": item["label"],
                "quote": item["quote"],
                "start_id": item["start_id"],
                "end_id": item["end_id"],
            }
            for item in self.resolved(sequence)
        ]
