"""Bag-semantics executor that actually runs the chosen tree.

Every scan produces qualified columns (``alias.column``).  LEFT joins pad the
*whole* right subtree schema with NULLs, derived from the declared schema rather
than from sampled rows, so empty tables keep working.  No deduplication ever
happens: equal values with different occurrence_ids survive as separate rows.
"""
from __future__ import annotations

from common import (
    Schema,
    eval_predicate,
    flatten_conjuncts,
    normalize_plan,
    plan_aliases,
)


def execute(plan: dict, tables: dict, metrics: list | None = None,
            schema: Schema | None = None) -> list[dict]:
    """Execute a normalized or raw plan and return the output bag.

    ``metrics`` (when supplied) collects one dict per join with real input and
    output row counts, in post-order (bottom-up).
    """
    node = plan if _looks_normalized(plan) else normalize_plan(plan)
    return _execute(node, tables, metrics, schema)


def _looks_normalized(plan: dict) -> bool:
    if "filter" in plan or "scan" in plan:
        return True
    if "join" in plan:
        on = plan.get("on")
        # A dict ON may still contain legacy list operands; re-normalize to be
        # safe, so normalization is structural rather than shallow.
        return isinstance(on, dict) and _predicate_is_fully_normalized(on)
    return False


def _predicate_is_fully_normalized(predicate: dict) -> bool:
    op = predicate["op"]
    if op in ("and", "or"):
        return all(isinstance(a, dict) and _predicate_is_fully_normalized(a)
                   for a in predicate["args"])
    if op == "not":
        return _predicate_is_fully_normalized(predicate["arg"])
    if op in ("isnull", "isnotnull"):
        return isinstance(predicate["arg"], (str, dict))
    if op == "cmp":
        return all(isinstance(x, (str, dict))
                   for x in (predicate["left"], predicate["right"]))
    return False


def _execute(node: dict, tables: dict, metrics: list | None,
             schema: Schema | None) -> list[dict]:
    if "scan" in node:
        alias = node["scan"]
        declared = [q.split(".", 1)[1] for q in schema.columns(alias)] \
            if schema is not None else None
        output = []
        for row in tables[alias]:
            if declared is not None:
                # Bind against the declared schema: missing keys bind NULL,
                # undeclared keys are ignored, empty tables still have columns.
                qualified = {f"{alias}.{col}": row.get(col) for col in declared}
            else:
                qualified = {f"{alias}.{col}": value for col, value in row.items()}
            output.append(qualified)
        return output

    if "filter" in node:
        rows = _execute(node["filter"], tables, metrics, schema)
        return [row for row in rows if eval_predicate(node["where"], row) is True]

    left_rows = _execute(node["left"], tables, metrics, schema)
    right_rows = _execute(node["right"], tables, metrics, schema)
    before = len(metrics) if metrics is not None else 0
    output = _join(node, left_rows, right_rows, schema)
    if metrics is not None:
        produced = metrics[before:]
        is_left = node["join"] == "left"
        if is_left:
            build_rows = len(right_rows)  # right side hashed, left drives
        else:
            build_rows = min(len(left_rows), len(right_rows))
        metrics[before:] = [{
            "join": node["join"],
            "on_predicates": len(flatten_conjuncts(node["on"])),
            "left_rows": len(left_rows),
            "right_rows": len(right_rows),
            "build_rows": build_rows,
            "out_rows": len(output),
            "nested_joins_below": produced,
        }]
    return output


def _join(node: dict, left_rows: list[dict], right_rows: list[dict],
          schema: Schema | None) -> list[dict]:
    on = node["on"]
    is_left = node["join"] == "left"
    equi_keys = _equi_join_keys(on, plan_aliases(node["left"]),
                                plan_aliases(node["right"]))
    if equi_keys is not None and right_rows:
        output = _hash_join(on, equi_keys, left_rows, right_rows, is_left)
    else:
        output = _nested_loop(on, left_rows, right_rows, is_left)

    if is_left:
        padding = _padding_columns(node["right"], schema)
        for row in output:
            if row.pop("__unmatched__", False):
                for column in padding:
                    row[column] = None
    else:
        # Defensive: the marker may only survive a LEFT join; never leak it.
        for row in output:
            row.pop("__unmatched__", None)
    return output


def _equi_join_keys(on: dict, left_aliases: set[str],
                    right_aliases: set[str]) -> list[tuple[str, str]] | None:
    """Return oriented (left-subtree key, right-subtree key) pairs.

    Predicate column order is independent of tree side: ``B.k = A.k`` under a
    join whose right child is A still hashes correctly.  A predicate whose
    columns do not split across the two subtrees is not a join key.
    """
    pairs: list[tuple[str, str]] = []
    for conjunct in flatten_conjuncts(on):
        if conjunct["op"] != "cmp" or conjunct["cmp"] != "=":
            return None
        left, right = conjunct["left"], conjunct["right"]
        if not isinstance(left, str) or not isinstance(right, str):
            return None
        left_alias = left.split(".", 1)[0]
        right_alias = right.split(".", 1)[0]
        if left_alias in left_aliases and right_alias in right_aliases:
            pairs.append((left, right))
        elif right_alias in left_aliases and left_alias in right_aliases:
            pairs.append((right, left))
        else:
            return None  # intra-subtree predicate: not a usable hash key
    return pairs or None


def _hash_join(on, equi_keys, left_rows, right_rows, is_left):
    # Keys are oriented (left-subtree column, right-subtree column).
    # LEFT join: the left table is the outer driver so unmatched outer rows are
    # preserved; only the right side may be hashed.  INNER join may hash either
    # side, so build on the smaller one.
    if is_left:
        index = _build_index(equi_keys, right_rows, side="right")
        output = []
        for left in left_rows:
            key = tuple(left.get(lk) for lk, _ in equi_keys)
            matches = [] if None in key else index.get(key, [])
            kept = [left | right for right in matches
                    if eval_predicate(on, left | right) is True]
            if kept:
                output.extend(kept)
            else:
                output.append(_unmatched(left, True))
        return output

    if len(right_rows) <= len(left_rows):
        # Build on the small right side; probe with left rows.
        index = _build_index(equi_keys, right_rows, side="right")
        output = []
        for left in left_rows:
            key = tuple(left.get(lk) for lk, _ in equi_keys)
            if None in key:
                continue
            output.extend(left | right for right in index.get(key, [])
                          if eval_predicate(on, left | right) is True)
        return output

    index = _build_index(equi_keys, left_rows, side="left")
    output = []
    for right in right_rows:
        key = tuple(right.get(rk) for _, rk in equi_keys)
        if None in key:
            continue
        for left in index.get(key, []):
            row = left | right
            if eval_predicate(on, row) is True:
                output.append(row)
    return output


def _build_index(equi_keys, rows, side):
    index: dict[tuple, list[dict]] = {}
    for row in rows:
        if side == "right":
            key = tuple(row.get(rk) for _, rk in equi_keys)
        else:
            key = tuple(row.get(lk) for lk, _ in equi_keys)
        if None in key:
            # NULL never equals anything: such rows can never hash-match.
            continue
        index.setdefault(key, []).append(row)
    return index


def _nested_loop(on, left_rows, right_rows, is_left):
    output: list[dict] = []
    for left in left_rows:
        matched = False
        for right in right_rows:
            row = left | right
            if eval_predicate(on, row) is True:
                output.append(row)
                matched = True
        if is_left and not matched:
            output.append(_unmatched(left, True))
    return output


def _unmatched(left_row: dict, is_left: bool) -> dict:
    row = dict(left_row)
    if is_left:
        row["__unmatched__"] = True  # replaced by schema-derived NULL padding
    return row


def _padding_columns(right_node: dict, schema: Schema | None) -> list[str]:
    if schema is None:
        return _infer_columns(right_node)
    return schema.all_columns(plan_aliases(right_node))


def _infer_columns(node: dict) -> list[str]:
    """Last-resort inference when no schema object is supplied (empty-safe only
    when a real :class:`Schema` is provided)."""
    if "scan" in node:
        return []
    if "filter" in node:
        return _infer_columns(node["filter"])
    return _infer_columns(node["left"]) + _infer_columns(node["right"])
