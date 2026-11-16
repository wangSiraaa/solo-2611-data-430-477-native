from logstore import leaf,parent,root_hash
def inclusion(rows,index):
    if len(rows)==1:return []
    k=1<<((len(rows)-1).bit_length()-1)
    if index<k:return inclusion(rows[:k],index)+[('R',root_hash(rows[k:]).hex())]
    return inclusion(rows[k:],index-k)+[('L',root_hash(rows[:k]).hex())]
def verify_inclusion(body,path,root):
    value=leaf(body)
    for side,node in path:
        other=bytes.fromhex(node);value=parent(value,other) if side=='R' else parent(other,value)
    return value.hex()==root
def consistency(store,old_size):
    rows=store.leaves()
    return {'old_size':old_size,'first':rows[0].hex(),'path':inclusion(rows,0)}
