"""Persistent DAG index + exact-phrase query compiler for question 459.

The index stores, per document, the analyzed DAG (nodes and term arcs) and a
posting list keyed by ``term`` that carries enough information to distinguish
the document identity, the arc identity, and the arc's start/end nodes.  It
never re-reads the original token array at query time: queries are compiled
with the *same* rule content identity that built the postings, and matching
walks the persisted arcs.

All mutation is transactional.  Bad input is analysed/validated before any row
is touched, so a rejected ``put`` leaves the previous version of the document
intact.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from analyzer import (
    DAG,
    MAX_COMPLETE_PATHS,
    MAX_DOC_TOKENS,
    MAX_MATCH_PATHS,
    PathLimitError,
    analyze,
    load_dag_from_rows,
)
from rules import (
    RuleError,
    RuleSet,
    RuleSetMismatch,
    validate_token_array,
)

SCHEMA_VERSION = "dag-index-v2"
MAX_DOCS = 100

# Documented, stable tie-break for choosing one piece of evidence per document.
EVIDENCE_ORDER = (
    "1. earliest original start token",
    "2. shortest original token span",
    "3. lexicographically smallest document arc-ord sequence",
    "4. smallest query complete-path index",
    "5. lexicographically smallest query arc-ord sequence",
)


class IndexError_(RuleError):
    """Base class for index-level failures."""


class IndexSchemaError(IndexError_):
    """The on-disk index is not in a schema this module understands."""


class QueryLimitError(IndexError_):
    """A query produced more candidate matches than the configured bound."""


@dataclass
class _Budget:
    remaining: int
    used: int = 0

    def consume(self) -> None:
        self.used += 1
        self.remaining -= 1
        if self.remaining < 0:
            raise QueryLimitError(
                f"query produced more than {MAX_MATCH_PATHS} candidate matches"
            )


def _row_bytes(values: Iterable[Any]) -> int:
    total = 0
    for value in values:
        if value is None:
            total += 1
        elif isinstance(value, int):
            total += 8
        else:
            total += len(str(value).encode("utf-8"))
    return total


class GraphIndex:
    """SQLite-backed persistent DAG phrase index."""

    def __init__(
        self,
        path: str,
        rules: RuleSet | Sequence[dict] | None = None,
        *,
        rebuild: bool = False,
    ):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._dag_cache: dict[str, DAG] = {}
        self._posting_cache: dict[str, list[sqlite3.Row]] = {}
        self.rules = self._resolve_rules(rules, rebuild)

    # ------------------------------------------------------------------
    # Setup / rules identity
    # ------------------------------------------------------------------
    def _tables(self) -> set[str]:
        rows = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r["name"] for r in rows}

    def _drop_all(self) -> None:
        for table in (
            "postings",
            "arcs",
            "nodes",
            "docs",
            "meta",
        ):
            self.db.execute(f"DROP TABLE IF EXISTS {table}")

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS docs(
                doc_id TEXT PRIMARY KEY,
                token_count INTEGER NOT NULL,
                entry TEXT NOT NULL,
                exit_node TEXT NOT NULL,
                path_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nodes(
                doc_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                ord INTEGER NOT NULL,
                slot INTEGER NOT NULL,
                kind TEXT NOT NULL,
                PRIMARY KEY(doc_id, node_id)
            );
            CREATE TABLE IF NOT EXISTS arcs(
                doc_id TEXT NOT NULL,
                arc_id TEXT NOT NULL,
                ord INTEGER NOT NULL,
                start_node TEXT NOT NULL,
                end_node TEXT NOT NULL,
                term TEXT NOT NULL,
                kind TEXT NOT NULL,
                branch TEXT NOT NULL,
                rule_id_json TEXT,
                source_start INTEGER NOT NULL,
                source_end INTEGER NOT NULL,
                slot INTEGER NOT NULL,
                position_length INTEGER NOT NULL,
                PRIMARY KEY(doc_id, arc_id)
            );
            CREATE TABLE IF NOT EXISTS postings(
                term TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                arc_id TEXT NOT NULL,
                start_node TEXT NOT NULL,
                end_node TEXT NOT NULL,
                slot INTEGER NOT NULL,
                PRIMARY KEY(term, doc_id, arc_id)
            );
            CREATE INDEX IF NOT EXISTS idx_postings_term_doc
                ON postings(term, doc_id);
            CREATE INDEX IF NOT EXISTS idx_arcs_doc_start
                ON arcs(doc_id, start_node);
            CREATE INDEX IF NOT EXISTS idx_arcs_doc_end
                ON arcs(doc_id, end_node);
            """
        )

    def _get_meta(self, key: str) -> str | None:
        row = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else row["value"]

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _resolve_rules(
        self,
        rules: RuleSet | Sequence[dict] | None,
        rebuild: bool,
    ) -> RuleSet:
        tables = self._tables()
        has_meta = "meta" in tables
        if has_meta:
            stored_version = self._get_meta("schema_version")
            if stored_version != SCHEMA_VERSION:
                if not rebuild:
                    raise IndexSchemaError(
                        f"index schema {stored_version!r} != {SCHEMA_VERSION!r}; "
                        "open with rebuild=True"
                    )
                with self.db:
                    self._drop_all()
                    self._create_schema()
                    return self._initialise_rules(rules)
            stored_fp = self._get_meta("rules_fingerprint")
            stored_json = self._get_meta("rules_json")
            if stored_fp is not None:
                stored_rules = RuleSet(json.loads(stored_json))
                if stored_rules.fingerprint != stored_fp:
                    raise IndexSchemaError("stored rules do not match fingerprint")
                if rules is None:
                    return stored_rules
                provided = RuleSet.load(rules)
                if provided.fingerprint == stored_fp:
                    return stored_rules
                if not rebuild:
                    raise RuleSetMismatch(
                        "ruleset content differs from the index postings; "
                        "pass rebuild=True to discard and rebuild (old postings "
                        "must never be interpreted with a new analyzer)"
                    )
                with self.db:
                    self._drop_all()
                    self._create_schema()
                    return self._initialise_rules(provided)
            # meta exists but no rules fingerprint: treat as unusable.
            if not rebuild:
                raise IndexSchemaError(
                    "index has no rules fingerprint; open with rebuild=True"
                )
            with self.db:
                self._drop_all()
                self._create_schema()
                return self._initialise_rules(rules)

        # No meta table.
        legacy = tables - {"sqlite_sequence"}
        if legacy and not rebuild:
            raise IndexSchemaError(
                f"found non-index tables {sorted(legacy)}; "
                "open with rebuild=True to replace them"
            )
        with self.db:
            if legacy:
                self._drop_all()
            self._create_schema()
            return self._initialise_rules(rules)

    def _initialise_rules(self, rules) -> RuleSet:
        ruleset = RuleSet.load(rules) if rules is not None else RuleSet([])
        self._set_meta("schema_version", SCHEMA_VERSION)
        self._set_meta("rules_fingerprint", ruleset.fingerprint)
        self._set_meta("rules_json", ruleset.to_json())
        self._set_meta(
            "rules_public",
            json.dumps(ruleset.to_public(), sort_keys=True),
        )
        return ruleset

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def put(self, doc_id: str, tokens: Sequence[str]) -> dict:
        if not isinstance(doc_id, str) or doc_id == "":
            raise RuleError("doc_id must be a non-empty string")
        clean = validate_token_array(tokens, "tokens")
        if len(clean) > MAX_DOC_TOKENS:
            raise RuleError(
                f"token array has {len(clean)} tokens, limit is {MAX_DOC_TOKENS}"
            )
        # Analyse completely before opening a write transaction: a path-limit
        # rejection here cannot damage the previous document version.
        dag = analyze(clean, self.rules)
        path_count = dag.count_complete_paths()

        with self.db:
            exists = self.db.execute(
                "SELECT 1 FROM docs WHERE doc_id=?", (doc_id,)
            ).fetchone()
            if not exists:
                count = self.db.execute(
                    "SELECT COUNT(*) AS n FROM docs"
                ).fetchone()["n"]
                if count >= MAX_DOCS:
                    raise RuleError(
                        f"document limit is {MAX_DOCS}; delete a document first"
                    )
            # Replacement: remove the old arcs/postings for this doc first.
            self._delete_rows(doc_id)
            self.db.execute(
                "INSERT INTO docs"
                "(doc_id,token_count,entry,exit_node,path_count)"
                " VALUES(?,?,?,?,?)",
                (doc_id, dag.token_count, dag.entry, dag.exit, path_count),
            )
            self.db.executemany(
                "INSERT INTO nodes"
                "(doc_id,node_id,ord,slot,kind) VALUES(?,?,?,?,?)",
                dag.node_rows(doc_id),
            )
            self.db.executemany(
                "INSERT INTO arcs"
                "(doc_id,arc_id,ord,start_node,end_node,term,kind,branch,"
                "rule_id_json,source_start,source_end,slot,position_length)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                dag.arc_rows(doc_id),
            )
            self.db.executemany(
                "INSERT INTO postings"
                "(term,doc_id,arc_id,start_node,end_node,slot)"
                " VALUES(?,?,?,?,?,?)",
                dag.posting_rows(doc_id),
            )
        self._dag_cache.pop(doc_id, None)
        self._posting_cache.clear()
        return {
            "doc_id": doc_id,
            "token_count": dag.token_count,
            "arcs": len(dag.arcs),
            "postings": len(dag.arcs),
            "nodes": len(dag.nodes),
            "path_count": path_count,
        }

    def _delete_rows(self, doc_id: str) -> None:
        self.db.execute("DELETE FROM postings WHERE doc_id=?", (doc_id,))
        self.db.execute("DELETE FROM arcs WHERE doc_id=?", (doc_id,))
        self.db.execute("DELETE FROM nodes WHERE doc_id=?", (doc_id,))
        self.db.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))

    def delete(self, doc_id: str) -> bool:
        if not isinstance(doc_id, str) or doc_id == "":
            raise RuleError("doc_id must be a non-empty string")
        with self.db:
            existed = self.db.execute(
                "SELECT 1 FROM docs WHERE doc_id=?", (doc_id,)
            ).fetchone()
            if not existed:
                return False
            self._delete_rows(doc_id)
        self._dag_cache.pop(doc_id, None)
        self._posting_cache.clear()
        return True

    # ------------------------------------------------------------------
    # Read helpers (persisted rows only; no re-tokenisation)
    # ------------------------------------------------------------------
    def doc_ids(self) -> list[str]:
        rows = self.db.execute(
            "SELECT doc_id FROM docs ORDER BY doc_id"
        ).fetchall()
        return [r["doc_id"] for r in rows]

    def postings(self, term: str) -> list[tuple]:
        rows = self.db.execute(
            "SELECT doc_id, arc_id, start_node, end_node, slot "
            "FROM postings WHERE term=? ORDER BY doc_id, slot, arc_id",
            (term,),
        ).fetchall()
        return [tuple(r) for r in rows]

    def _postings_for(self, term: str) -> list[sqlite3.Row]:
        cached = self._posting_cache.get(term)
        if cached is None:
            cached = self.db.execute(
                "SELECT doc_id, arc_id, start_node, end_node, slot "
                "FROM postings WHERE term=? ORDER BY doc_id, slot, arc_id",
                (term,),
            ).fetchall()
            self._posting_cache[term] = cached
        return cached

    def _load_dag(self, doc_id: str) -> DAG:
        cached = self._dag_cache.get(doc_id)
        if cached is not None:
            return cached
        row = self.db.execute(
            "SELECT token_count, entry, exit_node FROM docs WHERE doc_id=?",
            (doc_id,),
        ).fetchone()
        if row is None:
            raise IndexSchemaError(f"posting references missing doc {doc_id!r}")
        nodes = self.db.execute(
            "SELECT doc_id, node_id, ord, slot, kind FROM nodes "
            "WHERE doc_id=? ORDER BY ord",
            (doc_id,),
        ).fetchall()
        arcs = self.db.execute(
            "SELECT doc_id, arc_id, ord, start_node, end_node, term, kind, "
            "branch, rule_id_json, source_start, source_end, slot, "
            "position_length FROM arcs "
            "WHERE doc_id=? ORDER BY ord",
            (doc_id,),
        ).fetchall()
        dag = load_dag_from_rows(
            doc_id,
            [tuple(r) for r in nodes],
            [tuple(r) for r in arcs],
            row["token_count"],
            row["entry"],
            row["exit_node"],
        )
        self._dag_cache[doc_id] = dag
        return dag

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def query(self, tokens: Sequence[str]) -> dict:
        clean = validate_token_array(tokens, "query tokens")
        if len(clean) == 0:
            return {
                "query_terms": [],
                "hits": [],
                "rules": self.rules.to_public(),
                "query_graph": None,
                "stats": {
                    "query_paths": 0,
                    "candidate_postings": 0,
                    "candidate_arcs": 0,
                    "candidate_docs": 0,
                    "document_graphs_loaded": 0,
                    "matches_considered": 0,
                },
                "evidence_order": list(EVIDENCE_ORDER),
            }
        # Path-limit rejection is raised by analyze before any matching.
        qdag = analyze(clean, self.rules)
        qpaths = qdag.enumerate_complete_paths()

        # Group query complete paths by their term sequence.  Matching is a
        # property of the term sequence; the query arc sequence is evidence.
        by_terms: dict[tuple[str, ...], list[tuple[int, tuple[str, ...]]]] = {}
        for qidx, qarcs in enumerate(qpaths):
            terms = tuple(qdag.path_terms(qarcs))
            by_terms.setdefault(terms, []).append((qidx, tuple(qarcs)))

        candidate_postings = 0
        candidate_arcs: set[tuple[str, str]] = set()
        candidate_docs: set[str] = set()
        loaded: set[str] = set()
        matches_considered = 0
        per_doc: dict[str, list[dict]] = {}

        for terms, variants in by_terms.items():
            first = terms[0]
            rows = self._postings_for(first)
            candidate_postings += len(rows)
            if not rows:
                continue
            by_doc: dict[str, list[str]] = {}
            for row in rows:
                by_doc.setdefault(row["doc_id"], []).append(row["arc_id"])
                candidate_arcs.add((row["doc_id"], row["arc_id"]))
                candidate_docs.add(row["doc_id"])
            for doc_id, start_ids in by_doc.items():
                dag = self._load_dag(doc_id)  # persisted rows, not raw text
                loaded.add(doc_id)
                budget = _Budget(MAX_MATCH_PATHS)
                matches = _find_matches(dag, terms, start_ids, budget)
                matches_considered += len(matches)
                if not matches:
                    continue
                # Smallest query variant for this term sequence.
                qidx, qarcs = min(variants, key=lambda v: (v[0], tuple(
                    qdag.arc_by_id[a].ord for a in v[1]
                )))
                for doc_arcs in matches:
                    evidence = _build_evidence(
                        doc_id, dag, doc_arcs, qdag, qidx, qarcs
                    )
                    per_doc.setdefault(doc_id, []).append(evidence)

        hits = []
        for doc_id in sorted(per_doc):
            best = min(per_doc[doc_id], key=_evidence_key)
            hits.append(best)

        return {
            "query_terms": clean,
            "hits": hits,
            "rules": self.rules.to_public(),
            "query_graph": qdag.to_public(),
            "stats": {
                "query_paths": len(qpaths),
                "query_term_sequences": len(by_terms),
                "candidate_postings": candidate_postings,
                "candidate_arcs": len(candidate_arcs),
                "candidate_docs": len(candidate_docs),
                "document_graphs_loaded": len(loaded),
                "matches_considered": matches_considered,
                "diagnostics": {
                    "max_complete_paths": MAX_COMPLETE_PATHS,
                    "max_match_paths": MAX_MATCH_PATHS,
                },
            },
            "evidence_order": list(EVIDENCE_ORDER),
        }

    # ------------------------------------------------------------------
    # Inspection / storage statistics
    # ------------------------------------------------------------------
    def inspect(self) -> dict:
        docs = self.db.execute(
            "SELECT doc_id, token_count, entry, exit_node, path_count "
            "FROM docs ORDER BY doc_id"
        ).fetchall()
        node_count = self.db.execute("SELECT COUNT(*) AS n FROM nodes").fetchone()["n"]
        arc_count = self.db.execute("SELECT COUNT(*) AS n FROM arcs").fetchone()["n"]
        posting_count = self.db.execute(
            "SELECT COUNT(*) AS n FROM postings"
        ).fetchone()["n"]
        terms = self.db.execute(
            "SELECT COUNT(DISTINCT term) AS n FROM postings"
        ).fetchone()["n"]

        arc_bytes = 0
        for row in self.db.execute(
            "SELECT doc_id,arc_id,ord,start_node,end_node,term,kind,branch,"
            "rule_id_json,source_start,source_end,slot,position_length FROM arcs"
        ):
            arc_bytes += _row_bytes(tuple(row))
        posting_bytes = 0
        for row in self.db.execute(
            "SELECT term,doc_id,arc_id,start_node,end_node,slot FROM postings"
        ):
            posting_bytes += _row_bytes(tuple(row))
        node_bytes = 0
        for row in self.db.execute(
            "SELECT doc_id,node_id,ord,slot,kind FROM nodes"
        ):
            node_bytes += _row_bytes(tuple(row))

        enumerated_path_bytes = 0
        for doc in docs:
            dag = self._load_dag(doc["doc_id"])
            payload = json.dumps(dag.path_records(), separators=(",", ":"))
            enumerated_path_bytes += len(payload.encode("utf-8"))

        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        page_count = self.db.execute("PRAGMA page_count").fetchone()[0]

        doc_rows = []
        for doc in docs:
            dag = self._load_dag(doc["doc_id"])
            doc_rows.append(
                {
                    "doc_id": doc["doc_id"],
                    "token_count": doc["token_count"],
                    "entry": doc["entry"],
                    "exit_node": doc["exit_node"],
                    "path_count": doc["path_count"],
                    "node_count": len(dag.nodes),
                    "arc_count": len(dag.arcs),
                    "multi_position_arcs": [
                        {
                            "arc_id": a.id,
                            "term": a.term,
                            "position_length": dag.position_length(a),
                            "rule_id": a.rule_id,
                        }
                        for a in dag.arcs
                        if dag.position_length(a) > 1
                    ],
                }
            )

        return {
            "schema_version": SCHEMA_VERSION,
            "rules": self.rules.to_public(),
            "document_count": len(docs),
            "max_documents": MAX_DOCS,
            "documents": doc_rows,
            "distinct_terms": terms,
            "nodes": node_count,
            "arcs": arc_count,
            "postings": posting_count,
            "limits": {
                "max_doc_tokens": MAX_DOC_TOKENS,
                "max_complete_paths": MAX_COMPLETE_PATHS,
                "max_match_paths": MAX_MATCH_PATHS,
            },
            "evidence_order": list(EVIDENCE_ORDER),
            "storage_bytes": {
                "sqlite_file": page_size * page_count,
                "page_size": page_size,
                "page_count": page_count,
                "nodes_rows_estimate": node_bytes,
                "arcs_rows_estimate": arc_bytes,
                "postings_rows_estimate": posting_bytes,
                # The DAG's arcs (with start/end + doc identity) *are* the path
                # index, so nothing extra is persisted for paths.  The number
                # below is the size an eager enumeration would cost.
                "persisted_path_index": 0,
                "enumerated_paths_json_estimate": enumerated_path_bytes,
            },
            "inspection": {
                "posting_fields": [
                    "term",
                    "doc_id",
                    "arc_id",
                    "start_node",
                    "end_node",
                    "slot",
                ],
                "path_index": "arcs(doc_id, arc_id, start_node, end_node, ...)",
            },
        }

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "GraphIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Matching against a persisted DAG
# ---------------------------------------------------------------------------


def _find_matches(
    dag: DAG,
    terms: tuple[str, ...],
    start_arc_ids: Sequence[str],
    budget: _Budget,
) -> list[tuple[str, ...]]:
    """Find contiguous directed paths in ``dag`` equal to ``terms``.

    The first arc must be one of ``start_arc_ids`` (from the posting list).
    Every following arc must start exactly where the previous one ended.
    """
    results: list[tuple[str, ...]] = []
    if not terms:
        return results
    for aid in start_arc_ids:
        arc = dag.arc_by_id.get(aid)
        if arc is None:
            raise IndexSchemaError(
                f"posting references missing arc {aid!r} in {dag.entry!r} doc"
            )
        if arc.term != terms[0]:
            continue
        prefix = [aid]
        if len(terms) == 1:
            results.append(tuple(prefix))
            budget.consume()
        else:
            _dfs_matches(dag, arc.end, terms, 1, prefix, results, budget)
        if budget.remaining < 0:
            break
    return results


def _dfs_matches(
    dag: DAG,
    node_id: str,
    terms: tuple[str, ...],
    index: int,
    prefix: list[str],
    results: list[tuple[str, ...]],
    budget: _Budget,
) -> None:
    if index == len(terms):
        results.append(tuple(prefix))
        budget.consume()
        return
    for aid in dag.out[node_id]:
        if dag.arc_by_id[aid].term != terms[index]:
            continue
        prefix.append(aid)
        _dfs_matches(
            dag, dag.arc_by_id[aid].end, terms, index + 1, prefix, results, budget
        )
        prefix.pop()
        if budget.remaining < 0:
            return


def _build_evidence(
    doc_id: str,
    dag: DAG,
    doc_arcs: tuple[str, ...],
    qdag: DAG,
    qpath_index: int,
    query_arcs: tuple[str, ...],
) -> dict:
    if not dag.is_connected_arc_path(doc_arcs):
        raise IndexSchemaError("matched document arcs are not connected")
    start_node = dag.arc_by_id[doc_arcs[0]].start
    end_node = dag.arc_by_id[doc_arcs[-1]].end
    doc_terms = dag.path_terms(doc_arcs)
    q_terms = qdag.path_terms(query_arcs)
    if doc_terms != q_terms:
        raise IndexSchemaError("matched terms differ from query terms")
    span = dag.source_span(doc_arcs)
    return {
        "doc_id": doc_id,
        "doc_start_node": start_node,
        "doc_end_node": end_node,
        "doc_arcs": list(doc_arcs),
        "doc_arc_ordinals": [dag.arc_by_id[a].ord for a in doc_arcs],
        "doc_terms": doc_terms,
        "query_arcs": list(query_arcs),
        "query_arc_ordinals": [qdag.arc_by_id[a].ord for a in query_arcs],
        "query_terms": q_terms,
        "query_path_index": qpath_index,
        "doc_source_span": [span[0], span[1]],
        "doc_arc_evidence": [dag.arc_public(arc) for arc in dag.path_arcs(doc_arcs)],
        "query_arc_evidence": [
            qdag.arc_public(arc) for arc in qdag.path_arcs(query_arcs)
        ],
        "connected": True,
        "maps_to_original_tokens": [span[0], span[1]],
    }


def _evidence_key(evidence: dict) -> tuple:
    span = evidence["doc_source_span"]
    return (
        span[0],
        span[1] - span[0],
        tuple(evidence["doc_arc_ordinals"]),
        evidence["query_path_index"],
        tuple(evidence["query_arc_ordinals"]),
    )
