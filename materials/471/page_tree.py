import json,threading
from pathlib import Path
class PageTree:
    def __init__(self,root,capacity=4):
        self.root=Path(root);self.root.mkdir(exist_ok=True);self.capacity=capacity;self.nextid=1;self.parent=[];self.trace=[];self.guard=threading.RLock()
        self.write(0,{'items':[],'right':None,'high':None})
    def read(self,pid):
        self.trace.append(('read',pid));return json.loads((self.root/f'{pid}.page').read_text())
    def write(self,pid,page):
        path=self.root/f'{pid}.page';tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(page));tmp.replace(path);self.trace.append(('write',pid))
    def child(self,key):
        pid=0
        for boundary,right in self.parent:
            if key>=boundary:pid=right
        return pid
    def insert(self,key,value,split_gate=None):
        with self.guard:
            pid=self.child(key);p=self.read(pid);rows=dict(p['items']);rows[key]=value;p['items']=sorted(rows.items())
            if len(rows)<=self.capacity:self.write(pid,p);return
            at=len(rows)//2;right=self.nextid;self.nextid+=1;boundary=p['items'][at][0]
            q={'items':p['items'][at:],'right':p['right'],'high':p['high']};p['items']=p['items'][:at];p['right']=right;p['high']=boundary
            self.write(right,q);self.write(pid,p)
        if split_gate:split_gate()
        with self.guard:self.parent.append((boundary,right));self.parent.sort()
