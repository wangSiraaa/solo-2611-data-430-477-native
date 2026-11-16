def evaluate(expr,row):
 if isinstance(expr,(int,str)) or expr is None:return expr
 if 'col' in expr:return row.get(expr['col'])
 a=evaluate(expr['args'][0],row);b=evaluate(expr['args'][1],row)
 if expr['op']=='coalesce':return b if a is None else a
 if a is None or b is None:return None
 if expr['op']=='+':return a+b
 if expr['op']=='concat':return str(a)+str(b)
 raise ValueError(expr)
def generate(columns,row):
 out=row.copy()
 for c in columns:
  if 'expr' in c:out[c['name']]=evaluate(c['expr'],out)
 return out
