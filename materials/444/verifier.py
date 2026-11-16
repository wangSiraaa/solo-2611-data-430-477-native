"""Independent verifier for the question-444 data contract.

This file deliberately does **not** import ``Sequence`` or the merge
implementation.  It rebuilds the character tree, tombstones and annotation
spans from an event DAG using only the documented contract:

* id = ``replica:seq:offset``;
* insert chains internally, first code point after ``after``;
* siblings sorted by ``(lamport, replica, seq, offset)`` descending;
* pre-order DFS; tombstones stay as anchors;
* annotations are inclusive endpoint character ids.

It is used by the test suite to cross-check the production code, and can be
run directly on a JSON dump of events.

Usage::

    python3 verifier.py events.json            # list of events
    python3 verifier.py --store replica.json   # a replica store
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional, Tuple


def _id_key(cid: str) -> Tuple[str, int, int]:
    replica, seq, offset = cid.split(":")
    return replica, int(seq), int(offset)


class IndependentView:
    def __init__(self, events: List[Dict[str, Any]]) -> None:
        self.events = events
        self.order_key: Dict[str, Tuple[int, str, int, int]] = {}
        self.char: Dict[str, str] = {}
        self.parent: Dict[str, Optional[str]] = {}
        self.deleted: set = set()

        for event in events:
            if event["op"] != "insert":
                continue
            previous: Optional[str] = event["after"]
            for offset, ch in enumerate(event["text"]):
                cid = f"{event['replica']}:{event['seq']}:{offset}"
                self.order_key[cid] = (
                    event["lamport"],
                    event["replica"],
                    event["seq"],
                    offset,
                )
                self.char[cid] = ch
                self.parent[cid] = previous
                previous = cid
        for event in events:
            if event["op"] == "delete":
                self.deleted.update(event["ids"])

        self.children: Dict[Optional[str], List[str]] = {}
        for cid, parent in self.parent.items():
            self.children.setdefault(parent, []).append(cid)
        for parent, ids in self.children.items():
            ids.sort(key=lambda cid: self.order_key[cid], reverse=True)

        self.order: List[str] = []
        stack = list(reversed(self.children.get(None, [])))
        while stack:
            cid = stack.pop()
            self.order.append(cid)
            stack.extend(reversed(self.children.get(cid, [])))

    def text(self) -> str:
        return "".join(self.char[cid] for cid in self.order if cid not in self.deleted)

    def visible_ids(self) -> List[str]:
        return [cid for cid in self.order if cid not in self.deleted]

    def span(self, start_id: str, end_id: str) -> Optional[Dict[str, Any]]:
        visible = self.visible_ids()
        if start_id not in visible or end_id not in visible:
            return None
        start = visible.index(start_id)
        end = visible.index(end_id)
        if start > end:
            return None
        quote = "".join(self.char[cid] for cid in visible[start : end + 1])
        return {"start": start, "end": end, "quote": quote}


def verify(events: List[Dict[str, Any]], annotations: List[Dict[str, Any]]) -> Dict[str, Any]:
    view = IndependentView(events)
    checked = []
    for annotation in annotations:
        if annotation["status"] != "RESOLVED":
            continue
        span = view.span(annotation["start_id"], annotation["end_id"])
        checked.append(
            {
                "id": annotation["id"],
                "matches_independent": span == {
                    "start": annotation["start"],
                    "end": annotation["end"],
                    "quote": annotation["quote"],
                },
                "independent_quote": None if span is None else span["quote"],
            }
        )
    return {"text": view.text(), "visible_ids": view.visible_ids(), "spans": checked}


def main(argv: List[str]) -> int:
    if "--store" in argv:
        with open(argv[argv.index("--store") + 1], encoding="utf-8") as handle:
            data = json.load(handle)
        events = data["events"]
    else:
        with open(argv[1], encoding="utf-8") as handle:
            events = json.load(handle)
    view = IndependentView(events)
    print(json.dumps({"text": view.text(), "visible_ids": view.visible_ids()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
