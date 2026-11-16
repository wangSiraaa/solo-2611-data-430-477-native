"""Scalar and iterator evaluator used by the current reporting endpoint."""
from model import Context

class Evaluator:
    def __init__(self,model):self.model=model
    def evaluate(self,node,context=None,stack=()):
        context=context or Context(); op=node['op']
        if op=='literal':return node['value']
        if op in ('sum','countrows'):
            return self.model.aggregate(op,node['table'],node.get('column'),context)
        if op=='column':
            for table,row in reversed(context.row_stack):
                if table==node['table']:return row[node['column']]
            raise ValueError('NO_ROW_CONTEXT')
        if op in ('add','mul'):
            a,b=[self.evaluate(x,context,stack) for x in node['args']]
            if a is None or b is None:return None
            return a+b if op=='add' else a*b
        if op=='measure':
            name=node['name']
            if name in stack:raise ValueError('MEASURE_CYCLE')
            return self.evaluate(self.model.measures[name],context,stack+(name,))
        if op=='sumx':
            values=[];table=node['table']
            for row in self.model.visible(table,context):
                inner=context.copy();inner.row_stack+=( (table,row), )
                # Legacy shortcut: automatically constrains every aggregate inside an iterator.
                for col,value in row.items():inner.filters[(table,col)]=frozenset([value])
                values.append(self.evaluate(node['expr'],inner,stack))
            values=[x for x in values if x is not None]
            return sum(values) if values else None
        if op=='calculate':
            inner=context.copy()
            for modifier in node['modifiers']:
                kind=modifier['kind']
                if kind=='relationship':
                    selected=next(r for r in self.model.relations if r['name']==modifier['name'])
                    for r in self.model.relations:
                        if (r['parent_table'],r['child_table'])==(selected['parent_table'],selected['child_table']):
                            self.model.active[r['name']]=r['name']==selected['name']
                else:
                    key=(modifier['table'],modifier['column'])
                    if kind=='remove':inner.filters.pop(key,None)
                    elif kind=='keep':inner.filters[key]=inner.filters.get(key,frozenset(modifier['values'])) & frozenset(modifier['values'])
                    elif kind=='set':inner.filters[key]=frozenset(modifier['values'])
                    else:raise ValueError('UNKNOWN_MODIFIER')
            return self.evaluate(node['expr'],inner,stack)
        raise ValueError('UNKNOWN_EXPRESSION')
