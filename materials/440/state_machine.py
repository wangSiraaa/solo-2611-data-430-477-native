def read(node,key):
    value=None
    for _,_,k,v in node.entries():
        if k==key:value=v
    return value
