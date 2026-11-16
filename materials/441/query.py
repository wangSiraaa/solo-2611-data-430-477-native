"""Grouped query adapter. Models are shared; query results are independent."""
from itertools import product
from model import Context
from evaluator import Evaluator

class QueryEngine:
    def __init__(self,model):self.model=model
    def run(self,query):
        context=Context({(f['table'],f['column']):frozenset(f['values']) for f in query.get('filters',[])})
        groups=query.get('group_by',[]); domain=[]
        for g in groups:
            values={r[g['column']] for r in self.model.visible(g['table'],context)}
            domain.append(sorted(values,key=lambda x:(x is not None,str(x))))
        combinations=list(product(*domain)) if groups else [()]
        output=[];evaluator=Evaluator(self.model)
        for values in combinations:
            inner=context.copy()
            for g,value in zip(groups,values):inner.filters[(g['table'],g['column'])]=frozenset([value])
            row=list(values)
            row += [evaluator.evaluate(n,inner) for n in query['expressions']]
            output.append(row)
        return {'columns':[g['table']+'.'+g['column'] for g in groups]+query['labels'],'rows':output}
