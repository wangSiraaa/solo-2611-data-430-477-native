# Question 459 — design notes

All code lives in `materials/459/`. Entry points:

```
cd materials/459
python3 probe.py          # original expectations + evidence + storage stats
python3 tests_459.py      # 726 independent-oracle checks
```

## Files

| file | role |
| --- | --- |
| `rules.py` | strict token/rule validation, rule ordering, content-identity fingerprint |
| `analyzer.py` | token array -> breadth-compact DAG with `position_length` |
| `graph_index.py` | persistent SQLite index, `put`/`delete`/`query`/`inspect`, matching |
| `phrase.py` | compatibility `search` / `search_detailed` / `explain` |
| `probe.py` | required probe (`new york city` hits `nyc city`, not `nyc york`) |
| `tests_459.py` | independent oracle + evidence validation + limit/rebuild tests |
| `rules.json` | sample rules (now with explicit unique ids) |

## Input contract

Tokens are already tokenized: non-empty, printable ASCII, already lowercase,
at most 32 bytes each. Empty arrays are allowed; the empty query matches
nothing. No Unicode normalization or natural-language tokenization is done.
Rejections raise `rules.RuleError` before any persistent state changes.

## Rule compilation

* `lhs`, `rhs` each 1..3 tokens; unique id (string or int).
* Matching priority is published: **lhs length descending, then id ascending**.
  `RuleSet.to_public()` prints the exact ranked order and the sort key.
* Greedy left-to-right scan of the *original* tokens: first match consumes the
  whole lhs; otherwise one token is consumed; the produced rhs is never
  rescanned.
* Content identity is SHA-256 over canonical `{id,lhs,rhs}` records sorted by
  id, so file order does not matter. It is stored in the index; opening a
  non-empty index with a different ruleset raises `RuleSetMismatch` unless
  `rebuild=True`.

## DAG model

Each consumed fragment is a two-branch diamond: shared entry and shared exit.
The original-lhs branch and the rhs branch each have one arc per term; the
shorter branch's last arc spans the remaining width, other arcs advance one
position. Local width is `max(len(lhs), len(rhs))`; the fragment occupies
`width` slots and slot values are persisted, so `position_length` is a
first-class property of every arc (e.g. the `nyc` arc for `new york` has
`position_length = 2`). Fragments concatenate sequentially. Every node and arc
has an identity, so repeated identical tokens keep distinct arc identities
(`new` at three positions yields three arcs).

The DAG is validated to have at most 256 complete entry->exit paths; more is
rejected at `put`/`query` time (`PathLimitError`) before persistence.

## Posting / path index

`postings(term, doc_id, arc_id, start_node, end_node, slot)`. This is enough to
distinguish document identity, arc identity and the arc's start/end nodes; the
`arcs` table (with `doc_id`, `arc_id`, `start_node`, `end_node`, terms, source
spans) *is* the path index. No enumerated path list is stored, so
`persisted_path_index = 0` bytes. `inspect()` also reports the hypothetical
size of an eager JSON enumeration for comparison.

Query never reads raw tokens: it compiles the query with the same ruleset
identity, reads candidate postings, loads the persisted DAG rows for candidate
documents only, and walks connected arcs. A `tokens`/raw-text table does not
exist (asserted by a test).

## Matching semantics

`exact phrase` holds iff some **complete query path**'s term sequence equals a
**contiguous directed** path in the document DAG. Document tokens before/after
are allowed; the query must match in full (no prefix-only hit); arcs must be
truly connected (no jumping over a missing edge or over a sibling branch).

Because synonyms make a fragment a diamond, `new york -> nyc` yields paths
`new york ...` and `nyc ...`, so `nyc city` matches but `nyc york` does not:
the `nyc` arc ends at the fragment exit, and `york` only exists on the other
branch. Multi-token rhs (`usa -> united states`) similarly expands.

For each hit the result contains `doc_id`, document start/end nodes, the full
document arc sequence, the chosen query arc sequence, per-arc `start_node` /
`end_node` / `term` / `source` span / `position_length` / `rule_id`, and the
mapped original token span. One hit per document is returned, chosen by the
published order in `EVIDENCE_ORDER`:
earliest source start, shortest source span, smallest document arc ordinals,
smallest query path index, then smallest query arc ordinals.

## Persistence / lifecycle

* `put(id, tokens)` validates and analyzes first, then in one transaction
  deletes the old doc's nodes/arcs/postings and inserts the new version.
  A rejected input leaves the previous version untouched.
* `delete(id)` removes rows in one transaction; returning `False` if absent.
  After close/reopen a deleted doc stays gone (no replay log).
* `rebuild=True` discards all tables and re-initialises the ruleset; old
  postings are never interpreted with a new analyzer.
* Document limit 100; token limit 100; complete-path limit 256 per graph.

## Enumeration vs retained DAG

| aspect | eager enumeration | retained DAG (this code) |
| --- | --- | --- |
| storage | O(paths) — every entry->exit path stored; grows exponentially with synonyms | O(arcs), linear in tokens + rules |
| path-count cap | must materialize to check | counted by DP, saturating at 257 |
| query cost | substring/index lookup over path strings | posting lookup + DFS over candidate arcs, bounded by `MAX_MATCH_PATHS` |
| evidence | direct path id, then map to arcs | arc walk is itself the evidence; source spans are per arc |
| rebuild | re-enumerate on any rule change | same content-identity check, rows rebuilt |

The retained DAG wins on storage and on pathological synonym growth; the
eager form would make evidence trivial but can blow up before hitting the
256-path limit check. The tests use an *independent* generator (rules applied
to raw tokens producing string path sets, then naive contiguous subsequence
matching) as the oracle, plus a randomized differential fuzz. When the formal
route enumerates (it does, for bounded sizes), the oracle is still generated
from the original rules and tokens, not from the posted rows.
