"""Stable-identity sequence CRDT and event-contract validation.

The sequence is an RGA-style tree of characters:

* A character id is ``"<replica>:<seq>:<offset>"``.  It is allocated once,
  when the insert event is created, and is never re-numbered afterwards.
* An insert event names the character it is anchored *after* (``None`` is the
  document root).  The first code point uses that anchor; every further code
  point in the same event is chained after the previous code point of that
  event.
* Children of an anchor are visited in descending
  ``(lamport, replica, seq, offset)`` order.  A pre-order depth-first walk of
  the tree is the document order.
* Deleted characters stay in the tree as tombstones and still act as anchors.
* Text is handled as raw Unicode code points.  No NFC/NFD normalisation is
  performed anywhere.

This module intentionally contains no import-time dependency on the merge or
annotation modules, so the data contract and the ordering interpreter can be
used independently.
"""

from __future__ import annotations

import json
import re
import weakref
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Contract limits.  Keep them public so callers and tests use the same values.
MAX_EVENTS_PER_REPLICA = 500
MAX_VISIBLE_CODEPOINTS = 2000
MAX_REPLICAS = 3

_REPLICA_RE = re.compile(r"^[^\s:]+$")

# Legacy convenience registry.  ``Annotations.add`` historically accepted
# integer offsets; with no sequence argument it cannot know which characters
# those offsets referred to.  Live sequences register here so that such a call
# can be anchored to every currently known sequence's visible characters at
# creation time.  New code should pass character ids (or use
# ``Replica.annotate``) and never rely on this adapter.
_ACTIVE_SEQUENCES: "weakref.WeakSet[Sequence]" = weakref.WeakSet()


class CRDTError(ValueError):
    """Base class for contract violations."""


class EventError(CRDTError):
    """An event is malformed or violates the event data contract."""


class MergeError(CRDTError):
    """An import packet must be rejected as a whole."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    # bool is a subclass of int, but it is not a valid seq/lamport/offset.
    return isinstance(value, int) and not isinstance(value, bool)


def valid_replica(replica: Any) -> bool:
    return isinstance(replica, str) and bool(_REPLICA_RE.match(replica))


def char_id(replica: str, seq: int, offset: int) -> str:
    """Return the canonical character id for ``(replica, seq, offset)``."""

    if not valid_replica(replica):
        raise EventError("invalid replica id")
    if not _is_int(seq) or seq < 1:
        raise EventError("seq must be an integer >= 1")
    if not _is_int(offset) or offset < 0:
        raise EventError("offset must be an integer >= 0")
    return f"{replica}:{seq}:{offset}"


def parse_char_id(value: Any) -> Tuple[str, int, int]:
    """Parse and canonicalise ``"replica:seq:offset"``.

    Aliases such as ``"R:01:0"`` are rejected so a character always has one
    spelling.  This matters because character ids are the stable anchors used
    by deletes and annotations.
    """

    if not isinstance(value, str):
        raise EventError("character id must be a string")
    parts = value.split(":")
    if len(parts) != 3:
        raise EventError(f"invalid character id {value!r}")
    replica, seq_s, offset_s = parts
    if not valid_replica(replica):
        raise EventError(f"invalid replica in character id {value!r}")
    if not seq_s.isdigit() or not offset_s.isdigit():
        raise EventError(f"invalid character id {value!r}")
    seq, offset = int(seq_s), int(offset_s)
    canonical = char_id(replica, seq, offset)
    if value != canonical:
        raise EventError(f"non-canonical character id {value!r}")
    return replica, seq, offset


def event_key(event: Dict[str, Any]) -> Tuple[str, int]:
    return event["replica"], event["seq"]


def event_id(event: Dict[str, Any]) -> str:
    return f"{event['replica']}:{event['seq']}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _check_codepoints(text: Any, what: str = "text") -> str:
    if not isinstance(text, str):
        raise EventError(f"{what} must be a string")
    if not text:
        raise EventError(f"{what} must not be empty")
    for ch in text:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            raise EventError(f"{what} contains an unpaired surrogate code point")
    return text


def _canonical_context(context: Any) -> Dict[str, int]:
    if not isinstance(context, dict):
        raise EventError("context must be an object mapping replica -> seq")
    out: Dict[str, int] = {}
    for replica, seq in context.items():
        if not valid_replica(replica):
            raise EventError(f"invalid replica in context: {replica!r}")
        if not _is_int(seq) or seq < 0:
            raise EventError("context entries must be integers >= 0")
        if seq:
            # A zero entry means "nothing known" and is not part of the
            # canonical vector, so {} and {"R": 0} denote the same context.
            out[replica] = seq
    return out


# ---------------------------------------------------------------------------
# Event canonicalisation
# ---------------------------------------------------------------------------

_BASE_KEYS = {"replica", "seq", "op", "context", "lamport"}
_OP_KEYS = {
    "insert": {"after", "text"},
    "delete": {"ids"},
    "annotate": {"start", "end", "label", "quote", "annotation_id"},
}


def canonical_event(event: Any) -> Dict[str, Any]:
    """Validate an event and return its canonical representation.

    Event identity is ``(replica, seq)``.  The canonical representation is
    what gets compared when the same identity arrives twice: identical content
    is a no-op, different content rejects the import packet.
    """

    if not isinstance(event, dict):
        raise EventError("event must be an object")
    allowed = set(_BASE_KEYS)
    op = event.get("op")
    if op not in _OP_KEYS:
        raise EventError(f"unknown or missing op: {op!r}")
    allowed |= _OP_KEYS[op]
    extra = set(event) - allowed
    if extra:
        raise EventError(f"unexpected event fields: {sorted(extra)!r}")
    missing = _BASE_KEYS - set(event)
    if missing:
        raise EventError(f"missing event fields: {sorted(missing)!r}")

    replica = event["replica"]
    if not valid_replica(replica):
        raise EventError("invalid replica id")
    seq = event["seq"]
    if not _is_int(seq) or seq < 1:
        raise EventError("seq must be an integer >= 1")
    if seq > MAX_EVENTS_PER_REPLICA:
        raise EventError(
            f"replica {replica!r} exceeds the {MAX_EVENTS_PER_REPLICA}-event limit"
        )
    context = _canonical_context(event["context"])
    # A replica's context is the state *before* this event, so it must know
    # exactly its own prior events and no event from this replica at or after
    # this seq.
    if context.get(replica, 0) != seq - 1:
        raise EventError(
            f"event {replica}:{seq} must have context[{replica!r}] == {seq - 1}"
        )
    lamport = event["lamport"]
    if not _is_int(lamport) or lamport < 1:
        raise EventError("lamport must be an integer >= 1")

    out: Dict[str, Any] = {
        "replica": replica,
        "seq": seq,
        "op": op,
        "context": context,
        "lamport": lamport,
    }

    if op == "insert":
        after = event["after"]
        if after is not None:
            anchor_replica, anchor_seq, _ = parse_char_id(after)
            if (anchor_replica, anchor_seq) == (replica, seq):
                # Characters later in this same event are chained internally;
                # the explicit ``after`` must name a *previously known* char.
                raise EventError("insert cannot anchor on its own event")
        text = _check_codepoints(event["text"], "insert text")
        out["after"] = after
        out["text"] = text
    elif op == "delete":
        ids = event["ids"]
        if not isinstance(ids, list):
            raise EventError("delete ids must be a list")
        canonical_ids: List[str] = []
        for cid in ids:
            parse_char_id(cid)
            canonical_ids.append(cid)
        out["ids"] = canonical_ids
    else:  # annotate
        start = event["start"]
        end = event["end"]
        parse_char_id(start)
        parse_char_id(end)
        label = event["label"]
        if not isinstance(label, str) or not label:
            raise EventError("annotation label must be a non-empty string")
        quote = event["quote"]
        _check_codepoints(quote, "annotation quote")
        out["start"] = start
        out["end"] = end
        out["label"] = label
        out["quote"] = quote
        annotation_id = event.get("annotation_id")
        if annotation_id is not None:
            if not isinstance(annotation_id, str) or not annotation_id:
                raise EventError("annotation_id must be a non-empty string")
            out["annotation_id"] = annotation_id

    return out


def canonical_event_key(event: Dict[str, Any]) -> Tuple[Tuple[str, int], str]:
    return event_key(event), canonical_json(canonical_event(event))


# ---------------------------------------------------------------------------
# Tree construction (shared by Sequence and the validators)
# ---------------------------------------------------------------------------

def _event_nodes(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the character nodes created by an insert event, in text order."""

    replica, seq = event["replica"], event["seq"]
    nodes: List[Dict[str, Any]] = []
    previous: Optional[str] = event["after"]
    for offset, ch in enumerate(event["text"]):
        cid = char_id(replica, seq, offset)
        nodes.append(
            {
                "id": cid,
                "char": ch,
                "after": previous,
                "replica": replica,
                "seq": seq,
                "offset": offset,
                "lamport": event["lamport"],
                "deleted": False,
            }
        )
        previous = cid
    return nodes


def node_order_key(node: Dict[str, Any]) -> Tuple[int, str, int, int]:
    return (node["lamport"], node["replica"], node["seq"], node["offset"])


def build_children(nodes: Dict[str, Dict[str, Any]]) -> Dict[Optional[str], List[str]]:
    children: Dict[Optional[str], List[str]] = {}
    for cid, node in nodes.items():
        parent = node["after"]
        if parent is not None and parent not in nodes:
            raise EventError(f"anchor {parent!r} of {cid!r} is missing")
        children.setdefault(parent, []).append(cid)
    for parent, ids in children.items():
        ids.sort(key=lambda cid: node_order_key(nodes[cid]), reverse=True)
    return children


def tree_order(nodes: Dict[str, Dict[str, Any]]) -> List[str]:
    """Pre-order DFS of the character tree, tombstones included."""

    children = build_children(nodes)
    result: List[str] = []
    # Iterative DFS: the visible text limit is small, but tombstoned history
    # may be much larger, so recursion is not worth risking.
    stack: List[str] = list(reversed(children.get(None, [])))
    while stack:
        cid = stack.pop()
        result.append(cid)
        stack.extend(reversed(children.get(cid, [])))
    if len(result) != len(nodes):
        raise EventError("character tree is not a single tree rooted at None")
    return result


def nodes_from_events(events: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Collect character nodes from insert events, independent of event order."""

    nodes: Dict[str, Dict[str, Any]] = {}
    for event in events:
        if event["op"] != "insert":
            continue
        for node in _event_nodes(event):
            cid = node["id"]
            old = nodes.get(cid)
            if old is not None:
                if old["char"] != node["char"] or old["after"] != node["after"]:
                    raise EventError(f"conflicting content for character {cid!r}")
                continue
            nodes[cid] = node
    # Anchors are checked after every node has been collected, so the caller's
    # iteration order cannot change acceptance.
    for cid, node in nodes.items():
        parent = node["after"]
        if parent is not None and parent not in nodes:
            raise EventError(f"anchor {parent!r} of {cid!r} is missing")
    return nodes


def tombstoned_ids(events: Iterable[Dict[str, Any]]) -> Set[str]:
    out: Set[str] = set()
    for event in events:
        if event["op"] == "delete":
            out.update(event["ids"])
    return out


# ---------------------------------------------------------------------------
# Sequence
# ---------------------------------------------------------------------------

class Sequence:
    """Materialised view of a character tree.

    The authoritative data is the event DAG held by :class:`merge.Replica`;
    this class is a deterministic projection.  It contains no arrival-order
    dependent state.
    """

    def __init__(self) -> None:
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._visible: List[str] = []
        self._vis_index: Dict[str, int] = {}
        _ACTIVE_SEQUENCES.add(self)

    # -- construction -----------------------------------------------------
    def clear(self) -> None:
        self.nodes.clear()
        self._order = []
        self._visible = []
        self._vis_index = {}

    def insert(self, event: Dict[str, Any]) -> None:
        """Apply one insert event directly (idempotent by character id)."""

        for node in _event_nodes(event):
            cid = node["id"]
            parent = node["after"]
            if parent is not None and parent not in self.nodes:
                raise EventError(f"anchor {parent!r} of {cid!r} is missing")
            old = self.nodes.get(cid)
            if old is not None:
                if old["char"] != node["char"] or old["after"] != node["after"]:
                    raise EventError(f"conflicting content for character {cid!r}")
                continue
            self.nodes[cid] = node
        self._rebuild()

    def delete(self, ids: Iterable[str], tombstone: bool = True) -> None:
        for cid in ids:
            node = self.nodes.get(cid)
            if node is None:
                # A validated delete event always references characters that
                # exist.  Keep direct calls tolerant so a delete can be
                # applied before its insert without losing the tombstone.
                continue
            node["deleted"] = bool(tombstone)
        self._rebuild()

    def apply_events(self, events: Iterable[Dict[str, Any]]) -> None:
        """Rebuild the view from the complete event DAG.

        ``nodes_from_events`` collects every character before checking anchors,
        so neither the tree nor the tombstones depend on event arrival order.
        """

        events = list(events)
        nodes = nodes_from_events(events)
        for cid in tombstoned_ids(events):
            if cid in nodes:
                nodes[cid]["deleted"] = True
        self.nodes = nodes
        self._rebuild()

    def _rebuild(self) -> None:
        self._order = tree_order(self.nodes)
        self._visible = [cid for cid in self._order if not self.nodes[cid]["deleted"]]
        self._vis_index = {cid: i for i, cid in enumerate(self._visible)}

    # -- queries ----------------------------------------------------------
    def ids(self) -> List[str]:
        return list(self._order)

    def visible_ids(self) -> List[str]:
        return list(self._visible)

    def text(self) -> str:
        return "".join(self.nodes[cid]["char"] for cid in self._visible)

    def visible_length(self) -> int:
        return len(self._visible)

    def index_of(self, cid: str) -> int:
        """Visible index of a character.  Raises KeyError for tombstones."""

        return self._vis_index[cid]

    def id_at_visible(self, index: int) -> str:
        return self._visible[index]

    def visible_range_ids(self, start: int, end: int) -> List[str]:
        """Inclusive visible-index range, used by annotation creation."""

        if not (_is_int(start) and _is_int(end)):
            raise EventError("range endpoints must be integers")
        if start < 0 or end < start or end >= len(self._visible):
            raise EventError("annotation range must be a non-empty visible range")
        return self._visible[start : end + 1]

    def visible_range_halfopen(self, start: int, end: int) -> List[str]:
        """Half-open ``[start, end)`` visible range (the probe's convention)."""

        if not (_is_int(start) and _is_int(end)):
            raise EventError("range endpoints must be integers")
        if start < 0 or end <= start or end > len(self._visible):
            raise EventError("annotation range must be a non-empty visible range")
        return self._visible[start:end]

    def visible_slice(self, start: int, end: int) -> str:
        """Inclusive visible text slice."""

        return "".join(self.nodes[cid]["char"] for cid in self.visible_range_ids(start, end))

    def range_between(self, start_cid: str, end_cid: str) -> Optional[Tuple[int, int]]:
        """Return inclusive visible indices for two endpoint ids.

        ``None`` means at least one endpoint is not currently visible, or the
        endpoints are in the wrong order.  Callers must not "repair" this by
        substituting a neighbour.
        """

        if start_cid not in self._visible or end_cid not in self._visible:
            return None
        start = self.index_of(start_cid)
        end = self.index_of(end_cid)
        if start > end:
            return None
        return start, end

    def render(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": cid,
                "char": self.nodes[cid]["char"],
                "deleted": self.nodes[cid]["deleted"],
            }
            for cid in self._order
        ]
