#!/usr/bin/env python3
"""Independent verification suite for question 459.

The oracle never reads the index under test.  It regenerates the *set of
complete string paths* directly from the original token arrays and the rule
content (greedy scan, longer-lhs-first / id tie-break, no rhs recursion), then
checks phrase containment by naive contiguous-subsequence comparison.

For every reported hit we also verify that the supplied arc evidence is a
genuinely connected directed path, that its terms equal the query path terms,
that it maps back to the reported original-token span, and that the query terms
occur contiguously in some independently generated document path.

Run with:  python3 tests_459.py
"""

from __future__ import annotations

import itertools
import json
import os
import tempfile
import traceback

from graph_index import (
    GraphIndex,
    IndexSchemaError,
    PathLimitError,
    QueryLimitError,
)
from rules import RuleError, RuleSet, RuleSetMismatch

HERE = os.path.dirname(os.path.abspath(__file__))
FAILURES: list[str] = []
CHECKS = 0


def check(cond: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(message)


# ---------------------------------------------------------------------------
# Oracle (independent of the implementation under test)
# ---------------------------------------------------------------------------


def ordered_rules(rule_objs):
    """Published priority: longer lhs first, then id ascending."""
    return sorted(rule_objs, key=lambda r: (-len(r["lhs"]), r["id"]))


def independent_path_terms(tokens, rule_objs):
    """All complete string paths, generated directly from tokens + rules."""
    ordered = ordered_rules(rule_objs)
    seqs = {()}
    pos = 0
    while pos < len(tokens):
        chosen = None
        for rule in ordered:
            lhs = rule["lhs"]
            if len(lhs) <= len(tokens) - pos and list(tokens[pos : pos + len(lhs)]) == lhs:
                chosen = rule
                break
        if chosen is None:
            options = [(tokens[pos],)]
            pos += 1
        else:
            options = [tuple(chosen["lhs"]), tuple(chosen["rhs"])]
            pos += len(chosen["lhs"])
        seqs = {prefix + option for prefix in seqs for option in options}
    return seqs


def independent_query_path_terms(tokens, rule_objs):
    return independent_path_terms(tokens, rule_objs)


def contains_contiguous(haystack, needle):
    n = len(needle)
    if n == 0:
        return True
    for i in range(len(haystack) - n + 1):
        if tuple(haystack[i : i + n]) == tuple(needle):
            return True
    return False


def oracle_docs(docs, query_tokens, rule_objs):
    qseqs = independent_query_path_terms(query_tokens, rule_objs)
    hits = set()
    for doc_id, tokens in docs.items():
        doc_paths = independent_path_terms(tokens, rule_objs)
        for qseq in qseqs:
            if any(contains_contiguous(path, qseq) for path in doc_paths):
                hits.add(doc_id)
                break
    return hits


# ---------------------------------------------------------------------------
# Evidence validation
# ---------------------------------------------------------------------------


def verify_evidence(result, docs, rule_objs):
    q_terms = tuple(result["query_terms"])
    qseqs = independent_query_path_terms(q_terms, rule_objs)
    check(len(qseqs) > 0, "query should have at least one complete path")
    seen_docs = [hit["doc_id"] for hit in result["hits"]]
    check(len(seen_docs) == len(set(seen_docs)), "duplicate doc in hits")

    for hit in result["hits"]:
        doc_id = hit["doc_id"]
        check(doc_id in docs, f"hit doc {doc_id} not in corpus")
        evidence = hit["doc_arc_evidence"]
        check(len(evidence) == len(hit["doc_arcs"]), "doc_arcs/evidence length")
        check(bool(evidence), "empty evidence")
        if not evidence:
            continue
        check(evidence[0]["start_node"] == hit["doc_start_node"], "start node")
        check(evidence[-1]["end_node"] == hit["doc_end_node"], "end node")
        # Connectivity of the supplied arcs.
        prev_end = None
        for arc in evidence:
            if prev_end is not None:
                check(arc["start_node"] == prev_end, "arc chain broken")
            prev_end = arc["end_node"]
        # Terms equal the query path.
        doc_terms = tuple(a["term"] for a in evidence)
        check(doc_terms == q_terms, f"doc terms {doc_terms} != query {q_terms}")
        check(
            tuple(hit["doc_terms"]) == q_terms,
            "doc_terms field disagrees with evidence",
        )
        # Span maps back to original tokens.
        span = hit["doc_source_span"]
        check(span == hit["maps_to_original_tokens"], "span aliases disagree")
        check(0 <= span[0] <= span[1] <= len(docs[doc_id]), "span out of bounds")
        for arc in evidence:
            s, e = arc["source"]
            check(span[0] <= s <= e <= span[1], "arc source outside span")
            check(e > s, "empty arc source span")
            check(idx_len(docs[doc_id], s, e) >= 1, "arc source empty")
        # Position length sanity for multi-token synonyms.
        for arc in evidence:
            check(arc["position_length"] >= 1, "position_length >= 1")
            if arc["kind"] == "rhs" and arc["rule_id"] is not None:
                pass
        # Query arc evidence connected + same terms.
        qe = hit["query_arc_evidence"]
        prev = None
        for arc in qe:
            if prev is not None:
                check(arc["start_node"] == prev, "query arc chain broken")
            prev = arc["end_node"]
        check(
            tuple(a["term"] for a in qe) == q_terms,
            "query evidence terms mismatch",
        )
        # The query term sequence occurs contiguously in some independent
        # complete document path.
        doc_paths = independent_path_terms(docs[doc_id], rule_objs)
        check(
            any(contains_contiguous(path, q_terms) for path in doc_paths),
            f"oracle says {doc_id} should not match {q_terms}",
        )


def idx_len(tokens, start, end):
    return len(tokens[start:end])


# ---------------------------------------------------------------------------
# Test harness helpers
# ---------------------------------------------------------------------------


def make_rules(entries):
    return RuleSet(entries)


def run_case(name, rule_entries, docs, query, expect_hit_docs=None):
    rule_objs = [
        {"id": r["id"], "lhs": list(r["lhs"]), "rhs": list(r["rhs"])}
        for r in rule_entries
    ]
    rules = make_rules(rule_objs)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "idx.sqlite")
        index = GraphIndex(path, rules)
        for doc_id, tokens in docs.items():
            index.put(doc_id, tokens)
        result = index.query(query)
        got = {hit["doc_id"] for hit in result["hits"]}
        expected = oracle_docs(docs, query, rule_objs)
        check(
            got == expected,
            f"[{name}] got {sorted(got)} expected {sorted(expected)} for {query}",
        )
        if expect_hit_docs is not None:
            check(
                got == set(expect_hit_docs),
                f"[{name}] explicit expectation {expect_hit_docs} != {sorted(got)}",
            )
        verify_evidence(result, docs, rule_objs)
        index.close()
        # Reopen and repeat: persisted index must be sufficient.
        index2 = GraphIndex(path, rules)
        result2 = index2.query(query)
        got2 = {hit["doc_id"] for hit in result2["hits"]}
        check(got2 == expected, f"[{name}] reopen query changed results")
        index2.close()


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_probe_semantics():
    rules = [
        {"id": "new-york", "lhs": ["new", "york"], "rhs": ["nyc"]},
        {"id": "machine-learning", "lhs": ["machine", "learning"], "rhs": ["ml"]},
    ]
    docs = {"d": ["new", "york", "city"]}
    run_case("probe-normal", rules, docs, ["new", "york"], {"d"})
    run_case("probe-false", rules, docs, ["nyc", "york"], set())
    run_case("probe-nyc-city", rules, docs, ["nyc", "city"], {"d"})
    run_case("probe-original", rules, docs, ["new", "york", "city"], {"d"})


def test_rhs_longer_than_lhs():
    rules = [{"id": "r", "lhs": ["usa"], "rhs": ["united", "states"]}]
    docs = {"d": ["usa", "x"]}
    run_case("rhs-longer-query-expansion", rules, docs, ["united", "states"], {"d"})
    run_case("rhs-longer-original", rules, docs, ["usa", "x"], {"d"})
    run_case("rhs-longer-no-jump", rules, docs, ["united"], {"d"})

    # Verify the DAG has two arcs leaving the same position with different
    # position lengths.
    rset = make_rules(rules)
    with tempfile.TemporaryDirectory() as tmp:
        index = GraphIndex(os.path.join(tmp, "i.sqlite"), rset)
        index.put("d", ["usa", "x"])
        dag = index._load_dag("d")
        from_entry = [dag.arc_by_id[a] for a in dag.out["b0"]]
        terms = sorted((a.term, dag.position_length(a)) for a in from_entry)
        check(
            terms == [("united", 1), ("usa", 2)],
            f"expected same-position arcs usa/2 united/1, got {terms}",
        )
        index.close()


def test_same_position_different_length_arcs():
    rules = [{"id": "ny", "lhs": ["new", "york"], "rhs": ["nyc"]}]
    rset = make_rules(rules)
    with tempfile.TemporaryDirectory() as tmp:
        index = GraphIndex(os.path.join(tmp, "i.sqlite"), rset)
        index.put("d", ["new", "york", "city"])
        dag = index._load_dag("d")
        entry_arcs = sorted(
            (dag.arc_by_id[a].term, dag.position_length(dag.arc_by_id[a]))
            for a in dag.out["b0"]
        )
        check(
            entry_arcs == [("new", 1), ("nyc", 2)],
            f"same-position different-length arcs wrong: {entry_arcs}",
        )
        # The short synonym must not reach across the exit into 'city'.
        hits_nyc_york = index.query(["nyc", "york"])["hits"]
        check(hits_nyc_york == [], "nyc york must not match")
        hits_nyc_city = index.query(["nyc", "city"])
        check([h["doc_id"] for h in hits_nyc_city["hits"]] == ["d"], "nyc city")
        # Inspect exposes position_length for persisted arcs.
        info = index.inspect()
        check(info["arcs"] == 4, f"expected 4 arcs (new,york,nyc,city), got {info['arcs']}")
        index.close()


def test_two_independent_fragments():
    rules = [
        {"id": "ny", "lhs": ["new", "york"], "rhs": ["nyc"]},
        {"id": "ml", "lhs": ["machine", "learning"], "rhs": ["ml"]},
    ]
    docs = {"d": ["new", "york", "machine", "learning"]}
    run_case("two-frag-both-rhs", rules, docs, ["nyc", "ml"], {"d"})
    run_case("two-frag-cross-forbidden", rules, docs, ["nyc", "learning"], set())
    run_case("two-frag-mixed", rules, docs, ["new", "york", "ml"], {"d"})
    run_case("two-frag-full-original", rules, docs, ["new", "york", "machine", "learning"], {"d"})


def test_repeated_identical_tokens():
    rules = [{"id": "ny", "lhs": ["new", "york"], "rhs": ["nyc"]}]
    docs = {"d": ["new", "new", "york", "new", "york"]}
    run_case("repeat-nyc-nyc", rules, docs, ["nyc", "nyc"], {"d"})
    run_case("repeat-new-nyc", rules, docs, ["new", "nyc"], {"d"})
    run_case("repeat-nyc-new", rules, docs, ["nyc", "new"], {"d"})
    run_case("repeat-original-3", rules, docs, ["new", "york", "new"], {"d"})
    # Distinct arc identities for repeated identical terms.
    rset = make_rules(rules)
    with tempfile.TemporaryDirectory() as tmp:
        index = GraphIndex(os.path.join(tmp, "i.sqlite"), rset)
        index.put("d", docs["d"])
        news = index.postings("new")
        check(len(news) >= 3, f"expected >=3 'new' arcs, got {len(news)}")
        check(len({p[1] for p in news}) == len(news), "arc ids must be distinct")
        nycs = index.postings("nyc")
        check(len(nycs) == 2, f"expected 2 nyc arcs, got {len(nycs)}")
        check(len({p[1] for p in nycs}) == 2, "nyc arc ids must be distinct")
        # Each posting carries doc identity plus start/end nodes.
        for row in index.postings("new"):
            check(row[0] == "d", "posting doc id")
            check(row[2] and row[3], "posting start/end nodes present")
        index.close()


def test_rule_length_overlap():
    # Longer lhs wins at the same start; shorter still fires elsewhere.
    rules = [
        {"id": "ab", "lhs": ["a", "b"], "rhs": ["x"]},
        {"id": "abc", "lhs": ["a", "b", "c"], "rhs": ["y"]},
    ]
    docs = {
        "long": ["a", "b", "c", "a", "b"],
        "short": ["a", "b"],
    }
    run_case("overlap-long-y-x", rules, docs, ["y", "x"], {"long"})
    run_case("overlap-abc-original", rules, docs, ["a", "b", "c", "x"], {"long"})
    run_case("overlap-short", rules, docs, ["x"], {"long", "short"})
    run_case("overlap-y-abc", rules, docs, ["y", "a", "b"], {"long"})


def test_tie_same_length_by_id():
    # Same lhs, different rhs: lower id wins.
    rules = [
        {"id": "z", "lhs": ["a", "b"], "rhs": ["zzz"]},
        {"id": "a", "lhs": ["a", "b"], "rhs": ["aaa"]},
    ]
    docs = {"d": ["a", "b"]}
    run_case("tie-id-winner", rules, docs, ["aaa"], {"d"})
    run_case("tie-id-loser", rules, docs, ["zzz"], set())


def test_cross_fragment_phrase():
    rules = [
        {"id": "ny", "lhs": ["new", "york"], "rhs": ["nyc"]},
        {"id": "sf", "lhs": ["san", "francisco"], "rhs": ["sfx"]},
    ]
    docs = {"d": ["new", "york", "san", "francisco"]}
    run_case("cross-frag-nyc-sfx", rules, docs, ["nyc", "sfx"], {"d"})
    # york is the lhs branch exit of frag 1 and sfx begins frag 2: connected.
    run_case("cross-frag-york-sfx", rules, docs, ["york", "sfx"], {"d"})
    # nyc spans to the frag-1 exit, so nyc->san is valid, but nyc->francisco
    # would need to jump over san: forbidden.
    run_case("cross-frag-nyc-san", rules, docs, ["nyc", "san"], {"d"})
    run_case("cross-frag-nyc-francisco", rules, docs, ["nyc", "francisco"], set())
    run_case("cross-frag-token-between", rules, docs, ["new", "york", "san"], {"d"})


def test_document_replacement_and_delete():
    rules = [{"id": "ny", "lhs": ["new", "york"], "rhs": ["nyc"]}]
    rset = make_rules(rules)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "i.sqlite")
        with GraphIndex(path, rset) as index:
            index.put("d", ["new", "york", "city"])
            check(
                [h["doc_id"] for h in index.query(["nyc", "city"])["hits"]]
                == ["d"],
                "initial put",
            )
            index.put("d", ["new", "york", "state"])
            check(index.query(["nyc", "city"])["hits"] == [], "old arcs must vanish")
            check(
                [h["doc_id"] for h in index.query(["nyc", "state"])["hits"]]
                == ["d"],
                "replacement doc query",
            )
            check(index.doc_ids() == ["d"], "replacement should not add a doc")
            check(index.delete("d") is True, "delete existing")
            check(index.delete("d") is False, "delete missing")
            check(index.query(["nyc", "state"])["hits"] == [], "deleted doc gone")
        # Reopen: deletion and replacement must persist.
        with GraphIndex(path, rset) as index:
            check(index.doc_ids() == [], "deleted doc revived after reopen")
            check(index.query(["nyc", "state"])["hits"] == [], "deleted query")
            index.put("d", ["new", "york", "city"])
            check(
                [h["doc_id"] for h in index.query(["nyc", "city"])["hits"]]
                == ["d"],
                "re-put after reopen",
            )


def test_bad_input_does_not_damage_document():
    rules = [{"id": "ny", "lhs": ["new", "york"], "rhs": ["nyc"]}]
    rset = make_rules(rules)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "i.sqlite")
        with GraphIndex(path, rset) as index:
            index.put("d", ["new", "york", "city"])
            bad_inputs = [
                ["new", "york", "é"],
                ["NEW", "york"],
                ["new", "york", "a" * 33],
                ["new", "york", "a b"],
                ["new", ""],
                ["new", "york", 7],
                "not a list",
            ]
            for bad in bad_inputs:
                try:
                    index.put("d", bad)
                    check(False, f"bad input accepted: {bad!r}")
                except RuleError:
                    pass
                hits = index.query(["nyc", "city"])["hits"]
                check(
                    [h["doc_id"] for h in hits] == ["d"],
                    f"document damaged by rejected put {bad!r}",
                )
            # Too many tokens.
            try:
                index.put("d", ["a"] * 101)
                check(False, "101 tokens accepted")
            except RuleError:
                pass
            check(
                [h["doc_id"] for h in index.query(["nyc", "city"])["hits"]]
                == ["d"],
                "document damaged by token-count rejection",
            )
            # Query token limits.
            for bad in (["é"], ["A"], [""], [1]):
                try:
                    index.query(bad)
                    check(False, f"bad query accepted: {bad!r}")
                except RuleError:
                    pass


def test_path_limit_rejection():
    rules = [{"id": "r", "lhs": ["a", "b"], "rhs": ["x", "y"]}]
    rset = make_rules(rules)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "i.sqlite")
        with GraphIndex(path, rset) as index:
            # 8 independent fragments -> 2**8 = 256 paths: allowed.
            ok_tokens = ["a", "b"] * 8
            info = index.put("ok", ok_tokens)
            check(info["path_count"] == 256, f"expected 256 got {info['path_count']}")
            # 9 fragments -> 512 paths: rejected, prior docs intact.
            try:
                index.put("too-big", ["a", "b"] * 9)
                check(False, "put with 512 paths accepted")
            except PathLimitError:
                pass
            check("too-big" not in index.doc_ids(), "rejected put must not appear")
            check("ok" in index.doc_ids(), "existing doc must survive rejection")
            # Query with too many paths is rejected explicitly.
            try:
                index.query(["a", "b"] * 9)
                check(False, "query with 512 paths accepted")
            except PathLimitError:
                pass
            # 256-path query is accepted and is queryable.
            res = index.query(["a", "b"] * 8)
            check(res["stats"]["query_paths"] == 256, "256-path query accepted")


def test_rule_identity_and_rebuild():
    rules_a = [{"id": "r1", "lhs": ["a", "b"], "rhs": ["c"]}]
    rules_b = [{"id": "r1", "lhs": ["a", "b"], "rhs": ["d"]}]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "i.sqlite")
        with GraphIndex(path, make_rules(rules_a)) as index:
            index.put("d", ["a", "b"])
            check([h["doc_id"] for h in index.query(["c"])["hits"]] == ["d"], "rules A")
        # Reopening with different rules must not reinterpret old postings.
        try:
            GraphIndex(path, make_rules(rules_b))
            check(False, "mismatched ruleset was accepted")
        except RuleSetMismatch:
            pass
        # Reopen with the same content identity works.
        with GraphIndex(path, make_rules(rules_a)) as index:
            check(
                [h["doc_id"] for h in index.query(["c"])["hits"]] == ["d"],
                "matching ruleset reopen",
            )
            check(index.rules.fingerprint == make_rules(rules_a).fingerprint, "fp")
        # Explicit rebuild discards old postings and uses the new analyzer.
        with GraphIndex(path, make_rules(rules_b), rebuild=True) as index:
            check(index.doc_ids() == [], "rebuild must clear old documents")
            index.put("d", ["a", "b"])
            check([h["doc_id"] for h in index.query(["d"])["hits"]] == ["d"], "rules B")
            check(index.query(["c"])["hits"] == [], "old rhs must be gone")
        # Rule order in JSON must not change content identity.
        reordered = list(reversed(rules_b))
        with GraphIndex(path, make_rules(reordered)) as index:
            check(
                index.rules.fingerprint == make_rules(rules_b).fingerprint,
                "content identity must be order independent",
            )
            check(
                [h["doc_id"] for h in index.query(["d"])["hits"]] == ["d"],
                "reordered same rules reopen",
            )


def test_evidence_tie_break_and_single_hit():
    # Two distinct paths in one doc match the same query; only one hit and the
    # documented tie-break chooses the earliest start.
    rules = [{"id": "r", "lhs": ["a", "b"], "rhs": ["q"]}]
    docs = {"d": ["a", "b", "q", "a", "b"]}
    rset = make_rules(rules)
    with tempfile.TemporaryDirectory() as tmp:
        index = GraphIndex(os.path.join(tmp, "i.sqlite"), rset)
        index.put("d", docs["d"])
        res = index.query(["q"])
        check(len(res["hits"]) == 1, "one hit per doc")
        hit = res["hits"][0]
        check(hit["doc_source_span"][0] == 0, "earliest span wins")
        check(res["evidence_order"] == list(__import__("graph_index").EVIDENCE_ORDER), "order")
        index.close()


def test_inspect_and_storage_stats():
    rules = [{"id": "ny", "lhs": ["new", "york"], "rhs": ["nyc"]}]
    rset = make_rules(rules)
    with tempfile.TemporaryDirectory() as tmp:
        index = GraphIndex(os.path.join(tmp, "i.sqlite"), rset)
        index.put("d", ["new", "york", "city"])
        info = index.inspect()
        for key in (
            "schema_version",
            "rules",
            "document_count",
            "documents",
            "distinct_terms",
            "nodes",
            "arcs",
            "postings",
            "limits",
            "evidence_order",
            "storage_bytes",
        ):
            check(key in info, f"inspect missing {key}")
        sb = info["storage_bytes"]
        for key in (
            "sqlite_file",
            "arcs_rows_estimate",
            "postings_rows_estimate",
            "persisted_path_index",
            "enumerated_paths_json_estimate",
        ):
            check(key in sb, f"storage missing {key}")
        check(sb["persisted_path_index"] == 0, "paths are implicit in arcs")
        check(info["document_count"] == 1, "doc count")
        check(info["arcs"] == 4, f"expected 4 arcs, got {info['arcs']}")
        check(info["postings"] == 4, "one posting per arc")
        check(info["distinct_terms"] == 4, f"distinct terms {info['distinct_terms']}")
        q = index.query(["nyc", "city"])
        check("candidate_postings" in q["stats"], "query stats")
        check(q["stats"]["candidate_docs"] == 1, "candidate docs")
        index.close()


def test_doc_count_limit():
    rule_objs = []
    rset = make_rules(rule_objs)
    with tempfile.TemporaryDirectory() as tmp:
        index = GraphIndex(os.path.join(tmp, "i.sqlite"), rset)
        for i in range(100):
            index.put(f"d{i:03d}", ["token"])
        try:
            index.put("d100", ["token"])
            check(False, "101st document accepted")
        except RuleError:
            pass
        check(len(index.doc_ids()) == 100, "doc limit kept 100")
        # Replacing an existing doc at the limit is fine.
        index.put("d000", ["other"])
        check(len(index.doc_ids()) == 100, "replacement doesn't grow docs")
        index.close()


# ---------------------------------------------------------------------------
# Cross-check: DAG path enumeration agrees with independent string paths
# ---------------------------------------------------------------------------


def test_dag_paths_equal_independent_paths():
    from analyzer import analyze

    cases = [
        (["new", "york", "city"], [{"id": "ny", "lhs": ["new", "york"], "rhs": ["nyc"]}]),
        (["a", "b", "c", "a", "b"], [
            {"id": "ab", "lhs": ["a", "b"], "rhs": ["x"]},
            {"id": "abc", "lhs": ["a", "b", "c"], "rhs": ["y"]},
        ]),
        (["usa", "x"], [{"id": "r", "lhs": ["usa"], "rhs": ["united", "states"]}]),
        (["new", "new", "york"], [{"id": "ny", "lhs": ["new", "york"], "rhs": ["nyc"]}]),
    ]
    for tokens, rule_objs in cases:
        rset = make_rules(rule_objs)
        dag = analyze(tokens, rset)
        dag_paths = {tuple(dag.path_terms(p)) for p in dag.enumerate_complete_paths()}
        indep = independent_path_terms(tokens, rule_objs)
        check(
            dag_paths == indep,
            f"DAG paths {sorted(dag_paths)} != independent {sorted(indep)} for {tokens}",
        )


def test_legacy_database_rejected_and_rebuilt():
    import sqlite3

    rules = [{"id": "ny", "lhs": ["new", "york"], "rhs": ["nyc"]}]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "legacy.sqlite")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE arcs(doc TEXT, start INTEGER, term TEXT)")
        db.execute("INSERT INTO arcs VALUES('old',0,'new')")
        db.commit()
        db.close()
        try:
            GraphIndex(path, make_rules(rules))
            check(False, "legacy DB accepted without rebuild")
        except IndexSchemaError:
            pass
        # Explicit rebuild discards the incompatible legacy schema.
        with GraphIndex(path, make_rules(rules), rebuild=True) as index:
            check(index.doc_ids() == [], "legacy rows must be discarded")
            index.put("d", ["new", "york"])
            check([h["doc_id"] for h in index.query(["nyc"])["hits"]] == ["d"], "rebuilt")


def test_query_uses_persisted_rows_only():
    # No original token text is stored outside the DAG arcs; a reopen query
    # must therefore be answered from persisted rows alone.
    rules = [{"id": "ny", "lhs": ["new", "york"], "rhs": ["nyc"]}]
    rset = make_rules(rules)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "i.sqlite")
        with GraphIndex(path, rset) as index:
            index.put("d", ["new", "york", "city"])
        import sqlite3

        db = sqlite3.connect(path)
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        db.close()
        check("tokens" not in tables and "docs_tokens" not in tables, "no raw token table")
        with GraphIndex(path, rset) as index:
            hits = index.query(["nyc", "city"])["hits"]
            check([h["doc_id"] for h in hits] == ["d"], "reopen query from rows")
            # Query statistics show candidates, not a text scan.
            stats = index.query(["nyc", "city"])["stats"]
            check(stats["candidate_postings"] >= 1, "candidate posting count")
            check(stats["document_graphs_loaded"] == 1, "one graph loaded")


def main() -> int:
    tests = [
        test_probe_semantics,
        test_rhs_longer_than_lhs,
        test_same_position_different_length_arcs,
        test_two_independent_fragments,
        test_repeated_identical_tokens,
        test_rule_length_overlap,
        test_tie_same_length_by_id,
        test_cross_fragment_phrase,
        test_document_replacement_and_delete,
        test_bad_input_does_not_damage_document,
        test_path_limit_rejection,
        test_rule_identity_and_rebuild,
        test_evidence_tie_break_and_single_hit,
        test_inspect_and_storage_stats,
        test_doc_count_limit,
        test_dag_paths_equal_independent_paths,
        test_legacy_database_rejected_and_rebuilt,
        test_query_uses_persisted_rows_only,
    ]
    for test in tests:
        try:
            test()
        except Exception:
            FAILURES.append(f"{test.__name__} raised:\n{traceback.format_exc()}")
    if FAILURES:
        print(f"FAILED {len(FAILURES)} / {CHECKS} checks")
        for failure in FAILURES:
            print(" -", failure)
        return 1
    print(f"OK {CHECKS} checks across {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
