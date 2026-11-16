def bind(expr,parent):
 if 'parent' in expr:return parent[expr['parent']]
 return expr['const']
