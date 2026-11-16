"""Independent contract tests for the measure query engine.

Covers, with small hand-built tables and a SQLite oracle:
  * explicit full parent domain with NULL foreign keys (Model.visible)
  * bare aggregate vs named measure inside SUMX
  * nested iterators, row_stack shadowing and no-row-context column
  * set/keep/remove modifiers and consecutive / nested relationship switches
  * NULL ordering, empty groups, empty domains, empty tables
  * illegal model / AST / query rejection before execution
  * shared Model serving different, interleaved and concurrent requests
"""
import json
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path

from model import Model, Context, ModelError
from evaluator import Evaluator
from query import QueryEngine

ROOT = Path(__file__).parent
DOC = json.loads((ROOT / 'model.json').read_text())

FAILURES = []


def check(name, condition, detail=''):
    status = 'ok ' if condition else 'FAIL'
    print('[%s] %s %s' % (status, name, detail if not condition else ''))
    if not condition:
        FAILURES.append(name)


def expect_error(name, fn):
    try:
        fn()
    except (ModelError, ValueError):
        check(name, True)
        return
    check(name, False, 'no error raised')


def engine(doc=DOC):
    return QueryEngine(Model(deepcopy(doc)))


def eval_scalar(ast, filters=None, model_doc=DOC):
    model = Model(deepcopy(model_doc))
    ctx = Context(dict(filters or {}))
    return Evaluator(model).evaluate(ast, ctx)


# --------------------------------------------------------------------- #
# 1. Probe-level contract numbers on the shipped material
# --------------------------------------------------------------------- #
normal = engine().run(json.loads((ROOT / 'normal.json').read_text())[0])
check('normal grouping east/west = 10/30',
      normal['rows'] == [['east', 10], ['west', 30]])

contexts = json.loads((ROOT / 'contexts.json').read_text())
model = Model(deepcopy(DOC))
actual = [QueryEngine(model).run(q) for q in contexts]
check('bare SUMX 94 vs measure SUMX 40',
      actual[0]['rows'] == [[94, 40]])
check('ship=30 then default order_date restores 47',
      actual[1]['rows'] == [[30, 47]])
check('result keeps columns and rows keys',
      set(normal.keys()) == {'columns', 'rows'})

# Unfiltered total and NULL amount skipping.
check('unfiltered revenue is 47', eval_scalar(
    {'op': 'sum', 'table': 'Sales', 'column': 'amount'}) == 47)
check('countrows counts all three fact rows', eval_scalar(
    {'op': 'countrows', 'table': 'Sales'}) == 3)
check('sum of only-NULL values is NULL', eval_scalar(
    {'op': 'sum', 'table': 'Sales', 'column': 'ship_date'},
    {('Sales', 'id'): frozenset(['s3'])}) is None)
check('add/mul propagate NULL',
      eval_scalar({'op': 'add', 'args': [
          {'op': 'literal', 'value': None},
          {'op': 'literal', 'value': 1}]}) is None)

# --------------------------------------------------------------------- #
# 2. NULL foreign key and the restricted-domain rule
# --------------------------------------------------------------------- #
visible_model = Model(deepcopy(DOC))
explicit = visible_model.visible(
    'Sales', Context({('Customers', 'id'): frozenset(['a', 'b'])}))
check('explicit full parent domain filters NULL-FK orphan',
      [r['id'] for r in explicit] == ['s1', 's2'])

# Same parent rows, but no explicit/propagated restriction: orphan survives.
unrestricted = Model(deepcopy(DOC)).visible('Sales', Context())
check('unrestricted parent keeps NULL-FK row',
      sorted(r['id'] for r in unrestricted) == ['s1', 's2', 's3'])

# A filter that happens to cover every parent row is still a restriction.
all_parent = Model(deepcopy(DOC)).visible(
    'Sales', Context({('Customers', 'id'): frozenset(['a', 'b'])}))
check('full-coverage parent domain is still restricted',
      [r['id'] for r in all_parent] == ['s1', 's2'])

# Restricted child domain intersects two incoming parent restrictions.
intersect = Model(deepcopy(DOC)).visible(
    'Sales', Context({
        ('Customers', 'id'): frozenset(['a', 'b']),
        ('Calendar', 'date'): frozenset([1]),
    }))
check('two propagated restrictions intersect',
      [r['id'] for r in intersect] == ['s1', 's2'])

# Empty allowed set means no rows.
empty_domain = Model(deepcopy(DOC)).visible(
    'Sales', Context({('Customers', 'id'): frozenset()}))
check('empty parent domain removes every child row', empty_domain == [])

# --------------------------------------------------------------------- #
# 3. SQLite oracle for the headline numbers
# --------------------------------------------------------------------- #
db = sqlite3.connect(':memory:')
db.execute('create table sales(customer text, ord integer, '
           'ship integer, amount integer)')
db.executemany('insert into sales values(?,?,?,?)',
               [(r['customer_id'], r['order_date'], r['ship_date'],
                 r['amount']) for r in DOC['tables']['Sales']])
sql_total = db.execute('select sum(amount) from sales').fetchone()[0]
sql_ord1 = db.execute(
    'select sum(amount) from sales where ord=1').fetchone()[0]
sql_ship1 = db.execute(
    'select sum(amount) from sales where ship=1').fetchone()[0]
sql_notnull = db.execute(
    'select sum(amount) from sales where customer is not null').fetchone()[0]
check('sql oracle: total', sql_total == 47 == eval_scalar(
    {'op': 'sum', 'table': 'Sales', 'column': 'amount'}))
check('sql oracle: order_date=1', sql_ord1 == eval_scalar(
    {'op': 'sum', 'table': 'Sales', 'column': 'amount'},
    {('Calendar', 'date'): frozenset([1])}))
check('sql oracle: ship_date=1 via relationship override', sql_ship1 == 30)
check('sql oracle: not-null customers matches measure SUMX 40',
      sql_notnull == 40)

# --------------------------------------------------------------------- #
# 4. Nested iterators, shadowing, column semantics
# --------------------------------------------------------------------- #
nested_doc = {
    'schemas': {
        'P': {'key': 'id', 'columns': {'id': 'int'}},
        'C': {'key': 'cid', 'columns': {
            'cid': 'text', 'p': 'nullable_int', 'w': 'int'}},
    },
    'tables': {
        'P': [{'id': 1}, {'id': 2}],
        'C': [{'cid': 'c1', 'p': 1, 'w': 10},
              {'cid': 'c2', 'p': 1, 'w': 20},
              {'cid': 'c3', 'p': 2, 'w': 30}],
    },
    'relationships': [{
        'name': 'p_c', 'parent_table': 'P', 'parent_column': 'id',
        'child_table': 'C', 'child_column': 'p', 'active': True}],
    'measures': {
        'Wsum': {'op': 'sum', 'table': 'C', 'column': 'w'}},
}
# Outer P rows do not filter inner C: every outer row sees all three C rows.
bare_nested = eval_scalar(
    {'op': 'sumx', 'table': 'P', 'expr': {
        'op': 'sumx', 'table': 'C', 'expr': {
            'op': 'column', 'table': 'C', 'column': 'w'}}},
    model_doc=nested_doc)
check('nested SUMX bare column reads rows, no transition (2 * 60 = 120)',
      bare_nested == 120)

# Column can see the outer table's row while iterating the inner table.
outer_ref = eval_scalar(
    {'op': 'sumx', 'table': 'P', 'expr': {
        'op': 'sumx', 'table': 'C', 'expr': {
            'op': 'add', 'args': [
                {'op': 'column', 'table': 'P', 'column': 'id'},
                {'op': 'column', 'table': 'C', 'column': 'w'}]}}},
    model_doc=nested_doc)
# p=1: 11+21+31=63 ; p=2: 12+22+32=66
check('inner expression reaches outer row on row_stack', outer_ref == 129)

# Same-table nesting: the inner row shadows the outer row for column lookups.
# Asymmetric weights make the two readings distinguishable.  The inner SUMX
# enumerates all three rows for every outer row, so the nearest (inner) row
# yields 1+2+4 = 7 per outer row, total 21; an engine that leaked the outer
# row would return 1+2+4 once (outer weights 1,2,4) but the column would be
# constant per outer group, i.e. 1*3 + 2*3 + 4*3 = 21 as well -- therefore a
# second, discriminating expression compares nearest-row vs outer-row reading.
shadow_doc = {
    'schemas': {'T': {'key': 'id', 'columns': {
        'id': 'int', 'w': 'int', 'tag': 'int'}}},
    'tables': {'T': [{'id': 1, 'w': 1, 'tag': 9},
                     {'id': 2, 'w': 2, 'tag': 9},
                     {'id': 3, 'w': 4, 'tag': 0}]},
    'relationships': [],
    'measures': {},
}
# Same-table nesting: column must resolve to the nearest (inner) row.
# Two asymmetric columns make nearest-row vs outer-row readings distinct.
shadow_doc = {
    'schemas': {'T': {'key': 'id', 'columns': {
        'id': 'int', 'w': 'int', 'm': 'int'}}},
    'tables': {'T': [{'id': 1, 'w': 1, 'm': 1},
                     {'id': 2, 'w': 2, 'm': 10},
                     {'id': 3, 'w': 4, 'm': 100}]},
    'relationships': [],
    'measures': {},
}
# mul(w, m) where both columns are read from the nearest (inner) row:
# each outer row contributes 1*1 + 2*10 + 4*100 = 421, total 3*421 = 1263.
# An engine that leaked the outer row for w would instead sum, per outer
# weight, w_outer * (1+10+100) = w_outer * 111, i.e. 7*111 = 777.
shadow_nearest = eval_scalar(
    {'op': 'sumx', 'table': 'T', 'expr': {
        'op': 'sumx', 'table': 'T', 'expr': {
            'op': 'mul', 'args': [
                {'op': 'column', 'table': 'T', 'column': 'w'},
                {'op': 'column', 'table': 'T', 'column': 'm'}]}}},
    model_doc=shadow_doc)
check('same-table nested column resolves to nearest inner row (1263 vs 777)',
      shadow_nearest == 1263, 'got %s' % shadow_nearest)
# Companion: the w column alone, nearest-row total 3 * (1+2+4) = 21.
shadow_w = eval_scalar(
    {'op': 'sumx', 'table': 'T', 'expr': {
        'op': 'sumx', 'table': 'T', 'expr': {
            'op': 'column', 'table': 'T', 'column': 'w'}}},
    model_doc=shadow_doc)
check('nested nearest w sums the inner domain per outer row (21)',
      shadow_w == 21, 'got %s' % shadow_w)

# Named measure inside the iterators performs the transition.
measure_iter = eval_scalar(
    {'op': 'sumx', 'table': 'C', 'expr': {'op': 'measure', 'name': 'Wsum'}},
    model_doc=nested_doc)
check('measure in SUMX transitions each row (10+20+30)', measure_iter == 60)
bare_iter = eval_scalar(
    {'op': 'sumx', 'table': 'C', 'expr': {
        'op': 'sum', 'table': 'C', 'column': 'w'}},
    model_doc=nested_doc)
check('bare sum in SUMX stays global, repeated per row (3 * 60)',
      bare_iter == 180)

# column never reads filters: a filter without a row is still an error.
expect_error('column with no row context rejected even with filters',
             lambda: Evaluator(Model(deepcopy(nested_doc))).evaluate(
                 {'op': 'column', 'table': 'C', 'column': 'w'},
                 Context({('C', 'cid'): frozenset(['c1'])})))

# SUMX over an empty domain returns NULL; countrows returns 0.
empty_doc = {
    'schemas': {'E': {'key': 'k', 'columns': {'k': 'int', 'v': 'int'}},
                'F': {'key': 'k', 'columns': {
                    'k': 'int', 'f': 'nullable_int'}}},
    'tables': {'E': [], 'F': [{'k': 1, 'f': None}]},
    'relationships': [{
        'name': 'e_f', 'parent_table': 'E', 'parent_column': 'k',
        'child_table': 'F', 'child_column': 'f', 'active': True}],
    'measures': {},
}
check('sumx over empty table is NULL', eval_scalar(
    {'op': 'sumx', 'table': 'E', 'expr': {
        'op': 'column', 'table': 'E', 'column': 'v'}},
    model_doc=empty_doc) is None)
check('countrows over empty table is 0', eval_scalar(
    {'op': 'countrows', 'table': 'E'}, model_doc=empty_doc) == 0)
grouped_empty = QueryEngine(Model(deepcopy(empty_doc))).run({
    'labels': ['n'], 'group_by': [{'table': 'E', 'column': 'k'}],
    'expressions': [{'op': 'countrows', 'table': 'E'}]})
check('grouping column with empty domain gives no rows',
      grouped_empty['rows'] == [])
no_group_empty = QueryEngine(Model(deepcopy(empty_doc))).run({
    'labels': ['n'],
    'expressions': [{'op': 'countrows', 'table': 'E'}]})
check('no group_by always yields one row', no_group_empty['rows'] == [[0]])

# --------------------------------------------------------------------- #
# 5. Modifiers: set / keep / remove and relationship locality
# --------------------------------------------------------------------- #
date1 = {('Calendar', 'date'): frozenset([1])}
check('set replaces the existing domain', eval_scalar(
    {'op': 'calculate', 'expr': {
        'op': 'sum', 'table': 'Sales', 'column': 'amount'},
     'modifiers': [{'kind': 'set', 'table': 'Calendar',
                    'column': 'date', 'values': [2]}]},
    filters=dict(date1)) is None)
check('keep intersects the existing domain (keep {1,2} -> {1})',
      eval_scalar({'op': 'calculate', 'expr': {
          'op': 'sum', 'table': 'Sales', 'column': 'amount'},
          'modifiers': [{'kind': 'keep', 'table': 'Calendar',
                         'column': 'date', 'values': [1, 2]}]},
          filters=dict(date1)) == 47)
check('keep with disjoint domain empties the table', eval_scalar(
    {'op': 'calculate', 'expr': {
        'op': 'sum', 'table': 'Sales', 'column': 'amount'},
     'modifiers': [{'kind': 'keep', 'table': 'Calendar',
                    'column': 'date', 'values': [2]}]},
    filters=dict(date1)) is None)
check('keep without an old domain adopts the given domain', eval_scalar(
    {'op': 'calculate', 'expr': {
        'op': 'sum', 'table': 'Sales', 'column': 'amount'},
     'modifiers': [{'kind': 'keep', 'table': 'Calendar',
                    'column': 'date', 'values': [1]}]}) == 47)
check('remove drops the column constraint', eval_scalar(
    {'op': 'calculate', 'expr': {
        'op': 'sum', 'table': 'Sales', 'column': 'amount'},
     'modifiers': [{'kind': 'remove', 'table': 'Calendar',
                    'column': 'date'}]},
    filters=dict(date1)) == 47)

# Modifier order is significant: remove then set yields the set domain.
check('modifiers apply left to right', eval_scalar(
    {'op': 'calculate', 'expr': {
        'op': 'sum', 'table': 'Sales', 'column': 'amount'},
     'modifiers': [
         {'kind': 'remove', 'table': 'Calendar', 'column': 'date'},
         {'kind': 'set', 'table': 'Calendar', 'column': 'date',
          'values': [1]}]},
    filters=dict(date1)) == 47)

shared = Model(deepcopy(DOC))
# Nested relationship override: inner ship_date must restore outer order_date.
nested_rel = Evaluator(shared).evaluate({
    'op': 'calculate',
    'modifiers': [{'kind': 'relationship', 'name': 'order_date'}],
    'expr': {'op': 'calculate',
             'modifiers': [{'kind': 'relationship', 'name': 'ship_date'}],
             'expr': {'op': 'sum', 'table': 'Sales',
                      'column': 'amount'}}},
    Context({('Calendar', 'date'): frozenset([1])}))
check('nested relationship override selects ship_date (30)', nested_rel == 30)
after = Evaluator(shared).evaluate(
    {'op': 'sum', 'table': 'Sales', 'column': 'amount'},
    Context({('Calendar', 'date'): frozenset([1])}))
check('after nested call the default order_date relationship is back (47)',
      after == 47)
check('shared model declared active flags never change',
      shared.active == {r['name']: r['active'] for r in DOC['relationships']})

# --------------------------------------------------------------------- #
# 6. Grouping: cartesian product, NULL first, empty combos retained
# --------------------------------------------------------------------- #
grid = engine().run({
    'labels': ['rev'],
    'group_by': [{'table': 'Customers', 'column': 'region'},
                 {'table': 'Calendar', 'column': 'date'}],
    'expressions': [{'op': 'measure', 'name': 'Revenue'}]})
check('cartesian groups retained even with no fact rows',
      len(grid['rows']) == 4 and None in [r[2] for r in grid['rows']])
check('group columns and label appear in output columns',
      grid['columns'] == ['Customers.region', 'Calendar.date', 'rev'])

null_groups = engine().run({
    'labels': ['amt'],
    'group_by': [{'table': 'Sales', 'column': 'ship_date'}],
    'expressions': [{'op': 'sum', 'table': 'Sales', 'column': 'amount'}]})
check('NULL sorts first, then numeric order',
      null_groups['rows'] == [[None, 7], [1, 30], [2, 10]])

emptied = engine().run({
    'labels': ['n'],
    'filters': [{'table': 'Customers', 'column': 'region', 'values': []}],
    'group_by': [{'table': 'Customers', 'column': 'region'}],
    'expressions': [{'op': 'countrows', 'table': 'Sales'}]})
check('empty domain of a grouped column yields zero rows',
      emptied['rows'] == [])
# A restriction on a different table leaves the grouping domain intact;
# the expression simply evaluates against an empty fact domain.
emptied_other = engine().run({
    'labels': ['n'],
    'filters': [{'table': 'Sales', 'column': 'id', 'values': []}],
    'group_by': [{'table': 'Customers', 'column': 'region'}],
    'expressions': [{'op': 'countrows', 'table': 'Sales'}]})
check('empty fact domain keeps groups with zero counts',
      emptied_other['rows'] == [['east', 0], ['west', 0]])
one_row = engine().run({
    'labels': ['n', 'total'],
    'filters': [{'table': 'Sales', 'column': 'id', 'values': []}],
    'expressions': [
        {'op': 'countrows', 'table': 'Sales'},
        {'op': 'sum', 'table': 'Sales', 'column': 'amount'}]})
check('empty filter without group_by is one NULL/zero row',
      one_row['rows'] == [[0, None]])

# --------------------------------------------------------------------- #
# 7. Rejected models
# --------------------------------------------------------------------- #
def bad_doc(mutate):
    doc = deepcopy(DOC)
    mutate(doc)
    return doc


expect_error('bool is not accepted as int', lambda: Model(bad_doc(
    lambda d: d['tables']['Customers'][0].__setitem__('factor', True))))
expect_error('duplicate primary key', lambda: Model(bad_doc(
    lambda d: d['tables']['Customers'].append(
        {'id': 'a', 'region': 'x', 'factor': 9}))))
expect_error('dangling non-NULL foreign key', lambda: Model(bad_doc(
    lambda d: d['tables']['Sales'][2].__setitem__('customer_id', 'zz'))))
expect_error('unknown column type', lambda: Model(bad_doc(
    lambda d: d['schemas']['Customers']['columns'].__setitem__(
        'factor', 'bool'))))
expect_error('nullable primary key', lambda: Model(bad_doc(
    lambda d: d['schemas']['Customers']['columns'].__setitem__(
        'id', 'nullable_text'))))
expect_error('row with wrong column set', lambda: Model(bad_doc(
    lambda d: d['tables']['Sales'][0].__setitem__('extra', 1))))
expect_error('unknown table in relationship', lambda: Model(bad_doc(
    lambda d: d['relationships'][0].__setitem__('parent_table', 'Nope'))))
expect_error('two active relationships on one parent/child pair',
             lambda: Model(bad_doc(
                 lambda d: d['relationships'][2].__setitem__('active', True))))
expect_error('measure reference cycle', lambda: Model(bad_doc(
    lambda d: d['measures'].update({
        'A': {'op': 'measure', 'name': 'B'},
        'B': {'op': 'measure', 'name': 'A'}}))))

cycle_doc = {
    'schemas': {
        'A': {'key': 'id', 'columns': {'id': 'int', 'cid': 'nullable_int'}},
        'B': {'key': 'id', 'columns': {'id': 'int', 'aid': 'nullable_int'}},
        'C': {'key': 'id', 'columns': {'id': 'int', 'bid': 'nullable_int'}},
    },
    'tables': {
        'A': [{'id': 1, 'cid': 1}],
        'B': [{'id': 1, 'aid': 1}],
        'C': [{'id': 1, 'bid': 1}],
    },
    'relationships': [
        {'name': 'ab', 'parent_table': 'A', 'parent_column': 'id',
         'child_table': 'B', 'child_column': 'aid', 'active': True},
        {'name': 'bc', 'parent_table': 'B', 'parent_column': 'id',
         'child_table': 'C', 'child_column': 'bid', 'active': True},
        {'name': 'ca', 'parent_table': 'C', 'parent_column': 'id',
         'child_table': 'A', 'child_column': 'cid', 'active': True},
    ],
    'measures': {},
}
expect_error('relationship cycle rejected', lambda: Model(cycle_doc))

# A rejected construction must not corrupt a separately shared model.
stable = Model(deepcopy(DOC))
try:
    Model(bad_doc(lambda d: d['tables']['Sales'][0].__setitem__(
        'amount', 'not-an-int')))
except ValueError:
    pass
check('shared model still serves correct results after a rejected build',
      Evaluator(stable).evaluate(
          {'op': 'sum', 'table': 'Sales', 'column': 'amount'}) == 47)

# --------------------------------------------------------------------- #
# 8. Rejected ASTs and queries
# --------------------------------------------------------------------- #
good_model = Model(deepcopy(DOC))
expect_error('unknown op', lambda: Evaluator(good_model).evaluate(
    {'op': 'frobnicate'}))
expect_error('missing required field', lambda: Evaluator(good_model).evaluate(
    {'op': 'sumx', 'table': 'Sales'}))
expect_error('add needs exactly two args', lambda: Evaluator(good_model).evaluate(
    {'op': 'add', 'args': [{'op': 'literal', 'value': 1}]}))
expect_error('text literal rejected', lambda: Evaluator(good_model).evaluate(
    {'op': 'literal', 'value': 'x'}))
expect_error('unknown measure', lambda: Evaluator(good_model).evaluate(
    {'op': 'measure', 'name': 'Nope'}))
expect_error('unknown relationship modifier', lambda: Evaluator(good_model).evaluate(
    {'op': 'calculate', 'expr': {'op': 'countrows', 'table': 'Sales'},
     'modifiers': [{'kind': 'relationship', 'name': 'nope'}]}))
expect_error('unknown modifier kind', lambda: Evaluator(good_model).evaluate(
    {'op': 'calculate', 'expr': {'op': 'countrows', 'table': 'Sales'},
     'modifiers': [{'kind': 'frob', 'table': 'Sales',
                    'column': 'id', 'values': []}]}))
expect_error('sum on text column rejected', lambda: Evaluator(good_model).evaluate(
    {'op': 'sum', 'table': 'Sales', 'column': 'id'}))

deep_ast = {'op': 'literal', 'value': 1}
for _ in range(24):
    deep_ast = {'op': 'add', 'args': [deep_ast, {'op': 'literal', 'value': 0}]}
expect_error('AST deeper than 24 rejected',
             lambda: Evaluator(good_model).evaluate(deep_ast))

wide_ast = {'op': 'literal', 'value': 1}
for _ in range(2000):
    wide_ast = {'op': 'add', 'args': [wide_ast, {'op': 'literal', 'value': 1}]}
expect_error('AST with more than 2000 expanded nodes rejected',
             lambda: Evaluator(good_model).evaluate(wide_ast))

expect_error('more than 2000 filter values rejected',
             lambda: QueryEngine(good_model).run({
                 'labels': ['n'],
                 'filters': [{'table': 'Calendar', 'column': 'date',
                              'values': list(range(2001))}],
                 'expressions': [{'op': 'countrows', 'table': 'Calendar'}]}))
expect_error('duplicate labels rejected',
             lambda: QueryEngine(good_model).run({
                 'labels': ['x', 'x'],
                 'expressions': [
                     {'op': 'countrows', 'table': 'Sales'},
                     {'op': 'countrows', 'table': 'Sales'}]}))
expect_error('three grouping columns rejected',
             lambda: QueryEngine(good_model).run({
                 'labels': ['x'],
                 'group_by': [
                     {'table': 'Customers', 'column': 'id'},
                     {'table': 'Customers', 'column': 'region'},
                     {'table': 'Calendar', 'column': 'date'}],
                 'expressions': [{'op': 'countrows', 'table': 'Sales'}]}))
expect_error('duplicate filter column rejected',
             lambda: QueryEngine(good_model).run({
                 'labels': ['x'],
                 'filters': [
                     {'table': 'Calendar', 'column': 'date', 'values': [1]},
                     {'table': 'Calendar', 'column': 'date', 'values': [2]}],
                 'expressions': [{'op': 'countrows', 'table': 'Sales'}]}))
check('model remains usable after rejected queries',
      Evaluator(good_model).evaluate(
          {'op': 'countrows', 'table': 'Sales'}) == 3)

# --------------------------------------------------------------------- #
# 9. Shared model: interleaved and concurrent requests never cross domains
# --------------------------------------------------------------------- #
shared_model = Model(deepcopy(DOC))
eng_a = QueryEngine(shared_model)
eng_b = QueryEngine(shared_model)
ship_query = contexts[1]
plain_query = {
    'labels': ['total'],
    'expressions': [{'op': 'sum', 'table': 'Sales', 'column': 'amount'}]}
results = []
for _ in range(5):
    results.append(eng_a.run(ship_query)['rows'])
    results.append(eng_b.run(plain_query)['rows'])
check('interleaved requests keep independent relationship state',
      all(r == [[30, 47]] for r in results[0::2])
      and all(r == [[47]] for r in results[1::2]))
check('declared defaults untouched after interleaving',
      shared_model.active['ship_date'] is False)

errors = []


def worker():
    try:
        local = QueryEngine(shared_model)
        for _ in range(50):
            assert local.run(contexts[0])['rows'] == [[94, 40]]
            assert local.run(contexts[1])['rows'] == [[30, 47]]
            assert Evaluator(shared_model).evaluate(
                {'op': 'sum', 'table': 'Sales', 'column': 'amount'}) == 47
    except AssertionError as exc:  # pragma: no cover
        errors.append(str(exc))


threads = [threading.Thread(target=worker) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check('concurrent requests on one Model stay isolated', not errors, str(errors))

# Direct Context relationship override is call-local as well.
override_ctx = Context(
    {('Calendar', 'date'): frozenset([1])},
    relationships={('Calendar', 'Sales'): 'ship_date'})
check('explicit context override uses ship_date',
      Evaluator(shared_model).evaluate(
          {'op': 'sum', 'table': 'Sales', 'column': 'amount'},
          override_ctx) == 30)
check('next call without override back to order_date',
      Evaluator(shared_model).evaluate(
          {'op': 'sum', 'table': 'Sales', 'column': 'amount'},
          Context({('Calendar', 'date'): frozenset([1])})) == 47)

print()
if FAILURES:
    print('%d FAILURE(S): %s' % (len(FAILURES), ', '.join(FAILURES)))
    raise SystemExit(1)
print('all contract tests passed')
