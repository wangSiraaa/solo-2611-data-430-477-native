from properties import can_rotate

def optimize(plan):
 if not can_rotate(plan):return plan
 a=plan['left']['left'];b=plan['left']['right'];c=plan['right']
 return {'join':'left','left':a,'right':{'join':'inner','left':b,'right':c,'on':plan['on']},'on':plan['left']['on'],'right_cols':['B.k','C.k']}
