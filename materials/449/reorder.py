"""Limited, proof-carrying outer join reorderer.

The planner never trusts a generic join-ordering rule.  Every candidate must be
produced by one of two proven transformations:

* **R1 – outer-inner reassociation**
  ``(X LEFT Y ON p) INNER Z ON q``  ->
  ``X INNER Z ON q LEFT Y ON p``
  allowed only when ``q`` references none of Y, the NULL-extended aliases.

* **R2 – LEFT -> INNER reduction**
  a LEFT join becomes INNER only when an ancestor INNER ON predicate or the
  top-level WHERE clause is *proven* (abstract 3VL, no data sampling) to reject
  its NULL-extended rows.

Once a subtree is all INNER, ordinary associativity/commutativity enumeration is
sound.  Any shape that cannot be reached through these rules is rejected with
an explicit reason; the original tree is always kept as a candidate, so the
planner degrades to the original semantics rather than guessing.
"""
from __future__ import annotations

from itertools import permutations

from common import (
    canonical,
    clone,
    conjunction,
    flatten_conjuncts,
    normalize_plan,
    plan_aliases,
)
from properties import (
    analyze,
    check_left_reductions,
    check_outer_inner_reassociation,
)

STRATEGIES = ("local", "bounded")


def optimize(plan: dict, schema=None, stats: dict | None = None,
             strategy: str = "bounded") -> dict:
    """Return the chosen (normalized) plan.  See :func:`plan` for full detail."""
    return plan(plan, schema=schema, stats=stats, strategy=strategy)["chosen"]


def plan(plan: dict, schema=None, stats: dict | None = None,
         strategy: str = "bounded") -> dict:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; expect one of {STRATEGIES}")
    root = normalize_plan(plan)
    body, where = _split_filter(root)
    aliases = sorted(plan_aliases(body))
    stats = stats or {}

    raw_candidates = _generate_candidates(body, where, strategy)
    candidates = []
    seen = set()
    for candidate_body, proof in raw_candidates:
        candidate = _attach_filter(candidate_body, where)
        key = canonical(candidate)
        if key in seen:
            continue
        seen.add(key)
        estimate = estimate_cost(candidate, stats)
        candidates.append({
            "plan": candidate,
            "proof": proof,
            "estimate": estimate,
            "score": (estimate["max_intermediate"], estimate["total_cost"]),
        })

    original = _attach_filter(body, where)
    baseline = next(c for c in candidates if canonical(c["plan"]) == canonical(original))
    rejections = _rejection_report(body, where)
    chosen = min(candidates, key=lambda c: c["score"] + (0 if c is baseline else 1,))
    return {
        "original": original,
        "chosen": chosen["plan"],
        "changed": canonical(chosen["plan"]) != canonical(original),
        "candidates": candidates,
        "rejections": rejections,
        "baseline_estimate": baseline["estimate"],
        "chosen_estimate": chosen["estimate"],
        "strategy": strategy,
        "aliases": aliases,
        "stats_used": stats,
    }


# --------------------------------------------------------------------------- #
# Candidate generation
# --------------------------------------------------------------------------- #

def _generate_candidates(body: dict, where, strategy: str) -> list[tuple[dict, dict]]:
    candidates: list[tuple[dict, dict]] = [(clone(body), {"rules": ["KEEP_ORIGINAL"]})]
    # Property checks run on the full root so the top WHERE is visible; the
    # rewrite itself still targets the join body and keeps WHERE on top.
    root = _attach_filter(body, where)

    r1 = check_outer_inner_reassociation(root)
    if r1["ok"]:
        candidates.append((_apply_r1(body, r1), {"rules": ["R1_OUTER_INNER"],
                                                 "detail": r1["reason"]}))
    reductions = check_left_reductions(root)

    if strategy == "local":
        # S1: only the single, explicitly proven R1 rewrite (plus original).
        return candidates

    # S2: R2 reductions unlock the all-INNER shape space for three tables.
    reduced_plans = _apply_r2(body, reductions)
    for reduced, proof in reduced_plans:
        for inner_body in _enumerate_inner_shapes(reduced):
            candidates.append((inner_body, {
                "rules": proof["rules"] + ["INNER_ASSOC_COMM"],
                "detail": proof.get("detail", ""),
            }))
    # R1 already exposes an all-INNER subtree (X INNER Z); enumerate it too.
    for r1_body, proof in list(candidates):
        if "R1_OUTER_INNER" in proof["rules"]:
            for inner_body in _enumerate_inner_shapes(r1_body):
                candidates.append((inner_body, {
                    "rules": proof["rules"] + ["INNER_ASSOC_COMM"],
                    "detail": proof.get("detail", ""),
                }))
    return candidates


def _apply_r1(body: dict, r1: dict) -> dict:
    if r1["shape"] == "left":
        inner = {"join": "inner", "left": clone(r1["x"]),
                 "right": clone(r1["z"]), "on": clone(r1["q"])}
        return {"join": "left", "left": inner, "right": clone(r1["y"]),
                "on": clone(r1["p"])}
    # right shape: X INNER (Y LEFT Z ON r) ON q -> (Y INNER X ON q) LEFT Z ON r
    inner = {"join": "inner", "left": clone(r1["y"]),
             "right": clone(r1["x"]), "on": clone(r1["q"])}
    return {"join": "left", "left": inner, "right": clone(r1["z"]),
            "on": clone(r1["p"])}


def _apply_r2(body: dict, reductions: list[dict]) -> list[tuple[dict, dict]]:
    reducible = [r for r in reductions if r["reducible"]]
    if not reducible:
        return []
    out = []
    for reduction in reducible:
        reduced = clone(body)
        # The analysis ran on the filter-wrapped root; a root filter adds a
        # single leading "f" step that the join body does not have.
        full_id = reduction["id"]
        body_id = full_id[1:] if full_id and full_id[0] == "f" else full_id
        target = _node_at(reduced, body_id)
        target["join"] = "inner"
        detail = "; ".join(
            f"{f['where']} rejects padded columns {f['columns']}"
            for f in reduction["rejected_by"])
        out.append((reduced, {
            "rules": ["R2_LEFT_TO_INNER", f"padded={reduction['padded_aliases']}"],
            "detail": detail,
        }))
    return out


def _enumerate_inner_shapes(body: dict) -> list[dict]:
    """All INNER trees over the same aliases, each with a feasible ON split."""
    if not _is_all_inner(body):
        return [body]
    aliases = sorted(plan_aliases(body))
    # Collect every ON conjunct in the subtree (all are inner predicates here).
    conjuncts = _collect_conjuncts(body)
    shapes = []
    for shape in _all_tree_shapes(aliases):
        assigned = _assign_conjuncts(shape, conjuncts)
        if assigned is not None:
            shapes.append(assigned)
    return shapes


def _is_all_inner(node: dict) -> bool:
    if "scan" in node:
        return True
    if "filter" in node:
        return _is_all_inner(node["filter"])
    return node["join"] == "inner" and _is_all_inner(node["left"]) \
        and _is_all_inner(node["right"])


def _collect_conjuncts(node: dict) -> list[dict]:
    if "scan" in node:
        return []
    if "filter" in node:
        return _collect_conjuncts(node["filter"])
    return (flatten_conjuncts(node["on"]) + _collect_conjuncts(node["left"])
            + _collect_conjuncts(node["right"]))


def _all_tree_shapes(aliases: list[str]) -> list[dict]:
    if len(aliases) == 1:
        return [{"scan": aliases[0]}]
    shapes = []
    for order in permutations(aliases):
        order = list(order)
        # Left-deep chain over this permutation.
        node = {"scan": order[0]}
        for alias in order[1:]:
            node = {"join": "inner", "left": node,
                    "right": {"scan": alias}, "on": None}
        shapes.append(node)
        # Right-deep chain.
        node = {"scan": order[-1]}
        for alias in reversed(order[:-1]):
            node = {"join": "inner", "left": {"scan": alias},
                    "right": node, "on": None}
        shapes.append(node)
    unique = {}
    for shape in shapes:
        unique[canonical(shape)] = shape
    return list(unique.values())


def _assign_conjuncts(shape: dict, conjuncts: list[dict]) -> dict | None:
    """Place each conjunct at a join whose inputs cover its referenced aliases.

    A join without any assigned predicate would be a Cartesian product that the
    original tree never had, so such a shape is rejected as infeasible.
    """
    from properties import ref_aliases_of
    remaining = list(conjuncts)
    if not _fill(shape, remaining, ref_aliases_of):
        return None
    if remaining:
        return None
    for node in _iter_joins(shape):
        if not node["_on"]:
            return None
        node["on"] = conjunction(node.pop("_on"))
    return shape


def _fill(node, remaining, ref_aliases_of) -> bool:
    if "scan" in node:
        return True
    if not (_fill(node["left"], remaining, ref_aliases_of)
            and _fill(node["right"], remaining, ref_aliases_of)):
        return False
    available = plan_aliases(node["left"]) | plan_aliases(node["right"])
    node["_on"] = []
    for conjunct in list(remaining):
        if ref_aliases_of(conjunct) <= available:
            node["_on"].append(conjunct)
            remaining.remove(conjunct)
    return True


def _iter_joins(node: dict):
    if "scan" in node:
        return
    yield from _iter_joins(node["left"])
    yield from _iter_joins(node["right"])
    yield node


# --------------------------------------------------------------------------- #
# Rejection report (why unproven shapes are refused)
# --------------------------------------------------------------------------- #

def _rejection_report(body: dict, where) -> list[dict]:
    report = []
    root = _attach_filter(body, where)
    r1 = check_outer_inner_reassociation(root)
    if not r1["ok"]:
        report.append({
            "transform": "R1_OUTER_INNER_REASSOCIATION",
            "rejected": True,
            "reason": r1["reason"],
        })
    else:
        report.append({
            "transform": "R1_OUTER_INNER_REASSOCIATION",
            "rejected": False,
            "reason": r1["reason"],
            "forbidden_aliases": r1["forbidden_aliases"],
        })
    for reduction in check_left_reductions(root):
        report.append({
            "transform": "R2_LEFT_TO_INNER",
            "rejected": not reduction["reducible"],
            "padded_aliases": reduction["padded_aliases"],
            "reason": " ; ".join(reduction["reasons"])
                    if reduction["reasons"]
                    else "padding is rejected: "
                         + "; ".join(f["where"] for f in reduction["rejected_by"]),
        })
    # The generic associativity rule is explicitly refused across LEFT joins.
    for node_id, record in analyze(body).items():
        if record.get("join") == "left":
            report.append({
                "transform": "GENERIC_INNER_ASSOCIATIVITY_ON_LEFT",
                "rejected": True,
                "padded_aliases": sorted(record["padded_aliases"]),
                "reason": "inner-join associativity cannot be applied across a "
                          "LEFT join without proving null-rejection first; this "
                          "rule alone never certifies an outer-join reorder",
            })
    if where is not None:
        report.append({
            "transform": "PUSH_WHERE_INTO_ON",
            "rejected": True,
            "reason": "WHERE stays at its original logical position; predicates "
                      "tested on padded NULLs are not moved into an ON clause",
        })
    return report


# --------------------------------------------------------------------------- #
# Cost estimation from published statistics only (no candidate execution)
# --------------------------------------------------------------------------- #

def estimate_cost(node: dict, stats: dict) -> dict:
    """Estimate per-join cardinalities using rows / key NDV stats.

    Stats shape: ``{"A": {"rows": n, "keys": {"A.k": ndv, ...}}}``.
    Missing NDVs fall back to conservative estimates, never to execution.
    """
    details: list[dict] = []
    total, maximum = _cost_node(node, stats, details)
    return {"total_cost": total, "max_intermediate": maximum, "joins": details}


def _table_stats(alias: str, stats: dict) -> dict:
    return stats.get(alias, {"rows": None, "keys": {}})


def _rows(alias: str, stats: dict) -> int:
    rows = _table_stats(alias, stats).get("rows")
    if rows is None:
        raise ValueError(
            f"missing published row count for table {alias}; the planner must "
            "not run candidates to discover it")
    return rows


def _ndv(column: str, stats: dict) -> int | None:
    alias = column.split(".", 1)[0]
    return _table_stats(alias, stats).get("keys", {}).get(column)


def _cost_node(node: dict, stats: dict, details: list[dict]) -> tuple[int, int]:
    if "scan" in node:
        rows = _rows(node["scan"], stats)
        return rows, 0  # scans themselves are not counted as intermediates
    if "filter" in node:
        total, maximum = _cost_node(node["filter"], stats, details)
        est_rows = max(1, total // 2)  # generic conservative selectivity 1/2
        details.append({"filter": True, "in_rows": total, "out_rows": est_rows})
        return est_rows, max(maximum, est_rows)

    left_rows, left_max = _cost_node(node["left"], stats, details)
    right_rows, right_max = _cost_node(node["right"], stats, details)
    out_rows = _estimate_join_rows(node, left_rows, right_rows, stats)
    build_side = min(left_rows, right_rows)
    details.append({
        "join": node["join"],
        "left_rows": left_rows,
        "right_rows": right_rows,
        "build_rows": build_side,
        "out_rows": out_rows,
    })
    return out_rows, max(left_max, right_max, out_rows)


def _estimate_join_rows(node, left_rows, right_rows, stats) -> int:
    pairs = _equi_pairs(node["on"])
    if pairs:
        # Equality L.k = R.k with uniform-key assumption: the shared key domain
        # is the larger NDV, and expected output is |L| * |R| / max(NDV_l, NDV_r).
        expected = float(left_rows) * right_rows
        for left_key, right_key in pairs:
            ndv_l = _ndv(left_key, stats) or max(1, left_rows)
            ndv_r = _ndv(right_key, stats) or max(1, right_rows)
            expected /= max(1, max(ndv_l, ndv_r))
        inner_rows = max(1, round(expected))
    else:
        # Non-equi ON: conservative generic Cartesian * 1/2 factor.
        inner_rows = max(1, round(left_rows * right_rows * 0.5))
    if node["join"] == "left":
        # Expected matches per outer row = |right| / NDV(right join key).
        # LEFT output = outer rows + successful matches, bounded below by the
        # outer input and above by outer * (1 + expected fan-out).
        fan_out = 1.0
        if pairs:
            right_key = pairs[0][1]
            ndv_r = _ndv(right_key, stats) or max(1, right_rows)
            fan_out = max(1.0, right_rows / max(1, ndv_r))
        return max(left_rows, round(left_rows * fan_out))
    return inner_rows


def _equi_pairs(on) -> list[tuple[str, str]]:
    pairs = []
    for conjunct in flatten_conjuncts(on):
        if conjunct["op"] == "cmp" and conjunct["cmp"] == "=" \
                and isinstance(conjunct["left"], str) \
                and isinstance(conjunct["right"], str):
            pairs.append((conjunct["left"], conjunct["right"]))
    return pairs


# --------------------------------------------------------------------------- #
# Small structural helpers
# --------------------------------------------------------------------------- #

def _split_filter(root: dict) -> tuple[dict, dict | None]:
    if "filter" in root:
        return root["filter"], root["where"]
    return root, None


def _attach_filter(body: dict, where) -> dict:
    return {"filter": body, "where": where} if where is not None else body


def _node_at(root: dict, node_id: tuple) -> dict:
    step_to_key = {"l": "left", "r": "right", "f": "filter"}
    node = root
    for step in node_id:
        node = node[step_to_key[step]]
    return node
