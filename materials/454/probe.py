import json,tempfile,itertools
from source import Source
from sampler import collect
from artifact import save,load
from estimator import estimate
s=Source('seed');out=[]
for seed in [0,1,5]:
 a=collect(s,2,seed)
 with tempfile.TemporaryDirectory() as d:save(a,d+'/s.json');a=load(d+'/s.json')
 out.append({'pages':[p['id'] for p in a['pages']],'estimate':estimate(a)})
naive=[];ht=[]
for pair in itertools.combinations(['a','b','c'],2):
 ps=[{'id':id,'rows':s.page(id)} for id in pair];a={'N':5,'pages':ps};naive.append(estimate(a));ht.append(sum(r['value'] for p in ps for r in p['rows'])*3/2)
print(json.dumps({'normal_persisted_draws':out,'all_pair_naive':naive,'naive_design_expectation':sum(naive)/3,'true_total':40,'expected_HT_totals':ht,'HT_expectation':sum(ht)/3,'reads':s.reads}))
