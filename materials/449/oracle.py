"""Independent oracles for bag-equivalence checking.

Two oracles are used, neither of which shares planner code:

* :func:`naive_original` – a straightforward nested-loop execution of the
  *original* tree (separate implementation from the optimized executor),
* :func:`sqlite_original` – the same semantics expressed as SQL and run by the
  bundled SQLite engine.

Both order output rows identically so bags can be compared exactly, including
duplicate occurrences and NULL-padded rows.
"""
from __future__ import annotations

import sqlite3

from common import (
    Schema,
    eval_predicate,
    normalize_plan,
    plan_aliases,
)


def _sort_key(row: dict):
    return tuple((col, _sortable(row[col])) for col in sorted(row))


def _sortable(value):
    return (1, str(type(value)), str(value)) if value is not None else (0, "NULL", "")


def sorted_bag(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=_sort_key)


# --------------------------------------------------------------------------- #
# Oracle 1: naive nested-loop execution of the original tree
# --------------------------------------------------------------------------- #

def naive_original(plan: dict, tables: dict, schema: Schema) -> list[dict]:
    root = normalize_plan(plan)
    rows = _naive_node(root, tables, schema)
    return sorted_bag(rows)


def _naive_node(node, tables, schema):
    if "scan" in node:
        alias = node["scan"]
        declared = [q.split(".", 1)[1] for q in schema.columns(alias)]
        return [{f"{alias}.{c}": row.get(c) for c in declared}
                for row in tables[alias]]
    if "filter" in node:
        rows = _naive_node(node["filter"], tables, schema)
        return [r for r in rows if eval_predicate(node["where"], r) is True]

    left = _naive_node(node["left"], tables, schema)
    right = _naive_node(node["right"], tables, schema)
    out = []
    for lrow in left:
        matched = False
        for rrow in right:
            row = lrow | rrow
            if eval_predicate(node["on"], row) is True:
                out.append(row)
                matched = True
        if node["join"] == "left" and not matched:
            padded = dict(lrow)
            for column in schema.all_columns(plan_aliases(node["right"])):
                padded[column] = None
            out.append(padded)
    return out


# --------------------------------------------------------------------------- #
# Oracle 2: SQLite execution
# --------------------------------------------------------------------------- #

_SQL_TYPE = {"int64": "INTEGER", "text": "TEXT"}


def _ddl(alias: str, schema: Schema) -> str:
    cols = []
    for qualified in schema.columns(alias):
        _, col = qualified.split(".", 1)
        decl = schema.column_type(qualified)
        nullability = "" if schema.is_nullable(qualified) else " NOT NULL"
        cols.append(f'"{col}" {_SQL_TYPE[decl]}{nullability}')
    return f'CREATE TABLE "{alias}" (' + ", ".join(cols) + ")"


def _sql_operand(operand, prefix: str = "") -> str:
    if isinstance(operand, dict):
        literal = operand["lit"]
        if literal is None:
            return "NULL"
        if isinstance(literal, str):
            return "'" + literal.replace("'", "''") + "'"
        return str(int(literal) if isinstance(literal, bool) else literal)
    alias, col = operand.split(".", 1)
    if prefix:
        return f'{prefix}"{alias}.{col}"'
    return f'"{alias}"."{col}"'


def sql_predicate(predicate: dict, prefix: str = "") -> str:
    op = predicate["op"]
    if op == "and":
        return "(" + " AND ".join(sql_predicate(a, prefix)
                                  for a in predicate["args"]) + ")"
    if op == "or":
        return "(" + " OR ".join(sql_predicate(a, prefix)
                                 for a in predicate["args"]) + ")"
    if op == "not":
        return "NOT " + sql_predicate(predicate["arg"], prefix)
    if op == "isnull":
        return _sql_operand(predicate["arg"], prefix) + " IS NULL"
    if op == "isnotnull":
        return _sql_operand(predicate["arg"], prefix) + " IS NOT NULL"
    if op == "cmp":
        return (f"{_sql_operand(predicate['left'], prefix)} {predicate['cmp']} "
                f"{_sql_operand(predicate['right'], prefix)}")
    raise ValueError(f"unsupported predicate op: {op!r}")


def plan_to_sql(node: dict, schema: Schema) -> str:
    return _full_select(normalize_plan(node), schema)


def _full_select(node, schema):
    """A complete SELECT whose output columns use dotted ``alias.column``."""
    if "filter" in node:
        inner = _full_select(node["filter"], schema)
        aliases = _node_aliases(node)
        qualified = _qualified_columns(aliases, schema)
        projection = ", ".join(f'_sub."{q}" AS "{q}"' for q in qualified)
        where = sql_predicate(node["where"], prefix="_sub.")
        return f"SELECT {projection} FROM ({inner}) AS _sub WHERE {where}"
    from_sql, aliases = _sql_from(node, schema)
    return f"SELECT {_projection(aliases, schema)} FROM {from_sql}"


def _qualified_columns(aliases, schema):
    columns = []
    for alias in aliases:
        columns.extend(schema.columns(alias))
    return columns


def _projection(aliases, schema):
    columns = []
    for alias in aliases:
        for qualified in schema.columns(alias):
            _, col = qualified.split(".", 1)
            columns.append(f'"{alias}"."{col}" AS "{alias}.{col}"')
    return ", ".join(columns)


def _sql_from(node, schema):
    if "scan" in node:
        return f'"{node["scan"]}"', [node["scan"]]
    if "filter" in node:
        return _full_select(node, schema), _node_aliases(node)
    left_sql, left_aliases = _sql_from(node["left"], schema)
    right_sql, right_aliases = _sql_from(node["right"], schema)
    kind = "INNER JOIN" if node["join"] == "inner" else "LEFT JOIN"
    left = _wrap(left_sql)
    right = _wrap(right_sql)
    return (f"{left} {kind} {right} ON {sql_predicate(node['on'])}",
            left_aliases + right_aliases)


def _wrap(sql_fragment):
    # A bare table name stands alone; every compound expression is parenthesized.
    return sql_fragment if sql_fragment.startswith('"') and " " not in \
        sql_fragment else f"({sql_fragment})"


def _node_aliases(node):
    if "scan" in node:
        return [node["scan"]]
    if "filter" in node:
        return _node_aliases(node["filter"])
    return _node_aliases(node["left"]) + _node_aliases(node["right"])


def sqlite_original(plan: dict, tables: dict, schema: Schema) -> list[dict]:
    connection = sqlite3.connect(":memory:")
    try:
        for alias in schema.aliases():
            connection.execute(_ddl(alias, schema))
            columns = [q.split(".", 1)[1] for q in schema.columns(alias)]
            placeholders = ", ".join("?" for _ in columns)
            connection.executemany(
                f'INSERT INTO "{alias}" ({", ".join(columns)}) '
                f"VALUES ({placeholders})",
                [[row.get(col) for col in columns] for row in tables[alias]],
            )
        query = plan_to_sql(normalize_plan(plan), schema)
        cursor = connection.execute(query)
        names = [desc[0] for desc in cursor.description]
        rows = [dict(zip(names, record)) for record in cursor.fetchall()]
        return sorted_bag(rows)
    finally:
        connection.close()
