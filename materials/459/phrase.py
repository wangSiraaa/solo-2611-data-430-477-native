"""Compatibility query helpers for question 459.

``search`` keeps the original narrow signature (return the matching document
ids) for callers such as ``probe.py``.  ``search_detailed`` and
``explain`` expose the full compiled query plus per-document path evidence.
"""

from __future__ import annotations

from typing import Sequence


def search(index, terms: Sequence[str]) -> list[str]:
    """Return the ids of documents containing ``terms`` as an exact phrase."""
    return [hit["doc_id"] for hit in index.query(terms)["hits"]]


def search_detailed(index, terms: Sequence[str]) -> dict:
    """Return the full query result (compiled query, evidence, statistics)."""
    return index.query(terms)


def explain(index, terms: Sequence[str]) -> dict:
    """Return query results restricted to the evidence fields."""
    result = index.query(terms)
    return {
        "query_terms": result["query_terms"],
        "matches": [
            {
                "doc_id": hit["doc_id"],
                "doc_start_node": hit["doc_start_node"],
                "doc_end_node": hit["doc_end_node"],
                "doc_arcs": hit["doc_arcs"],
                "doc_terms": hit["doc_terms"],
                "query_arcs": hit["query_arcs"],
                "query_terms": hit["query_terms"],
                "doc_source_span": hit["doc_source_span"],
            }
            for hit in result["hits"]
        ],
        "evidence_order": result["evidence_order"],
    }
