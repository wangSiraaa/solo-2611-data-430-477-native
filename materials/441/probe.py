"""Independent fixed-example expectations; no target evaluator implementation."""
import json,sqlite3
from pathlib import Path
from model import Model,Context
from query import QueryEngine
root=Path(__file__).parent;doc=json.loads((root/'model.json').read_text())
normal=QueryEngine(Model(doc)).run(json.loads((root/'normal.json').read_text())[0])
model=Model(doc);queries=json.loads((root/'contexts.json').read_text());actual=[QueryEngine(model).run(q) for q in queries]
db=sqlite3.connect(':memory:');db.execute('create table sales(customer text,ord integer,ship integer,amount integer)')
db.executemany('insert into sales values(?,?,?,?)',[(r['customer_id'],r['order_date'],r['ship_date'],r['amount']) for r in doc['tables']['Sales']])
total=db.execute('select sum(amount) from sales').fetchone()[0]
expected=[[total*len(doc['tables']['Customers']),db.execute('select sum(amount) from sales where customer is not null').fetchone()[0]],[db.execute('select sum(amount) from sales where ship=1').fetchone()[0],db.execute('select sum(amount) from sales where ord=1').fetchone()[0]]]
visible=Model(doc).visible('Sales',Context({('Customers','id'):frozenset(['a','b'])}))
print(json.dumps({'normal':normal,'actual':actual,'expected_rows':expected,'model_visible_actual':[r['id'] for r in visible],'model_visible_expected':['s1','s2'],'observed_bugs':['bare SUM inside SUMX incorrectly gets a filter transition','relationship override leaks into subsequent expression','explicit full parent domain incorrectly preserves orphan child'],'scope':'fixed independent SQL oracle; not a complete target implementation'},ensure_ascii=False))
