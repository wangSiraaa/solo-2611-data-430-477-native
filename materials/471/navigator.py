def find(tree,key):
    pid=tree.child(key);page=tree.read(pid)
    return dict(page['items']).get(key)
