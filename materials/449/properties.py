"""Property derivation for the finite join language.

Two properties drive every reordering decision:

1. **Lineage** – which base-table aliases feed each subtree, and which join
   introduces NULL extension for which aliases (the right subtree of every
   LEFT join).  This comes purely from the tree shape, never from data.

2. **Null rejection** – given the aliases whose columns are guaranteed NULL on
   an outer-join-padded row, can a predicate ever evaluate TRUE for that row?
   We answer with an abstract three-valued evaluation: padded columns are
   abstract NULL, every other column may be any value *including a real NULL*,
   literals keep their concrete values.  TRUE absent from the abstract outcome
   means the predicate rejects the padded row.  Nothing here samples data, so a
   predicate that only happens to reject on the current instance is not
   accepted.
"""
from __future__ import annotations

from common import and3, column_refs, not3, or3, plan_aliases

PADDED = "padded"   # column is NULL because of outer-join NULL extension
FREE = "free"       # column may hold any value, including a real NULL


# --------------------------------------------------------------------------- #
# Structural properties
# --------------------------------------------------------------------------- #

def lineage(node: dict) -> set[str]:
    return plan_aliases(node)


def null_supplied_aliases(node: dict) -> set[str]:
    """Aliases that can be NULL-extended somewhere inside ``node``."""
    if "scan" in node:
        return set()
    if "filter" in node:
        return null_supplied_aliases(node["filter"])
    aliases = (null_supplied_aliases(node["left"])
               | null_supplied_aliases(node["right"]))
    if node["join"] == "left":
        aliases |= lineage(node["right"])
    return aliases


def is_join(node: dict) -> bool:
    return "join" in node


# --------------------------------------------------------------------------- #
# Abstract 3VL evaluation
# --------------------------------------------------------------------------- #

def _abstract_operand(operand: Any, padded: set[str]) -> Any:
    if isinstance(operand, dict):
        return {operand["lit"]}
    alias = operand.split(".", 1)[0]
    return {PADDED} if alias in padded else {FREE}


def _lift(combinators, value_sets: list[set]) -> set:
    if len(value_sets) == 1:
        return set(value_sets[0])
    outcomes = set()

    def walk(index, values):
        if index == len(value_sets):
            outcomes.add(combinators(*values))
            return
        for value in value_sets[index]:
            walk(index + 1, values + [value])
    walk(0, [])
    return outcomes


def abstract_outcomes(predicate: dict, padded: set[str]) -> set:
    """Possible SQL truth values when columns of ``padded`` aliases are NULL."""
    op = predicate["op"]
    if op == "and":
        return _lift(and3, [abstract_outcomes(a, padded) for a in predicate["args"]])
    if op == "or":
        return _lift(or3, [abstract_outcomes(a, padded) for a in predicate["args"]])
    if op == "not":
        return {not3(v) for v in abstract_outcomes(predicate["arg"], padded)}
    if op == "isnull":
        values = _abstract_operand(predicate["arg"], padded)
        if values == {PADDED}:
            return {True}
        if values == {FREE}:
            return {True, False}
        return {v is None for v in values}
    if op == "isnotnull":
        values = _abstract_operand(predicate["arg"], padded)
        if values == {PADDED}:
            return {False}
        if values == {FREE}:
            return {True, False}
        return {v is not None for v in values}
    if op == "cmp":
        left = _abstract_operand(predicate["left"], padded)
        right = _abstract_operand(predicate["right"], padded)
        if PADDED in left or PADDED in right:
            return {None}  # NULL operand -> UNKNOWN, regardless of the rest
        if FREE in left or FREE in right:
            return {True, False, None}
        # Both sides are singleton literal sets.
        from common import compare3
        return {compare3(predicate["cmp"], next(iter(left)), next(iter(right)))}
    raise ValueError(f"unsupported predicate op: {op!r}")


def rejects_padding(predicate: dict | None, padded_aliases: set[str]) -> bool:
    """True iff the predicate cannot be TRUE on a NULL-extended row."""
    if predicate is None or not padded_aliases:
        return False
    return True not in abstract_outcomes(predicate, padded_aliases)


def rejecting_columns(predicate: dict, padded_aliases: set[str]) -> list[str]:
    return sorted(
        ref for ref in column_refs(predicate)
        if ref.split(".", 1)[0] in padded_aliases
    )


# --------------------------------------------------------------------------- #
# Whole-tree analysis
# --------------------------------------------------------------------------- #

def analyze(node: dict) -> dict:
    """Walk the tree and annotate every join/filter with derived properties."""
    info = {}
    _walk(node, (), None, [], info)
    return info


def _walk(node, node_id, parent, ancestors, info):
    record = {
        "id": node_id,
        "parent": parent,
        "ancestors": list(ancestors),
        "aliases": lineage(node),
        "null_supplied": null_supplied_aliases(node),
    }
    if "scan" in node:
        info[node_id] = record
        return
    if "filter" in node:
        info[node_id] = record
        _walk(node["filter"], node_id + ("f",), node_id,
              ancestors + [node_id], info)
        return

    record["join"] = node["join"]
    record["on"] = node["on"]
    record["left_aliases"] = lineage(node["left"])
    record["right_aliases"] = lineage(node["right"])
    if node["join"] == "left":
        record["padded_aliases"] = lineage(node["right"])
    else:
        record["padded_aliases"] = set()
    info[node_id] = record
    _walk(node["left"], node_id + ("l",), node_id,
          ancestors + [node_id], info)
    _walk(node["right"], node_id + ("r",), node_id,
          ancestors + [node_id], info)


def find_top_filter(node: dict):
    """Return (filter node id path, where predicate) if the root is a filter."""
    if "filter" in node:
        return (), node["where"]
    return None


def get_node(root: dict, node_id: tuple) -> dict:
    step_to_key = {"l": "left", "r": "right", "f": "filter"}
    node = root
    for step in node_id:
        node = node[step_to_key[step]]
    return node


# --------------------------------------------------------------------------- #
# Rule R1: outer-inner reassociation (the publicly supported family)
#
#   (X LEFT Y ON p) INNER Z ON q   ==   X INNER Z ON q LEFT Y ON p
#
# sound exactly when q does not reference Y, the NULL-extended table.
# --------------------------------------------------------------------------- #

def check_outer_inner_reassociation(root: dict) -> dict:
    # A top-level filter stays at its original logical position and does not
    # change the join-level eligibility, so inspect the join body underneath.
    where = None
    body = root
    if "filter" in body:
        where = body["where"]
        body = body["filter"]
    if "join" not in body:
        return {"ok": False, "shape": None,
                "reason": "root is not a join; no outer-inner pattern exists"}
    if body["join"] != "inner":
        return {"ok": False, "shape": None,
                "reason": f"root is {body['join'].upper()} JOIN; associativity "
                          "across the outer side of a LEFT join is not valid"}

    left, right = body["left"], body["right"]
    if "join" in left and left["join"] == "left":
        x, y = left["left"], left["right"]
        z = right
        forbidden = lineage(y)
        shape = "left"
    elif "join" in right and right["join"] == "left":
        # X INNER (Y LEFT Z ON r) ON q  ==  (Y INNER X ON q) LEFT Z ON r
        x, y, z = left, right["left"], right["right"]
        forbidden = lineage(z)
        shape = "right"
    else:
        return {"ok": False, "shape": None,
                "reason": "neither child is a LEFT join; the R1 pattern "
                          "INNER(LEFT, ...) / INNER(..., LEFT) does not occur"}

    # R1 eligibility depends only on the join-level INNER ON.  A top WHERE is
    # left above the rewritten joins and tests padded and real rows alike, so it
    # never blocks the reassociation (and is never pushed into an ON clause).
    referenced = ref_aliases_of(body["on"])
    offending = sorted(referenced & forbidden)
    if offending:
        return {
            "ok": False, "shape": shape, "forbidden_aliases": sorted(forbidden),
            "reason": ("the INNER ON / WHERE predicate references the "
                       f"NULL-extended table(s) {offending}: after "
                       "reassociation its padded rows would fail the predicate "
                       "and be dropped, changing the bag result (probe.py "
                       "counterexample family)"),
        }
    return {
        "ok": True, "shape": shape,
        "x": x, "y": y, "z": z,
        "p": left["on"] if shape == "left" else right["on"],
        "q": body["on"],
        "forbidden_aliases": sorted(forbidden),
        "reason": "INNER ON (and any top WHERE) reference only non-null-supplied "
                  "tables, so reassociation preserves every padded row and "
                  "multiplicity; WHERE remains above the joins",
    }


def ref_aliases_of(predicate: dict) -> set[str]:
    return {ref.split(".", 1)[0] for ref in column_refs(predicate)}


# --------------------------------------------------------------------------- #
# Rule R2: LEFT -> INNER reduction when a later filter rejects the padding
# --------------------------------------------------------------------------- #

def check_left_reductions(root: dict) -> list[dict]:
    """For every LEFT node, prove or refute that its NULL extension is dead.

    A rejecting filter only certifies the reduction when the LEFT subtree is on
    the *preserving* side of every join between the two: the left child of a
    LEFT join, or either child of an INNER join.  If the LEFT subtree itself
    sits in the nullable right subtree of another LEFT join, its padded rows
    can still be observed and the reduction is refused.
    """
    info = analyze(root)
    top_filter = find_top_filter(root)
    results = []
    for node_id, record in info.items():
        if record.get("join") != "left":
            continue
        padded = record["padded_aliases"]
        blockers = _blocking_left_ancestors(node_id, info)
        rejecting = _rejecting_filters(node_id, padded, root, info, top_filter)
        reducible = not blockers and bool(rejecting)
        reasons = []
        if blockers:
            reasons.append(
                "a LEFT ancestor keeps this subtree on its nullable right side; "
                "padding cannot be proven dead from above ("
                + ", ".join(".".join(b) for b in blockers) + ")")
        if not rejecting:
            reasons.append(
                "no ancestor INNER ON predicate or WHERE clause rejects rows "
                "where " + ", ".join(sorted(padded)) + " is NULL; padded rows "
                "must be retained, so the LEFT join cannot become INNER")
        results.append({
            "id": node_id,
            "padded_aliases": sorted(padded),
            "reducible": reducible,
            "rejected_by": rejecting,
            "reasons": reasons,
        })
    return results


def _blocking_left_ancestors(node_id, info) -> list[tuple]:
    """LEFT ancestors where the node lies in the nullable right subtree."""
    blockers = []
    record = info[node_id]
    for ancestor_id in record["ancestors"]:
        ancestor = info[ancestor_id]
        if ancestor.get("join") != "left":
            continue
        if _is_in_right_subtree(ancestor_id, node_id):
            blockers.append(ancestor_id)
    return blockers


def _is_in_right_subtree(ancestor_id, node_id) -> bool:
    # node_id extends ancestor_id; the first step beyond it must be "r".
    return len(node_id) > len(ancestor_id) and node_id[len(ancestor_id)] == "r"


def _rejecting_filters(node_id, padded, root, info, top_filter) -> list[dict]:
    found = []
    record = info[node_id]
    for ancestor_id in record["ancestors"]:
        ancestor = info[ancestor_id]
        if ancestor.get("join") == "inner" and rejects_padding(
                ancestor["on"], padded):
            found.append({
                "where": "inner_on",
                "node_id": ancestor_id,
                "columns": rejecting_columns(ancestor["on"], padded),
                "predicate": ancestor["on"],
            })
        if "filter" in get_node(root, ancestor_id):
            where = get_node(root, ancestor_id)["where"]
            if rejects_padding(where, padded):
                found.append({
                    "where": "filter",
                    "node_id": ancestor_id,
                    "columns": rejecting_columns(where, padded),
                    "predicate": where,
                })
    if top_filter is not None:
        _, where = top_filter
        if rejects_padding(where, padded):
            found.append({
                "where": "where",
                "node_id": (),
                "columns": rejecting_columns(where, padded),
                "predicate": where,
            })
    return found
