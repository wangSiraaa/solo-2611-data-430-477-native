class InodeStore:
    def __init__(self,lookup):self.lookup=lookup;self.lookup.build();self.upper={};self.serial=0
    def read(self,path):
        if path in self.upper:return self.upper[path]['data']
        n=self.lookup.node(path)
        if n['kind']=='symlink':return self.read(n['target'])
        return self.lookup.read_body(n)
    def write(self,path,data):
        self.serial+=1;self.upper[path]={'inode':f'upper{self.serial}','data':bytes(data),'kind':'file'}
    def stat(self,path):return self.upper.get(path,self.lookup.node(path))
    def paths(self):return sorted(set(self.lookup.visible)|set(self.upper))
