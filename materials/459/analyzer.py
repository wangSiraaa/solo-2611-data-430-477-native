"""DAG analyzer for question 459.

Input is an already-tokenized array of lowercase ASCII tokens.  No natural
language tokenization or Unicode normalization happens here.

Construction model
------------------
Scan the original tokens left to right.  At each position the first matching
rule (``len(lhs)`` descending, then ``id`` ascending) consumes its entire path;
otherwise the single token is consumed.  Generated rhs tokens are *not*
rescanned.  Every consumed fragment becomes a small two-branch DAG joined at a
shared entry and a shared exit node:

* the original ``lhs`` path (one arc per lhs token), and
* the ``rhs`` replacement path (one arc per rhs token).

The local width is ``max(len(lhs), len(rhs))``.  The shorter branch's final arc
spans the remaining positions to the common exit; every other arc advances one
position.  Fragments are concatenated sequentially (exit of one is the entry of
the next) while the original token spans and rule provenance stay attached to
arcs.

Every arc and node has an explicit identity, so repeated identical terms still
have distinct arc identities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from rules import RuleError, RuleSet, validate_token_array

MAX_COMPLETE_PATHS = 256
MAX_DOC_TOKENS = 100

# Enumeration of matches inside one candidate document is only needed for
# evidence selection.  This bound merely turns pathological input into a clear
# error instead of an unbounded walk.
MAX_MATCH_PATHS = 1_000_000


class PathLimitError(RuleError):
    """A DAG would have more complete paths than the configured limit."""


class DocLimitError(RuleError):
    """The document-count limit would be exceeded."""


@dataclass(frozen=True)
class Node:
    id: str
    ord: int
    slot: int
    kind: str  # entry | exit | boundary | lhs | rhs


@dataclass(frozen=True)
class Arc:
    id: str
    ord: int
    start: str
    end: str
    term: str
    kind: str  # token | lhs | rhs
    branch: str  # token | lhs | rhs
    rule_id: Any
    source_start: int
    source_end: int
    slot: int

    def rule_id_json(self) -> str | None:
        if self.rule_id is None:
            return None
        return json.dumps(self.rule_id, separators=(",", ":"), ensure_ascii=True)


class DAG:
    """A compact directed acyclic graph of term arcs."""

    def __init__(
        self,
        nodes: Iterable[Node],
        arcs: Iterable[Arc],
        entry: str,
        exit_node: str,
        token_count: int,
    ):
        self.nodes = list(nodes)
        self.arcs = list(arcs)
        self.node_by_id = {n.id: n for n in self.nodes}
        self.arc_by_id = {a.id: a for a in self.arcs}
        self.entry = entry
        self.exit = exit_node
        self.token_count = token_count

        if entry not in self.node_by_id or exit_node not in self.node_by_id:
            raise RuleError("DAG entry/exit node missing")
        self.out: dict[str, list[str]] = {n.id: [] for n in self.nodes}
        self.inc: dict[str, list[str]] = {n.id: [] for n in self.nodes}
        for arc in self.arcs:
            if arc.start not in self.node_by_id or arc.end not in self.node_by_id:
                raise RuleError(f"arc {arc.id} references an unknown node")
            self.out[arc.start].append(arc.id)
            self.inc[arc.end].append(arc.id)
        for node_id in self.out:
            self.out[node_id].sort(key=lambda aid: self.arc_by_id[aid].ord)
            self.inc[node_id].sort(key=lambda aid: self.arc_by_id[aid].ord)

    # -- path computation -------------------------------------------------
    def count_complete_paths(self, cap: int = MAX_COMPLETE_PATHS + 1) -> int:
        """Count entry->exit paths, saturating at ``cap``."""
        return self._count_from(self.entry, cap, {})

    def _count_from(self, node_id: str, cap: int, memo: dict[str, int]) -> int:
        if node_id == self.exit:
            return 1
        if node_id in memo:
            return memo[node_id]
        total = 0
        for aid in self.out[node_id]:
            total += self._count_from(self.arc_by_id[aid].end, cap, memo)
            if total >= cap:
                total = cap
                break
        memo[node_id] = total
        return total

    def enumerate_complete_paths(
        self, cap: int = MAX_COMPLETE_PATHS
    ) -> list[list[str]]:
        paths: list[list[str]] = []
        self._walk_complete(self.entry, [], paths, cap)
        return paths

    def _walk_complete(
        self, node_id: str, prefix: list[str], paths: list[list[str]], cap: int
    ) -> None:
        if node_id == self.exit:
            paths.append(list(prefix))
            if len(paths) > cap:
                raise PathLimitError(
                    f"DAG has more than {cap} complete paths"
                )
            return
        for aid in self.out[node_id]:
            prefix.append(aid)
            self._walk_complete(self.arc_by_id[aid].end, prefix, paths, cap)
            prefix.pop()

    def path_terms(self, arc_ids: Sequence[str]) -> list[str]:
        return [self.arc_by_id[aid].term for aid in arc_ids]

    def path_arcs(self, arc_ids: Sequence[str]) -> list[Arc]:
        return [self.arc_by_id[aid] for aid in arc_ids]

    def position_length(self, arc: Arc) -> int:
        """Number of DAG positions the arc spans (Lucene-style positionLength).

        A multi-token synonym arc can span more than one position, e.g. the
        ``nyc`` arc produced by ``new york -> nyc`` spans 2 positions.
        """
        return self.node_by_id[arc.end].slot - self.node_by_id[arc.start].slot

    def arc_public(self, arc: Arc, *, include_rule: bool = True) -> dict:
        record = {
            "arc_id": arc.id,
            "term": arc.term,
            "start_node": arc.start,
            "end_node": arc.end,
            "kind": arc.kind,
            "branch": arc.branch,
            "rule_id": arc.rule_id if include_rule else None,
            "source": [arc.source_start, arc.source_end],
            "slot": arc.slot,
            "position_length": self.position_length(arc),
        }
        return record

    def is_connected_arc_path(self, arc_ids: Sequence[str]) -> bool:
        if not arc_ids:
            return False
        prev_end = None
        for aid in arc_ids:
            arc = self.arc_by_id[aid]
            if prev_end is not None and arc.start != prev_end:
                return False
            prev_end = arc.end
        return True

    def source_span(self, arc_ids: Sequence[str]) -> tuple[int, int]:
        arcs = self.path_arcs(arc_ids)
        if not arcs:
            return (0, 0)
        return (
            min(a.source_start for a in arcs),
            max(a.source_end for a in arcs),
        )

    # -- serialisation ----------------------------------------------------
    def node_rows(self, doc_id: str) -> list[tuple]:
        return [
            (doc_id, n.id, n.ord, n.slot, n.kind) for n in self.nodes
        ]

    def arc_rows(self, doc_id: str) -> list[tuple]:
        return [
            (
                doc_id,
                a.id,
                a.ord,
                a.start,
                a.end,
                a.term,
                a.kind,
                a.branch,
                a.rule_id_json(),
                a.source_start,
                a.source_end,
                a.slot,
                self.position_length(a),
            )
            for a in self.arcs
        ]

    def posting_rows(self, doc_id: str) -> list[tuple]:
        return [
            (a.term, doc_id, a.id, a.start, a.end, a.slot) for a in self.arcs
        ]

    def path_records(self) -> list[dict]:
        out = []
        for i, arc_ids in enumerate(self.enumerate_complete_paths()):
            out.append(
                {
                    "index": i,
                    "arcs": list(arc_ids),
                    "terms": self.path_terms(arc_ids),
                }
            )
        return out

    def to_public(self) -> dict:
        count = self.count_complete_paths()
        return {
            "entry": self.entry,
            "exit": self.exit,
            "token_count": self.token_count,
            "nodes": [
                {"id": n.id, "ord": n.ord, "slot": n.slot, "kind": n.kind}
                for n in self.nodes
            ],
            "arcs": [
                {
                    "id": a.id,
                    "ord": a.ord,
                    "start": a.start,
                    "end": a.end,
                    "term": a.term,
                    "kind": a.kind,
                    "branch": a.branch,
                    "rule_id": a.rule_id,
                    "source": [a.source_start, a.source_end],
                    "slot": a.slot,
                    "position_length": self.position_length(a),
                }
                for a in self.arcs
            ],
            "path_count": count,
            "paths": self.path_records() if count <= MAX_COMPLETE_PATHS else None,
        }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def analyze(tokens: Sequence[str], rules: RuleSet | None = None) -> DAG:
    """Analyze a token array into a DAG using the supplied compiled rules."""
    ruleset = RuleSet.load(rules) if rules is not None else RuleSet([])
    clean = validate_token_array(tokens, "tokens")
    if len(clean) > MAX_DOC_TOKENS:
        raise RuleError(
            f"token array has {len(clean)} tokens, limit is {MAX_DOC_TOKENS}"
        )

    nodes: list[Node] = []

    def add_node(node_id: str, slot: int, kind: str) -> None:
        nodes.append(Node(node_id, len(nodes), slot, kind))

    arcs: list[Arc] = []

    def add_arc(
        arc_id: str,
        start: str,
        end: str,
        term: str,
        kind: str,
        branch: str,
        rule_id: Any,
        source_start: int,
        source_end: int,
        slot: int,
    ) -> None:
        arcs.append(
            Arc(
                id=arc_id,
                ord=len(arcs),
                start=start,
                end=end,
                term=term,
                kind=kind,
                branch=branch,
                rule_id=rule_id,
                source_start=source_start,
                source_end=source_end,
                slot=slot,
            )
        )

    add_node("b0", 0, "entry")
    prev = "b0"
    slot = 0
    pos = 0
    frag = 0

    while pos < len(clean):
        rule = ruleset.match_at(clean, pos)
        if rule is None:
            exit_node = f"b{frag + 1}"
            add_node(exit_node, slot + 1, "boundary")
            add_arc(
                f"a{frag}_t0",
                prev,
                exit_node,
                clean[pos],
                "token",
                "token",
                None,
                pos,
                pos + 1,
                slot,
            )
            pos += 1
            slot += 1
            frag += 1
            prev = exit_node
            continue

        lhs, rhs = rule.lhs, rule.rhs
        m, n = len(lhs), len(rhs)
        width = max(m, n)
        exit_node = f"b{frag + 1}"
        exit_slot = slot + width

        # Internal nodes for each branch.  The final arc of each branch ends at
        # the shared exit, so only j=1..len-1 need internal nodes.
        for j in range(1, m):
            add_node(f"f{frag}_l{j}", slot + j, "lhs")
        for j in range(1, n):
            add_node(f"f{frag}_r{j}", slot + j, "rhs")
        add_node(exit_node, exit_slot, "boundary")

        for j, term in enumerate(lhs):
            start = prev if j == 0 else f"f{frag}_l{j}"
            end = exit_node if j == m - 1 else f"f{frag}_l{j + 1}"
            # Matched lhs tokens map one-to-one onto original tokens.
            add_arc(
                f"a{frag}_l{j}",
                start,
                end,
                term,
                "lhs",
                "lhs",
                rule.id,
                pos + j,
                pos + j + 1,
                slot + j,
            )

        for j, term in enumerate(rhs):
            start = prev if j == 0 else f"f{frag}_r{j}"
            end = exit_node if j == n - 1 else f"f{frag}_r{j + 1}"
            # Generated rhs terms have no original token of their own; they
            # map back to the whole consumed lhs span.
            add_arc(
                f"a{frag}_r{j}",
                start,
                end,
                term,
                "rhs",
                "rhs",
                rule.id,
                pos,
                pos + m,
                slot + j,
            )

        pos += m
        slot = exit_slot
        frag += 1
        prev = exit_node

    # If the last fragment was a rule, the exit boundary already exists.  If
    # the input was empty, make the single entry node also the exit.
    if prev == "b0":
        nodes[0] = Node("b0", 0, 0, "exit")
    elif prev != f"b{frag}":
        raise RuleError("internal DAG construction error")

    dag = DAG(nodes, arcs, "b0", prev, len(clean))
    if len(clean) > 0 and prev == "b0":  # pragma: no cover - defensive
        raise RuleError("internal DAG construction error")
    path_count = dag.count_complete_paths()
    if path_count > MAX_COMPLETE_PATHS:
        raise PathLimitError(
            f"graph has more than {MAX_COMPLETE_PATHS} complete paths "
            f"({path_count} at least)"
        )
    return dag


def load_dag_from_rows(
    doc_id: str,
    node_rows: Iterable[tuple],
    arc_rows: Iterable[tuple],
    token_count: int,
    entry: str,
    exit_node: str,
) -> DAG:
    """Rebuild a DAG view from persisted rows (no re-analysis of text)."""
    node_rows = list(node_rows)
    arc_rows = list(arc_rows)
    nodes = [
        Node(id=row[1], ord=row[2], slot=row[3], kind=row[4])
        for row in node_rows
    ]
    arcs = []
    for row in arc_rows:
        (
            _doc,
            arc_id,
            arc_ord,
            start,
            end,
            term,
            kind,
            branch,
            rule_id_json,
            source_start,
            source_end,
            slot,
            stored_position_length,
        ) = row
        rule_id = None if rule_id_json is None else json.loads(rule_id_json)
        arcs.append(
            Arc(
                id=arc_id,
                ord=arc_ord,
                start=start,
                end=end,
                term=term,
                kind=kind,
                branch=branch,
                rule_id=rule_id,
                source_start=source_start,
                source_end=source_end,
                slot=slot,
            )
        )
    dag = DAG(nodes, arcs, entry, exit_node, token_count)
    # The persisted position_length must agree with node slots; this guards
    # against a stale/corrupt path index being read back.
    for row in arc_rows:
        arc = dag.arc_by_id[row[1]]
        if dag.position_length(arc) != row[12]:
            raise RuleError(
                f"persisted position_length mismatch on arc {arc.id!r}"
            )
    return dag
