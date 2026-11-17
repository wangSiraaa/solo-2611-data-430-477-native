"""Shared representation: schemas, the limited predicate/plan language, SQL 3VL.

The accepted language is intentionally finite:

* leaf predicates: column/column or column/literal comparisons
  (=, <>, <, <=, >, >=), IS NULL, IS NOT NULL
* boolean structure: AND / OR / NOT
* plan nodes: scan, binary INNER/LEFT join with an ON conjunction, top filter

There are no functions, no arbitrary SQL and no side effects.  Truth values use
SQL's three-valued logic: ``True`` / ``False`` / ``None`` (UNKNOWN).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Three-valued logic (SQL semantics: True / False / None == UNKNOWN)
# --------------------------------------------------------------------------- #

def not3(value: Any) -> Any:
    if value is None:
        return None
    return not value


def and3(left: Any, right: Any) -> Any:
    # False dominates; otherwise UNKNOWN dominates; otherwise True.
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def or3(left: Any, right: Any) -> Any:
    # True dominates; otherwise UNKNOWN dominates; otherwise False.
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


_TYPE_RANK = {bool: 0, int: 0, float: 0, str: 1}


def _comparable(left: Any, right: Any) -> bool:
    """Ordering across types is defined (numbers before text), NULL excluded."""
    return _TYPE_RANK.get(type(left), 2) == _TYPE_RANK.get(type(right), 2)


def compare3(op: str, left: Any, right: Any) -> Any:
    """Comparison under 3VL: any NULL operand yields UNKNOWN."""
    if left is None or right is None:
        return None
    if op == "=":
        # Cross-type equality is simply false in this language (no coercion).
        return left == right
    if op == "<>":
        return left != right
    if not _comparable(left, right):
        # Deterministic total order: numbers sort before text.
        result = _TYPE_RANK.get(type(left), 2) < _TYPE_RANK.get(type(right), 2)
    else:
        result = {
            "<": left < right,
            "<=": left <= right,
            ">": left > right,
            ">=": left >= right,
        }[op]
    return bool(result)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class Schema:
    """alias -> ordered column map; survives empty tables (no row guessing)."""

    def __init__(self, definition: dict[str, dict[str, dict]]):
        self._definition = definition

    @classmethod
    def load(cls, path: str | Path) -> "Schema":
        return cls(json.loads(Path(path).read_text()))

    def columns(self, alias: str) -> list[str]:
        return [f"{alias}.{col}" for col in self._definition[alias]]

    def column_type(self, qualified: str) -> str:
        alias, col = qualified.split(".", 1)
        return self._definition[alias][col]["type"]

    def is_nullable(self, qualified: str) -> bool:
        alias, col = qualified.split(".", 1)
        return bool(self._definition[alias][col].get("nullable", True))

    def aliases(self) -> list[str]:
        return list(self._definition)

    def all_columns(self, aliases: set[str]) -> list[str]:
        ordered: list[str] = []
        for alias in self._definition:
            if alias in aliases:
                ordered.extend(self.columns(alias))
        return ordered


# --------------------------------------------------------------------------- #
# Predicate normalization / walking
# --------------------------------------------------------------------------- #

def normalize_predicate(raw: Any) -> dict:
    """Accept legacy ``["A.k","B.k"]`` lists plus the explicit dict language."""
    if isinstance(raw, list):
        # A bare two-element list is an equality; a list of predicates is AND.
        if len(raw) == 2 and all(isinstance(term, str) for term in raw):
            return {"op": "cmp", "cmp": "=", "left": raw[0], "right": raw[1]}
        return {"op": "and", "args": [normalize_predicate(term) for term in raw]}
    if not isinstance(raw, dict):
        raise ValueError(f"unsupported predicate: {raw!r}")
    if raw["op"] in ("and", "or"):
        return {"op": raw["op"],
                "args": [normalize_predicate(a) for a in raw["args"]]}
    if raw["op"] == "not":
        return {"op": "not", "arg": normalize_predicate(raw["arg"])}
    if raw["op"] in ("isnull", "isnotnull"):
        return {"op": raw["op"], "arg": normalize_operand(raw["arg"])}
    if raw["op"] == "cmp":
        return {
            "op": "cmp",
            "cmp": raw["cmp"],
            "left": normalize_operand(raw["left"]),
            "right": normalize_operand(raw["right"]),
        }
    raise ValueError(f"unsupported predicate op: {raw['op']!r}")


def normalize_operand(raw: Any) -> Any:
    if isinstance(raw, str):
        return raw  # qualified column reference alias.column
    if isinstance(raw, dict) and "lit" in raw:
        return {"lit": raw["lit"]}
    raise ValueError(f"unsupported operand: {raw!r}")


def conjunction(predicates: list[dict]) -> dict | None:
    if not predicates:
        return None
    if len(predicates) == 1:
        return predicates[0]
    return {"op": "and", "args": list(predicates)}


def flatten_conjuncts(predicate: dict | None) -> list[dict]:
    if predicate is None:
        return []
    if predicate["op"] == "and":
        out: list[dict] = []
        for arg in predicate["args"]:
            out.extend(flatten_conjuncts(arg))
        return out
    return [predicate]


def column_refs(predicate: dict | None) -> set[str]:
    if predicate is None:
        return set()
    op = predicate["op"]
    if op == "and" or op == "or":
        refs = set()
        for arg in predicate["args"]:
            refs |= column_refs(arg)
        return refs
    if op == "not":
        return column_refs(predicate["arg"])
    if op in ("isnull", "isnotnull"):
        return _operand_refs(predicate["arg"])
    if op == "cmp":
        return _operand_refs(predicate["left"]) | _operand_refs(predicate["right"])
    raise ValueError(f"unsupported predicate op: {op!r}")


def _operand_refs(operand: Any) -> set[str]:
    return {operand} if isinstance(operand, str) else set()


def ref_aliases(predicate: dict | None) -> set[str]:
    return {ref.split(".", 1)[0] for ref in column_refs(predicate)}


# --------------------------------------------------------------------------- #
# Runtime predicate evaluation
# --------------------------------------------------------------------------- #

def _eval_operand(operand: Any, row: dict) -> Any:
    if isinstance(operand, dict):
        return operand["lit"]
    return row.get(operand)  # missing or NULL both surface as None


def eval_predicate(predicate: dict, row: dict) -> Any:
    op = predicate["op"]
    if op == "and":
        result = True
        for arg in predicate["args"]:
            result = and3(result, eval_predicate(arg, row))
        return result
    if op == "or":
        result = False
        for arg in predicate["args"]:
            result = or3(result, eval_predicate(arg, row))
        return result
    if op == "not":
        return not3(eval_predicate(predicate["arg"], row))
    if op == "isnull":
        return _eval_operand(predicate["arg"], row) is None
    if op == "isnotnull":
        return _eval_operand(predicate["arg"], row) is not None
    if op == "cmp":
        return compare3(
            predicate["cmp"],
            _eval_operand(predicate["left"], row),
            _eval_operand(predicate["right"], row),
        )
    raise ValueError(f"unsupported predicate op: {op!r}")


# --------------------------------------------------------------------------- #
# Plan normalization
# --------------------------------------------------------------------------- #

def normalize_plan(raw: dict) -> dict:
    """Normalize a plan dict; also accepts a top-level ``where`` shortcut."""
    where = raw.get("where")
    node = _normalize_node(raw)
    if where is not None:
        node = {"filter": node, "where": normalize_predicate(where)}
    return node


def _normalize_node(raw: dict) -> dict:
    if "scan" in raw:
        return {"scan": raw["scan"]}
    if "filter" in raw:
        return {
            "filter": _normalize_node(raw["filter"]),
            "where": normalize_predicate(raw["where"]),
        }
    if "join" in raw:
        on = raw.get("on", [])
        if isinstance(on, dict) or (on and isinstance(on[0], dict)):
            predicate = normalize_predicate(on)
        else:
            predicate = normalize_predicate(on)  # legacy string-list form
        node = {
            "join": raw["join"],
            "left": _normalize_node(raw["left"]),
            "right": _normalize_node(raw["right"]),
            "on": predicate,
        }
        return node
    raise ValueError(f"unsupported plan node: {raw!r}")


def plan_aliases(node: dict) -> set[str]:
    if "scan" in node:
        return {node["scan"]}
    if "filter" in node:
        return plan_aliases(node["filter"])
    return plan_aliases(node["left"]) | plan_aliases(node["right"])


def canonical(node: dict) -> str:
    return json.dumps(node, sort_keys=True, ensure_ascii=False)


def clone(node: dict) -> dict:
    return json.loads(json.dumps(node, ensure_ascii=False))


def render(node: dict, indent: int = 0) -> str:
    pad = "  " * indent
    if "scan" in node:
        return f"{pad}Scan {node['scan']}"
    if "filter" in node:
        return f"{pad}Filter {predicate_text(node['where'])}\n" + render(
            node["filter"], indent + 1
        )
    head = f"{pad}{node['join'].upper()} JOIN ON {predicate_text(node['on'])}"
    return head + "\n" + render(node["left"], indent + 1) + "\n" + render(
        node["right"], indent + 1
    )


def predicate_text(predicate: dict | None) -> str:
    if predicate is None:
        return "TRUE"
    op = predicate["op"]
    if op == "and":
        return "(" + " AND ".join(predicate_text(a) for a in predicate["args"]) + ")"
    if op == "or":
        return "(" + " OR ".join(predicate_text(a) for a in predicate["args"]) + ")"
    if op == "not":
        return f"NOT {predicate_text(predicate['arg'])}"
    if op == "isnull":
        return f"{_operand_text(predicate['arg'])} IS NULL"
    if op == "isnotnull":
        return f"{_operand_text(predicate['arg'])} IS NOT NULL"
    if op == "cmp":
        return (
            f"{_operand_text(predicate['left'])} {predicate['cmp']} "
            f"{_operand_text(predicate['right'])}"
        )
    raise ValueError(f"unsupported predicate op: {op!r}")


def _operand_text(operand: Any) -> str:
    if isinstance(operand, dict):
        return json.dumps(operand["lit"], ensure_ascii=False)
    return operand
