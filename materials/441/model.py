"""Model layer: validation, restricted domains and one-way relationship propagation.

A request's evaluation state lives in :class:`Context` (filters, row_stack and
per-call relationship overrides).  The shared :class:`Model` only holds the
validated declaration and data, so concurrent or consecutive requests can never
leak filter domains or relationship choices into each other.
"""
from dataclasses import dataclass, field
from copy import deepcopy

SCALAR_TYPES = ('int', 'text', 'nullable_int', 'nullable_text')
MAX_TABLES = 6
MAX_ROWS = 2000
MAX_DEPTH = 24
MAX_NODES = 2000
MAX_FILTER_VALUES = 2000


class ModelError(ValueError):
    """All contract rejections are ModelError (still a ValueError)."""


def _required(obj, key, where):
    if not isinstance(obj, dict) or key not in obj:
        raise ModelError('MISSING_FIELD:%s:%s' % (where, key))
    return obj[key]


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value):
    return isinstance(value, str)


def _value_matches_type(value, col_type):
    """bool is never accepted as int."""
    nullable = col_type.startswith('nullable_')
    if value is None:
        return nullable
    base = col_type[9:] if nullable else col_type
    if base == 'int':
        return _is_int(value)
    if base == 'text':
        return _is_text(value)
    return False


@dataclass
class Context:
    """Per-request evaluation state.

    filters:       (table, column) -> frozenset of allowed values
    row_stack:     tuple of (table, row), outermost first
    relationships: (parent_table, child_table) -> name of the relationship that
                   is active for this evaluation call only
    """
    filters: dict = field(default_factory=dict)
    row_stack: tuple = ()
    relationships: dict = field(default_factory=dict)

    def copy(self):
        return Context(dict(self.filters), tuple(self.row_stack),
                       dict(self.relationships))


class Model:
    def __init__(self, document):
        if not isinstance(document, dict):
            raise ModelError('BAD_DOCUMENT')
        # Keep the validated model immutable from the caller's input objects.
        self.schemas = deepcopy(document.get('schemas', {}))
        self.tables = deepcopy(document.get('tables', {}))
        self.relations = deepcopy(document.get('relationships', []))
        self.measures = deepcopy(document.get('measures', {}))
        self.active = {r['name']: r['active'] for r in self.relations}
        self.audit = []
        self._validate()
        self._topo = self._topological_order()

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def _validate(self):
        if not isinstance(self.schemas, dict) or not isinstance(self.tables, dict):
            raise ModelError('BAD_DOCUMENT')
        if not isinstance(self.relations, list) or not isinstance(self.measures, dict):
            raise ModelError('BAD_DOCUMENT')
        if len(self.tables) > MAX_TABLES:
            raise ModelError('TOO_MANY_TABLES')

        for name, schema in self.schemas.items():
            if not isinstance(schema, dict):
                raise ModelError('BAD_SCHEMA:' + str(name))
            key = _required(schema, 'key', 'schema:' + str(name))
            cols = _required(schema, 'columns', 'schema:' + str(name))
            if not isinstance(cols, dict) or key not in cols:
                raise ModelError('BAD_SCHEMA:' + str(name))
            for col, col_type in cols.items():
                if col_type not in SCALAR_TYPES:
                    raise ModelError('BAD_TYPE:%s.%s' % (name, col))
            if cols[key].startswith('nullable_'):
                raise ModelError('NULLABLE_KEY:%s.%s' % (name, key))

        unknown = [t for t in self.tables if t not in self.schemas]
        if unknown:
            raise ModelError('UNKNOWN_TABLE:' + unknown[0])
        missing = [t for t in self.schemas if t not in self.tables]
        if missing:
            raise ModelError('MISSING_TABLE:' + missing[0])

        for table, rows in self.tables.items():
            if not isinstance(rows, list) or len(rows) > MAX_ROWS:
                raise ModelError('BAD_TABLE:' + table)
            schema = self.schemas[table]
            key = schema['key']
            col_types = schema['columns']
            seen = set()
            for row in rows:
                if not isinstance(row, dict) or set(row.keys()) != set(col_types):
                    raise ModelError('ROW_COLUMNS:' + table)
                for col, col_type in col_types.items():
                    if not _value_matches_type(row[col], col_type):
                        raise ModelError('TYPE_MISMATCH:%s.%s' % (table, col))
                key_value = row[key]
                if key_value is None:
                    raise ModelError('NULL_KEY:' + table)
                if key_value in seen:
                    raise ModelError('DUPLICATE_KEY:' + table)
                seen.add(key_value)

        self._validate_relationships()
        self._check_relation_cycle()
        for name, ast in self.measures.items():
            self.validate_ast(ast)
        self._check_measure_cycles()

    def _validate_relationships(self):
        names = set()
        pair_active = {}
        for rel in self.relations:
            if not isinstance(rel, dict):
                raise ModelError('BAD_RELATIONSHIP')
            name = _required(rel, 'name', 'relationship')
            if name in names:
                raise ModelError('DUPLICATE_RELATIONSHIP:' + str(name))
            names.add(name)
            parent = _required(rel, 'parent_table', name)
            child = _required(rel, 'child_table', name)
            pcol = _required(rel, 'parent_column', name)
            ccol = _required(rel, 'child_column', name)
            active = _required(rel, 'active', name)
            if not isinstance(active, bool):
                raise ModelError('BAD_RELATIONSHIP:' + name)
            if parent not in self.schemas or child not in self.schemas:
                raise ModelError('UNKNOWN_TABLE:' + name)
            p_schema = self.schemas[parent]
            c_schema = self.schemas[child]
            if pcol not in p_schema['columns'] or ccol not in c_schema['columns']:
                raise ModelError('UNKNOWN_COLUMN:' + name)
            if pcol != p_schema['key']:
                raise ModelError('PARENT_NOT_KEY:' + name)
            # Type-compatible: same scalar family (int/text), nullability may differ.
            if c_schema['columns'][ccol].replace('nullable_', '') != \
                    p_schema['columns'][pcol].replace('nullable_', ''):
                raise ModelError('RELATIONSHIP_TYPE:' + name)
            # Non-NULL foreign keys must exist in the parent key domain.
            parent_keys = {r[pcol] for r in self.tables[parent]}
            for row in self.tables[child]:
                fk = row[ccol]
                if fk is not None and fk not in parent_keys:
                    raise ModelError('DANGLING_FOREIGN_KEY:' + name)
            pair = (parent, child)
            if active:
                if pair_active.get(pair):
                    raise ModelError('MULTIPLE_ACTIVE:' + name)
                pair_active[pair] = name

    def _check_relation_cycle(self):
        adjacency = {}
        for rel in self.relations:
            adjacency.setdefault(rel['parent_table'], []).append(rel['child_table'])
        state = {}

        def visit(node):
            state[node] = 1
            for nxt in adjacency.get(node, ()):
                if state.get(nxt) == 1:
                    raise ModelError('RELATIONSHIP_CYCLE')
                if nxt not in state:
                    visit(nxt)
            state[node] = 2

        for table in self.tables:
            if table not in state:
                visit(table)

    def _check_measure_cycles(self):
        """Reject cycles among named measure references at load time."""
        state = {}

        def visit(name):
            state[name] = 1
            for referenced in self._measure_refs(self.measures[name]):
                if state.get(referenced) == 1:
                    raise ModelError('MEASURE_CYCLE:' + str(referenced))
                if referenced not in state:
                    visit(referenced)
            state[name] = 2

        for name in self.measures:
            if name not in state:
                visit(name)

    def _measure_refs(self, node):
        refs = []
        if isinstance(node, dict):
            if node.get('op') == 'measure':
                refs.append(node['name'])
            for value in node.values():
                if isinstance(value, (dict, list)):
                    refs.extend(self._measure_refs(value))
        elif isinstance(node, list):
            for item in node:
                refs.extend(self._measure_refs(item))
        return refs

    def _topological_order(self):
        """Parents before children over the (acyclic) relationship graph."""
        indegree = {t: 0 for t in self.tables}
        adjacency = {t: [] for t in self.tables}
        for rel in self.relations:
            adjacency[rel['parent_table']].append(rel['child_table'])
            indegree[rel['child_table']] += 1
        order = []
        ready = sorted(t for t, d in indegree.items() if d == 0)
        while ready:
            node = ready.pop(0)
            order.append(node)
            for nxt in adjacency[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
            ready.sort()
        if len(order) != len(self.tables):
            raise ModelError('RELATIONSHIP_CYCLE')
        return order

    # ------------------------------------------------------------------ #
    # AST / query validation (rejected before execution)
    # ------------------------------------------------------------------ #
    def validate_ast(self, node, depth=1):
        if depth > MAX_DEPTH:
            raise ModelError('AST_TOO_DEEP')
        if not isinstance(node, dict) or 'op' not in node:
            raise ModelError('BAD_AST')
        op = node['op']
        if op in ('sum',):
            table = _required(node, 'table', op)
            column = _required(node, 'column', op)
            self._check_column(table, column)
            if not self.schemas[table]['columns'][column].endswith('int'):
                raise ModelError('TYPE_MISMATCH:%s.%s' % (table, column))
        elif op == 'countrows':
            self._check_table(_required(node, 'table', op))
        elif op == 'column':
            self._check_column(_required(node, 'table', op),
                               _required(node, 'column', op))
        elif op in ('add', 'mul'):
            args = _required(node, 'args', op)
            if not isinstance(args, (list, tuple)) or len(args) != 2:
                raise ModelError('BAD_ARGS:' + op)
            for arg in args:
                self.validate_ast(arg, depth + 1)
        elif op == 'literal':
            value = _required(node, 'value', op)
            if value is not None and not _is_int(value):
                raise ModelError('BAD_LITERAL')
        elif op == 'measure':
            name = _required(node, 'name', op)
            if name not in self.measures:
                raise ModelError('UNKNOWN_MEASURE:' + str(name))
        elif op == 'sumx':
            self._check_table(_required(node, 'table', op))
            self.validate_ast(_required(node, 'expr', op), depth + 1)
        elif op == 'calculate':
            self.validate_ast(_required(node, 'expr', op), depth + 1)
            modifiers = _required(node, 'modifiers', op)
            if not isinstance(modifiers, list):
                raise ModelError('BAD_MODIFIERS')
            for modifier in modifiers:
                if not isinstance(modifier, dict) or 'kind' not in modifier:
                    raise ModelError('BAD_MODIFIERS')
                kind = modifier['kind']
                if kind == 'relationship':
                    name = _required(modifier, 'name', 'relationship')
                    if name not in self.active:
                        raise ModelError('UNKNOWN_RELATIONSHIP:' + str(name))
                elif kind in ('set', 'keep', 'remove'):
                    table = _required(modifier, 'table', kind)
                    column = _required(modifier, 'column', kind)
                    self._check_column(table, column)
                    if kind != 'remove':
                        values = _required(modifier, 'values', kind)
                        if not isinstance(values, list) or len(values) > MAX_FILTER_VALUES:
                            raise ModelError('BAD_VALUES')
                        col_type = self.schemas[table]['columns'][column]
                        for value in values:
                            if not _value_matches_type(value, col_type):
                                raise ModelError('TYPE_MISMATCH:%s.%s' % (table, column))
                else:
                    raise ModelError('UNKNOWN_MODIFIER:' + str(kind))
        else:
            raise ModelError('UNKNOWN_OP:' + str(op))

    def expanded_stats(self, node, seen=None, depth=1):
        """Return (node_count, max_depth) with measure AST expanded inline."""
        if depth > MAX_DEPTH:
            raise ModelError('AST_TOO_DEEP')
        seen = seen if seen is not None else set()
        count = 1
        max_depth = depth
        if not isinstance(node, dict) or 'op' not in node:
            raise ModelError('BAD_AST')
        op = node['op']
        if op == 'measure':
            name = node['name']
            if name in seen:
                raise ModelError('MEASURE_CYCLE')
            sub_count, sub_depth = self.expanded_stats(
                self.measures[name], seen | {name}, depth + 1)
            count += sub_count
            max_depth = max(max_depth, sub_depth)
        if isinstance(node.get('expr'), dict):
            sub_count, sub_depth = self.expanded_stats(
                node['expr'], seen, depth + 1)
            count += sub_count
            max_depth = max(max_depth, sub_depth)
        if isinstance(node.get('args'), (list, tuple)):
            for arg in node['args']:
                sub_count, sub_depth = self.expanded_stats(
                    arg, seen, depth + 1)
                count += sub_count
                max_depth = max(max_depth, sub_depth)
        if count > MAX_NODES:
            raise ModelError('AST_TOO_LARGE')
        return count, max_depth

    def validate_query(self, query):
        if not isinstance(query, dict):
            raise ModelError('BAD_QUERY')
        expressions = _required(query, 'expressions', 'query')
        labels = _required(query, 'labels', 'query')
        if not isinstance(expressions, list) or not isinstance(labels, list):
            raise ModelError('BAD_QUERY')
        if len(expressions) != len(labels):
            raise ModelError('LABEL_LENGTH')
        if len(set(labels)) != len(labels):
            raise ModelError('DUP_LABEL')
        filters = query.get('filters', [])
        if not isinstance(filters, list):
            raise ModelError('BAD_QUERY')
        seen_filter_cols = set()
        for f in filters:
            table = _required(f, 'table', 'filter')
            column = _required(f, 'column', 'filter')
            self._check_column(table, column)
            values = _required(f, 'values', 'filter')
            if not isinstance(values, list) or len(values) > MAX_FILTER_VALUES:
                raise ModelError('BAD_VALUES')
            col_type = self.schemas[table]['columns'][column]
            for value in values:
                if not _value_matches_type(value, col_type):
                    raise ModelError('TYPE_MISMATCH:%s.%s' % (table, column))
            if (table, column) in seen_filter_cols:
                raise ModelError('DUP_FILTER')
            seen_filter_cols.add((table, column))
        groups = query.get('group_by', [])
        if not isinstance(groups, list) or len(groups) > 2:
            raise ModelError('BAD_GROUP_BY')
        seen_group_cols = set()
        for g in groups:
            table = _required(g, 'table', 'group_by')
            column = _required(g, 'column', 'group_by')
            self._check_column(table, column)
            if (table, column) in seen_group_cols:
                raise ModelError('DUP_GROUP_BY')
            seen_group_cols.add((table, column))
        for ast in expressions:
            self.validate_ast(ast)
            node_count, _ = self.expanded_stats(ast)
            if node_count > MAX_NODES:
                raise ModelError('AST_TOO_LARGE')

    def _check_table(self, table):
        if table not in self.schemas:
            raise ModelError('UNKNOWN_TABLE:' + str(table))

    def _check_column(self, table, column):
        self._check_table(table)
        if column not in self.schemas[table]['columns']:
            raise ModelError('UNKNOWN_COLUMN:%s.%s' % (table, column))

    # ------------------------------------------------------------------ #
    # Domain computation
    # ------------------------------------------------------------------ #
    def _relation_active(self, relation, context):
        """Active per the request-local override, else the declared default."""
        selected = context.relationships.get(
            (relation['parent_table'], relation['child_table']))
        if selected is not None:
            return relation['name'] == selected
        return self.active[relation['name']]

    def visible(self, table, context):
        """Public reusable domain interface.

        A table is *restricted* when it carries an explicit column filter or
        receives a propagated restriction from a restricted parent.  A
        restricted parent domain filters child foreign keys (NULL never
        matches); an unrestricted parent leaves child NULL rows untouched.
        Multiple incoming restrictions intersect naturally because each one
        narrows the surviving row list.
        """
        self._check_table(table)
        restricted = set()
        domains = {}
        for name in self._topo:
            rows = self.tables[name]
            direct = [((t, col), values) for (t, col), values
                      in context.filters.items() if t == name]
            if direct:
                restricted.add(name)
                rows = [r for r in rows
                        if all(r[col] in values for (_, col), values in direct)]
            for relation in self.relations:
                if relation['child_table'] != name:
                    continue
                if not self._relation_active(relation, context):
                    continue
                parent = relation['parent_table']
                if parent not in restricted:
                    # Unrestricted parent: the relationship removes nothing,
                    # in particular NULL foreign-key rows survive.
                    continue
                parent_keys = {r[relation['parent_column']]
                               for r in domains[parent]}
                rows = [r for r in rows
                        if r[relation['child_column']] in parent_keys]
                restricted.add(name)
            domains[name] = rows
        return domains[table]

    def aggregate(self, op, table, column, context):
        rows = self.visible(table, context)
        self.audit.append({
            'op': op, 'table': table,
            'row_ids': [r[self.schemas[table]['key']] for r in rows],
            'active_relationships': sorted(
                r['name'] for r in self.relations
                if self._relation_active(r, context)),
        })
        if op == 'countrows':
            return len(rows)
        values = [r[column] for r in rows if r[column] is not None]
        return sum(values) if values else None
