# Design notes: tree ordering and stable position identity

## Character identity

A character's identity is `(replica, seq, offset)` rendered as
`"replica:seq:offset"`. It is allocated at insert-event creation and is never
re-derived from a position, so arrival order, packet shuffling, duplicate
delivery and indirect forwarding cannot renumber it. This is what lets a
delete, an insert anchor and an annotation endpoint keep pointing at the same
code point after the surrounding text moves.

## Tree order

Characters form an RGA-style tree. An insert event's first code point is a
child of its `after` anchor (or of the root when `after` is null); the
remaining code points of the same event chain after the previous code point.
Siblings of one anchor are ordered by `(lamport, replica, seq, offset)`
**descending**, and the document is the pre-order DFS of the tree. Because the
comparison key is built only from immutable event fields, every replica that
has the same event DAG materialises the same text.

Tradeoffs accepted:

* **Tombstones as anchors.** Deleted characters stay in the tree. A concurrent
  insert that anchored on a character another replica deleted still has a
  valid parent, so the new text lands in a deterministic place instead of
  being lost or moved. Cost: history and tree nodes grow without bound; we
  deliberately do not garbage-collect, as required.
* **Concurrent equal characters.** Two replicas inserting the same character
  at the same anchor are kept as two distinct nodes and ordered by the total
  key. Deleting one identity never deletes the other, even though the text is
  identical.
* **Deterministic re-materialisation.** `Sequence` is a projection, not the
  source of truth. `apply_events` walks the whole DAG, so the visible text and
  tombstones depend only on the set of events, never on the packet order.

## Merge

A merge validates the union of the local DAG and the packet before touching
state. It checks: canonical event shape; contiguous `1..n` prefixes per
replica; complete, transitively closed context version vectors; exact lamport
`1 + max(context lamports)`; anchor/delete/annotation references exist and are
covered by the creating event's context; deletes name only characters visible
in their own context; annotation endpoints are visible, ordered and quote-
consistent in their creation context; a single rooted tree; and the size
limits. Only after all checks pass is the union persisted and adopted. A
rejection is atomic: memory and the backing store are untouched, so reopening
reproduces the pre-import state (verified by a reopen-after-reject test).

Tradeoffs:

* **Whole-packet rejection.** One bad event rejects valid siblings in the same
  packet. This is simpler to reason about and guarantees no partial application;
  the sender retries with a corrected packet.
* **Context is authoritative for causality.** References are validated against
  each event's context, not against "whatever happens to be local", so a
  forwarded event cannot smuggle in a reference the creator had not seen.

## Annotation decoding

Annotations store inclusive first/last character ids plus the creation quote.
At export:

* Both endpoints visible and ordered -> `RESOLVED`. The span is the current
  visible run *between* the endpoints, so concurrent insertions in the middle
  are included, and the current code-point range, quote and endpoint ids are
  emitted.
* Any endpoint tombstoned (or misordered) -> `UNRESOLVED`. The original anchors
  and creation quote are preserved verbatim; there is no silent fallback to a
  neighbour and the item never enters training data.

Tradeoffs:

* **Anchors over offsets.** Integer offsets would silently retarget after an
  insertion; endpoint ids do not. The cost is that a deleted endpoint cannot be
  reconstructed to a "best guess" span, which is intentional: a wrong training
  sample is worse than a retained unresolved one.
* **Every annotation has a destination.** Export always returns each
  annotation, partitioned into `resolved` and `unresolved`, so none is dropped.
  Training data is built only from the resolved list.
