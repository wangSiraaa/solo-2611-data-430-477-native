"""Grouped query adapter.

A shared :class:`Model` may serve many :class:`QueryEngine` requests.  Every
request builds its own :class:`Context`, and every group combination gets a
fresh copy of the request's *initial* filter context, so neither filters nor
relationship choices can leak between expressions, groups or requests.
"""
from itertools import product
from model import Context
from evaluator import Evaluator


class QueryEngine:
    def __init__(self, model):
        self.model = model
        self.evaluator = Evaluator(model)

    def run(self, query):
        # Reject malformed requests before any evaluation side effect
        # (the model's audit log is not touched on failure).
        self.model.validate_query(query)

        base = Context({
            (f['table'], f['column']): frozenset(f['values'])
            for f in query.get('filters', [])
        })
        groups = query.get('group_by', [])

        domains = []
        for g in groups:
            values = {r[g['column']]
                      for r in self.model.visible(g['table'], base)}
            # NULL first; ints numerically, text by Unicode order.
            # A grouped column has one fixed type, so values compare safely.
            domains.append(sorted(values, key=lambda v: (v is not None, v)))

        if groups:
            combinations = list(product(*domains))
            # A grouped column with an empty domain yields no result rows.
            if any(len(d) == 0 for d in domains):
                combinations = []
        else:
            combinations = [()]  # no group_by: exactly one result row

        output = []
        for values in combinations:
            # Fresh copy of the initial context for every combination;
            # expression-level relationship overrides die with the expression.
            inner = base.copy()
            for g, value in zip(groups, values):
                inner.filters[(g['table'], g['column'])] = frozenset([value])
            row = list(values)
            for ast in query['expressions']:
                row.append(self.evaluator.evaluate(ast, inner))
            output.append(row)

        return {
            'columns': [g['table'] + '.' + g['column'] for g in groups]
                       + list(query['labels']),
            'rows': output,
        }
