class Cursor:
    def __init__(self,tree,left,right):self.tree=tree;self.left=left;self.right=right;self.pid=tree.child(left);self.buffer=[];self.done=False
    def page(self):
        if self.done:return []
        p=self.tree.read(self.pid);out=[(k,v) for k,v in p['items'] if self.left<=k<self.right]
        self.pid=p['right'];self.done=self.pid is None
        return out
    def all(self):
        rows=[]
        while not self.done:rows.extend(self.page())
        return rows
