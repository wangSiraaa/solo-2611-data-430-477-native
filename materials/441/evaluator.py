"""Scalar and iterator evaluator.

The evaluator never mutates the shared :class:`Model`.  Every context
transition, modifier and relationship override produces a new :class:`Context`
whose lifetime is bounded by one evaluation call, so nested calls restore the
outer filters and relationships on return.

Row iteration (``row_stack``) is kept separate from column filters:

* bare aggregates (``sum``, ``countrows``) and ``sumx`` read filters only and
  never convert the current row into a filter;
* named measure references and ``calculate`` perform an explicit context
  transition from the ``row_stack`` before evaluating their expression.
"""
from model import Context, ModelError


class Evaluator:
    def __init__(self, model):
        self.model = model

    def evaluate(self, node, context=None):
        if context is None:
            context = Context()
        if not isinstance(context, Context):
            raise ModelError('BAD_CONTEXT')
        # Reject malformed/oversized AST (measures expanded inline) before
        # touching any evaluation state.
        self.model.validate_ast(node)
        self.model.expanded_stats(node)
        return self._eval(node, context, ())

    # ------------------------------------------------------------------ #
    def _context_transition(self, context):
        """Outer-to-inner row_stack becomes single-value column filters.

        Same-column old filters are replaced; inner rows of the same table
        overwrite outer rows; filters of all other tables stay in place.
        """
        inner = context.copy()
        for table, row in context.row_stack:
            for column, value in row.items():
                inner.filters[(table, column)] = frozenset([value])
        return inner

    def _eval(self, node, context, measure_stack):
        op = node['op']
        if op == 'literal':
            return node['value']

        if op in ('sum', 'countrows'):
            # Bare aggregate: no row-to-filter transition, even inside SUMX.
            return self.model.aggregate(
                op, node['table'], node.get('column'), context)

        if op == 'column':
            # Reads the nearest row of the table; never reads filters.
            for table, row in reversed(context.row_stack):
                if table == node['table']:
                    return row[node['column']]
            raise ModelError('NO_ROW_CONTEXT:%s.%s'
                             % (node['table'], node['column']))

        if op in ('add', 'mul'):
            left = self._eval(node['args'][0], context, measure_stack)
            right = self._eval(node['args'][1], context, measure_stack)
            if left is None or right is None:
                return None
            return left + right if op == 'add' else left * right

        if op == 'measure':
            name = node['name']
            if name in measure_stack:
                raise ModelError('MEASURE_CYCLE:' + name)
            transitioned = self._context_transition(context)
            return self._eval(self.model.measures[name], transitioned,
                              measure_stack + (name,))

        if op == 'sumx':
            table = node['table']
            values = []
            # Enumerate the rows of the current filter domain; the row is
            # pushed onto row_stack only, with no filter mutation.
            for row in self.model.visible(table, context):
                inner = context.copy()
                inner.row_stack = context.row_stack + ((table, row),)
                values.append(self._eval(node['expr'], inner, measure_stack))
            values = [v for v in values if v is not None]
            return sum(values) if values else None

        if op == 'calculate':
            inner = self._context_transition(context)
            # Modifiers run strictly left to right.
            for modifier in node['modifiers']:
                kind = modifier['kind']
                if kind == 'relationship':
                    selected = next(
                        r for r in self.model.relations
                        if r['name'] == modifier['name'])
                    pair = (selected['parent_table'], selected['child_table'])
                    # Request-local override: other relations of the same
                    # parent/child pair are inactive for this call only.
                    inner.relationships[pair] = selected['name']
                    continue
                key = (modifier['table'], modifier['column'])
                if kind == 'set':
                    inner.filters[key] = frozenset(modifier['values'])
                elif kind == 'keep':
                    domain = frozenset(modifier['values'])
                    if key in inner.filters:
                        inner.filters[key] = inner.filters[key] & domain
                    else:
                        inner.filters[key] = domain
                elif kind == 'remove':
                    inner.filters.pop(key, None)
                else:  # pragma: no cover - rejected at validation time
                    raise ModelError('UNKNOWN_MODIFIER:' + str(kind))
            return self._eval(node['expr'], inner, measure_stack)

        raise ModelError('UNKNOWN_OP:' + str(op))  # pragma: no cover
