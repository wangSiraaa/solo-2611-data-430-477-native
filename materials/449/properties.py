def nullable_aliases(plan):
 if 'scan' in plan:return set()
 return nullable_aliases(plan['left'])|nullable_aliases(plan['right'])
def can_rotate(plan):return plan.get('join')=='inner' and plan.get('left',{}).get('join')=='left'
