def keep(leaf,expr,params):
 op=expr['op']
 if op=='eq' and expr['col']=='x':
  value=params[expr['param']]
  if value is None:return False
  if leaf['default']:return False
  return leaf['lo']<=value<leaf['hi']
 return True
